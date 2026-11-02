"""报警策略（只管"什么时候记日志、什么时候截图、什么时候发消息"）。

两条线：
  预警线 warn （默认 50）：从上方跌破 -> 记日志 + 截图（文件名无后缀）
  报警线 alert（默认 35）：从上方跌破 -> 记日志 + 截图（后缀 _EX）+ 钉钉
回升跨回报警线 -> 可选的"已恢复正常"通知；跨回预警线只记日志。

"跌破"指的是穿过，不是每次读数低于就发——所以不会一直骚扰，
持续低于报警线时按 realert_sec 重提醒（例如每 30 分钟提醒一次）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from .dingtalk import DingTalk, DingTalkError

ZONE_NORMAL = "normal"
ZONE_WARN = "warn"
ZONE_ALERT = "alert"


def zone_of(value: float | None, warn_v: float, alert_v: float) -> str:
    if value is None:
        return "unknown"
    if value < alert_v:
        return ZONE_ALERT
    if value < warn_v:
        return ZONE_WARN
    return ZONE_NORMAL


@dataclass
class RuleState:
    zone: str = "unknown"
    value: float | None = None
    prev: float | None = None
    text: str = ""
    score: float = 0.0
    last_read: float = 0.0
    last_check: float = 0.0
    last_change: float = 0.0
    last_notify: float = 0.0
    fail_count: int = 0
    fail_notified: bool = False
    black: bool = False
    rejected: int = 0
    fingerprint: object = None
    last_snapshot: str = ""
    rect: dict | None = None
    learned_region: dict | None = None   # 自动重定位后学到的小框
    pending_value: float | None = None   # 疑似跳变、等下一轮确认的值
    pending_at: float = 0.0


class AlertPolicy:
    def __init__(self, cfg_provider, recorder, logger: logging.Logger | None = None) -> None:
        self.cfg_provider = cfg_provider
        self.recorder = recorder
        self.log = logger or logging.getLogger(__name__)
        self.on_event = None  # UI 回调

    # ---------- 工具 ----------
    @staticmethod
    def _f(d: dict, key: str, default: float) -> float:
        try:
            return float((d or {}).get(key, default))
        except (TypeError, ValueError):
            return default

    def _emit(self, evt: dict) -> None:
        if self.on_event:
            try:
                self.on_event(evt)
            except Exception:
                self.log.debug("UI 回调异常", exc_info=True)

    def _render(self, template: str, rule: dict, **extra) -> str:
        from .numbers import format_value

        def _fmt(key, default="-"):
            val = extra.get(key, default)
            if isinstance(val, (int, float)):
                return format_value(float(val))
            return val

        fields = {
            "name": rule.get("name", "规则"),
            "warn": _fmt("warn"),
            "alert": _fmt("alert"),
            "value": _fmt("value"),
            "prev": _fmt("prev"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host": os.environ.get("COMPUTERNAME", "unknown"),
            "snapshot": extra.get("snapshot", "（截图保存失败）"),
            "text": extra.get("text", ""),
        }
        try:
            return template.format(**fields)
        except (KeyError, IndexError, ValueError):
            return (
                f"【{fields['name']}】当前值 {fields['value']}，"
                f"已低于报警线 {fields['alert']}。\n时间：{fields['time']}"
            )

    def _send(self, rule: dict, st: RuleState, content: str, cooldown: float) -> bool:
        now = time.time()
        if cooldown > 0 and (now - st.last_notify) < cooldown:
            self.log.info("通知冷却中（%.0fs 内已发过），本次跳过发送", cooldown)
            return False

        # 网络请求放到后台线程：钉钉超时(默认 8s)不能把采样循环卡住
        st.last_notify = now

        def worker() -> None:
            try:
                DingTalk(self.cfg_provider().get("dingtalk", {})).send_text(content)
            except DingTalkError as exc:
                self.log.error("钉钉发送失败: %s", exc)
                self.recorder.event("notify_failed", rule=rule.get("name", ""), error=str(exc))
                self._emit({"type": "error", "message": f"钉钉发送失败: {exc}"})
                return
            except Exception as exc:
                self.log.error("钉钉发送异常: %s", exc)
                self._emit({"type": "error", "message": f"钉钉发送失败: {exc}"})
                return
            self.log.info("已发送钉钉提醒：%s", content.splitlines()[0] if content else "")
            self.recorder.event("notify", rule=rule.get("name", ""), content=content)
            self._emit({"type": "notify", "message": content})

        threading.Thread(target=worker, name="dingtalk", daemon=True).start()
        return True

    # ---------- 读数事件 ----------
    def on_read(self, rule: dict, st: RuleState, value: float, text: str, score: float) -> None:
        warn_v = self._f(rule.get("warn"), "value", 50.0)
        alert_v = self._f(rule.get("alert"), "value", 35.0)
        prev_value = st.value
        prev_zone = st.zone
        zone = zone_of(value, warn_v, alert_v)
        changed = zone != prev_zone

        self._emit({"type": "sample", "rule": rule.get("name"), "value": value,
                    "text": text, "score": score, "zone": zone})

        if not changed:
            st.prev = prev_value
            st.value = value
            st.zone = zone
            # 持续处于报警区：按重提醒间隔再发（并重新截图留证）
            if zone == ZONE_ALERT:
                self._maybe_realert(rule, st, value, warn_v, alert_v)
            return

        if zone == ZONE_ALERT:
            self._enter_alert(rule, st, value, prev_value, warn_v, alert_v)
        elif zone == ZONE_WARN and prev_zone in (ZONE_NORMAL, "unknown"):
            self._enter_warn(rule, st, value, prev_value, warn_v, alert_v)
        elif zone == ZONE_NORMAL and prev_zone in (ZONE_WARN, ZONE_ALERT):
            self._back_to_normal(rule, st, value, prev_value, warn_v, alert_v)
        elif zone == ZONE_WARN and prev_zone == ZONE_ALERT:
            self._recover_from_alert(rule, st, value, prev_value, warn_v, alert_v, to="warn")

        st.prev = prev_value
        st.value = value
        st.zone = zone

    def _enter_alert(self, rule, st, value, prev_value, warn_v, alert_v) -> None:
        alert_cfg = rule.get("alert") or {}
        suffix = str(alert_cfg.get("suffix", "_EX"))
        snapshot = self.recorder.snapshot(rule, suffix, reason="跌破报警线") if alert_cfg.get(
            "snapshot", True) else None
        st.last_snapshot = snapshot or ""
        if alert_cfg.get("log", True):
            self.recorder.event(
                "alert_cross", rule=rule.get("name", ""), value=value, prev=prev_value,
                alert=alert_v, warn=warn_v, snapshot=snapshot or "",
            )
        self.log.warning("【%s】跌破报警线 %s：%s -> %s", rule.get("name"),
                         alert_v, prev_value, value)
        if alert_cfg.get("notify", True):
            content = self._render(
                alert_cfg.get("template") or "【{name}】当前值 {value}，已低于报警线 {alert}。",
                rule, warn=warn_v, alert=alert_v, value=value, prev=prev_value,
                snapshot=snapshot, text=st.text,
            )
            self._send(rule, st, content, self._f(alert_cfg, "cooldown_sec", 60))

    def _enter_warn(self, rule, st, value, prev_value, warn_v, alert_v) -> None:
        warn_cfg = rule.get("warn") or {}
        suffix = str(warn_cfg.get("suffix", ""))
        snapshot = self.recorder.snapshot(rule, suffix, reason="跌破预警线") if warn_cfg.get(
            "snapshot", True) else None
        st.last_snapshot = snapshot or ""
        if warn_cfg.get("log", True):
            self.recorder.event(
                "warn_cross", rule=rule.get("name", ""), value=value, prev=prev_value,
                warn=warn_v, alert=alert_v, snapshot=snapshot or "",
            )
        self.log.info("【%s】跌破预警线 %s：%s -> %s", rule.get("name"), warn_v, prev_value, value)

    def _back_to_normal(self, rule, st, value, prev_value, warn_v, alert_v) -> None:
        was_alert = st.zone == ZONE_ALERT
        if (rule.get("alert") or {}).get("log", True):
            self.recorder.event("normal_cross", rule=rule.get("name", ""), value=value,
                                prev=prev_value, warn=warn_v, alert=alert_v)
        self.log.info("【%s】回到正常区间：%s -> %s", rule.get("name"), prev_value, value)
        if was_alert:
            self._recover_from_alert(rule, st, value, prev_value, warn_v, alert_v, to="normal")

    def _recover_from_alert(self, rule, st, value, prev_value, warn_v, alert_v, to: str) -> None:
        rec = rule.get("recover") or {}
        if (rule.get("alert") or {}).get("log", True):
            self.recorder.event("recover", rule=rule.get("name", ""), value=value,
                                prev=prev_value, to=to)
        if not rec.get("notify", True):
            return
        content = (
            f"【{rule.get('name')}】已恢复，当前值 {value}（报警线 {alert_v:g}）。\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._send(rule, st, content, self._f(rule.get("alert"), "cooldown_sec", 60))

    def _maybe_realert(self, rule, st, value, warn_v, alert_v) -> None:
        alert_cfg = rule.get("alert") or {}
        interval = self._f(alert_cfg, "realert_sec", 1800)
        if interval <= 0 or (time.time() - st.last_notify) < interval:
            return
        if not alert_cfg.get("notify", True):
            return
        suffix = str(alert_cfg.get("suffix", "_EX"))
        snapshot = self.recorder.snapshot(rule, suffix, reason="持续低于报警线") if alert_cfg.get(
            "snapshot", True) else None
        content = self._render(
            alert_cfg.get("template") or "【{name}】仍低于报警线，当前值 {value}。",
            rule, warn=warn_v, alert=alert_v, value=value, prev=st.prev,
            snapshot=snapshot, text=st.text,
        )
        if alert_cfg.get("log", True):
            self.recorder.event("alert_repeat", rule=rule.get("name", ""), value=value,
                                alert=alert_v, snapshot=snapshot or "")
        self._send(rule, st, content, 0)

    # ---------- 识别失败 ----------
    def on_failure(self, rule: dict, st: RuleState, reason: str) -> None:
        st.fail_count += 1
        # 每次失败只进 debug（另有 samples.log 记 "fail"），不刷 app.log
        self.log.debug("【%s】本次读数无效：%s（连续 %d 次）", rule.get("name"), reason,
                       st.fail_count)
        if st.fail_count == 5:
            self.log.info("【%s】已连续 5 次读不到数值，检查一下游戏窗口和识别区域", rule.get("name"))
        self._emit({"type": "sample", "rule": rule.get("name"), "value": None,
                    "text": f"无效读数：{reason}", "zone": st.zone})
        need = int(rule.get("fail_alert_after", 0) or 0)
        if need and st.fail_count >= need and not st.fail_notified:
            content = (
                f"【{rule.get('name')}】连续 {st.fail_count} 次没能读到有效数值，"
                f"请检查游戏窗口是否被遮挡、区域是否偏移。\n"
                f"最后一次原因：{reason}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            self.recorder.event("read_failed", rule=rule.get("name", ""), reason=reason,
                                count=st.fail_count)
            if self._send(rule, st, content, self._f(rule.get("alert"), "cooldown_sec", 60)):
                st.fail_notified = True

    def on_success(self, rule: dict, st: RuleState) -> None:
        # 偶发一两次失败很常见，只有"连续失败后恢复"才值得写一行
        if st.fail_count >= 3:
            self.log.info("【%s】读数恢复正常（此前连续失败 %d 次）", rule.get("name"),
                          st.fail_count)
        else:
            self.log.debug("【%s】读数恢复", rule.get("name"))
        st.fail_count = 0
        st.fail_notified = False
