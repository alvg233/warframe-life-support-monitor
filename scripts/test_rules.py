r"""报警策略 + 采样循环集成测试（假截图 / 假 OCR / 假钉钉 / 假记录器）。

覆盖：穿线记日志与截图、_EX 后缀、冷却、重提醒、黑屏保护、
以及"大跳变必须连续两轮确认"（防止单帧误读取值直接报警）。

    $env:PYTHONPATH=".vendor"; python scripts\test_rules.py
"""
from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

import numpy as np  # noqa: E402

import screen_ocr_alert.alerts as alerts_mod  # noqa: E402
from screen_ocr_alert import config as cfgmod  # noqa: E402
from screen_ocr_alert.alerts import AlertPolicy, RuleState, zone_of  # noqa: E402
from screen_ocr_alert.monitor import MonitorService  # noqa: E402
from screen_ocr_alert.ocr import TextLine  # noqa: E402

SENT: list[str] = []


class FakeBot:
    def __init__(self, conf=None, logger=None) -> None:
        pass

    def send_text(self, content, at_mobiles=None, at_all=None):
        SENT.append(content)
        return {"errcode": 0}


alerts_mod.DingTalk = FakeBot


class FakeCapture:
    """level 改变时画面才变化，用来测"画面没变就跳过 OCR"。"""

    def __init__(self) -> None:
        self.level = 0
        self.snapshot_calls = 0

    def grab_region_auto(self, region):
        return np.full((40, 120, 3), self.level, dtype=np.uint8), "fake"

    def grab_for_snapshot(self):
        from screen_ocr_alert.capture import Frame

        self.snapshot_calls += 1
        return Frame(image=np.full((60, 80, 3), 40, dtype=np.uint8), source="fake")


class FakeOcr:
    def __init__(self) -> None:
        self.text = ""
        self.calls = 0

    def recognize(self, _img, scale=None, rec_only=False):
        self.calls += 1
        if not self.text:
            return [], ""
        return [TextLine(text=self.text, score=0.98, box=(0, 0, 60, 16))], self.text


class FakeRecorder:
    def __init__(self) -> None:
        self.snapshots: list[tuple[str, str]] = []
        self.events: list[dict] = []

    def snapshot(self, rule=None, suffix="", reason=""):
        self.snapshots.append((reason, suffix))
        return f"snapshots/2026-10-30/2026-10-30_06-40-12-317{suffix}.png"

    def event(self, event, **fields):
        self.events.append({"event": event, **fields})

    def cleanup(self):
        return 0

    def of(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]


