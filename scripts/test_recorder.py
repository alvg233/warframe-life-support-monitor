r"""截图与事件日志测试：真实时间命名、_EX 后缀、只记业务事件、失败不影响主流程。

    $env:PYTHONPATH=".vendor"; python scripts\test_recorder.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

HOME = Path(tempfile.mkdtemp(prefix="soa-rec-"))
os.environ["SCREEN_OCR_ALERT_HOME"] = str(HOME)

import numpy as np  # noqa: E402

from screen_ocr_alert import config as cfgmod  # noqa: E402
from screen_ocr_alert.capture import Frame  # noqa: E402
from screen_ocr_alert.recorder import Recorder  # noqa: E402


class FakeCapture:
    def __init__(self, black=False):
        self.black = black

    def grab_for_snapshot(self):
        img = (np.zeros((48, 64, 3), dtype=np.uint8) if self.black
               else np.full((48, 64, 3), 90, np.uint8))
        return Frame(image=img, source="fake")


def read_events() -> list[dict]:
    path = HOME / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + detail) if detail else ''}")
        ok = ok and cond

    cfg = cfgmod.load(HOME / "config.json")
    rule = cfg["rules"][0]
    rec = Recorder(lambda: cfg, FakeCapture())

    path = rec.snapshot(rule, "_EX", reason="跌破报警线")
    check("返回相对路径", bool(path) and path.endswith(".png"), str(path))
    parts = Path(path).parts
    check("落在 snapshots/日期 下", len(parts) == 3 and parts[0] == "snapshots", str(parts))
    check("文件真的存在", (HOME / path).exists(), str(HOME / path))
    check("文件名是真实时间 + _EX", bool(re.search(
        r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{3}_EX\.png$", Path(path).name)), Path(path).name)

    path2 = rec.snapshot(rule, "", reason="跌破预警线")
    check("预警截图无后缀", "_EX" not in Path(path2).name, Path(path2).name)

    # 截图本身不再写事件（避免噪音）
    check("截图不产生事件记录", read_events() == [], str(read_events()))

    # 业务事件要记
    rec.event("warn_cross", rule="维生系统", value=48.0, prev=62.0, warn=50.0,
              alert=35.0, snapshot=path2)
    rec.event("alert_cross", rule="维生系统", value=31.0, prev=48.0, warn=50.0,
              alert=35.0, snapshot=path)
    events = read_events()
    kinds = [e["event"] for e in events]
    check("记录跌破预警线/报警线", kinds == ["warn_cross", "alert_cross"], str(kinds))
    check("事件含截图与阈值", events[0].get("snapshot") and events[0].get("alert") == 35.0)
    check("事件含时间戳", all("time" in e for e in events))

    # 技术噪音不记
    rec.event("black_frame", rule="维生系统", source="fake")
    rec.event("snapshot_failed", reason="测试", error="模拟")
    rec.event("read_failed", rule="维生系统", reason="没有解析出数字")
    rec.event("notify_failed", rule="维生系统", error="模拟")
    kinds = [e["event"] for e in read_events()]
    check("技术噪音不进记录", kinds == ["warn_cross", "alert_cross"], str(kinds))

    # 截图开关关闭时不落盘
    cfg["snapshot"]["enabled"] = False
    rec2 = Recorder(lambda: cfg, FakeCapture())
    check("关闭截图则不保存", rec2.snapshot(rule, "_EX", reason="测试") is None)
    cfg["snapshot"]["enabled"] = True

    # 截图失败要返回 None 而不是崩
    class Broken:
        def grab_for_snapshot(self):
            from screen_ocr_alert.capture import CaptureError

            raise CaptureError("模拟失败")

    rec3 = Recorder(lambda: cfg, Broken())
    check("截图失败返回 None", rec3.snapshot(rule, "_EX", "测试") is None)
    kinds = [e["event"] for e in read_events()]
    check("截图失败也不污染记录", kinds == ["warn_cross", "alert_cross"], str(kinds))

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
