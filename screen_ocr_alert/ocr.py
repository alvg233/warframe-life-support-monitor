"""RapidOCR 封装。

兼容两个版本的返回结构：
- rapidocr 3.x  -> 返回 RapidOCROutput(txts/scores/boxes) 或 (output, elapse)
- rapidocr_onnxruntime 2.x -> 返回 ([[box, text, score], ...], elapse)

小字号先自动放大再识别（屏幕上 20px 的字直接喂给模型容易糊），
放大倍数按高度自适应，最多 4 倍，兼顾准确率和耗时。
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass

import numpy as np

DEFAULT_MIN_HEIGHT = 64  # 低于这个高度就放大，小字块靠放大救准确率


@dataclass
class TextLine:
    text: str
    score: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1


def _box_to_xyxy(box) -> tuple[float, float, float, float]:
    arr = np.asarray(box, dtype=float).reshape(-1, 2)
    xs, ys = arr[:, 0], arr[:, 1]
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def _normalize_output(out) -> list[tuple[tuple[float, float, float, float], str, float]]:
    res = out[0] if isinstance(out, tuple) else out
    if res is None:
        return []
    rows: list[tuple[tuple[float, float, float, float], str, float]] = []
    txts = getattr(res, "txts", None)
    if txts is not None:
        boxes = getattr(res, "boxes", None)
        scores = getattr(res, "scores", None)
        boxes = [] if boxes is None else list(boxes)
        scores = [] if scores is None else list(scores)
        for i, text in enumerate(txts):
            box = _box_to_xyxy(boxes[i]) if i < len(boxes) else (0, 0, 0, 0)
            score = float(scores[i]) if i < len(scores) else 1.0
            rows.append((box, str(text), score))
        return rows
    if isinstance(res, (list, tuple)):
        for item in res:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            box = _box_to_xyxy(item[0])
            text = str(item[1])
            score = float(item[2]) if len(item) > 2 else 1.0
            rows.append((box, text, score))
    return rows


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    """灰度 + CLAHE 增强：游戏 HUD 是半透明的，提对比度后识别率明显更高。"""
    try:
        import cv2

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return cv2.cvtColor(clahe.apply(gray), cv2.COLOR_GRAY2BGR)
    except Exception:
        return img


def _group_lines(rows) -> list[TextLine]:
    """按纵坐标分行，行内按横坐标排序，保证阅读顺序。"""
    items = [
        TextLine(text=t, score=s, box=b) for b, t, s in rows if t and t.strip()
    ]
    items.sort(key=lambda d: (d.box[1], d.box[0]))
    lines: list[list[TextLine]] = []
    for it in items:
        placed = False
        for line in lines:
            ref = line[0]
            h = max(1.0, ref.box[3] - ref.box[1])
            if abs((it.box[1] + it.box[3]) / 2 - (ref.box[1] + ref.box[3]) / 2) < h * 0.6:
                line.append(it)
                placed = True
                break
        if not placed:
            lines.append([it])
    out: list[TextLine] = []
    for line in lines:
        line.sort(key=lambda d: d.box[0])
        out.append(
            TextLine(
                text=" ".join(i.text for i in line),
                score=min(i.score for i in line),
                box=(
                    min(i.box[0] for i in line),
                    min(i.box[1] for i in line),
                    max(i.box[2] for i in line),
                    max(i.box[3] for i in line),
                ),
            )
        )
    return out


def _upscale(img: np.ndarray, target_min_height: int = DEFAULT_MIN_HEIGHT,
             scale_hint: float | None = None) -> tuple[np.ndarray, float]:
    """返回 (放大后的图, 实际倍率)。倍率用于把识别框坐标换算回原图。"""
    h = img.shape[0]
    if h <= 0:
        return img, 1.0
    if scale_hint:
        scale = float(scale_hint)
    else:
        scale = min(4.0, max(1.0, math.ceil(target_min_height / h)))
    if scale <= 1.0:
        return img, 1.0
    try:
        import cv2

        return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC), scale
    except Exception:
        k = max(1, int(round(scale)))
        return np.repeat(np.repeat(img, k, axis=0), k, axis=1), float(k)


class OcrEngine:
    """懒加载 + 线程内串行调用，进程里只保留一个模型实例。"""
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or logging.getLogger(__name__)
        self._engine = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._engine is not None:
            return self._engine
        # onnxruntime 默认会开满所有核心，小模型也照样铺满 CPU。
        # 限成 1 线程：识别本身不需要并行，单线程反而更省电、风扇不飙。
        import os as _os

        _os.environ.setdefault("OMP_NUM_THREADS", "1")
        params = {
            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        try:
            from rapidocr import RapidOCR  # 3.x

            try:
                self._engine = RapidOCR(params=params)
            except TypeError:
                self._engine = RapidOCR()
        except Exception:
            from rapidocr_onnxruntime import RapidOCR  # 2.x 老包名

            self._engine = RapidOCR()
        self.log.info("OCR 引擎已加载: %s（限 2 线程）", type(self._engine).__name__)
        return self._engine

    def rebuild(self) -> None:
        """引擎异常后重建，避免一次崩溃导致永久失效。"""
        with self._lock:
            self._engine = None
        self.warmup()

    def warmup(self) -> None:
        try:
            blank = np.full((48, 160, 3), 255, dtype=np.uint8)
            self.recognize(blank)
        except Exception as exc:  # 预热失败不影响主流程
            self.log.warning("OCR 预热失败: %s", exc)

    def recognize(self, img_bgr: np.ndarray, scale: float | None = None,
                  rec_only: bool = False) -> tuple[list[TextLine], str]:
        """返回 (行列表, 全部文本)。识别框坐标已换算回原图坐标系。

        rec_only=True 时跳过文本检测器，直接整张图做识别：
        裁剪得很小的单行区域里，检测器最容易"什么都没有"，而识别器往往还能读出来。
        """
        img, used_scale = _upscale(img_bgr, scale_hint=scale)
        with self._lock:
            engine = self._ensure()
            if rec_only:
                try:
                    out = engine(img, use_det=False, use_cls=False, use_rec=True)
                except TypeError:      # 老版本不支持关键字参数
                    out = engine(img)
            else:
                out = engine(img)
        lines = _group_lines(_normalize_output(out))
        if not lines and rec_only:
            txt = getattr(out, "txt", None) or getattr(out, "text", None)
            if txt:
                score = float(getattr(out, "score", 1.0) or 1.0)
                h, w = img.shape[:2]
                lines = [TextLine(text=str(txt), score=score, box=(0.0, 0.0, float(w), float(h)))]
        if used_scale != 1.0:
            for line in lines:
                x0, y0, x1, y1 = line.box
                line.box = (x0 / used_scale, y0 / used_scale, x1 / used_scale, y1 / used_scale)
        text = "\n".join(line.text for line in lines) if lines else ""
        return lines, text
