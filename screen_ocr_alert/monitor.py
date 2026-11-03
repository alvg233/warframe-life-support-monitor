"""采样循环：抓图 -> 变化检测 -> 判定读数 -> 交给报警策略。

只负责流程编排，判断逻辑在各模块内：
- 读数可不可信  -> readcheck.ReadGate
- 要不要记日志/截图/发消息 -> alerts.AlertPolicy
- 图存哪里、事件写哪里 -> recorder.Recorder
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from .alerts import ZONE_ALERT, AlertPolicy, RuleState
from .capture import CaptureError, ScreenCapture, looks_black
from .emergency import EmergencyStopper
from .readcheck import ReadGate
from .recorder import Recorder


class MonitorService(threading.Thread):
    def __init__(
        self,
        cfg_provider,
        on_event=None,
        logger: logging.Logger | None = None,
        capture: ScreenCapture | None = None,
        ocr=None,
        reader: ReadGate | None = None,
        policy: AlertPolicy | None = None,
        recorder: Recorder | None = None,
        emergency=None,
        states: dict[str, RuleState] | None = None,
    ) -> None:
        super().__init__(name="monitor", daemon=True)
        self.cfg_provider = cfg_provider
        self.on_event = on_event or (lambda evt: None)
        self.log = logger or logging.getLogger(__name__)
        self.capture = capture
        self._ocr = ocr
        self.reader = reader
        self.policy = policy
        self.recorder = recorder
        self.emergency = emergency
        self._states: dict[str, RuleState] = states if states is not None else {}
        self._stop = threading.Event()
        self._cap_sig: tuple | None = None
        self._last_cleanup = 0.0
        self.tick_count = 0
        self._fail_streak = 0
        self._learn_tried = False

    # ---------- 对外 ----------
    def stop(self, join: bool = False, timeout: float = 5.0) -> None:
        """停止采样线程。join=True 时等线程真正退出（避免旧循环还在跑就开始新循环）。"""
        self._stop.set()
        if join and self.is_alive() and self is not threading.current_thread():
            self.join(timeout)

    def state_of(self, rule_id: str) -> RuleState:
        return self._states.setdefault(rule_id, RuleState())

    def prepare(self, cfg: dict | None = None) -> None:
        """按当前配置装好截图/OCR/记录器/策略（UI 的"测试截图"也会用）。"""
        self._components(cfg or self.cfg_provider())

    def run_now(self, rule: dict) -> None:
        """UI 的"立即识别一次"：同步跑一次，结果通过事件回传。"""
        try:
            cfg = self.cfg_provider()
            self._components(cfg)
            self._check(rule, cfg, force=True)
        except Exception:
            self.log.error("手动识别失败:\n%s", traceback.format_exc())
            self._emit({"type": "error", "message": "识别失败，详见日志"})

    # ---------- 组件装配 ----------
    def _components(self, cfg: dict) -> None:
        snap = cfg.get("snapshot") or {}
        sig = (snap.get("backend", "window"), snap.get("window_title", ""),
               snap.get("process_name", ""))
        if self.capture is None:
            self.capture = ScreenCapture(backend=sig[0], window_title=sig[1],
                                         process_name=sig[2])
            self._cap_sig = sig
        elif self._cap_sig is None:
            # 外部注入的截图器（测试/自定义），先记住签名，之后配置真变了才重建
            self._cap_sig = sig
        elif sig != self._cap_sig:
            self.capture = ScreenCapture(backend=sig[0], window_title=sig[1],
                                         process_name=sig[2])
            self._cap_sig = sig
            if self.recorder is not None:
                self.recorder.capture = self.capture
        if self._ocr is None:
            from .ocr import OcrEngine

            self._ocr = OcrEngine(self.log)
            self._ocr.warmup()
        if self.reader is None:
            self.reader = ReadGate(self.cfg_provider, self.log, self._ocr)
        if self.recorder is None:
            self.recorder = Recorder(self.cfg_provider, self.capture, self.log)
        if self.policy is None:
            self.policy = AlertPolicy(self.cfg_provider, self.recorder, self.log)
            self.policy.on_event = self.on_event
        if self.emergency is None:
            self.emergency = EmergencyStopper(
                self.cfg_provider, self.capture, self._ocr, self.log,
                notify=self._send_notification,
                on_time=lambda secs, text: self._emit(
                    {"type": "time", "seconds": secs, "text": text}),
                on_stage=lambda msg, running: self._emit(
                    {"type": "emergency_stage", "message": msg, "running": running}),
            )
            self.emergency.recorder = self.recorder
        else:
            self.emergency.capture = self.capture

    def _send_notification(self, text: str) -> bool:
        from .dingtalk import DingTalk, DingTalkError

        def worker() -> None:
            try:
                DingTalk(self.cfg_provider().get("dingtalk", {})).send_text(text)
                self.recorder.event("notify", text=text)
            except DingTalkError as exc:
                self.log.error("通知发送失败: %s", exc)
                self._emit({"type": "error", "message": f"钉钉发送失败: {exc}"})
            except Exception as exc:
                self.log.error("通知发送异常: %s", exc)

        threading.Thread(target=worker, name="dingtalk", daemon=True).start()
        return True

    # ---------- 主循环 ----------
    def run(self) -> None:
        self.log.info("监控线程启动")
        while not self._stop.is_set():
            started = time.time()
            try:
                cfg = self.cfg_provider()
                self.tick(cfg)
            except Exception:
                self.log.error("本轮采样异常（已跳过，线程继续）:\n%s", traceback.format_exc())
            interval = self._interval()
            self._stop.wait(max(0.2, interval - (time.time() - started)))
        self.log.info("监控线程已停止")

    def _interval(self) -> float:
        try:
            return max(0.5, float((self.cfg_provider().get("sample") or {}).get(
                "interval_sec", 1.5)))
        except (TypeError, ValueError):
            return 1.5

    def tick(self, cfg: dict | None = None) -> None:
        cfg = cfg or self.cfg_provider()
        self._components(cfg)
        rules = [r for r in (cfg.get("rules") or []) if r.get("enabled", True)]
        if not rules:
            return
        self.tick_count += 1
        if time.time() - self._last_cleanup > 3600:
            self._last_cleanup = time.time()
            try:
                self.recorder.cleanup()
            except Exception:
                self.log.debug("清理旧截图失败", exc_info=True)
        for rule in rules:
            if self._stop.is_set():
                return
            try:
                self._check(rule, cfg)
            except CaptureError as exc:
                st = self.state_of(str(rule.get("id") or rule.get("name")))
                st.black = True
                self.log.error("【%s】截图失败：%s", rule.get("name"), exc)
                self._emit({"type": "error", "message": f"截图失败：{exc}"})
            except ValueError as exc:
                # 配置类错误（如区域未设置）只提示一次，避免每次采样刷屏
                key = str(exc)
                if getattr(self, "_last_value_error", None) != key:
                    self._last_value_error = key
                    self.log.error("【%s】%s", rule.get("name"), exc)
                    self._emit({"type": "error", "message": str(exc)})
            except Exception:
                self.log.error("规则「%s」检查失败:\n%s", rule.get("name"),
                               traceback.format_exc())
                self._emit({"type": "error", "message": "检查异常，详见日志"})

        # 时间区域连续读不到：在左上方大范围内自动定位一次，交给界面更新配置
        try:
            stopper = self.emergency
            if stopper is not None and not stopper.fired:
                emg = (rules[0].get("emergency") or {}) if rules else {}
                if (emg.get("enabled") and (emg.get("triggers") or {}).get("elapsed_minutes")
                        and getattr(stopper, "_time_miss", 0) >= 5
                        and time.time() - getattr(self, "_last_time_locate", 0.0) > 300):
                    self._last_time_locate = time.time()
                    located = self.locate_time_region(rules[0])
                    if located:
                        stopper._time_miss = 0
                        self.log.info("已自动定位时间区域：%s", located)
                        self._emit({"type": "time_region", "region": located})
                        self._emit({"type": "error",
                                    "message": "时间区域读取失败，已自动重新定位"})
        except Exception:
            self.log.debug("自动定位时间区域失败", exc_info=True)

    def _auto_locate(self, rule: dict) -> dict | None:
        """小框读不到时，在搜索区里找带 % 的那一行，换算成新的小框。"""
        from .numbers import parse_value

        zone = rule.get("search_zone") or {}
        if not zone:
            return None
        try:
            img, _src = self.capture.grab_region_auto(zone)
            lines, _text = self._ocr.recognize(img)      # 多行定位必须用检测器
        except Exception:
            self.log.debug("搜索区定位失败", exc_info=True)
            return None
        best = None
        for line in lines:
            if "%" not in line.text:
                continue
            if parse_value(line.text, "first", True) is None:
                continue
            best = line
            break
        if best is None:
            return None

        zpx = self.capture.region_pixels(zone)
        x0, y0, x1, y1 = best.box
        pad = max(3.0, (y1 - y0) * 0.5)
        ax0 = zpx["x"] + x0 - pad
        ay0 = zpx["y"] + y0 - pad
        aw = max(8.0, (x1 - x0) + 2 * pad)
        ah = max(8.0, (y1 - y0) + 2 * pad)
        bx, by, bw, bh = self.capture.base_rect()
        if bw <= 0 or bh <= 0:
            return None
        rx = max(0.0, (ax0 - bx) / bw)
        ry = max(0.0, (ay0 - by) / bh)
        rw = min(1.0 - rx, aw / bw)
        rh = min(1.0 - ry, ah / bh)
        if rw <= 0 or rh <= 0:
            return None
        return {
            "mode": "ratio",
            "x": round(rx, 4),
            "y": round(ry, 4),
            "w": round(min(1.0, aw / bw), 4),
            "h": round(min(1.0, ah / bh), 4),
        }

    def locate_time_region(self, rule: dict,
                           wide: dict | None = None) -> dict | None:
        """在左上方大范围内找形如 mm:ss 的那一行，换算成时间区域。"""
        from .numbers import parse_clock

        zone = wide or {"mode": "ratio", "x": 0.0, "y": 0.08, "w": 0.5, "h": 0.45}
        try:
            img, _src = self.capture.grab_region_auto(zone)
            lines, _text = self._ocr.recognize(img, scale=2.0)
        except Exception:
            self.log.debug("时间区域定位失败", exc_info=True)
            return None

        best = None
        for line in lines:
            if "%" in line.text:
                continue
            if parse_clock(line.text) is None:
                continue
            best = line
            break
        if best is None:
            return None

        x0, y0, x1, y1 = best.box
        height = max(10.0, y1 - y0)
        width = max(24.0, x1 - x0)                      # 整行，允许带上右侧水印
        zpx = self.capture.region_pixels(zone)
        bx, by, bw, bh = self.capture.base_rect()
        if bw <= 0 or bh <= 0:
            return None

        candidates = []
        for pad_scale, width_scale in ((0.5, 1.0), (0.9, 1.0), (0.5, 1.4)):
            pad = height * pad_scale
            cw = width * width_scale
            ax0 = zpx["x"] + x0 - pad
            ay0 = zpx["y"] + y0 - pad
            rx = max(0.0, (ax0 - bx) / bw)
            ry = max(0.0, (ay0 - by) / bh)
            candidates.append({
                "mode": "ratio",
                "x": round(rx, 4),
                "y": round(ry, 4),
                "w": round(min(1.0 - rx, (cw + 2 * pad) / bw), 4),
                "h": round(min(1.0 - ry, (height + 2 * pad) / bh), 4),
            })

        # 每个候选验 3 次，至少命中 2 次才采纳（定位点每次会略有偏移，必须挑稳住的那个）
        for region in candidates:
            hits = 0
            for _ in range(3):
                try:
                    check, _src = self.capture.grab_region_auto(region)
                    _l, text = self._ocr.recognize(check, scale=2.0)
                except Exception:
                    break
                if parse_clock(text) is not None:
                    hits += 1
            if hits >= 2:
                return region
        return None

    # ---------- 单条规则 ----------
    def _check(self, rule: dict, cfg: dict, force: bool = False) -> None:
        sample = cfg.get("sample") or {}
        log_samples = bool(sample.get("log_enabled", True))
        rid = str(rule.get("id") or rule.get("name") or "rule")
        st = self.state_of(rid)
        now = time.time()

        region = rule.get("region") or {}
        try:
            rw, rh = float(region.get("w", 0)), float(region.get("h", 0))
        except (TypeError, ValueError):
            rw, rh = 0.0, 0.0
        if rw <= 0 or rh <= 0:
            raise ValueError("规则区域未设置，请先框选区域")

        img, source = self.capture.grab_region_auto(region)

        # 比例模式下如果没找到游戏窗口，区域会退化到"整屏定位"——这通常就是
        # "框好的区域不跟着游戏走"的原因，明确提示一次。
        mode = str(region.get("mode") or "absolute").lower()
        if mode == "ratio" and getattr(self.capture, "_hwnd", None) and not self.capture._hwnd():
            if not getattr(self, "_warned_no_window", False):
                self._warned_no_window = True
                self.log.warning("没找到游戏窗口（标题/进程名不匹配），区域暂时按整屏比例定位；"
                                 "参数在 高级设置 → 截图存档 → 窗口标题/进程名")
                self._emit({"type": "error",
                            "message": "没找到游戏窗口：现在按整屏定位，区域可能不跟游戏走。"})
        else:
            self._warned_no_window = False

        # 比例区域会随窗口缩放变化：把实际落点同步给边框显示
        try:
            px = self.capture.region_pixels(region)
            if st.rect != px:
                st.rect = px
                self._emit({"type": "region", "rect": px})
        except Exception:
            self.log.debug("区域换算失败", exc_info=True)

        # 黑屏 / 锁屏保护：读不到画面就不要乱报警，只记一次日志
        if looks_black(img):
            if not st.black:
                st.black = True
                self.log.warning("【%s】画面全黑（通道 %s），可能显示器已关闭或窗口被遮挡；"
                                 "期间不会报警，恢复后自动继续", rule.get("name"), source)
                self.recorder.event("black_frame", rule=rule.get("name", ""), source=source)
                self._emit({"type": "error", "message": "画面全黑，暂停判定，等待恢复"})
            return
        if st.black:
            st.black = False
            self.log.info("【%s】画面已恢复（通道 %s）", rule.get("name"), source)
            self._emit({"type": "error", "message": "画面已恢复"})

        # 急停：放在变化检测之前，时间触发不依赖主区域是否变化
        if self.emergency is not None and not self.emergency.fired:
            try:
                res = self.emergency.check(rule, st.value)
            except Exception:
                self.log.error("急停检查异常:\n%s", traceback.format_exc())
                res = None
            if res is not None:
                self._emit({"type": "emergency", "rule": rule.get("name"),
                            "reason": res.reason, "success": res.success,
                            "message": (f"急停已触发（{res.reason}）："
                                        f"{'成功' if res.success else '未确认'}，"
                                        f"读数 {res.detail}")})
                if res.success:
                    # 急停成功 -> 自动停止监控（线程会在本轮结束后退出）
                    self.log.info("【%s】急停成功，自动停止监控", rule.get("name"))
                    self._emit({"type": "stopped",
                                "message": "急停成功，已自动停止监控"})
                    self.stop()
                return

        fp = ScreenCapture.fingerprint(img)
        diff = ScreenCapture.diff(st.fingerprint, fp)
        changed = diff > float(sample.get("diff_threshold", 1.5))
        if changed or st.fingerprint is None:
            st.last_change = now
        st.fingerprint = fp

        stale = (now - st.last_change) < float(sample.get("force_ocr_sec", 600))
        skip = (bool(sample.get("change_detection", True)) and not changed and not force
                and st.value is not None and stale and st.zone != ZONE_ALERT)
        if skip:
            st.last_check = now
            return

        elapsed = (now - st.last_read) if st.last_read else None
        degraded = self._fail_streak >= 3
        if degraded and (self.tick_count % 2 == 0):
            # 连续失败后的退避：隔轮跳过，别让风扇一直转
            st.last_check = now
            return
        result = self.reader.read(
            grab=lambda: self.capture.grab_region_auto(region)[0],
            rule=rule,
            last_value=st.value,
            elapsed=elapsed,
            quick=degraded,
        )
        st.last_check = now

        # 学到的位置也可能失效：失败就先丢弃它，用配置区域再试一次
        if not result.ok and st.learned_region is not None:
            st.learned_region = None
            region = rule.get("region") or {}
            result = self.reader.read(
                grab=lambda: self.capture.grab_region_auto(region)[0],
                rule=rule, last_value=st.value, elapsed=elapsed, quick=degraded)


        # 读不到时才用搜索区重试一次；失败时最多再花一轮，不做额外定位
        if not result.ok and not degraded:
            zone = rule.get("search_zone") or {}
            if zone and zone != region:
                zres = self.reader.read(
                    grab=lambda: self.capture.grab_region_auto(zone)[0],
                    rule=rule, last_value=st.value, elapsed=elapsed)
                if zres.ok:
                    self.log.info("【%s】改用搜索区读取成功", rule.get("name"))
                    result = zres

        if not result.ok:
            st.rejected += 1
            st.pending_value = None
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
            # 连续读不到就重建一次引擎（最多 10 分钟一次：重建很贵，不能频繁做）
            if self._fail_streak >= 5:
                self._fail_streak = 0
                st.learned_region = None
                self._learn_tried = False
                if time.time() - getattr(self, "_last_rebuild", 0.0) > 600:
                    self._last_rebuild = time.time()
                    try:
                        if hasattr(self._ocr, "rebuild"):
                            self._ocr.rebuild()
                        self.log.warning("【%s】连续 5 次读不到，已重建 OCR 引擎并重置区域",
                                         rule.get("name"))
                        self._emit({"type": "error",
                                    "message": "连续读不到数字，已重建识别引擎并重置区域"})
                    except Exception:
                        self.log.error("重建 OCR 引擎失败", exc_info=True)
            from .applog import samples_logger

            if log_samples:
                samples_logger().info("fail  %s", (result.reason or "")[:32])
            self.policy.on_failure(rule, st, result.reason or "未通过校验")
            return

        self._fail_streak = 0

        from .applog import samples_logger
        from .numbers import format_value

        # 重读一致但仍违反物理降速：不立刻当真，要求连续两轮读到同一个值
        if result.violates_rate:
            agree = float((cfg.get("guard") or {}).get("agree_tolerance", 1.0))
            if st.pending_value is not None and abs(result.value - st.pending_value) <= agree:
                self.log.warning("【%s】连续两轮都读到 %s（上一读数 %s），按真实跳变处理",
                                 rule.get("name"), format_value(result.value),
                                 format_value(st.value))
                st.pending_value = None
                if log_samples:
                    samples_logger().info("%s", format_value(result.value))
            else:
                st.pending_value = result.value
                st.pending_at = now
                self.log.info("【%s】读数 %s 比上一轮 %s 掉得太快，等下一轮确认",
                              rule.get("name"), format_value(result.value),
                              format_value(st.value))
                if log_samples:
                    samples_logger().info("%s?", format_value(result.value))
                self._emit({"type": "sample", "rule": rule.get("name"), "value": None,
                            "text": f"疑似跳变到 {format_value(result.value)}，等下一轮确认",
                            "zone": st.zone})
                return
        else:
            st.pending_value = None
            if log_samples:
                samples_logger().info("%s", format_value(result.value))

        self.policy.on_success(rule, st)
        st.text = result.text
        st.score = result.score
        self.policy.on_read(rule, st, result.value, result.text, result.score)
        st.last_read = now

    def calibrate(self, rule: dict) -> dict | None:
        """自动校准：找到那行百分比，再从"刚好包住"到"多留余量"逐级试，
        返回能把数值读出来的最小比例区域（既小又不至于把字切掉）。
        """
        from .numbers import extract_numbers, parse_value

        cfg = self.cfg_provider()
        self._components(cfg)
        region = rule.get("region") or {}
        px = self.capture.region_pixels(region)
        img, _source = self.capture.grab_region_auto(region)
        lines, _text = self._ocr.recognize(img)
        cands = [ln for ln in lines if "%" in ln.text and extract_numbers(ln.text)]
        if not cands:
            return None
        line = max(cands, key=lambda ln: ln.score)
        x0, y0, x1, y1 = line.box
        bx, by, bw, bh = self.capture.base_rect()
        if bw <= 0 or bh <= 0:
            return None

        num = rule.get("number") or {}
        mode = num.get("mode", "first")
        require_percent = bool(num.get("require_percent"))
        rng = num.get("valid_range")

        def make_zone(scale: float, pad_ratio: float) -> dict:
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            line_h = max(4.0, y1 - y0)
            pad = max(4.0, line_h * pad_ratio)
            half_w = (x1 - x0) * scale / 2 + pad
            half_h = line_h * scale / 2 + pad
            ax0 = px["x"] + cx - half_w
            ay0 = px["y"] + cy - half_h
            return {
                "mode": "ratio",
                "x": round(max(0.0, (ax0 - bx) / bw), 4),
                "y": round(max(0.0, (ay0 - by) / bh), 4),
                "w": round(min(1.0, 2 * half_w / bw), 4),
                "h": round(min(1.0, 2 * half_h / bh), 4),
            }

        fallback = None
        # 由小到大试；同一块区域要连续两次都读得出数值，才认为"稳定可用"
        for scale, pad_ratio in ((1.0, 0.5), (1.2, 0.65), (1.45, 0.85), (1.9, 1.1)):
            zone = make_zone(scale, pad_ratio)
            if fallback is None:
                fallback = zone
            hits = 0
            last_text = ""
            for _ in range(2):
                try:
                    crop, _src = self.capture.grab_region_auto(zone)
                    _lines, text = self._ocr.recognize(crop)
                except Exception:
                    break
                last_text = text
                value = parse_value(text, mode, require_percent)
                if value is None:
                    break
                if isinstance(rng, (list, tuple)) and len(rng) == 2:
                    if not (float(rng[0]) <= value <= float(rng[1])):
                        break
                hits += 1
            if hits >= 2:
                self.log.info("校准命中：区域 %s（原文 %r）", zone, last_text)
                return zone
        self.log.warning("校准未能稳定读出数值，返回最小候选区域（可手动在高级设置里微调）")
        return fallback

    def _emit(self, evt: dict) -> None:
        try:
            self.on_event(evt)
        except Exception:
            self.log.debug("事件回调异常", exc_info=True)
