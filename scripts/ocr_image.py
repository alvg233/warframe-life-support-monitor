r"""对一张图片跑一次 OCR（调试区域坐标用）。

    $env:PYTHONPATH=".vendor"; python scripts\ocr_image.py 图片.png [--crop x y w h]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from screen_ocr_alert.numbers import extract_numbers, format_value  # noqa: E402
from screen_ocr_alert.ocr import OcrEngine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--crop", nargs=4, type=int, metavar=("X", "Y", "W", "H"))
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGB")
    print(f"图片尺寸: {img.size[0]} x {img.size[1]}")
    if args.crop:
        x, y, w, h = args.crop
        img = img.crop((x, y, x + w, y + h))
        print(f"裁剪: x={x} y={y} w={w} h={h}")
    bgr = np.asarray(img)[:, :, ::-1].copy()

    engine = OcrEngine()
    lines, text = engine.recognize(bgr)
    for line in lines:
        x0, y0, x1, y1 = line.box
        print(f"  [{line.score:.2f}] ({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f}) {line.text!r}")
    print("全文:", repr(text))
    print("数字:", [format_value(v) for v in extract_numbers(text)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
