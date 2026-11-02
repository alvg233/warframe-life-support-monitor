"""拖拽框选屏幕区域。

全屏半透明遮罩 + 十字光标，按下拖动出矩形，松手返回物理像素坐标。
Esc 或右键取消。启动时已声明 DPI 感知，取得的是真实像素坐标。
"""
from __future__ import annotations

import tkinter as tk

from .capture import virtual_screen_rect


def pick_region(parent: tk.Misc, initial: dict | None = None) -> dict | None:
    vx, vy, vw, vh = virtual_screen_rect()
    top = tk.Toplevel(parent)
    top.overrideredirect(True)
    top.geometry(f"{vw}x{vh}+{vx}+{vy}")
    top.attributes("-topmost", True)
    try:
        top.attributes("-alpha", 0.30)
    except tk.TclError:
        pass
    top.configure(bg="#0d1216")

    canvas = tk.Canvas(top, bg="#0d1216", highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)

    canvas.create_text(
        48, 44, anchor="nw",
        text="拖动鼠标框选要识别的数值区域（例如「86%」）\nEsc 或右键取消",
        fill="#8ef0c8", font=("Microsoft YaHei UI", 15),
    )

    state: dict = {"start": None, "rect": None, "info": None}
    result: dict | None = None

    def on_press(event):
        state["start"] = (event.x, event.y)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#2ee6a8", width=2
        )
        state["info"] = canvas.create_text(
            event.x + 8, event.y + 8, anchor="nw", text="", fill="#ffffff",
            font=("Consolas", 11),
        )

    def on_move(event):
        if state["start"] is None:
            return
        x0, y0 = state["start"]
        canvas.coords(state["rect"], x0, y0, event.x, event.y)
        canvas.coords(state["info"], min(x0, event.x) + 8, min(y0, event.y) + 8)
        canvas.itemconfigure(
            state["info"],
            text=f"{abs(event.x - x0)} x {abs(event.y - y0)}",
        )

    def on_release(event):
        nonlocal result
        if state["start"] is None:
            return
        x0, y0 = state["start"]
        x1, y1 = event.x, event.y
        left, top_ = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 6 or h < 6:
            state["start"] = None
            return
        result = {"x": int(vx + left), "y": int(vy + top_), "w": int(w), "h": int(h)}
        close()

    def close(*_):
        try:
            top.grab_release()
        except tk.TclError:
            pass
        top.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<ButtonPress-3>", close)
    top.bind("<Escape>", close)
    top.bind("<ButtonPress-3>", close)

    if initial:
        canvas.create_rectangle(
            initial.get("x", 0) - vx, initial.get("y", 0) - vy,
            initial.get("x", 0) - vx + initial.get("w", 0),
            initial.get("y", 0) - vy + initial.get("h", 0),
            outline="#f5c542", width=2, dash=(6, 4),
        )

    try:
        top.grab_set()
        top.focus_force()
    except tk.TclError:
        pass
    parent.wait_window(top)
    return result
