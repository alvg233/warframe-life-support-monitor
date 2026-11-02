r"""数字解析单元测试（纯逻辑，秒出）。

    $env:PYTHONPATH=".vendor"; python scripts\test_numbers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

from screen_ocr_alert.numbers import extract_numbers, format_value, parse_value  # noqa: E402

CASES = [
    ("86%", 86.0, 86.0),
    ("维生系统 86%", 86.0, 86.0),
    ("维生系统 86% 时间 17:54", 86.0, 86.0),
    ("86% +300", 86.0, 86.0),
    ("维生系统 86% +300 +1,200", 86.0, 86.0),
    ("+300%", 300.0, None),
    ("1,234", 1234.0, None),
    ("1 234", 1234.0, None),
    ("１２ ３４", 1234.0, None),
    ("８６％", 86.0, 86.0),
    ("12 .5%", 12.5, 12.5),
    ("-3.5", -3.5, None),
    ("1.2万", 12000.0, None),
    ("6th", 6.0, None),      # 没有 % 时 require_percent 必须拒绝
    ("", None, None),
    ("维生系统", None, None),
]


def main() -> int:
    ok = True
    for text, expect_any, expect_percent in CASES:
        got_any = parse_value(text, "first", False)
        got_pct = parse_value(text, "first", True)
        a = (got_any is None and expect_any is None) or (
            got_any is not None and expect_any is not None and abs(got_any - expect_any) < 1e-6
        )
        b = (got_pct is None and expect_percent is None) or (
            got_pct is not None and expect_percent is not None and abs(got_pct - expect_percent) < 1e-6
        )
        flag = "PASS" if a and b else "FAIL"
        ok = ok and a and b
        print(f"{flag}  {text!r:32} 普通={format_value(got_any):>8}  只认%={format_value(got_pct):>8}")

    # 取值方式
    multi = "甲 3 乙 9 丙 5"
    checks = {
        "first": 3.0, "last": 5.0, "max": 9.0, "min": 3.0, "sum": 17.0,
    }
    for mode, expect in checks.items():
        got = parse_value(multi, mode)
        flag = "PASS" if got == expect else "FAIL"
        ok = ok and got == expect
        print(f"{flag}  mode={mode:<6} 期望 {expect} 实际 {got}")

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
