"""截屏后端：桌面截图（mss）+ 游戏窗口截图（PrintWindow）双通道。

为什么两套：
- 桌面截图（mss）走 DWM 合成结果，窗口化/无边框最常见，速度最快；
  但显示器熄灭后如果系统切换到锁屏/安全桌面，可能整张黑图。
- 窗口截图（PrintWindow + PW_RENDERFULLCONTENT）直接问窗口要画面，
  黑屏/被遮挡时往往还有内容，缺点是独占全屏（D3D exclusive）截不到。

所以 backend="auto"：先桌面，判定"黑图/无内容"后自动换窗口再试，
每帧都带上 source 字段，方便日志里看清当时用的哪条通道。
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

PW_RENDERFULLCONTENT = 0x00000002
BLACK_MEAN = 6.0      # 平均亮度低于这个值认为是黑图
BLACK_STD = 2.0       # 且几乎没有变化


class CaptureError(RuntimeError):
    pass


@dataclass
class Frame:
    image: np.ndarray
    source: str = "desktop"
    black: bool = False
    note: str = ""


def enable_dpi_awareness() -> None:
    """声明 DPI 感知，保证 tkinter 选的坐标 = 截图用的物理像素坐标。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """整个虚拟桌面（含多显示器）的 (left, top, width, height)。"""
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics
        x, y = gsm(76), gsm(77)
        w, h = gsm(78), gsm(79)
        if w > 0 and h > 0:
            return int(x), int(y), int(w), int(h)
    except Exception:
        pass
    return 0, 0, 1920, 1080


