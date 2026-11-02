"""Windows 输入/前台窗口操作：给"急停"用。

实现要点：
  - 前台锁会让 SetForegroundWindow 静默失败，必须 AttachThreadInput 再切；
  - 游戏常忽略 VK 注入，同时尝试扫描码与虚拟键两种方式；
  - 每次动作都留诊断信息，方便判断到底是没切前台还是没收到键。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_user32 = ctypes.windll.user32

SW_RESTORE = 9
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
MAPVK_VK_TO_VSC = 0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def foreground_hwnd() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


_kernel32 = ctypes.windll.kernel32
_advapi32 = ctypes.windll.advapi32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def is_elevated(pid: int) -> bool | None:
    """判断某个进程是否以管理员权限运行；查不到返回 None。"""
    if not pid:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            info = _TOKEN_ELEVATION()
            size = wintypes.DWORD(ctypes.sizeof(info))
            if not _advapi32.GetTokenInformation(
                    token, TokenElevation, ctypes.byref(info),
                    ctypes.sizeof(info), ctypes.byref(size)):
                return None
            return bool(info.TokenIsElevated)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(handle)


def self_elevated() -> bool:
    return bool(is_elevated(ctypes.windll.kernel32.GetCurrentProcessId()) or False)


def _alt_unlock() -> None:
    """发一下 ALT 的空按键，解除"前台锁"，之后 SetForegroundWindow 才有机会生效。"""
    try:
        _send_input(0x12, 0, 0)                       # VK_MENU down
        _send_input(0x12, 0, KEYEVENTF_KEYUP)         # VK_MENU up
    except Exception:
        pass


MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_CXSCREEN = 0
SM_CYSCREEN = 1


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _MOUSEUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _MOUSE_INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _MOUSEUNION)]


def _send_mouse(flags: int, x: int = 0, y: int = 0) -> bool:
    inp = (_MOUSE_INPUT * 1)()
    inp[0].type = 0  # INPUT_MOUSE
    inp[0].u.mi = _MOUSEINPUT(x, y, 0, flags, 0, None)
    sent = _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_MOUSE_INPUT))
    return bool(sent)


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


def make_click_through(hwnd: int) -> bool:
    """把窗口设成"仅可见"：鼠标穿透、不抢焦点、不进 Alt+Tab。

    两条约束：
      1. 不能改 WS_EX_LAYERED —— 会把 Tk 的 -alpha 清成 0，窗口直接看不见；
      2. Tk 的 Toplevel 外面还有包装窗口，必须沿 GetParent 逐层设置，
         只设最内层的话外层仍会吃掉鼠标点击。
    """
    try:
        _user32.GetParent.restype = wintypes.HWND
        _user32.GetParent.argtypes = [wintypes.HWND]
    except Exception:
        pass
    ok = False
    handle = hwnd
    for _ in range(8):
        if not handle:
            break
        try:
            style = _user32.GetWindowLongW(handle, GWL_EXSTYLE)
            # 只叠加穿透/不激活/工具窗口，绝不动 WS_EX_LAYERED：
            # 那一位是 Tk 的半透明(-alpha)和透明色(-transparentcolor)的命根子。
            style |= (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            _user32.SetWindowLongW(handle, GWL_EXSTYLE, style)
            ok = True
        except Exception:
            break
        try:
            handle = _user32.GetParent(handle)
        except Exception:
            break
    return ok


def hit_test_window(x: int, y: int) -> int:
    """返回屏幕该点最上层的窗口句柄，用于验证"点击是否穿透"。"""
    try:
        _user32.WindowFromPoint.restype = wintypes.HWND
        _user32.WindowFromPoint.argtypes = [wintypes.POINT]
        return int(_user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)
    except Exception:
        return 0


def ensure_topmost(hwnd: int) -> None:
    try:
        _user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOPMOST + NOSIZE
    except Exception:
        pass


def click_at(screen_x: int, screen_y: int) -> tuple[bool, str]:
    """在屏幕绝对坐标点一下左键（先移动再按下抬起）。"""
    sw = max(1, _user32.GetSystemMetrics(SM_CXSCREEN))
    sh = max(1, _user32.GetSystemMetrics(SM_CYSCREEN))
    nx = int(screen_x * 65535 / sw)
    ny = int(screen_y * 65535 / sh)
    try:
        _user32.SetCursorPos(int(screen_x), int(screen_y))
        ok = _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny)
        ok = ok and _send_mouse(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.05)
        ok = ok and _send_mouse(MOUSEEVENTF_LEFTUP)
        if ok:
            return True, f"已在 ({screen_x},{screen_y}) 点击"
    except Exception as exc:
        return False, f"点击异常: {exc}"
    try:
        _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True, "mouse_event 兜底点击"
    except Exception as exc:
        return False, f"点击失败: {exc}"


def switch_to_window(hwnd: int) -> bool:
    """备选切换方式：SwitchToThisWindow + ALT 解锁 + 置顶。"""
    try:
        _alt_unlock()
        _user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.1)
    except Exception:
        pass
    try:
        _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
    except Exception:
        pass
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
    except Exception:
        pass
    return foreground_hwnd() == hwnd


def window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 2)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def activate_window(hwnd: int, attempts: int = 3, logger=None) -> tuple[bool, str]:
    """把窗口恢复并切到前台，返回 (是否成功, 诊断信息)。"""
    if not hwnd:
        return False, "没有窗口句柄"

    notes: list[str] = []
    for i in range(max(1, attempts)):
        try:
            _user32.ShowWindow(hwnd, SW_RESTORE)
            if _user32.IsIconic(hwnd):
                time.sleep(0.08)
            if _user32.SetForegroundWindow(hwnd):
                time.sleep(0.05)
                if foreground_hwnd() == hwnd:
                    return True, f"第{i + 1}次 SetForegroundWindow 成功"
                notes.append(f"第{i + 1}次返回成功但前台仍是 {window_title(foreground_hwnd())!r}")

            # 前台锁：把自己的输入队列临时挂到当前前台线程上
            fg = foreground_hwnd()
            target_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
            this_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            if target_thread:
                attached = _user32.AttachThreadInput(this_thread, target_thread, True)
                try:
                    _user32.BringWindowToTop(hwnd)
                    _user32.SetForegroundWindow(hwnd)
                    time.sleep(0.05)
                    if foreground_hwnd() == hwnd:
                        return True, f"第{i + 1}次 AttachThreadInput 后成功"
                    notes.append(f"第{i + 1}次 AttachThreadInput 后前台={window_title(foreground_hwnd())!r}")
                finally:
                    if attached:
                        _user32.AttachThreadInput(this_thread, target_thread, False)
            else:
                notes.append(f"第{i + 1}次拿不到前台线程")

            # 最后再试"解除前台锁 + SwitchToThisWindow"这条路
            if switch_to_window(hwnd):
                return True, f"第{i + 1}次 SwitchToThisWindow 成功"
            notes.append(f"第{i + 1}次 SwitchToThisWindow 仍未到前台")
        except Exception as exc:
            notes.append(f"第{i + 1}次异常: {exc}")
        time.sleep(0.25)

    detail = "；".join(notes) or "未知原因"
    if logger:
        logger.warning("急停：切前台失败（%s）", detail)
    return False, detail


def _send_input(vk: int, scan: int, flags: int = 0) -> bool:
    inputs = (_INPUT * 1)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].u.ki = _KEYBDINPUT(vk, scan, flags, 0, None)
    sent = _user32.SendInput(1, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
    return bool(sent)


def press_key(vk: int = VK_ESCAPE) -> tuple[bool, str]:
    """发送一次按键。先扫描码，再虚拟键，最后 keybd_event 兜底。"""
    scan = _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    try:
        ok = (_send_input(vk, scan, KEYEVENTF_SCANCODE)
              and _send_input(vk, scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP))
        if ok:
            return True, "SendInput 扫描码"
    except Exception:
        pass
    try:
        ok = (_send_input(vk, 0, 0) and _send_input(vk, 0, KEYEVENTF_KEYUP))
        if ok:
            return True, "SendInput 虚拟键"
    except Exception:
        pass
    try:
        _user32.keybd_event(vk, scan, 0, 0)
        time.sleep(0.03)
        _user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)
        return True, "keybd_event"
    except Exception:
        return False, "所有注入方式都失败"


def press_escape() -> bool:
    return press_key(VK_ESCAPE)[0]
