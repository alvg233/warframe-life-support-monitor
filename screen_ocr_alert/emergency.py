"""急停模式。

触发条件（可多选，也可都不选）：
  - 百分比归零：主读数 <= zero_threshold
  - 时间到达：单独读一块"时间 mm:ss"区域，达到指定分钟数

触发后的动作：
  1. 把游戏窗口调到前台
  2. 给前台发 ESC
  3. 连续识别 verify_reads 次（默认 5 次），每次间隔 verify_interval_sec
  4. 5 次都没有合法数字 => 急停成功；否则记为"未确认"
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .numbers import format_clock, format_value, parse_clock, parse_value
from .wininput import (activate_window, click_at, foreground_hwnd, is_elevated, press_key,
                       self_elevated, window_pid, window_title)


def looks_like_new_round(previous: float | None, current: float | None) -> bool:
    """判断计时器是否归零/进入新一局。

    急停后游戏停在结算或菜单，计时器不再变化；再次开局时计时器从 0 重新走。
    判定依据：新值比旧值小 60 秒以上，或新值本身 <= 120 秒。
    """
    if previous is None or current is None:
        return False
    if current >= previous:
        return False
    return (previous - current) > 60 or current <= 120


@dataclass
class EmergencyResult:
    triggered: bool = False
    reason: str = ""
    success: bool = False
    reads: list[float | None] = field(default_factory=list)
    time_text: str = ""
    error: str = ""
    diagnostics: str = ""

    @property
    def detail(self) -> str:
        return "、".join("无" if v is None else format_value(v) for v in self.reads) or "-"


class EmergencyStopper:
    def __init__(self, cfg_provider, capture, ocr, logger=None, notify=None,
                 on_time=None, on_stage=None) -> None:
        self.cfg_provider = cfg_provider
        self.capture = capture
        self.ocr = ocr
        self.log = logger or logging.getLogger(__name__)
        self.notify = notify
        self.on_time = on_time      # 每次读到时间就回调，用于界面持续显示
        self.on_stage = on_stage    # 每个执行阶段回调，让界面"看得见"急停
        self.fired = False          # 触发后锁定，避免重复按 ESC
        self.last_result: EmergencyResult | None = None
        self._last_time: float | None = None
        self._last_time_at = 0.0
        self._run_lock = threading.Lock()   # 同一时刻只允许一次急停执行

    def _stage(self, message: str, running: bool | None = None) -> None:
        """running=None 时自动取"当前是否正在执行急停"，避免例行提示也点亮执行状态。"""
        if running is None:
            running = getattr(self, "_executing", False)
        self.log.info("急停: %s", message)
        if self.on_stage:
            try:
                self.on_stage(message, running)
            except Exception:
                pass

    # ---------- 对外 ----------
    def reset(self) -> None:
        self.fired = False

    def run_now(self, rule: dict, cfg: dict, reason: str = "手动执行") -> EmergencyResult:
        """手动执行一次（界面按钮用）：与自动触发互斥，避免同时按键。"""
        self._run_lock.acquire()
        try:
            self.fired = True
            return self._run(rule, cfg, reason)
        finally:
            self._run_lock.release()

    def check(self, rule: dict, value: float | None) -> EmergencyResult | None:
        cfg = rule.get("emergency") or {}
        if not cfg.get("enabled") or self.fired:
            return None

        reason = self._match_trigger(rule, cfg, value)
        if not reason:
            return None

        # 手动执行/后台执行可能同时发生：拿不到执行权就放弃本次，避免按两次 ESC
        if not self._run_lock.acquire(blocking=False):
            self.log.warning("急停正在执行中，跳过本次触发")
            return None
        try:
            self.fired = True
            self._stage(f"触发急停：{reason}")
            self.log.warning("【%s】急停触发：%s", rule.get("name"), reason)
            result = self._run(rule, cfg, reason)
        finally:
            self._run_lock.release()
        self.last_result = result
        self._report(rule, cfg, result)
        return result

    # ---------- 内部 ----------
    def _match_trigger(self, rule: dict, cfg: dict, value: float | None) -> str:
        triggers = cfg.get("triggers") or {}
        if triggers.get("zero_percent") and value is not None:
            if value <= float(cfg.get("zero_threshold", 0.5)):
                return f"百分比归零（{format_value(value)}）"
        if triggers.get("elapsed_minutes"):
            # 计时器变化慢，默认每两轮读一次就够，省一半 OCR
            every = max(1, int(cfg.get("time_read_every_n", 2) or 2))
            self._time_tick = getattr(self, "_time_tick", 0) + 1
            if (self._time_tick - 1) % every == 0 or self._last_time is None:
                seconds, text = self._read_time_validated(rule, cfg)
            else:
                seconds, text = self._last_time, ""
            if seconds is not None and self.on_time:
                try:
                    self.on_time(seconds, text)
                except Exception:
                    pass
            limit = float(cfg.get("elapsed_minutes", 10)) * 60
            if seconds is not None and seconds >= limit:
                return f"时间达到 {format_value(limit / 60)} 分钟（{format_clock(seconds)}）"
        return ""

    def _read_time_validated(self, rule: dict, cfg: dict) -> tuple[float | None, str]:
        """时间读数合理性校验。

        计时器只会缓慢递增，所以：
          - 涨得比"经过时间×3+5秒"还快 -> 视为多认/错认，直接丢弃（不触发急停）；
          - 突然大幅变小 -> 可能是新一局重新计时，重读两次一致才采纳。
        """
        seconds, text = self.read_time(rule, cfg)
        if seconds is None:
            return None, text
        now = time.time()
        if self._last_time is None:
            self._last_time, self._last_time_at = seconds, now
            return seconds, text

        elapsed = max(0.2, now - self._last_time_at)
        jump = seconds - self._last_time
        allowed = elapsed * 3 + 5

        if jump > allowed:
            # 可能是上一轮基线本身读错了（比如把 205:14 读成 05:02）。
            # 先挂起，下一轮还是同一个值就认它是真的。
            pending = getattr(self, "_pending_time", None)
            if pending is not None and abs(seconds - pending) <= 3:
                self._stage(f"时间 {format_clock(seconds)} 连续两轮一致，按真实值处理"
                            f"（原基线 {format_clock(self._last_time)} 作废）")
                self._last_time, self._last_time_at = seconds, now
                self._pending_time = None
                return seconds, text
            self._pending_time = seconds
            self._stage(f"时间读数 {format_clock(seconds)} 比上一轮 "
                        f"{format_clock(self._last_time)} 涨得太多，先挂起等下一轮确认")
            if self.on_time:      # 挂起值也显示出来，别让界面回退成旧值
                try:
                    self.on_time(seconds, text)
                except Exception:
                    pass
            return None, text

        self._pending_time = None

        if jump < -2:      # 数值变小
            # "少一位"误读：旧值已经很大，而新值既不是刚开局的小值、差距又巨大，
            # 这种情况直接判为误读，绝不回退（比如 226:11 被读成 26:11）。
            if (self._last_time and self._last_time > 600 and seconds > 300
                    and (self._last_time - seconds) > 600):
                self._stage(f"时间读数 {format_clock(seconds)} 比上一轮 "
                            f"{format_clock(self._last_time)} 少了很多位，判为误读，保留原值")
                return None, text
            for _ in range(2):
                again, _text = self.read_time(rule, cfg)
                if again is not None and abs(again - seconds) <= 2:
                    self._stage(f"时间重置：{format_clock(self._last_time)} → "
                                f"{format_clock(again)}（已确认）")
                    self._last_time, self._last_time_at = again, time.time()
                    return again, text
                time.sleep(0.15)
            self._stage(f"时间从 {format_clock(self._last_time)} 掉到 "
                        f"{format_clock(seconds)}，未获确认，已丢弃")
            return None, text

        self._last_time, self._last_time_at = seconds, now
        return seconds, text

    def read_time(self, rule: dict, cfg: dict) -> tuple[float | None, str]:
        region = cfg.get("time_region") or {}
        if not region:
            return None, ""
        try:
            img, _src = self.capture.grab_region_auto(region)
        except Exception as exc:
            self.log.debug("时间区域抓取失败: %s", exc)
            self._time_miss = getattr(self, "_time_miss", 0) + 1
            return None, ""
        text = ""
        # 时间区域太小，检测器在 1x 下基本读不出；依次试 2x / 3x / 跳过检测器
        for scale, rec_only in ((2.0, False), (3.0, False), (2.0, True)):
            try:
                _lines, candidate = self.ocr.recognize(img, scale=scale, rec_only=rec_only)
            except Exception:
                continue
            if parse_clock(candidate) is not None:
                text = candidate
                break
            if not text:
                text = candidate
        seconds = parse_clock(text)
        self._time_miss = 0 if seconds is not None else getattr(self, "_time_miss", 0) + 1
        return seconds, text

    def _read_value(self, rule: dict) -> float | None:
        num = rule.get("number") or {}
        try:
            img, _src = self.capture.grab_region_auto(rule.get("region") or {})
            _lines, text = self.ocr.recognize(img)
        except Exception:
            return None
        value = parse_value(text, num.get("mode", "first"), bool(num.get("require_percent")))
        rng = num.get("valid_range")
        if value is not None and isinstance(rng, (list, tuple)) and len(rng) == 2:
            if not (float(rng[0]) <= value <= float(rng[1])):
                return None
        return value

    def _run(self, rule: dict, cfg: dict, reason: str) -> EmergencyResult:
        result = EmergencyResult(triggered=True, reason=reason)
        diag: list[str] = []
        self._executing = True
        try:
            hwnd = None
            try:
                hwnd = self.capture._hwnd()          # 复用截图器的窗口查找
            except Exception:
                hwnd = None

            # 先做权限体检：游戏是管理员、本程序不是的话，Windows 会直接拒绝切前台和注入按键
            title = window_title(hwnd) if hwnd else ""
            pid = window_pid(hwnd) if hwnd else 0
            game_admin = is_elevated(pid) if pid else None
            me_admin = self_elevated()
            diag.append(f"窗口={title!r} 游戏管理员={game_admin} 本程序管理员={me_admin}")
            self._stage(f"目标窗口：{title or '未找到'}；游戏管理员={game_admin}，"
                        f"本程序管理员={me_admin}")
            if game_admin and not me_admin:
                note = "权限不匹配：游戏以管理员运行，本程序没有，Windows 会阻止切前台/注入按键"
                diag.append(note)
                self._stage(running=True, message=note + "（请以管理员身份重新打开本程序）")

            if cfg.get("activate_window", True):
                self._stage(running=True, message="正在把游戏切到前台…")
                ok, detail = activate_window(hwnd)
                diag.append(f"切前台={'成功' if ok else '失败'}（{detail}）")
                self._stage(f"切前台{'成功' if ok else '失败'}：{detail}；"
                            f"当前前台={window_title(foreground_hwnd())!r}")
                time.sleep(0.4)

            # 很多游戏（尤其窗口化）要真实鼠标点击后才会接收键盘输入
            if cfg.get("click_before_key", True) and hwnd:
                try:
                    from .capture import virtual_screen_rect, window_rects

                    _win, client = window_rects(hwnd)
                    rx, ry = (cfg.get("click_ratio") or [0.05, 0.15])[:2]
                    px = int(client[0] + client[2] * float(rx))
                    py = int(client[1] + client[3] * float(ry))
                    # 窗口被拖到屏幕边缘时，点击点可能落到屏幕外 -> 夹回屏幕内
                    vx, vy, vw, vh = virtual_screen_rect()
                    margin = 8
                    cx = min(max(px, vx + margin), vx + vw - margin)
                    cy = min(max(py, vy + margin), vy + vh - margin)
                    if (cx, cy) != (px, py):
                        diag.append(f"点击点 ({px},{py}) 在屏幕外，已夹到 ({cx},{cy})")
                        self._stage(f"点击点超出屏幕，已调整为 ({cx},{cy})", running=True)
                    ok, detail = click_at(cx, cy)
                    diag.append(f"点击={'成功' if ok else '失败'}（{detail}）")
                    self._stage(f"点击游戏窗口（{cx},{cy}）：{'成功' if ok else '失败'}（{detail}）")
                    time.sleep(0.35)
                except Exception as exc:
                    diag.append(f"点击异常: {exc}")
                    self._stage(f"点击游戏窗口异常：{exc}")

            if str(cfg.get("press_key", "esc")).lower() == "esc":
                presses = max(1, int(cfg.get("press_count", 1) or 1))
                for i in range(presses):
                    ok, how = press_key()
                    diag.append(f"ESC={'已发送' if ok else '失败'}（{how}）")
                    self._stage(f"发送 ESC 第 {i + 1}/{presses} 次："
                                f"{'已发送' if ok else '失败'}（{how}）")
                    time.sleep(0.25)
                time.sleep(0.35)

            reads = int(cfg.get("verify_reads", 5) or 5)
            gap = float(cfg.get("verify_interval_sec", 0.6) or 0.6)
            self._stage(f"开始连续识别 {reads} 次确认…")
            for i in range(reads):
                result.reads.append(self._read_value(rule))
                if i < reads - 1:
                    time.sleep(max(0.1, gap))
            result.success = all(v is None for v in result.reads)
            self._stage(f"验证完成：{result.detail} → "
                        f"{'急停成功' if result.success else '未确认'}")
        except Exception as exc:
            result.error = str(exc)
            self.log.error("急停执行异常: %s", exc)
            self._stage(f"执行异常：{exc}")
        result.diagnostics = "；".join(diag)
        self._executing = False
        return result

    def _report(self, rule: dict, cfg: dict, result: EmergencyResult) -> None:
        self._event(rule, result)
        if not (cfg.get("notify", True) and self.notify):
            return
        template = cfg.get("template") or "【{name}】急停已触发（{reason}）。结果：{result}"
        text = template.format(
            name=rule.get("name", "规则"),
            reason=result.reason,
            reads=len(result.reads),
            detail=result.detail,
            result="急停成功" if result.success else "急停未确认",
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            self.notify(text)
        except Exception as exc:
            self.log.error("急停通知发送失败: %s", exc)

    def _event(self, rule: dict, result: EmergencyResult) -> None:
        cb = getattr(self, "recorder", None)
        if cb is None:
            return
        try:
            cb.event("emergency_stop", rule=rule.get("name", ""), reason=result.reason,
                     success=result.success,
                     reads=[None if v is None else round(v, 2) for v in result.reads])
        except Exception:
            pass