def input_desktop_name() -> str:
    """当前输入桌面名。正常是 "Default"；锁屏时是 "Winlogon"，此时普通截屏不可信。"""
    try:
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not hdesk:
            return "unknown"
        try:
            need = wintypes.DWORD(0)
            user32.GetUserObjectInformationW(hdesk, 2, None, 0, ctypes.byref(need))
            buf = ctypes.create_unicode_buffer(max(2, need.value // 2 + 1))
            if user32.GetUserObjectInformationW(hdesk, 2, buf, need.value, ctypes.byref(need)):
                return buf.value or "unknown"
        finally:
            user32.CloseDesktop(hdesk)
    except Exception:
        pass
    return "unknown"


def is_locked() -> bool:
    name = input_desktop_name().lower()
    return name not in ("default", "unknown")


def _pool_gray(img: np.ndarray, size: int = 32) -> np.ndarray:
    """把 BGR 图压成 size x size 的灰度矩阵（块平均，够判断变化了）。"""
    if img.ndim == 3:
        gray = img[:, :, 1].astype(np.float32)  # 绿色通道近似灰度，够用且快
    else:
        gray = img.astype(np.float32)
    h, w = gray.shape[:2]
    ys = np.linspace(0, h, size + 1).astype(int)
    xs = np.linspace(0, w, size + 1).astype(int)
    out = np.zeros((size, size), dtype=np.float32)
    for i in range(size):
        y0, y1 = ys[i], max(ys[i] + 1, ys[i + 1])
        row = gray[y0:y1].mean(axis=0)
        for j in range(size):
            x0, x1 = xs[j], max(xs[j] + 1, xs[j + 1])
            out[i, j] = row[x0:x1].mean()
    return out


def looks_black(img: np.ndarray) -> bool:
    if img is None or img.size == 0:
        return True
    sample = img[:: max(1, img.shape[0] // 64), :: max(1, img.shape[1] // 64)]
    return float(sample.mean()) < BLACK_MEAN and float(sample.std()) < BLACK_STD


# ---------------- Win32 窗口截图 ----------------
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

_user32.GetWindowDC.restype = wintypes.HDC
_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                                wintypes.LPARAM]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def list_windows() -> list[tuple[str, str]]:
    """列出当前有可见窗口的程序，返回 [(标题, 进程名)]，按标题排序去重。

    供设置向导使用：从下拉框选择窗口，不需要用户自行查找进程名。
    """
    items: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        try:
            import psutil

            name = psutil.Process(pid.value).name()
        except Exception:
            pass
        key = (title, name)
        if key not in seen:
            seen.add(key)
            items.append(key)
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    items.sort(key=lambda it: (it[1].lower(), it[0].lower()))
    return items


def find_window(title_substr: str = "", process_name: str = "") -> int | None:
    """按标题子串或进程名找可见窗口，返回 hwnd。两个条件都为空时返回 None。"""
    title_substr = (title_substr or "").lower()
    process_name = (process_name or "").lower()
    if not title_substr and not process_name:
        return None  # 不做"随便抓一个窗口"这种危险操作
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title_substr and title_substr not in title.lower():
            return True
        if process_name:
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                import psutil

                pname = psutil.Process(pid.value).name().lower()
            except Exception:
                return True
            if process_name not in pname:
                return True
        found.append(int(hwnd))
        return False  # 找到第一个就停

    _user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def window_rects(hwnd: int) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """返回 (窗口矩形, 客户区矩形)，都是屏幕物理像素 (left, top, w, h)。"""
    user32 = ctypes.windll.user32
    wr = wintypes.RECT()
    cr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    point = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    win = (int(wr.left), int(wr.top), int(wr.right - wr.left), int(wr.bottom - wr.top))
    client = (int(point.x), int(point.y), int(cr.right - cr.left), int(cr.bottom - cr.top))
    return win, client


def capture_window(hwnd: int) -> np.ndarray | None:
    """PrintWindow 抓指定窗口，返回 BGR 数组；失败返回 None。"""
    if not hwnd or not _user32.IsWindow(hwnd):
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    if w <= 0 or h <= 0:
        return None

    hdc_win = _user32.GetWindowDC(hwnd)
    if not hdc_win:
        return None
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_win)
    hbmp = _gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = _gdi32.SelectObject(hdc_mem, hbmp)
    try:
        ok = _user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        if not ok:
            _user32.PrintWindow(hwnd, hdc_mem, 0)
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h  # 负数 => 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = _gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        if not got:
            return None
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(arr[:, :, :3])
    finally:
        if old:
            _gdi32.SelectObject(hdc_mem, old)
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(hwnd, hdc_win)


class ScreenCapture:
    """线程内复用 mss 实例；窗口句柄缓存 10 秒，避免每帧枚举。"""

    def __init__(self, backend: str = "auto", window_title: str = "",
                 process_name: str = "") -> None:
        self.backend = (backend or "auto").lower()
        self.window_title = window_title or ""
        self.process_name = process_name or ""
        self._local = threading.local()
        self._hwnd_cache: tuple[float, int | None] = (0.0, None)

    # ---------- 桌面通道 ----------
    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss

            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
            self._local.sct = sct
        return sct

    def _desktop_scale(self) -> float:
        """坐标缩放补偿。

        情况 A（已声明 DPI 感知）：系统坐标就是物理像素，mss 也是物理像素 -> 比例 1.0。
        情况 B（DPI 感知没生效）：Windows 会把坐标按缩放比"虚拟化"（150% 时宽度变小），
        而 mss 拿到的仍是物理像素 -> 用 mss 宽度 / 系统宽度 得到 1.5，截图前乘回去。
        """
        cached = getattr(self._local, "scale", None)
        if cached is not None:
            return cached
        scale = 1.0
        try:
            mon = self._sct().monitors[0]
            physical_w = float(mon["width"])
            logical_w = float(ctypes.windll.user32.GetSystemMetrics(78))  # CX_VIRTUALSCREEN
            if logical_w > 0:
                ratio = physical_w / logical_w
                if 1.01 < ratio < 4.0:          # 只在明显不一致时启用
                    scale = ratio
        except Exception:
            scale = 1.0
        self._local.scale = scale
        if scale != 1.0:
            logging.getLogger(__name__).info(
                "检测到坐标缩放比 %.2f（DPI 感知未生效），截图坐标已自动补偿", scale)
        return scale

    def grab(self, region: dict) -> np.ndarray:
        """按区域抓桌面画面，返回 BGR uint8 数组。"""
        scale = self._desktop_scale()
        x = int(round(int(region.get("x", 0)) * scale))
        y = int(round(int(region.get("y", 0)) * scale))
        w = int(round(int(region.get("w", 0)) * scale))
        h = int(round(int(region.get("h", 0)) * scale))
        if w <= 0 or h <= 0:
            raise ValueError(f"区域尺寸非法: {region}")
        shot = self._sct().grab({"left": x, "top": y, "width": w, "height": h})
        arr = np.asarray(shot, dtype=np.uint8)
        return np.ascontiguousarray(arr[:, :, :3])

    def grab_full(self) -> np.ndarray:
        vx, vy, vw, vh = virtual_screen_rect()
        return self.grab({"x": vx, "y": vy, "w": vw, "h": vh})

    # ---------- 窗口通道 ----------
    def _hwnd(self) -> int | None:
        import time

        ts, hwnd = self._hwnd_cache
        if hwnd and time.time() - ts < 10:
            return hwnd
        hwnd = find_window(self.window_title, self.process_name)
        self._hwnd_cache = (time.time(), hwnd)
        return hwnd

    def window_frame(self, hwnd: int | None = None, max_age: float = 0.25):
        """抓整窗画面并缓存几百毫秒：同一轮里主区域和时间区域共用一次截图。"""
        if hwnd is None:
            hwnd = self._hwnd()
        if not hwnd:
            return None
        import time as _t

        cached = getattr(self, "_frame_cache", None)
        if (cached is not None and self._frame_hwnd == hwnd
                and _t.time() - self._frame_at < max_age):
            return cached
        img = capture_window(hwnd)
        if img is not None:
            self._frame_cache, self._frame_at, self._frame_hwnd = img, _t.time(), hwnd
        return img

    def grab_region_auto(self, region: dict) -> tuple[np.ndarray, str]:
        """按配置的通道顺序抓区域，返回 (图像, 通道名)。

        region 支持两种坐标：
          {"mode": "absolute", x, y, w, h}  屏幕物理像素
          {"mode": "ratio",    x, y, w, h}  相对游戏窗口客户区的比例（0~1）
        """
        px = self.region_pixels(region)
        order = {
            "window": ["window", "desktop"],
            "desktop": ["desktop", "window"],
            "auto": ["desktop", "window"],
        }.get(self.backend, ["desktop", "window"])
        last_err = "截图失败"
        for idx, source in enumerate(order):
            try:
                if source == "window":
                    hwnd = self._hwnd()
                    if not hwnd:
                        last_err = "未找到游戏窗口（请填窗口标题或进程名）"
                        continue
                    # 只取游戏窗口自身画面：游戏在前台时抓屏幕，会把压在上面的窗口一起抓进来。
                    full = self.window_frame(hwnd)
                    if full is None:
                        last_err = "窗口截图失败（独占全屏常见）"
                        continue
                    img = self._crop_window(full, hwnd, px)
                    if img is None:
                        last_err = "区域超出游戏窗口范围"
                        continue
                else:
                    img = self.grab(px)
                if looks_black(img) and idx < len(order) - 1:
                    last_err = f"{source} 截图是黑图"
                    continue
                return img, source
            except Exception as exc:
                last_err = f"{source} 截图异常: {exc}"
                continue
        raise CaptureError(last_err)

    @staticmethod
    def _foreground() -> int:
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    def base_rect(self) -> tuple[int, int, int, int]:
        """比例坐标的基准：优先游戏窗口客户区，找不到窗口就用整个虚拟桌面。"""
        hwnd = self._hwnd()
        if hwnd:
            _win, client = window_rects(hwnd)
            if client[2] > 0 and client[3] > 0:
                return client
        return virtual_screen_rect()

    def region_pixels(self, region: dict) -> dict:
        """把区域（比例或像素）换算成屏幕物理像素。"""
        mode = str(region.get("mode") or "absolute").lower()
        if mode != "ratio":
            return {"x": int(region.get("x", 0)), "y": int(region.get("y", 0)),
                    "w": max(1, int(region.get("w", 0))), "h": max(1, int(region.get("h", 0)))}

        bx, by, bw, bh = self.base_rect()
        rx, ry = float(region.get("x", 0.0)), float(region.get("y", 0.0))
        rw, rh = float(region.get("w", 0.0)), float(region.get("h", 0.0))
        x = int(round(bx + bw * rx))
        y = int(round(by + bh * ry))
        w = max(8, int(round(bw * rw)))
        h = max(8, int(round(bh * rh)))
        return {"x": x, "y": y, "w": w, "h": h}

    @staticmethod
    def _crop_window(full: np.ndarray, hwnd: int, region: dict) -> np.ndarray | None:
        """从整窗位图里裁出区域。

        声明 DPI 感知后 GetWindowRect 返回物理像素，
        而 PrintWindow 给的位图可能还是另一套比例（取决于 DWM 缩放），
        按"位图尺寸 / 窗口矩形"推算缩放比。
        """
        win, _client = window_rects(hwnd)
        if win[2] <= 0 or win[3] <= 0:
            return None
        img_h, img_w = full.shape[:2]
        sx = img_w / win[2]
        sy = img_h / win[3]
        x = (int(region["x"]) - win[0]) * sx
        y = (int(region["y"]) - win[1]) * sy
        w = int(region["w"]) * sx
        h = int(region["h"]) * sy
        x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
        x1 = min(img_w, int(round(x + w)))
        y1 = min(img_h, int(round(y + h)))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return None
        return np.ascontiguousarray(full[y0:y1, x0:x1])

    # ---------- 供截图存档用：整屏 / 窗口，自动兜底 ----------
    def grab_for_snapshot(self) -> Frame:
        order = {
            "window": ["window", "desktop"],
            "desktop": ["desktop", "window"],
            "auto": ["desktop", "window"],
        }.get(self.backend, ["window", "desktop"])
        last_note = ""
        for idx, source in enumerate(order):
            try:
                if source == "desktop":
                    img = self.grab_full()
                    note = "桌面截图"
                else:
                    hwnd = self._hwnd()
                    if not hwnd:
                        last_note = "未找到游戏窗口（可在配置里填窗口标题或进程名）"
                        continue
                    full = capture_window(hwnd)
                    if full is None:
                        last_note = "窗口截图失败（独占全屏常见）"
                        continue
                    img = self._drop_title_bar(full, hwnd)
                    note = "游戏窗口截图"
            except Exception as exc:
                last_note = f"{source} 截图异常: {exc}"
                continue
            black = looks_black(img)
            if black and idx < len(order) - 1:
                last_note = f"{source} 截图是黑图"
                continue
            return Frame(image=img, source=source, black=black, note=note)
        raise CaptureError(last_note or "截图失败")

    @staticmethod
    def _drop_title_bar(full: np.ndarray, hwnd: int) -> np.ndarray:
        """截窗口时顺带把标题栏裁掉，只留游戏画面（同样考虑位图缩放比）。"""
        win, client = window_rects(hwnd)
        if win[2] <= 0 or win[3] <= 0:
            return full
        h, w = full.shape[:2]
        sx = w / win[2]
        sy = h / win[3]
        dx = (client[0] - win[0]) * sx
        dy = (client[1] - win[1]) * sy
        cw = client[2] * sx if client[2] > 0 else w
        ch = client[3] * sy if client[3] > 0 else h
        x0, y0 = max(0, int(round(dx))), max(0, int(round(dy)))
        x1 = min(w, int(round(dx + cw)))
        y1 = min(h, int(round(dy + ch)))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return full
        return np.ascontiguousarray(full[y0:y1, x0:x1])

    # ---------- 变化检测 ----------
    @staticmethod
    def fingerprint(img: np.ndarray) -> np.ndarray:
        return _pool_gray(img)

    @staticmethod
    def diff(a: np.ndarray | None, b: np.ndarray) -> float:
        """0~255 尺度的平均绝对差。"""
        if a is None or a.shape != b.shape:
            return 255.0
        return float(np.abs(a - b).mean())