def make_cfg(warn=50.0, alert=35.0, cooldown=0, realert=0, recover=True) -> dict:
    cfg = copy.deepcopy(cfgmod.DEFAULT_CONFIG)
    cfg["sample"].update({"interval_sec": 1.5, "change_detection": True,
                          "diff_threshold": 1.5, "force_ocr_sec": 600,
                          "retry_count": 2, "retry_gap_sec": 0.0})
    cfg["guard"].update({"max_drop_per_1_5s": 1.0, "drop_tolerance": 1.0,
                         "agree_tolerance": 1.0, "min_confidence": 0.45})
    rule = cfg["rules"][0]
    rule["region"] = {"mode": "ratio", "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2}
    rule["number"] = {"mode": "first", "require_percent": True, "valid_range": [0, 100]}
    rule["warn"] = {"value": warn, "log": True, "snapshot": True, "suffix": ""}
    rule["alert"] = {"value": alert, "log": True, "snapshot": True, "suffix": "_EX",
                     "notify": True, "cooldown_sec": cooldown, "realert_sec": realert,
                     "template": "【{name}】值 {value} 低于 {alert}，截图 {snapshot}"}
    rule["recover"] = {"notify": recover, "snapshot": False}
    return cfg


class Harness:
    """一次采样 = 改画面 + 指定"距上次读数过了多少秒"。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.rule = cfg["rules"][0]
        self.cap = FakeCapture()
        self.ocr = FakeOcr()
        self.rec = FakeRecorder()
        self.policy = AlertPolicy(lambda: cfg, self.rec)
        self.svc = MonitorService(lambda: cfg, capture=self.cap, ocr=self.ocr,
                                  policy=self.policy, recorder=self.rec)

    @property
    def st(self) -> RuleState:
        return self.svc.state_of(self.rule["id"])

    def feed(self, text: str, level: int, elapsed: float = 30.0) -> None:
        self.ocr.text = text
        self.cap.level = level
        if self.st.last_read:
            self.st.last_read = time.time() - elapsed
        self.svc.tick(self.cfg)


def main() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + detail) if detail else ''}")
        ok = ok and cond

    # ---------- 正常区间 / 穿线 ----------
    cfg = make_cfg()
    h = Harness(cfg)
    SENT.clear()

    h.feed("维生系统 62%", 10)
    check("正常区间不记录", h.st.zone == "normal" and not h.rec.events, str(h.st.zone))

    h.feed("维生系统 48%", 60)
    check("跌破预警线进入 warn", h.st.zone == "warn", h.st.zone)
    check("记录 warn_cross", len(h.rec.of("warn_cross")) == 1)
    check("预警截图无后缀", h.rec.snapshots[-1][1] == "", str(h.rec.snapshots))
    check("预警不发钉钉", len(SENT) == 0, str(len(SENT)))

    calls = h.ocr.calls
    h.svc.tick(cfg)
    check("画面未变跳过 OCR", h.ocr.calls == calls, f"{calls} -> {h.ocr.calls}")

    h.feed("维生系统 31%", 120)
    check("跌破报警线进入 alert", h.st.zone == "alert", h.st.zone)
    check("记录 alert_cross", len(h.rec.of("alert_cross")) == 1)
    check("报警截图带 _EX", h.rec.snapshots[-1][1] == "_EX", str(h.rec.snapshots))
    check("报警发钉钉", len(SENT) == 1 and "31" in SENT[0], SENT[-1][:40] if SENT else "")

    snaps = len(h.rec.snapshots)
    h.feed("维生系统 44%", 160)
    check("回到 warn 区", h.st.zone == "warn", h.st.zone)
    check("发送恢复通知", len(SENT) == 2 and "恢复" in SENT[-1], SENT[-1][:40] if SENT else "")
    check("恢复不截图", len(h.rec.snapshots) == snaps, str(len(h.rec.snapshots)))

    h.feed("维生系统 70%", 200)
    check("回到 normal", h.st.zone == "normal", h.st.zone)
    check("记录 normal_cross", len(h.rec.of("normal_cross")) == 1)

    # ---------- 大跳变：必须连续两轮 ----------
    cfg2 = make_cfg()
    h2 = Harness(cfg2)
    SENT.clear()
    h2.feed("维生系统 100%", 10)
    h2.feed("维生系统 0%", 20, elapsed=1.5)
    check("单帧 0% 不报警", len(SENT) == 0, str(len(SENT)))
    check("跳变被挂起等待确认", h2.st.pending_value == 0.0, str(h2.st.pending_value))
    check("画面值没有被改写", h2.st.value == 100.0, str(h2.st.value))
    h2.feed("维生系统 0%", 30, elapsed=1.5)
    check("连续两轮 0% 才报警", len(SENT) == 1 and h2.st.zone == "alert",
          f"SENT={len(SENT)} zone={h2.st.zone}")

    # ---------- 重提醒 ----------
    cfg3 = make_cfg(realert=1800)
    h3 = Harness(cfg3)
    SENT.clear()
    h3.feed("维生系统 20%", 10)
    h3.st.last_notify -= 2000
    h3.feed("维生系统 20%", 20)
    check("持续报警会重提醒", len(SENT) == 2, str(len(SENT)))
    check("重提醒事件被记录", len(h3.rec.of("alert_repeat")) == 1, str(len(h3.rec.of("alert_repeat"))))

    # ---------- 冷却 ----------
    cfg4 = make_cfg(cooldown=600)
    rec4 = FakeRecorder()
    policy4 = AlertPolicy(lambda: cfg4, rec4)
    st4 = RuleState()
    SENT.clear()
    policy4._send(cfg4["rules"][0], st4, "第一条", 600)
    policy4._send(cfg4["rules"][0], st4, "第二条", 600)
    check("冷却期内第二条被丢弃", len(SENT) == 1, str(len(SENT)))

    # ---------- 黑屏保护 ----------
    cfg5 = make_cfg()
    rec5 = FakeRecorder()

    class BlackCapture(FakeCapture):
        def grab_region_auto(self, region):
            return np.zeros((40, 120, 3), dtype=np.uint8), "fake"

    cap5, ocr5 = BlackCapture(), FakeOcr()
    policy5 = AlertPolicy(lambda: cfg5, rec5)
    svc5 = MonitorService(lambda: cfg5, capture=cap5, ocr=ocr5, policy=policy5, recorder=rec5)
    ocr5.text = "维生系统 10%"
    SENT.clear()
    svc5.tick(cfg5)
    svc5.tick(cfg5)
    check("黑屏期间不报警", len(SENT) == 0, str(len(SENT)))
    check("黑屏只记一次日志", len(rec5.of("black_frame")) == 1, str(len(rec5.of("black_frame"))))
    check("黑屏不产生读数", svc5.state_of(cfg5["rules"][0]["id"]).value is None)

    check("zone 判定", zone_of(60, 50, 35) == "normal" and zone_of(40, 50, 35) == "warn"
          and zone_of(20, 50, 35) == "alert")

    # ---------- 急停成功后自动停止监控 ----------
    from screen_ocr_alert.emergency import EmergencyResult

    class FakeEmergency:
        capture = None
        fired = False

        def check(self, rule, value):
            return EmergencyResult(triggered=True, success=True, reason="测试",
                                   reads=[None] * 5)

    cfg_e = make_cfg()
    rec_e = FakeRecorder()
    cap_e = FakeCapture()
    cap_e.level = 100          # 非黑屏，否则会在黑屏保护处提前返回
    svc_e = MonitorService(lambda: cfg_e, capture=cap_e, ocr=FakeOcr(),
                           policy=AlertPolicy(lambda: cfg_e, rec_e), recorder=rec_e,
                           emergency=FakeEmergency())
    svc_e.tick(cfg_e)
    check("急停成功后自动停止监控", svc_e._stop.is_set())

    # ---------- 开关：关掉钉钉 / 本地记录 ----------
    cfg7 = make_cfg()
    cfg7["rules"][0]["alert"].update({"notify": False, "log": False})
    cfg7["rules"][0]["warn"].update({"log": False})
    h7 = Harness(cfg7)
    SENT.clear()
    h7.feed("维生系统 20%", 10)
    check("关掉钉钉提醒后不发送", len(SENT) == 0, str(len(SENT)))
    check("关掉本地记录后不写事件", len(h7.rec.of("alert_cross")) == 0,
          str([e["event"] for e in h7.rec.events]))

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
