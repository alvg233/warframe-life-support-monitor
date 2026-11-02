"""入口：图形界面 / 无界面 / 自检命令。

常用：
    run.bat                       图形界面（推荐）
    python -m screen_ocr_alert --headless      无界面常驻
    python -m screen_ocr_alert --detect-once   只识别一次，打印结果
    python -m screen_ocr_alert --test-dingtalk 发一条钉钉测试消息
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _bootstrap_path() -> None:
    """.vendor / .venv 里的依赖也能直接用，方便别人拿走就跑。"""
    root = Path(__file__).resolve().parent.parent
    for extra in (root / ".vendor",):
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))


WINDOW_TITLE = "屏幕数值监控 · 钉钉提醒"


def _activate_existing_window(title: str = WINDOW_TITLE) -> bool:
    """把已经在运行的那个窗口叫到前台（重复双击时用，比弹提示框友好）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        return True
    except Exception:
        return False


class Reporter:
    """GUI 版无控制台，print 可能失败：降级为写日志与弹框。"""

    def __init__(self, logger=None) -> None:
        self.lines: list[str] = []
        self.quiet = sys.stdout is None
        self.logger = logger

    def __call__(self, msg: str = "") -> None:
        self.lines.append(str(msg))
        if not self.quiet:
            try:
                print(msg, flush=True)
            except Exception:
                self.quiet = True

    def finish(self, title: str = "屏幕数值监控") -> None:
        if not self.quiet or not self.lines:
            return
        if self.logger is not None:
            for line in self.lines:
                self.logger.info("%s", line)
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, "\n".join(self.lines)[:3000], title, 0x40)
        except Exception:
            pass


def _detect_once(cfg: dict, logger, rep) -> int:
    from .capture import ScreenCapture
    from .numbers import format_value, parse_value
    from .ocr import OcrEngine

    rule = cfg["rules"][0]
    region = rule["region"]
    snap = cfg.get("snapshot") or {}
    cap = ScreenCapture(backend=snap.get("backend", "window"),
                        window_title=snap.get("window_title", ""),
                        process_name=snap.get("process_name", ""))
    px = cap.region_pixels(region)
    rep(f"区域: 模式={region.get('mode', 'absolute')} 配置={region}")
    rep(f"实际像素: x={px['x']} y={px['y']} w={px['w']} h={px['h']}")
    img, source = cap.grab_region_auto(region)
    rep(f"截图: {img.shape[1]}x{img.shape[0]}（通道 {source}）")
    engine = OcrEngine(logger)
    lines, text = engine.recognize(img)
    rep(f"OCR 文本: {text!r}")
    for line in lines:
        rep(f"  行: {line.text!r} 置信度={line.score:.3f}")
    value = parse_value(text, rule["number"].get("mode", "first"),
                        bool(rule["number"].get("require_percent")))
    rep(f"解析数值: {format_value(value)}")
    warn_v = float((rule.get("warn") or {}).get("value", 50))
    alert_v = float((rule.get("alert") or {}).get("value", 35))
    if value is None:
        zone = "无法判定"
    elif value < alert_v:
        zone = "报警区（会截图 + 钉钉）"
    elif value < warn_v:
        zone = "预警区（会记日志 + 截图）"
    else:
        zone = "正常"
    rep(f"阈值: 预警 {warn_v:g} / 报警 {alert_v:g} -> {zone}")
    return 0 if value is not None else 3


def _headless(cfg: dict, logger) -> int:
    import copy

    from .monitor import MonitorService
    from .singleton import lower_priority

    if cfg["app"].get("low_priority"):
        lower_priority(logger)
    frozen = copy.deepcopy(cfg)
    service = MonitorService(lambda: frozen, logger=logger)

    def on_event(evt: dict) -> None:
        if evt.get("type") == "sample":
            print(f"[样本] {evt.get('rule')} 值={evt.get('value')} 文本={evt.get('text')!r}")
        elif evt.get("type") == "notify":
            print(f"[通知] {evt.get('message')}")
        elif evt.get("type") == "error":
            print(f"[错误] {evt.get('message')}")

    service.on_event = on_event
    service.start()
    logger.info("无界面模式已启动，Ctrl+C 退出")
    try:
        while service.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("收到中断，正在退出")
        service.stop()
        service.join(timeout=5)
    return 0


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def trace(msg: str) -> None:
    """把启动过程写进 logs/startup.log，界面起不来时靠它定位。"""
    try:
        from . import config as cfgmod

        d = cfgmod.data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "startup.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        try:
            d = _base_dir() / "logs"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "startup.log", "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    trace("=== start === frozen=%s exe=%s cwd=%s argv=%r" % (
        getattr(sys, "frozen", False), sys.executable, os.getcwd(),
        sys.argv[1:] if argv is None else argv))
    try:
        return _main(argv)
    except SystemExit as exc:          # argparse 的正常退出，不当作崩溃
        trace(f"参数解析退出: {exc.code}")
        return int(exc.code or 0)
    except BaseException:
        import traceback as _tb

        text = _tb.format_exc()
        trace("!!! 未捕获异常:\n" + text)
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0, "启动失败，已把详细信息写入 logs\\startup.log\n\n" + text[-1200:],
                "屏幕数值监控", 0x10)
        except Exception:
            pass
        return 1


