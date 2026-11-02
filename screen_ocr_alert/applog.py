"""日志分三份，各管一件事，避免刷屏：

- logs/app.log     关键事件：启动、停止、报警、发送失败（少量）
- logs/samples.log 紧凑采样记录，一行一次：`2026-10-30 07:20:01  92` / `...  fail`
- logs/events.jsonl 只有跌破阈值那几类业务记录
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
SAMPLE_FORMAT = "%(asctime)s  %(message)s"


def setup(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("screen_ocr_alert")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        fh = RotatingFileHandler(
            log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(_FORMAT))
        fh.setLevel(level)
        logger.addHandler(fh)
        if sys.stdout is not None:  # 窗口版 exe 没有控制台
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(logging.Formatter(_FORMAT))
            sh.setLevel(level)
            logger.addHandler(sh)

    # 采样记录：独立 logger + 独立文件，只有时间和"值/fail"
    samples = logging.getLogger("screen_ocr_alert.samples")
    samples.setLevel(logging.INFO)
    samples.propagate = False
    if not samples.handlers:
        sh = RotatingFileHandler(
            log_dir / "samples.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        sh.setFormatter(logging.Formatter(SAMPLE_FORMAT, "%Y-%m-%d %H:%M:%S"))
        sh.setLevel(logging.INFO)
        samples.addHandler(sh)

    # 第三方库降噪
    for noisy in ("RapidOCR", "rapidocr", "onnxruntime", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


def samples_logger() -> logging.Logger:
    """紧凑采样记录。setup() 之前没有 handler，写入等于空操作。"""
    return logging.getLogger("screen_ocr_alert.samples")
