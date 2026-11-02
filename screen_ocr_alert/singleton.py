"""单实例锁 + 进程优先级。

"不容易被挤掉"的两个动作：
1. 命名互斥锁：双击多次只会跑一个实例，避免多份 OCR 抢 CPU。
2. 优先级降到 BELOW_NORMAL：和游戏/办公软件抢 CPU 时主动让路，
   系统不会因为它卡顿而清理它。
"""
from __future__ import annotations

import ctypes
import logging

import psutil

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = "ScreenOcrAlert.Singleton") -> None:
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        self._handle = kernel32.CreateMutexW(None, False, f"Global\\{self.name}")
        if not self._handle:
            return True  # 拿不到锁句柄也别拦着用户用
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def lower_priority(logger: logging.Logger | None = None) -> bool:
    log = logger or logging.getLogger(__name__)
    try:
        proc = psutil.Process()
        proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        log.info("进程优先级已设为 低于正常")
        return True
    except Exception as exc:
        log.warning("设置进程优先级失败: %s", exc)
        return False
