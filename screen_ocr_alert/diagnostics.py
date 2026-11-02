"""本地诊断包：把出错现场打包成一个 zip，方便发给作者排查。

原则：**完全本地**，不做任何联网上传；只写文件到数据目录。
包含内容（都是脱敏后的）：
  system.txt        系统与依赖版本、DPI、屏幕尺寸
  config.json       配置（Webhook / 密钥等已打码）
  app.log / startup.log
  samples.log       尾部若干行
  events.jsonl      尾部若干行
  crash.log         崩溃转储（若有）
  snapshots/        最近几张截图（若有）
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
import zipfile
from pathlib import Path

SENSITIVE_KEYS = ("webhook", "secret", "token", "password", "sign")
MAX_TEXT_BYTES = 2 * 1024 * 1024


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if any(word in str(key).lower() for word in SENSITIVE_KEYS):
                text = str(value or "")
                out[key] = f"***已打码（原长度 {len(text)}）***" if text else ""
            else:
                out[key] = _redact(value)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def system_info() -> str:
    lines = [
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"主机：{socket.gethostname()}",
        f"系统：{platform.platform()}",
        f"架构：{platform.machine()}",
        f"Python：{sys.version.split()[0]}（frozen={getattr(sys, 'frozen', False)}）",
        f"程序路径：{sys.executable}",
        f"工作目录：{os.getcwd()}",
    ]
    try:
        import ctypes

        user32 = ctypes.windll.user32
        lines.append(f"屏幕：{user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")
        try:
            lines.append(f"DPI：{user32.GetDpiForSystem()}")
        except Exception:
            pass
    except Exception:
        pass
    for name in ("rapidocr", "onnxruntime", "numpy", "PIL", "mss", "psutil", "requests", "pystray"):
        try:
            module = __import__(name)
            lines.append(f"{name}：{getattr(module, '__version__', '未知')}")
        except Exception:
            lines.append(f"{name}：未安装")
    return "\n".join(lines)


def _tail(path: Path, limit: int = 400) -> str:
    try:
        if not path.exists():
            return f"（无此文件：{path.name}）"
        data = path.read_bytes()
        if len(data) > MAX_TEXT_BYTES:
            data = data[-MAX_TEXT_BYTES:]
        text = data.decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-limit:])
    except Exception as exc:
        return f"（读取失败：{exc}）"


def export(reason: str = "手动导出", shots: int = 3) -> Path:
    """生成诊断包，返回 zip 路径。失败时抛异常，由调用方提示。"""
    from . import config as cfgmod

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = cfgmod.log_dir() / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"diag-{stamp}.zip"

    cfg = cfgmod.load()
    logs = cfgmod.log_dir()
    data_dir = cfgmod.data_dir()

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("reason.txt", f"导出原因：{reason}\n导出时间：{stamp}\n")
        zf.writestr("system.txt", system_info())
        try:
            zf.writestr("config.json",
                        json.dumps(_redact(cfg), ensure_ascii=False, indent=2))
        except Exception as exc:
            zf.writestr("config.json", f"（配置序列化失败：{exc}）")
        for name, limit in (("app.log", 800), ("startup.log", 400),
                            ("crash.log", 400), ("samples.log", 400),
                            ("events.jsonl", 400)):
            zf.writestr(f"logs/{name}", _tail(logs / name, limit))

        snap_dir = data_dir / str((cfg.get("snapshot") or {}).get("dir") or "snapshots")
        if shots > 0 and snap_dir.exists():
            files = sorted(snap_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime,
                           reverse=True)[:shots]
            for item in files:
                try:
                    import io

                    from PIL import Image

                    with Image.open(item) as im:
                        if im.width > 1280:
                            im = im.resize((1280, int(im.height * 1280 / im.width)),
                                           Image.LANCZOS)
                        buf = io.BytesIO()
                        im.convert("RGB").save(buf, "JPEG", quality=72)
                    zf.writestr(f"snapshots/{item.stem}.jpg", buf.getvalue())
                except Exception:
                    pass
    return target
