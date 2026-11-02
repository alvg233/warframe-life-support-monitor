"""系统托盘图标（可选）。pystray 不可用时 UI 会降级为只开窗口。"""
from __future__ import annotations

import logging
import queue
import threading


def _make_image():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(24, 120, 96, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 32), "%", font=font, fill=(230, 255, 246, 255), anchor="mm")
    return img


class Tray:
    def __init__(self, commands: "queue.Queue[str]") -> None:
        self.commands = commands
        self.icon = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("显示面板", lambda: self.commands.put("show"), default=True),
            pystray.MenuItem("开始监控", lambda: self.commands.put("start")),
            pystray.MenuItem("停止监控", lambda: self.commands.put("stop")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.commands.put("quit")),
        )
        self.icon = pystray.Icon("screen_ocr_alert", _make_image(), "屏幕数值监控", menu)
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """托盘线程自己吞异常：某些受限会话不允许建隐藏窗口，不能让它冒到控制台。"""
        try:
            self.icon.run()
        except Exception as exc:
            logging.getLogger("screen_ocr_alert").warning("托盘启动失败（不影响主功能）: %s", exc)

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
