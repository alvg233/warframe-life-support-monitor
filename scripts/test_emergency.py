r"""急停逻辑测试：零值触发 / 时间触发 / 5 次确认 / 锁 / 时间解析。

    $env:PYTHONPATH=".vendor"; python scripts\test_emergency.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

import numpy as np  # noqa: E402

import screen_ocr_alert.emergency as emg_mod  # noqa: E402
from screen_ocr_alert import config as cfgmod  # noqa: E402
from screen_ocr_alert.emergency import EmergencyStopper  # noqa: E402
from screen_ocr_alert.numbers import format_clock, parse_clock  # noqa: E402
from screen_ocr_alert.ocr import TextLine  # noqa: E402

ACTIONS: list[str] = []


def fake_activate(hwnd):
    ACTIONS.append(f"activate:{hwnd}")
    return True, "mock"


def fake_press_key(vk=0):
    ACTIONS.append("esc")
    return True, "mock"


emg_mod.activate_window = fake_activate
emg_mod.press_key = fake_press_key


class FakeCapture:
    def __init__(self):
        self.snap_calls = 0

    def grab_region_auto(self, region):
        return np.full((40, 120, 3), 20, dtype=np.uint8), "fake"

    def _hwnd(self):
        return 4242


class ScriptOcr:
    """按顺序喂文本；用完之后保持最后一句。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def recognize(self, _img, scale=None, rec_only=False):
        idx = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        text = self.texts[idx]
        if not text:
            return [], ""
        return [TextLine(text=text, score=0.99, box=(0, 0, 60, 16))], text


def make_rule(**emg) -> dict:
    cfg = copy.deepcopy(cfgmod.DEFAULT_CONFIG)
    rule = cfg["rules"][0]
    rule["number"] = {"mode": "first", "require_percent": True, "valid_range": [0, 100]}
    emergency = rule["emergency"]
    emergency.update({"enabled": True, "verify_reads": 5, "verify_interval_sec": 0.01, "time_read_every_n": 1,
                      "activate_window": True, "press_key": "esc", "notify": False})
    emergency["triggers"] = dict(emergency.get("triggers") or {})
    emergency.update(emg)
    return rule


