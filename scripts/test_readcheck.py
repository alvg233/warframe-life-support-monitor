r"""识别判定单元测试：降速校验、重读确认、作废、上升放行。

    $env:PYTHONPATH=".vendor"; python scripts\test_readcheck.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

import numpy as np  # noqa: E402

from screen_ocr_alert import config as cfgmod  # noqa: E402
from screen_ocr_alert.ocr import TextLine  # noqa: E402
from screen_ocr_alert.readcheck import ReadGate  # noqa: E402


class FakeOcr:
    """按"每次读取"给一段文本：一次读取内的多个变体返回同一句。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0
        self.reads = 0
        self.current = texts[0] if texts else ""

    def begin_read(self):
        idx = min(self.reads, len(self.texts) - 1)
        self.reads += 1
        self.current = self.texts[idx] if self.texts else ""

    def recognize(self, _img, scale=None, rec_only=False):
        self.calls += 1
        text = self.current
        if not text:
            return [], ""
        return [TextLine(text=text, score=0.97, box=(0, 0, 40, 16))], text


def make_cfg(retry_count: int = 2, gap: float = 0.0) -> dict:
    cfg = copy.deepcopy(cfgmod.DEFAULT_CONFIG)
    cfg["sample"].update({"retry_count": retry_count, "retry_gap_sec": gap})
    cfg["guard"].update({"max_drop_per_1_5s": 1.0, "drop_tolerance": 0.5,
                         "agree_tolerance": 1.0, "min_confidence": 0.45})
    cfg["rules"][0]["number"] = {"mode": "first", "require_percent": True,
                                 "valid_range": [0, 100]}
    return cfg


class Log:
    def info(self, *a, **k):
        pass

    warning = info
    error = info
    debug = info


IMG = np.zeros((40, 120, 3), dtype=np.uint8)


def main() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + detail) if detail else ''}")
        ok = ok and cond

    rule = make_cfg()["rules"][0]

    # 1) 正常小幅下降：直接采纳，不做额外重读
    cfg = make_cfg()
    ocr = FakeOcr(["50%", "49%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=50.0, elapsed=1.5)
    check("小幅下降直接采纳", r.value == 50.0 and ocr.calls == 1, f"value={r.value} calls={ocr.calls}")

    # 2) 1.5 秒内掉 10 个点：判定错读，重读两次一致 -> 作废（因为读数仍不一致时）
    cfg = make_cfg()
    ocr = FakeOcr(["40%", "41%", "40%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=50.0, elapsed=1.5)
    check("可疑降速触发重读", ocr.calls == 3, f"calls={ocr.calls}")
    check("重读一致后确认新值", r.value is not None and abs(r.value - 40) <= 1.0 and r.confirmed,
          f"value={r.value} confirmed={r.confirmed} reason={r.reason}")

    # 3) 可疑且重读结果互相矛盾 -> 本次作废
    cfg = make_cfg()
    ocr = FakeOcr(["30%", "48%", "12%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=50.0, elapsed=1.5)
    check("读数矛盾则作废", r.value is None, f"value={r.value} reason={r.reason}")
    check("作废保留原始读数便于追查", r.rejected == 30.0, str(r.rejected))
    check("作废时没有采纳任何值", not r.confirmed)

    # 4) 上升无限制：1.5 秒涨 40 个点也直接采纳
    cfg = make_cfg()
    ocr = FakeOcr(["90%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=50.0, elapsed=1.5)
    check("上升直接采纳", r.value == 90.0 and ocr.calls == 1, f"value={r.value} calls={ocr.calls}")

    # 5) 数字不合法（识别不到）-> 重读后仍无 -> 失败，不改变 last_value
    cfg = make_cfg()
    ocr = FakeOcr(["维生系统", "维生", ""])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=50.0, elapsed=1.5)
    check("识别不到数字时作废", r.value is None and ocr.calls >= 3,
          f"calls={ocr.calls} {r.reason}")

    # 6) 第一次不合法，第二次读到合法值 -> 只采纳一次合法读数（需两次一致，作废）
    cfg = make_cfg()
    ocr = FakeOcr(["维生系统", "48%", "维生系统"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=None, elapsed=None)
    check("仅一次合法读数不轻易采纳", r.value is None, f"value={r.value} reason={r.reason}")

    # 7) 首次不合法，重读两次一致 -> 采纳
    cfg = make_cfg()
    ocr = FakeOcr(["维生系统", "48%", "48.5%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=None, elapsed=None)
    check("重读一致后采纳", r.value is not None and abs(r.value - 48.25) < 0.6,
          f"value={r.value}")

    # 8) 超出有效范围（水印误读成 999）-> 视为不合法
    cfg = make_cfg()
    ocr = FakeOcr(["999%", "999%", "999%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=None, elapsed=None)
    check("超出范围被拒绝", r.value is None, f"value={r.value} reason={r.reason}")

    # 9) 长时间未读（比如黑屏半小时）后大幅下降应被放行，不当作错读
    cfg = make_cfg()
    ocr = FakeOcr(["20%"])
    gate = ReadGate(lambda: cfg, Log(), ocr)
    r = gate.read(lambda: IMG, rule, last_value=90.0, elapsed=1800.0)
    check("长时间间隔放行大幅下降", r.value == 20.0 and ocr.calls == 1,
          f"value={r.value} calls={ocr.calls}")

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
