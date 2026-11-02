"""常驻屏幕上的可拖动边框，标记当前检测区域。

- 窗口本身用 -transparentcolor 把内部挖空 => 中间部分鼠标点击可以"穿透"到游戏里，
  不会挡住你操作（这点对游戏很重要）。
- 抓 4~6px 的边框可以整体拖动，抓四角的小方块可以缩放。
- 边框只占几像素，CPU 几乎为零（不刷新就不重绘）。
"""
from __future__ import annotations

import tkinter as tk

_BG_KEY = "#010203"       # 被设为透明的颜色，实测不会和游戏画面冲突
_BORDER = "#2ee6a8"
_BORDER_ALERT = "#ff5d5d"
_HANDLE = 12
_GRAB = 6


class RegionOverlay:
    def __init__(self, root: tk.Misc, on_change=None) -> None:
        self.root = root
        self.on_change = on_change
        self.top: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self.region = {"x": 0, "y": 0, "w": 0, "h": 0}
        self._drag: dict | None = None
        self._label_id = None
        self._alert = False
        self._status: tuple[str, bool] = ("", False)

    # ---------- 生命周期 ----------
    def show(self, region: dict) -> None:
        self.region = {k: int(region.get(k, 0)) for k in ("x", "y", "w", "h")}
        if self.top is None:
            self._create()
        self._sync_geometry()
        self._redraw()

    def hide(self) -> None:
        if self.top is not None:
            try:
                self.top.withdraw()
            except tk.TclError:
                pass

    def destroy(self) -> None:
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None

    def visible(self) -> bool:
        return self.top is not None and self.top.winfo_viewable()

    def set_status(self, text: str, alert: bool = False) -> None:
        self._status = (text, alert)
        if self.top is not None and self._label_id is not None:
            color = _BORDER_ALERT if alert else _BORDER
            try:
                self.canvas.itemconfigure(self._label_id, text=text, fill=color)
            except tk.TclError:
                pass
            self._alert = alert
            self._redraw()

    def update_region(self, region: dict) -> None:
        """比例区域会随窗口缩放：把最新的实际落点同步过来。"""
        new = {k: int(region.get(k, 0)) for k in ("x", "y", "w", "h")}
        if new == self.region:
            return
        self.region = new
        self._sync_geometry()
        self._redraw()

    # ---------- 内部 ----------
    def _create(self) -> None:
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        try:
            top.attributes("-transparentcolor", _BG_KEY)
        except tk.TclError:
            top.attributes("-alpha", 0.6)
        canvas = tk.Canvas(top, bg=_BG_KEY, highlightthickness=0, cursor="fleur")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_motion)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        self.top, self.canvas = top, canvas

    def _sync_geometry(self) -> None:
        r = self.region
        w, h = max(8, r["w"]), max(8, r["h"])
        try:
            self.top.geometry(f"{w}x{h}+{r['x']}+{r['y']}")
            self.top.deiconify()
            self.top.attributes("-topmost", True)
            self._make_passthrough()
        except tk.TclError:
            pass

    def _make_passthrough(self) -> None:
        """边框只是指示器：整窗鼠标穿透，绝不吃点击/焦点。

        Tk 的 Toplevel 外面有包装窗口，必须逐层设置（见 wininput.make_click_through）；
        设完再把透明色/置顶补一遍——改扩展样式可能把分层属性冲掉，冲掉就变不透明黑块。
        """
        try:
            from .wininput import ensure_topmost, make_click_through

            hwnd = int(self.top.winfo_id())
            make_click_through(hwnd)
            try:
                self.top.attributes("-transparentcolor", _BG_KEY)
            except tk.TclError:
                pass
            ensure_topmost(hwnd)
        except Exception:
            pass

    def _redraw(self) -> None:
        if self.canvas is None:
            return
        c = self.canvas
        c.delete("all")
        w = max(8, self.region["w"])
        h = max(8, self.region["h"])
        color = _BORDER_ALERT if self._alert else _BORDER
        c.create_rectangle(1, 1, w - 1, h - 1, outline=color, width=3)
        for hx, hy in ((1, 1), (w - _HANDLE - 1, 1), (1, h - _HANDLE - 1),
                       (w - _HANDLE - 1, h - _HANDLE - 1)):
            c.create_rectangle(hx + 2, hy + 2, hx + _HANDLE, hy + _HANDLE,
                               fill=color, outline="")
        self._label_id = c.create_text(
            8, 8, anchor="nw", text=self._status[0],
            fill=color, font=("Consolas", 10)
        )

    def _mode(self, x: int, y: int) -> str:
        w, h = max(8, self.region["w"]), max(8, self.region["h"])
        near_l, near_r = x <= _GRAB + 4, x >= w - _GRAB - 4
        near_t, near_b = y <= _GRAB + 4, y >= h - _GRAB - 4
        if near_l and near_t:
            return "nw"
        if near_r and near_t:
            return "ne"
        if near_l and near_b:
            return "sw"
        if near_r and near_b:
            return "se"
        if near_l:
            return "w"
        if near_r:
            return "e"
        if near_t:
            return "n"
        if near_b:
            return "s"
        return "move"

    def _on_press(self, event) -> None:
        self._drag = {
            "mode": self._mode(event.x, event.y),
            "sx": event.x_root,
            "sy": event.y_root,
            "orig": dict(self.region),
        }

    def _on_motion(self, event) -> None:
        if not self._drag:
            return
        d = self._drag
        dx = event.x_root - d["sx"]
        dy = event.y_root - d["sy"]
        o = d["orig"]
        x, y, w, h = o["x"], o["y"], o["w"], o["h"]
        mode = d["mode"]
        if mode == "move":
            x, y = o["x"] + dx, o["y"] + dy
        else:
            if "w" in mode:
                x = o["x"] + dx
                w = o["w"] - dx
            if "e" in mode:
                w = o["w"] + dx
            if "n" in mode:
                y = o["y"] + dy
                h = o["h"] - dy
            if "s" in mode:
                h = o["h"] + dy
        if w < 8:
            w = 8
        if h < 8:
            h = 8
        self.region = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
        self._sync_geometry()
        self._redraw()

    def _on_release(self, _event) -> None:
        if not self._drag:
            return
        self._drag = None
        if self.on_change:
            self.on_change(dict(self.region))