_CRASH_LOG = None


def _install_crash_hooks(logger) -> None:
    """崩溃或未捕获异常时：写 crash.log，并自动生成一次诊断包（仅首次）。"""
    global _CRASH_LOG
    import faulthandler
    import threading
    import traceback

    from . import config as cfgmod

    try:
        _CRASH_LOG = open(cfgmod.log_dir() / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_CRASH_LOG, all_threads=True)
    except Exception:
        pass

    state = {"exported": False}

    def _export(reason: str) -> None:
        if state["exported"]:
            return
        state["exported"] = True
        try:
            from . import diagnostics

            path = diagnostics.export(reason)
            logger.error("已自动保存诊断包：%s", path)
        except Exception:
            logger.error("生成诊断包失败", exc_info=True)

    def _excepthook(exc_type, exc, tb):
        logger.error("未捕获异常：\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        _export("未捕获异常")
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_hook(args):
        logger.error("线程 %s 未捕获异常：\n%s", getattr(args.thread, "name", "?"),
                     "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                        args.exc_traceback)))
        _export("线程未捕获异常")

    sys.excepthook = _excepthook
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screen_ocr_alert", description="屏幕数值监控 + 钉钉提醒")
    parser.add_argument("--headless", action="store_true", help="无界面后台运行")
    parser.add_argument("--detect-once", action="store_true", help="只识别一次并打印结果")
    parser.add_argument("--test-dingtalk", action="store_true", help="发送钉钉测试消息")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--dump-config", action="store_true", help="写出默认配置后退出")
    parser.add_argument("--export-diag", action="store_true", help="生成诊断包后退出")
    parser.add_argument("--no-single-instance", action="store_true", help="跳过多开检测（调试用）")
    parser.add_argument("config_pos", nargs="?", default=None,
                        help="配置文件路径（可选，也可用 -c/--config）")
    # 用 parse_known_args：快捷方式里残留的旧参数不该让程序直接起不来
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        trace(f"忽略未知参数: {unknown!r}")
    trace(f"参数解析完成: {vars(args)}")

    from . import config as cfgmod
    from .applog import setup
    from .capture import enable_dpi_awareness

    trace("导入模块完成")
    enable_dpi_awareness()
    logger = setup(cfgmod.log_dir())
    _install_crash_hooks(logger)
    rep = Reporter(logger)
    cfg_path_arg = args.config
    if not cfg_path_arg and args.config_pos:
        # 位置参数只有当它真是个配置文件时才认，否则可能是快捷方式里残留的旧参数
        candidate = Path(args.config_pos).expanduser()
        if candidate.is_file():
            cfg_path_arg = str(candidate)
        else:
            trace(f"忽略不是配置文件的参数: {args.config_pos}")
    path = Path(cfg_path_arg).expanduser() if cfg_path_arg else cfgmod.config_path()
    cfg = cfgmod.load(path)
    trace(f"配置加载完成: {path}")

    if args.dump_config:
        cfgmod.save(cfg, path)
        rep(f"配置已写出: {path}")
        rep.finish()
        return 0

    if args.export_diag:
        try:
            from . import diagnostics

            target = diagnostics.export("命令行导出")
            rep(f"诊断包已生成: {target}")
            rep.finish()
            return 0
        except Exception as exc:
            rep(f"生成失败: {exc}")
            rep.finish()
            return 1

    if args.test_dingtalk:
        from .dingtalk import DingTalk, DingTalkError

        try:
            rep(DingTalk(cfg["dingtalk"], logger).test())
            rep.finish()
            return 0
        except DingTalkError as exc:
            rep(f"发送失败: {exc}")
            rep.finish()
            return 2

    if args.detect_once:
        _bootstrap_path()
        try:
            code = _detect_once(cfg, logger, rep)
        except Exception as exc:
            rep(f"识别失败: {exc}")
            code = 3
        rep.finish()
        return code

    if args.headless:
        _bootstrap_path()
        return _headless(cfg, logger)

    # 图形界面
    instance = None
    if not args.no_single_instance:
        from .singleton import SingleInstance

        instance = SingleInstance()
        if not instance.acquire():
            # 已经在跑：把那个窗口叫到前台，而不是弹一个让人困惑的提示框
            activated = _activate_existing_window()
            if not activated:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0, "屏幕数值监控已经在运行了，但没找到它的窗口。\n"
                       "可以到任务栏找「屏幕数值监控」，或结束进程后重开。",
                    "已在运行", 0x40,
                )
            return 0

    try:
        _bootstrap_path()
        trace("准备导入界面模块")
        from .ui import AppUI

        trace("界面模块导入完成，开始建窗口")
        AppUI(cfg, path, logger).run()
        trace("界面已退出")
    except Exception:
        import traceback as _tb

        trace("!!! 界面启动失败:\n" + _tb.format_exc())
        logger.exception("界面启动失败")
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, "启动失败，详见 logs\\startup.log", "屏幕数值监控", 0x10)
        except Exception:
            pass
        return 1
    finally:
        if instance is not None:
            instance.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
