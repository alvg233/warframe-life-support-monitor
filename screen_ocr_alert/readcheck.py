"""识别判定模块（只管"这次读数能不能信"，不关心提醒）。

规则来自实际踩坑：
1. 游戏画面半透明、叠层水印多，单次 OCR 很容易错读。
2. 维生系统每 1.5 秒最多掉 1%，掉得比这快基本就是读错了。
3. 上升不受物理限制（吃药/过关回血可以很快），所以不做上升校验。
4. 读数不合法（解析不出数字、超范围、置信度低）也要有一次"重读预案"。

流程：先读一次；若"非法"或"降速超限"，在更短间隔内重读 2 次；
两次以上重读读数一致（差 ≤ agree_tolerance）才采纳，否则整次作废、留给下一轮。
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field


@dataclass
class Attempt:
    value: float | None
    text: str = ""
    score: float = 0.0
    reason: str = ""


@dataclass
class ReadResult:
    value: float | None = None
    text: str = ""
    score: float = 0.0
    confirmed: bool = False       # 是否经过"重读确认"
    rejected: float | None = None  # 被作废的原始读数，便于日志追查
    reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    elapsed: float = 0.0
    violates_rate: bool = False   # 重读确认了，但仍然超出物理降速上限

    @property
    def ok(self) -> bool:
        return self.value is not None


class ReadGate:
    def __init__(self, cfg_provider, logger, ocr, variants=None) -> None:
        self.cfg_provider = cfg_provider
        self.log = logger
        self.ocr = ocr
        self.variants = variants

    # ---------- 单次读取 ----------
    def _ocr_text(self, img, rule) -> tuple[str, float]:
        """五级兜底，逐级降要求，只为把这一行文字读出来。

        1) 原图 + 检测器     2) 放大 2.5 倍     3) CLAHE 提对比度
        4) 跳过检测器直接认整行（小区域里最稳）
        5) 提对比度 + 2 倍 + 直接认整行
        """
        min_conf = float((self.cfg_provider().get("guard") or {}).get("min_confidence", 0.45))
        best_text, best_score = "", 0.0
        for img_variant, scale, rec_only in self._variants(img):
            try:
                lines, text = self.ocr.recognize(img_variant, scale=scale, rec_only=rec_only)
            except TypeError:  # 自定义识别器不支持 rec_only
                lines, text = self.ocr.recognize(img_variant, scale=scale)
            score = max((line.score for line in lines), default=0.0)
            if text.strip() and score >= min_conf:
                return text, score
            if score > best_score:
                best_text, best_score = text, score
        return best_text, best_score

    @staticmethod
    def _variants(img):
        """识别变体，按"成功率从高到低"排列。

        实测（Warframe 生存 HUD，161x133 区域）：检测器 1x 完全检不出文字，
        2x / 3x 稳定命中；rec_only 对该区域无效，仅作最后兜底。
        """
        from .ocr import enhance_contrast

        yield img, 2.0, False
        yield img, 3.0, False
        yield enhance_contrast(img), 2.0, False
        yield img, 2.5, True
        yield img, 2.0, True

    def _read_once(self, grab, rule) -> Attempt:
        return self._read_once_limited(grab, rule, None)

    def _read_once_limited(self, grab, rule, variant_limit: int | None) -> Attempt:
        from .numbers import parse_value

        num = rule.get("number") or {}
        mode = num.get("mode", "first")
        require_percent = bool(num.get("require_percent"))
        min_conf = float((self.cfg_provider().get("guard") or {}).get("min_confidence", 0.45))

        img = grab()
        begin = getattr(self.ocr, "begin_read", None)
        if begin:
            begin()
        best_text, best_score = "", 0.0
        variants = list(self._variants(img))
        if variant_limit:
            variants = variants[:variant_limit]
        for variant, scale, rec_only in variants:
            try:
                lines, text = self.ocr.recognize(variant, scale=scale, rec_only=rec_only)
            except TypeError:
                lines, text = self.ocr.recognize(variant, scale=scale)
            score = max((line.score for line in lines), default=0.0)
            if score > best_score:
                best_text, best_score = text, score
            if not text.strip() or score < min_conf:
                continue
            value = parse_value(text, mode, require_percent)
            if value is None:
                continue
            rng = num.get("valid_range")
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                if not (float(rng[0]) <= value <= float(rng[1])):
                    continue
            return Attempt(value=value, text=text, score=score)
        reason = "没有解析出数字" if best_text.strip() else "没有识别到文字"
        return Attempt(value=None, text=best_text, score=best_score, reason=reason)

    # ---------- 降速校验 ----------
    @staticmethod
    def _drop_too_fast(value: float | None, last_value: float | None,
                       elapsed: float | None, guard: dict) -> bool:
        if value is None or last_value is None or not elapsed or elapsed <= 0:
            return False
        base_sec = 1.5
        max_drop = float(guard.get("max_drop_per_1_5s", 1.0)) * (elapsed / base_sec)
        max_drop += float(guard.get("drop_tolerance", 0.5))
        drop = last_value - value
        return drop > max_drop

    # ---------- 对外 ----------
    def read(self, grab, rule: dict, last_value: float | None = None,
             elapsed: float | None = None, quick: bool = False) -> ReadResult:
        """quick=True 用于连续失败后的退避：只试最便宜的两级、不重读。"""
        cfg = self.cfg_provider()
        guard = cfg.get("guard") or {}
        sample = cfg.get("sample") or {}
        agree = float(guard.get("agree_tolerance", 1.0))
        if quick:
            retries = 0
            variant_limit = 2
        else:
            retries = max(0, int(sample.get("retry_count", 2)))
            variant_limit = None
        gap = max(0.0, float(sample.get("retry_gap_sec", 0.35)))

        started = time.time()
        first = self._read_once_limited(grab, rule, variant_limit)
        attempts = [first]
        suspicious = self._drop_too_fast(first.value, last_value, elapsed, guard)
        if suspicious:
            self.log.debug(
                "疑似错读：%.2f -> %.2f（%.1fs 内），开始重读",
                last_value or 0.0, first.value or 0.0, elapsed or 0.0,
            )

        result = ReadResult(elapsed=0.0)

        # 第一次就干净且降速合理 -> 直接采纳，不额外花 CPU
        if first.value is not None and not suspicious:
            return ReadResult(value=first.value, text=first.text, score=first.score,
                              attempts=attempts)

        # 非法或可疑 -> 快速重读
        for _ in range(retries):
            time.sleep(gap)
            attempts.append(self._read_once_limited(grab, rule, variant_limit))

        valid = [a.value for a in attempts if a.value is not None]
        result.attempts = attempts
        result.rejected = first.value
        result.elapsed = time.time() - started

        if not valid:
            result.reason = first.reason or "重读后仍无有效读数"
            return result

        if len(valid) < 2:
            # 首次就不合法，只读到一次：证据不足，宁可这一轮不采信
            result.reason = "只有一次有效读数，未能通过重读确认"
            self.log.debug("%s（首次：%s）", result.reason, first.reason or "-")
            return result

        if max(valid) - min(valid) <= agree:
            value = float(statistics.median(valid))
            result.value = value
            result.confirmed = True
            result.text = next((a.text for a in attempts if a.value is not None), "")
            result.score = next((a.score for a in attempts if a.value is not None), 0.0)
            # 即使几次重读一致，只要仍然违反物理降速，就不能直接当真：
            # 单帧错读有时会连续重复（水印/遮挡/动画），交给上层做"连续两轮"确认。
            if suspicious and self._drop_too_fast(value, last_value, elapsed, guard):
                result.violates_rate = True
                self.log.debug(
                    "读数 %.2f 比上一轮 %.2f 掉得太快（%.1fs），虽重读一致但仍需下一轮确认",
                    value, last_value or 0.0, elapsed or 0.0,
                )
            else:
                self.log.debug("重读确认读数 %s", value)
            return result

        result.reason = f"读数不一致：{[round(v, 2) for v in valid]}，本次作废"
        self.log.debug(result.reason)
        return result