def main() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + detail) if detail else ''}")
        ok = ok and cond

    # 时间解析
    from screen_ocr_alert.emergency import looks_like_new_round

    check("新一局判定：归零", looks_like_new_round(13100.0, 5.0))
    check("新一局判定：小幅波动不算", not looks_like_new_round(13100.0, 13095.0))
    check("43:27 -> 2607 秒", parse_clock("时间 43:27") == 2607, str(parse_clock("时间 43:27")))
    check("191:31 -> 11491 秒（不能丢千位）", parse_clock("时间: \n191:31") == 11491,
          str(parse_clock("时间: \n191:31")))
    check("1:02:33 -> 3753 秒", parse_clock("1:02:33") == 3753, str(parse_clock("1:02:33")))
    check("12分30秒 -> 750 秒", parse_clock("12分30秒") == 750, str(parse_clock("12分30秒")))
    check("无时间返回 None", parse_clock("维生系统 86%") is None)
    check("格式化", format_clock(2607) == "43:27", format_clock(2607))

    # 零值触发 + 5 次都读不到 -> 成功
    ACTIONS.clear()
    rule = make_rule(triggers={"zero_percent": True, "elapsed_minutes": False})
    cfg = {"dingtalk": {}, "snapshot": {"backend": "window"}}
    cap = FakeCapture()
    ocr = ScriptOcr([""] * 10)          # 验证阶段全部识别失败
    stopper = EmergencyStopper(lambda: cfg, cap, ocr, notify=None)
    res = stopper.check(rule, 0.0)
    check("零值触发", res is not None and res.triggered, str(res and res.reason))
    check("动作顺序：切前台 -> ESC", ACTIONS == ["activate:4242", "esc"], str(ACTIONS))
    check("5 次都无数字 = 急停成功", res.success and len(res.reads) == 5, res.detail)
    check("触发后加锁不重复", stopper.check(rule, 0.0) is None)

    # 有一次读到数字 -> 未确认
    ACTIONS.clear()
    rule2 = make_rule(triggers={"zero_percent": True, "elapsed_minutes": False})
    ocr2 = ScriptOcr(["", "", "维生系统 12%", "", ""])
    stopper2 = EmergencyStopper(lambda: cfg, FakeCapture(), ocr2, notify=None)
    res2 = stopper2.check(rule2, 0.0)
    check("有一次读到数字 = 未确认", res2 is not None and not res2.success,
          f"{res2.detail} success={res2.success}")

    # 未归零不触发
    stopper3 = EmergencyStopper(lambda: cfg, FakeCapture(), ScriptOcr([""]), notify=None)
    check("数值不为零不触发", stopper3.check(make_rule(
        triggers={"zero_percent": True, "elapsed_minutes": False}), 12.0) is None)

    # 时间触发
    ACTIONS.clear()
    rule4 = make_rule(triggers={"zero_percent": False, "elapsed_minutes": True},
                      elapsed_minutes=10)
    rule4["emergency"]["time_region"] = {"mode": "ratio", "x": 0, "y": 0, "w": 0.1, "h": 0.1}
    ocr4 = ScriptOcr(["时间 10:05"] + [""] * 8)   # 第一次读时间，之后是验证
    stopper4 = EmergencyStopper(lambda: cfg, FakeCapture(), ocr4, notify=None)
    res4 = stopper4.check(rule4, 80.0)            # 主数值还很高，但时间到了
    check("时间到达触发", res4 is not None and "时间" in (res4.reason or ""), str(res4 and res4.reason))
    check("时间触发后也按 ESC", "esc" in ACTIONS, str(ACTIONS))

    # 时间没到不触发
    ocr5 = ScriptOcr(["时间 09:30"])
    stopper5 = EmergencyStopper(lambda: cfg, FakeCapture(), ocr5, notify=None)
    check("时间未到不触发", stopper5.check(rule4, 80.0) is None)

    # 都没勾 -> 不触发
    rule6 = make_rule(triggers={"zero_percent": False, "elapsed_minutes": False})
    stopper6 = EmergencyStopper(lambda: cfg, FakeCapture(), ScriptOcr(["时间 99:99"]), notify=None)
    check("两个条件都不选则不触发", stopper6.check(rule6, 0.0) is None)

    # ---------- 时间合理性校验 ----------
    rule7 = make_rule(triggers={"zero_percent": False, "elapsed_minutes": True},
                      elapsed_minutes=200)
    rule7["emergency"]["time_region"] = {"mode": "ratio", "x": 0, "y": 0, "w": 0.1, "h": 0.1}
    texts7 = ["时间 100:00",    # 基准
              "时间 1100:00",   # 多认一位（涨幅不合理 -> 丢弃，不会重读）
              "时间 100:02",    # 正常递增
              "时间 5:00", "时间 5:00"]  # 归零/换局（重读确认）
    seen7: list[float] = []
    st7 = EmergencyStopper(lambda: cfg, FakeCapture(), ScriptOcr(texts7), notify=None,
                           on_time=lambda s, t: seen7.append(s))
    check("首次读时间不触发", st7.check(rule7, 90.0) is None)
    check("时间跳变（多认一位）被丢弃", st7.check(rule7, 90.0) is None, str(seen7))
    check("丢弃后不更新上次时间", st7._last_time == 6000.0, str(st7._last_time))
    check("正常递增被接受", st7.check(rule7, 90.0) is None and st7._last_time == 6002.0,
          str(st7._last_time))
    check("时间归零经确认后接受", st7.check(rule7, 90.0) is None and st7._last_time == 300.0,
          str(st7._last_time))

    # 基线读错（05:02）后，正确值 205:14 应在下一轮被接受
    rule8 = make_rule(triggers={"zero_percent": False, "elapsed_minutes": True},
                      elapsed_minutes=300)
    rule8["emergency"]["time_region"] = {"mode": "ratio", "x": 0, "y": 0, "w": 0.1, "h": 0.1}
    rule8["emergency"]["time_read_every_n"] = 1
    st8 = EmergencyStopper(lambda: cfg, FakeCapture(),
                           ScriptOcr(["时间 05:02", "时间 205:14", "时间 205:14"]),
                           notify=None)
    st8.check(rule8, 90.0)                       # 基线 302
    check("错误基线下的正确大值先挂起", st8.check(rule8, 90.0) is None,
          str(st8._last_time))
    check("下一轮同值则接受并修正基线",
          st8.check(rule8, 90.0) is None and st8._last_time == 12314.0,
          str(st8._last_time))

    # 基线被读小（02:33）后，正常前进的计时值应被接受，而不是无限挂起
    rule9 = make_rule(triggers={"zero_percent": False, "elapsed_minutes": True},
                      elapsed_minutes=300)
    rule9["emergency"]["time_region"] = {"mode": "ratio", "x": 0, "y": 0, "w": 0.1, "h": 0.1}
    rule9["emergency"]["time_read_every_n"] = 1
    st9 = EmergencyStopper(lambda: cfg, FakeCapture(),
                           ScriptOcr(["时间 02:33", "时间 23:22", "时间 23:27"]),
                           notify=None)
    st9.check(rule9, 90.0)                       # 错误基线 153 秒
    check("错误基线后大值先挂起", st9.check(rule9, 90.0) is None, str(st9._pending_time))
    check("计时器正常前进即接受（不再卡在挂起）",
          st9.check(rule9, 90.0) is None and st9._last_time == 1407.0,
          str(st9._last_time))

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
