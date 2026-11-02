r"""界面自检：真的把窗口建起来、切一次 Overlay、保存配置，然后自动关掉。

不需要人工点按钮，用于确认没有 Tcl/Tk 语法或布局错误。
    $env:PYTHONPATH=".vendor"; python scripts\selftest_ui.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

HOME = Path(tempfile.mkdtemp(prefix="soa-selftest-"))
os.environ["SCREEN_OCR_ALERT_HOME"] = str(HOME)

from screen_ocr_alert import config as cfgmod  # noqa: E402
from screen_ocr_alert.applog import setup  # noqa: E402
from screen_ocr_alert.capture import enable_dpi_awareness  # noqa: E402
from screen_ocr_alert.ui import AppUI  # noqa: E402

enable_dpi_awareness()
logger = setup(cfgmod.log_dir())
cfg = cfgmod.load(HOME / "config.json")
errors: list[str] = []


def main() -> int:
    app = AppUI(cfg, HOME / "config.json", logger)
    app.root.report_callback_exception = lambda *a: errors.append(repr(a[1]))

    def step_mid():
        app.overlay.show({"x": 100, "y": 100, "w": 200, "h": 60})
        app.overlay.set_status("86%", alert=False)
        app.overlay.update_region({"x": 120, "y": 110, "w": 200, "h": 60})
        app.vars["warn"].set("50")
        app.vars["alert"].set("41")
        app.vars["region_mode"].set("比例（相对游戏窗口）")
        app.vars["x"].set("0.050")
        app.vars["y"].set("0.100")
        app.vars["w"].set("0.120")
        app.vars["h"].set("0.080")
        app._collect()
        app._save()
        app._apply_region(app.snapshot()["rules"][0]["region"])
        app._show_overlay()
        app._append_log("[selftest] 界面正常")
        print("mid-step ok, region=", app.cfg["rules"][0]["region"],
              "alert=", app.cfg["rules"][0]["alert"]["value"])

        # 首页/高级切换 + 四步向导逐页点一遍
        app._show_advanced()
        app._show_simple()
        app.open_wizard(0)
        wiz = app._wizard
        if wiz is not None:
            wiz.refresh_windows()
            for step in range(4):
                wiz.notebook.select(step)
                app.root.update_idletasks()
            wiz.finish()
            print("wizard ok, items=", len(wiz._win_items) if wiz else 0)
        print("running:", app.monitor.is_alive())

        # 日志窗口：只显示业务记录
        app.open_log_viewer()
        viewer = app._log_viewer
        if viewer is not None:
            app.monitor.prepare(app.snapshot())
            app.monitor.recorder.event("warn_cross", rule="维生系统", value=47.0,
                                       prev=62.0, warn=50.0, alert=35.0,
                                       snapshot="snapshots/demo.png")
            viewer._tick()
            content = viewer.ev_text.get("1.0", "end")
            print("log viewer 报警记录:", content.count("\n"), "行，含跌破:", "跌破预警线" in content)
            viewer.top.destroy()

        # 截图浏览窗口：应能列出目录并显示张数
        app.monitor.prepare(app.snapshot())
        app.monitor.recorder.snapshot(cfg["rules"][0], "_TEST", reason="自检截图")
        app.open_snapshot_dir()
        browser = app._snapshot_browser
        if browser is not None:
            print("snapshot browser:", browser.info_var.get(), "| 行数:", len(browser._files))
            print("ShellExecuteW 返回码（>32 才算成功）:",
                  AppUI._shell_execute(Path(app._snapshot_root())))
            if browser.tree.get_children():
                browser.tree.selection_set(browser.tree.get_children()[0])
                browser.open_selected()
                iv = getattr(browser, "_image_viewer", None)
                print("image viewer 渲染成功:", bool(iv and iv._photo is not None))
                if iv:
                    iv.step(1)
                    iv.toggle_fit()
                    iv.top.destroy()
            browser.top.destroy()

    app.root.after(700, step_mid)
    app.root.after(3200, app._quit)
    app.run()

    saved = (HOME / "config.json").exists()
    print("config saved:", saved)
    print("callback errors:", errors or "none")
    return 0 if saved and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
