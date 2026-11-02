"""事件日志 + 截图存档（只管"留证据"，不判断要不要报警）。

- 截图落在 <程序目录>/snapshots/YYYY-MM-DD/ 下，文件名就是真实时间：
    2026-10-30_06-40-12-317.png        预警截图
    2026-10-30_06-40-12-317_EX.png     报警截图（后缀可配）
- 结构化事件写 logs/events.jsonl，一行一个 JSON，方便以后统计"这周掉了多少次"。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .capture import CaptureError


class Recorder:
    # 只有这几类"业务事件"才写入 events.jsonl：
    # 跌破预警线 / 跌破报警线 / 持续低于报警线 / 恢复正常。
    # 识别失败、黑屏、截图失败这类技术噪音只进 app.log，不污染业务记录。
    BUSINESS_EVENTS = frozenset({"warn_cross", "alert_cross", "alert_repeat", "recover",
                                 "emergency_stop"})

    def __init__(self, cfg_provider, capture, logger: logging.Logger | None = None) -> None:
        self.cfg_provider = cfg_provider
        self.capture = capture
        self.log = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    # ---------- 路径 ----------
    def _base_dir(self) -> Path:
        from . import config as cfgmod

        snap = (self.cfg_provider().get("snapshot") or {})
        sub = str(snap.get("dir") or "snapshots")
        return cfgmod.data_dir() / sub

    def events_path(self) -> Path:
        from . import config as cfgmod

        return cfgmod.log_dir() / "events.jsonl"

    # ---------- 事件 ----------
    def event(self, event: str, **fields) -> dict:
        from . import config as cfgmod

        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "epoch": round(time.time(), 3),
            "event": event,
            "host": os.environ.get("COMPUTERNAME", ""),
            "pid": os.getpid(),
        }
        record.update(fields)
        if event not in self.BUSINESS_EVENTS:
            # 技术噪音：只写运行日志，不进事件表
            self.log.debug("（技术事件，不计入记录）%s %s", event, fields)
            return record
        try:
            path = self.events_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                # 事件日志会持续增长：超过 5MB 滚动一份旧档，避免无限变大
                try:
                    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                        os.replace(path, path.with_suffix(path.suffix + ".1"))
                except OSError:
                    pass
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.log.warning("事件日志写入失败: %s", exc)
        return record

    # ---------- 截图 ----------
    def snapshot(self, rule: dict | None = None, suffix: str = "",
                 reason: str = "") -> str | None:
        """截一张图存盘，返回相对路径（相对程序目录）；失败返回 None。"""
        cfg = self.cfg_provider()
        snap_cfg = cfg.get("snapshot") or {}
        if not snap_cfg.get("enabled", True):
            return None

        now = datetime.now()
        try:
            frame = self.capture.grab_for_snapshot()
        except CaptureError as exc:
            self.log.error("截图失败（%s）：%s", reason, exc)
            self.event("snapshot_failed", reason=reason, rule=(rule or {}).get("name", ""),
                       error=str(exc))
            return None
        except Exception as exc:
            self.log.error("截图异常（%s）：%s", reason, exc)
            self.event("snapshot_failed", reason=reason, rule=(rule or {}).get("name", ""),
                       error=str(exc))
            return None

        day_dir = self._base_dir() / now.strftime("%Y-%m-%d")
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            name = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}-{now.microsecond // 1000:03d}{suffix}.png"
            path = day_dir / name
            from PIL import Image

            Image.fromarray(frame.image[:, :, ::-1]).save(path)
        except Exception as exc:
            self.log.error("截图保存失败: %s", exc)
            self.event("snapshot_failed", reason=reason, error=str(exc))
            return None

        try:
            rel = str(path.relative_to(self._base_dir().parent))
        except ValueError:
            rel = str(path)
        self.log.info("已保存截图 %s（%s，通道 %s）", rel, reason, frame.source)
        self.event("snapshot", reason=reason, rule=(rule or {}).get("name", ""),
                   path=rel, source=frame.source, black=frame.black)
        return rel

    # ---------- 清理 ----------
    def cleanup(self) -> int:
        """按 keep_days 清理旧截图目录，返回删除的目录数。"""
        now = time.time()
        if now - self._last_cleanup < 3600:
            return 0
        self._last_cleanup = now
        try:
            days = int((self.cfg_provider().get("snapshot") or {}).get("keep_days", 30) or 0)
        except (TypeError, ValueError):
            days = 30
        if days <= 0:
            return 0
        base = self._base_dir()
        if not base.exists():
            return 0
        cutoff = datetime.now() - timedelta(days=days)
        removed = 0
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                day = datetime.strptime(child.name, "%Y-%m-%d")
            except ValueError:
                continue
            if day < cutoff:
                try:
                    for f in child.iterdir():
                        f.unlink(missing_ok=True)
                    child.rmdir()
                    removed += 1
                except OSError as exc:
                    self.log.warning("清理旧截图失败 %s: %s", child, exc)
        if removed:
            self.log.info("已清理 %d 个过期截图目录（保留 %d 天）", removed, days)
        return removed
