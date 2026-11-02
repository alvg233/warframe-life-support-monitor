r"""离线冒烟测试：合成一张像游戏 UI 的图，验证 截图->OCR->取数 链路。

不需要抓屏，不需要钉钉，纯本地跑：
    $env:PYTHONPATH=".vendor"; python scripts\smoke_ocr.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from screen_ocr_alert.numbers import format_value, parse_value  # noqa: E402
from screen_ocr_alert.ocr import OcrEngine  # noqa: E402


def load_font(size: int):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf"):
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_image(text: str, size=(320, 90), bg=(28, 34, 40)) -> "Image.Image":
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    draw.text((12, size[1] // 2 - 26), "维生系统", font=load_font(30), fill=(190, 200, 210))
    draw.text((150, size[1] // 2 - 30), text, font=load_font(40), fill=(90, 220, 200))
    return img


def main() -> int:
    cases = ["86%", "39%", "12%", "04%", "100%"]
    engine = OcrEngine()
    engine.warmup()
    ok = True
    Path("_smoke").mkdir(exist_ok=True)
    for raw in cases:
        img = make_image(raw)
        path = Path("_smoke") / f"case_{raw.replace('%', 'pct')}.png"
        img.save(path)
        bgr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()
        lines, text = engine.recognize(bgr)
        value = parse_value(text, "first")
        print(f"[case] 期望 {raw:>5} | OCR={text!r} | 解析={format_value(value)} | 行数={len(lines)}")
        if value is None or abs(value - float(raw.rstrip("%"))) > 0.51:
            ok = False
            print(f"  !! 解析不符合预期（期望 {raw}）")

    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
