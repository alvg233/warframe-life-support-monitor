"""控制面板：区域框选、阈值、截图、钉钉、启停、日志。

线程模型：
- tkinter 主线程只做 UI；
- MonitorService 在后台线程做截图/OCR/判定/发消息；
- 后台通过 queue 把事件回传给 UI，UI 每 250ms 泵一次，绝不让线程直接碰控件。
"""
from __future__ import annotations

import copy
import ctypes
import logging
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import config as cfgmod
from .alerts import RuleState, zone_of
from .dingtalk import DingTalk, DingTalkError
from .monitor import MonitorService
from .numbers import format_value
from .overlay import RegionOverlay
from .picker import pick_region

DIRECTIONS = {"第一个数字": "first", "最后一个数字": "last", "最大": "max", "最小": "min"}
DIRECTIONS_REV = {v: k for k, v in DIRECTIONS.items()}
BACKENDS = {"游戏窗口": "window", "整屏": "desktop", "自动（先整屏）": "auto"}
BACKENDS_REV = {v: k for k, v in BACKENDS.items()}
REGION_MODES = {"比例（相对游戏窗口）": "ratio", "屏幕像素": "absolute"}
REGION_MODES_REV = {v: k for k, v in REGION_MODES.items()}


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


def foreground_window_info() -> tuple[str, str]:
    """当前前台窗口的标题与进程名，用来一键填截图目标。"""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    try:
        import psutil

        name = psutil.Process(pid.value).name()
    except Exception:
        pass
    return buf.value, name


class AppUI:
    def __init__(self, cfg: dict, cfg_path: Path, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.log = logger
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.commands: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()
        self._closing = False
        self._save_job = None
        self.overlay: RegionOverlay | None = None
        self.tray = None
        self._wizard = None
        self._wizard_after_region = None
        self._hud = None
        self._hud_sig = None
        self._last_value_text = "--"
        self._last_time_text = "--:--"
        self._last_alert = False
        self._await_new_round = False
        self._rearm_job = None
        self._rearm_fp = None
        self.states: dict[str, RuleState] = {}
        self.monitor = MonitorService(self.snapshot, self._on_event, logger, states=self.states)

        self.root = tk.Tk()
        self.root.title("屏幕数值监控 · 钉钉提醒")
        self.root.report_callback_exception = self._on_tk_callback_error
        self.root.geometry("620x660")
        self.root.minsize(560, 520)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass

        self._build_vars()
        self._build_widgets()
        self._bind_traces()

        self.log.addHandler(QueueLogHandler(self.log_queue))
        self.overlay = RegionOverlay(self.root, self._on_overlay_change)
        try:
            self.monitor.prepare(self.snapshot())   # 先建好截图器，边框才能按窗口定位
        except Exception:
            self.log.debug("初始化截图器失败", exc_info=True)
        if self.cfg["app"].get("show_overlay", True):
            self._show_overlay()
        self.root.after(1200, self._follow_region)

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.after(200, self._pump)
        self.root.after(150, self._bring_to_front)
        if self.cfg["app"].get("start_monitoring"):
            self.root.after(900, self.start_monitor)
        self._start_tray()
        if not self.cfg["app"].get("wizard_done"):
            self.root.after(1000, self.open_wizard)

    # ---------------- 配置快照 ----------------
    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.cfg)

    def _rule(self) -> dict:
        rules = self.cfg.get("rules") or []
        if not rules:
            rules = [copy.deepcopy(cfgmod.DEFAULT_RULE)]
            self.cfg["rules"] = rules
        return rules[0]

    # ---------------- 变量 ----------------
    def _build_vars(self) -> None:
        rule = self._rule()
        num = rule.get("number") or {}
        warn = rule.get("warn") or {}
        alert = rule.get("alert") or {}
        sample = self.cfg["sample"]
        guard = self.cfg["guard"]
        snap = self.cfg["snapshot"]
        ding = self.cfg["dingtalk"]
        app = self.cfg["app"]
        rng = num.get("valid_range")
        rng_text = f"{format_value(rng[0])}-{format_value(rng[1])}" if rng else ""

        v = self.vars = {}
        region = rule["region"]
        rmode = str(region.get("mode") or "ratio")
        v["region_mode"] = tk.StringVar(value=REGION_MODES_REV.get(rmode, "比例（相对游戏窗口）"))

        def _fmt(val):
            return f"{float(val):.3f}" if rmode == "ratio" else str(int(float(val)))

        v["x"] = tk.StringVar(value=_fmt(region.get("x", 0)))
        v["y"] = tk.StringVar(value=_fmt(region.get("y", 0)))
        v["w"] = tk.StringVar(value=_fmt(region.get("w", 0.3)))
        v["h"] = tk.StringVar(value=_fmt(region.get("h", 0.3)))
        v["mode"] = tk.StringVar(value=DIRECTIONS_REV.get(num.get("mode", "first"), "第一个数字"))
        v["require_percent"] = tk.BooleanVar(value=bool(num.get("require_percent", True)))
        v["range"] = tk.StringVar(value=rng_text)
        v["warn"] = tk.StringVar(value=format_value(warn.get("value", 50)))
        v["alert"] = tk.StringVar(value=format_value(alert.get("value", 35)))
        v["threshold_diff"] = tk.StringVar(value=format_value(warn.get("value", 50)))
        v["cooldown"] = tk.StringVar(value=str(alert.get("cooldown_sec", 60)))
        v["realert"] = tk.StringVar(value=str(alert.get("realert_sec", 1800)))
        v["recover_notify"] = tk.BooleanVar(value=bool((rule.get("recover") or {}).get("notify", True)))
        v["notify_enabled"] = tk.BooleanVar(value=bool((rule.get("alert") or {}).get("notify", True)))
        v["log_enabled"] = tk.BooleanVar(value=bool((rule.get("alert") or {}).get("log", True)))
        v["sample_log"] = tk.BooleanVar(
            value=bool((self.cfg.get("sample") or {}).get("log_enabled", True)))
        hud_cfg = (self.cfg.get("app") or {}).get("hud") or {}
        v["hud_enabled"] = tk.BooleanVar(value=bool(hud_cfg.get("enabled", True)))
        v["hud_pos"] = tk.StringVar(value=str(hud_cfg.get("position", "顶部居中")))
        v["hud_opacity"] = tk.StringVar(value=str(hud_cfg.get("opacity", 0.78)))
        v["hud_font"] = tk.StringVar(value=str(hud_cfg.get("font_size", 20)))
        v["interval"] = tk.StringVar(value=str(sample.get("interval_sec", 1.5)))
        v["retry_count"] = tk.StringVar(value=str(sample.get("retry_count", 2)))
        v["retry_gap"] = tk.StringVar(value=str(sample.get("retry_gap_sec", 0.35)))
        v["max_drop"] = tk.StringVar(value=str(guard.get("max_drop_per_1_5s", 1.0)))
        v["drop_tol"] = tk.StringVar(value=str(guard.get("drop_tolerance", 0.5)))
        v["min_conf"] = tk.StringVar(value=str(guard.get("min_confidence", 0.45)))
        v["snapshot_enabled"] = tk.BooleanVar(value=bool(snap.get("enabled", True)))
        v["backend"] = tk.StringVar(value=BACKENDS_REV.get(snap.get("backend", "window"), "游戏窗口"))
        v["window_title"] = tk.StringVar(value=snap.get("window_title", ""))
        v["process_name"] = tk.StringVar(value=snap.get("process_name", ""))
        v["keep_days"] = tk.StringVar(value=str(snap.get("keep_days", 30)))
        v["webhook"] = tk.StringVar(value=ding.get("webhook", ""))
        v["secret"] = tk.StringVar(value=ding.get("secret", ""))
        v["keyword"] = tk.StringVar(value=ding.get("keyword", ""))
        v["ats"] = tk.StringVar(value=",".join(str(m) for m in ding.get("at_mobiles") or []))
        v["autostart"] = tk.BooleanVar(value=bool(app.get("start_monitoring", False)))
        v["low_priority"] = tk.BooleanVar(value=bool(app.get("low_priority", True)))
        v["show_overlay"] = tk.BooleanVar(value=bool(app.get("show_overlay", True)))

        # 急停
        emg = rule.get("emergency") or {}
        trig = emg.get("triggers") or {}
        t_r = emg.get("time_region") or {"x": 0.02, "y": 0.22, "w": 0.14, "h": 0.10}
        v["emg_enabled"] = tk.BooleanVar(value=bool(emg.get("enabled", False)))
        v["emg_zero"] = tk.BooleanVar(value=bool(trig.get("zero_percent", True)))
        v["emg_time"] = tk.BooleanVar(value=bool(trig.get("elapsed_minutes", False)))
        v["emg_minutes"] = tk.StringVar(value=format_value(emg.get("elapsed_minutes", 10)))
        v["emg_reads"] = tk.StringVar(value=str(emg.get("verify_reads", 5)))
        v["emg_auto_rearm"] = tk.BooleanVar(value=bool(emg.get("auto_rearm", True)))
        v["emg_gap"] = tk.StringVar(value=str(emg.get("verify_interval_sec", 0.6)))
        v["emg_click"] = tk.BooleanVar(value=bool(emg.get("click_before_key", True)))
        click_ratio = emg.get("click_ratio") or [0.5, 0.5]
        v["emg_click_x"] = tk.StringVar(value=f"{float(click_ratio[0]):.2f}")
        v["emg_click_y"] = tk.StringVar(value=f"{float(click_ratio[1]):.2f}")
        v["t_x"] = tk.StringVar(value=f"{float(t_r.get('x', 0.02)):.3f}")
        v["t_y"] = tk.StringVar(value=f"{float(t_r.get('y', 0.22)):.3f}")
        v["t_w"] = tk.StringVar(value=f"{float(t_r.get('w', 0.14)):.3f}")
        v["t_h"] = tk.StringVar(value=f"{float(t_r.get('h', 0.10)):.3f}")

    # ---------------- 界面 ----------------
    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=(14, 10))
        outer.pack(fill="both", expand=True)
        self.simple_frame = ttk.Frame(outer)
        self.advanced_frame = ttk.Frame(outer)
        self._run_buttons: list[tuple[ttk.Button, ttk.Button]] = []

        self._build_simple(self.simple_frame)
        self._build_advanced(self.advanced_frame)
        if str(self.cfg["app"].get("ui_mode", "simple")) == "advanced":
            self._show_advanced()
        else:
            self._show_simple()

    # ---------- 简易首页 ----------
    def _build_simple(self, parent) -> None:
        self.title_var = tk.StringVar(value=self._rule().get("name", "数值"))
        ttk.Label(parent, textvariable=self.title_var, font=("Microsoft YaHei UI", 13)).pack(
            anchor="center", pady=(2, 0))
        self.big_var = tk.StringVar(value="--")
        self.big_label = tk.Label(parent, textvariable=self.big_var,
                                  font=("Microsoft YaHei UI", 52, "bold"), fg="#2f8f6a")
        self.big_label.pack(anchor="center")
        self.light_var = tk.StringVar(value="● 还没开始")
        self.light_label = tk.Label(parent, textvariable=self.light_var,
                                    font=("Microsoft YaHei UI", 12), fg="#777")
        self.light_label.pack(anchor="center")

        # 固定高度的状态区：文字变多/换行时不会把下面的按钮顶下去
        status_box = tk.Frame(parent, height=44)
        status_box.pack(fill="x", pady=(6, 0))
        status_box.pack_propagate(False)
        self.small_var = tk.StringVar(value="点「开始监控」，程序每 1.5 秒看一眼，低于设定值自动提醒。")
        ttk.Label(status_box, textvariable=self.small_var, foreground="#666",
                  wraplength=560, justify="center").pack(fill="both", expand=True)

        # 简易页也放急停开关与时间
        emg_row = ttk.Frame(parent)
        emg_row.pack(fill="x", padx=6, pady=(10, 0))
        ttk.Checkbutton(emg_row, text="急停", variable=self.vars["emg_enabled"]).pack(side="left")
        ttk.Checkbutton(emg_row, text="百分比归零触发", variable=self.vars["emg_zero"]).pack(
            side="left", padx=(12, 0))
        ttk.Checkbutton(emg_row, text="时间到达", variable=self.vars["emg_time"]).pack(
            side="left", padx=(12, 0))
        ttk.Entry(emg_row, textvariable=self.vars["emg_minutes"], width=5).pack(side="left", padx=4)
        ttk.Label(emg_row, text="分钟").pack(side="left")
        self.time_var = tk.StringVar(value="时间 --:--")
        tk.Label(emg_row, textvariable=self.time_var, font=("Consolas", 12),
                 fg="#2f8f6a").pack(side="right")

        row = ttk.Frame(parent)
        row.pack(anchor="center", pady=(12, 0))
        b1, b2 = self._make_run_buttons(row)
        b1.pack(side="left")
        b2.pack(side="left", padx=6)
        ttk.Button(row, text="设置向导", command=self.open_wizard).pack(side="left", padx=6)

        row2 = ttk.Frame(parent)
        row2.pack(anchor="center", pady=(8, 0))
        ttk.Button(row2, text="立即看一次", command=self.detect_once).pack(side="left", padx=4)
        ttk.Button(row2, text="截图文件夹", command=self.open_snapshot_dir).pack(side="left", padx=4)
        ttk.Button(row2, text="查看日志", command=self.open_log_viewer).pack(side="left", padx=4)
        ttk.Button(row2, text="高级设置", command=self._show_advanced).pack(side="left", padx=4)

        # 底部固定：提示行在最低，事件区在它上面，两块都不随文字长短移动
        ttk.Label(parent, text="作者 QQ：494735622（问题反馈请联系）",
                  foreground="#7a7a7a").pack(anchor="center", side="bottom", pady=(4, 0))
        ttk.Label(parent, text="阈值、采样快慢、截图通道都在「高级设置」里。",
                  foreground="#999").pack(anchor="center", side="bottom", pady=4)
        event_box = tk.Frame(parent, height=46)
        event_box.pack(side="bottom", fill="x", pady=(6, 0), padx=8)
        event_box.pack_propagate(False)
        self.last_event_var = tk.StringVar(value="")
        ttk.Label(event_box, textvariable=self.last_event_var, foreground="#a15c00",
                  wraplength=580, justify="left").pack(anchor="w", fill="both", expand=True)

    def _make_run_buttons(self, parent):
        b1 = ttk.Button(parent, text="开始监控", command=self.start_monitor)
        b2 = ttk.Button(parent, text="停止监控", command=self.stop_monitor, state="disabled")
        self._run_buttons.append((b1, b2))
        return b1, b2

    def _show_simple(self) -> None:
        self.advanced_frame.pack_forget()
        self.simple_frame.pack(fill="both", expand=True)
        self.cfg["app"]["ui_mode"] = "simple"
        self.root.geometry("620x660")

    def _show_advanced(self) -> None:
        self.simple_frame.pack_forget()
        self.advanced_frame.pack(fill="both", expand=True)
        self.cfg["app"]["ui_mode"] = "advanced"
        self.root.geometry("720x840")

    # ---------- 高级面板 ----------
    def _build_advanced(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 6))
        b1, b2 = self._make_run_buttons(bar)
        b1.pack(side="left")
        b2.pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="待机中")
        ttk.Label(bar, textvariable=self.status_var, foreground="#0a6").pack(side="left", padx=12)
        ttk.Button(bar, text="返回简易界面", command=self._show_simple).pack(side="right")

        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        self._tab_detect(nb)
        self._tab_emergency(nb)
        self._tab_snapshot(nb)
        self._tab_notify(nb)
        self._tab_runtime(nb)

    # ① 检测
    def _tab_detect(self, nb) -> None:
        v = self.vars
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  检测区域与判定  ")

        f1 = ttk.LabelFrame(tab, text=" 区域（比例模式会跟着窗口缩放） ", padding=8)
        f1.pack(fill="x")
        ttk.Label(f1, text="模式").grid(row=0, column=0, sticky="e")
        ttk.Combobox(f1, textvariable=v["region_mode"], values=list(REGION_MODES), width=17,
                     state="readonly").grid(row=0, column=1, sticky="w", padx=(4, 8))
        for i, (key, label) in enumerate((("x", "X"), ("y", "Y"), ("w", "宽"), ("h", "高"))):
            ttk.Label(f1, text=label).grid(row=0, column=2 + i * 2, sticky="e",
                                           padx=(0 if i == 0 else 8, 4))
            ttk.Entry(f1, textvariable=v[key], width=7).grid(row=0, column=3 + i * 2, sticky="w")
        ttk.Button(f1, text="框选区域", command=self.pick).grid(row=1, column=0, columnspan=2,
                                                              sticky="w", pady=(8, 0))
        ttk.Button(f1, text="自动校准（收紧到文字）", command=self.calibrate).grid(
            row=1, column=2, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(f1, text="显示边框", command=self._show_overlay).grid(row=1, column=5, sticky="w",
                                                                       pady=(8, 0))
        ttk.Button(f1, text="隐藏边框", command=self._hide_overlay).grid(row=1, column=7, sticky="w",
                                                                       pady=(8, 0))
        ttk.Label(f1, text="比例模式填 0~1（例：x=0.02 y=0.10 w=0.16 h=0.10，只框住那行百分比）",
                  foreground="#666").grid(row=2, column=0, columnspan=9, sticky="w", pady=(6, 0))

        f2 = ttk.LabelFrame(tab, text=" 读数校验（防半透明画面错读） ", padding=8)
        f2.pack(fill="x", pady=8)
        ttk.Label(f2, text="采样间隔(秒)").grid(row=0, column=0, sticky="e")
        ttk.Entry(f2, textvariable=v["interval"], width=7).grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(f2, text="重读次数").grid(row=0, column=2, sticky="e")
        ttk.Entry(f2, textvariable=v["retry_count"], width=5).grid(row=0, column=3, sticky="w", padx=(4, 12))
        ttk.Label(f2, text="重读间隔(秒)").grid(row=0, column=4, sticky="e")
        ttk.Entry(f2, textvariable=v["retry_gap"], width=6).grid(row=0, column=5, sticky="w", padx=(4, 0))

        ttk.Label(f2, text="最大降速(/1.5秒)").grid(row=1, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(f2, textvariable=v["max_drop"], width=7).grid(row=1, column=1, sticky="w",
                                                                padx=(4, 12), pady=(8, 0))
        ttk.Label(f2, text="容差").grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Entry(f2, textvariable=v["drop_tol"], width=5).grid(row=1, column=3, sticky="w",
                                                                padx=(4, 12), pady=(8, 0))
        ttk.Label(f2, text="最低置信度").grid(row=1, column=4, sticky="e", pady=(8, 0))
        ttk.Entry(f2, textvariable=v["min_conf"], width=6).grid(row=1, column=5, sticky="w",
                                                                padx=(4, 0), pady=(8, 0))

        ttk.Label(f2, text="取值方式").grid(row=2, column=0, sticky="e", pady=(8, 0))
        ttk.Combobox(f2, textvariable=v["mode"], values=list(DIRECTIONS), width=10,
                     state="readonly").grid(row=2, column=1, sticky="w", padx=(4, 12), pady=(8, 0))
        ttk.Label(f2, text="有效范围").grid(row=2, column=2, sticky="e", pady=(8, 0))
        ttk.Entry(f2, textvariable=v["range"], width=10).grid(row=2, column=3, sticky="w",
                                                              padx=(4, 12), pady=(8, 0))
        ttk.Checkbutton(f2, text="只接受带 % 的数值", variable=v["require_percent"]).grid(
            row=2, column=4, columnspan=2, sticky="w", pady=(8, 0))

        f3 = ttk.LabelFrame(tab, text=" 两条线：跌破就记录，跌破第二条就通知 ", padding=8)
        f3.pack(fill="x")
        ttk.Label(f3, text="预警线（记日志+截图）").grid(row=0, column=0, sticky="e")
        ttk.Entry(f3, textvariable=v["warn"], width=7).grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(f3, text="报警线（+钉钉）").grid(row=0, column=2, sticky="e")
        ttk.Entry(f3, textvariable=v["alert"], width=7).grid(row=0, column=3, sticky="w", padx=(4, 12))
        ttk.Label(f3, text="报警冷却(秒)").grid(row=0, column=4, sticky="e")
        ttk.Entry(f3, textvariable=v["cooldown"], width=7).grid(row=0, column=5, sticky="w", padx=(4, 0))

        ttk.Label(f3, text="持续低于报警线时的重提醒(秒)").grid(row=1, column=0, columnspan=2,
                                                              sticky="e", pady=(8, 0))
        ttk.Entry(f3, textvariable=v["realert"], width=7).grid(row=1, column=2, sticky="w",
                                                               padx=(4, 12), pady=(8, 0))
        ttk.Checkbutton(f3, text="回到报警线以上时通知「已恢复」",
                        variable=v["recover_notify"]).grid(row=1, column=3, columnspan=3,
                                                           sticky="w", pady=(8, 0))
        ttk.Checkbutton(f3, text="低于阈值时发钉钉提醒", variable=v["notify_enabled"]).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f3, text="低于阈值时写本地记录（报警记录/截图事件）",
                        variable=v["log_enabled"]).grid(row=2, column=2, columnspan=4,
                                                        sticky="w", pady=(8, 0))

        ttk.Button(tab, text="立即识别一次", command=self.detect_once).pack(anchor="w", pady=(10, 4))
        self.result_var = tk.StringVar(value="尚未识别。框选区域后点「立即识别一次」。")
        ttk.Label(tab, textvariable=self.result_var, foreground="#1f6f4a", wraplength=620,
                  justify="left").pack(anchor="w")

    # ② 截图
    def _tab_emergency(self, nb) -> None:
        v = self.vars
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  急停  ")

        ttk.Checkbutton(
            tab, text="启用急停（触发后：游戏切前台 → 按 ESC → 连续识别 5 次确认）",
            variable=v["emg_enabled"]).grid(row=0, column=0, columnspan=6, sticky="w")

        box = ttk.LabelFrame(tab, text=" 触发条件（可多选，也可都不选） ", padding=8)
        box.grid(row=1, column=0, columnspan=6, sticky="we", pady=(10, 0))
        ttk.Checkbutton(box, text="百分比归零", variable=v["emg_zero"]).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(box, text="时间到达", variable=v["emg_time"]).grid(
            row=0, column=2, sticky="w", padx=(18, 4))
        ttk.Entry(box, textvariable=v["emg_minutes"], width=6).grid(row=0, column=3, sticky="w")
        ttk.Label(box, text="分钟").grid(row=0, column=4, sticky="w", padx=(4, 0))

        tr = ttk.LabelFrame(tab, text=" 时间区域（框住「43:27」那一块） ", padding=8)
        tr.grid(row=2, column=0, columnspan=6, sticky="we", pady=(10, 0))
        for i, (key, label) in enumerate((("t_x", "X"), ("t_y", "Y"), ("t_w", "宽"), ("t_h", "高"))):
            ttk.Label(tr, text=label).grid(row=0, column=i * 2, sticky="e",
                                           padx=(0 if i == 0 else 8, 4))
            ttk.Entry(tr, textvariable=v[key], width=7).grid(row=0, column=i * 2 + 1, sticky="w")
        ttk.Button(tr, text="框选时间区域", command=self.pick_time_region).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(tr, text="识别一次时间", command=self.test_time_ocr).grid(
            row=1, column=3, columnspan=2, sticky="w", pady=(8, 0))

        act = ttk.LabelFrame(tab, text=" 确认动作 ", padding=8)
        act.grid(row=3, column=0, columnspan=6, sticky="we", pady=(10, 0))
        ttk.Label(act, text="连续识别次数").grid(row=0, column=0, sticky="e")
        ttk.Entry(act, textvariable=v["emg_reads"], width=5).grid(row=0, column=1, sticky="w", padx=(4, 14))
        ttk.Label(act, text="间隔(秒)").grid(row=0, column=2, sticky="e")
        ttk.Entry(act, textvariable=v["emg_gap"], width=6).grid(row=0, column=3, sticky="w", padx=(4, 14))
        ttk.Button(act, text="演练（只识别，不按 ESC）", command=self.emergency_dry_run).grid(
            row=0, column=4, sticky="w")
        ttk.Button(act, text="解除急停锁", command=self.release_emergency).grid(
            row=0, column=5, sticky="w", padx=(8, 0))
        ttk.Button(act, text="立即执行一次急停（真实按键）",
                   command=self.emergency_run_now).grid(row=0, column=6, sticky="w", padx=(8, 0))
        ttk.Button(act, text="诊断（窗口/权限）", command=self.emergency_diagnose).grid(
            row=0, column=7, sticky="w", padx=(8, 0))
        ttk.Checkbutton(act, text="急停后等待下一局并自动恢复监控（计时器归零即视为新一局）",
                        variable=v["emg_auto_rearm"]).grid(row=1, column=0, columnspan=8,
                                                           sticky="w", pady=(8, 0))

        click_box = ttk.LabelFrame(tab, text=" 点击游戏（很多游戏要真实点击后才接收按键） ", padding=8)
        click_box.grid(row=4, column=0, columnspan=6, sticky="we", pady=(10, 0))
        ttk.Checkbutton(click_box, text="切前台后先点一下游戏窗口，再按 ESC",
                        variable=v["emg_click"]).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(click_box, text="点击位置 X").grid(row=1, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(click_box, textvariable=v["emg_click_x"], width=6).grid(row=1, column=1, sticky="w",
                                                                         padx=(4, 12), pady=(8, 0))
        ttk.Label(click_box, text="Y").grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Entry(click_box, textvariable=v["emg_click_y"], width=6).grid(row=1, column=3, sticky="w",
                                                                         padx=(4, 0), pady=(8, 0))
        ttk.Label(click_box, text="（0~1，相对窗口客户区；0.5/0.5 是正中间）",
                  foreground="#666").grid(row=1, column=4, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(click_box, text="点击可能触发游戏内动作，建议选择安全位置。",
                  foreground="#a15c00").grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))

        ttk.Label(tab, text=(
            "5 次均无合法数字判定为急停成功；任一次读到数字记为未确认。\n"
            "急停触发后不会重复按 ESC，直到你点「解除急停锁」或重启程序。\n"
            "时间区域只用于「时间到达」触发；勾选后首页会持续显示时间。"
        ), foreground="#666", justify="left", wraplength=600).grid(
            row=5, column=0, columnspan=6, sticky="w", pady=(12, 0))

    # ---------- 急停相关操作 ----------
    def pick_time_region(self) -> None:
        self.root.withdraw()
        self.root.update_idletasks()
        try:
            region = pick_region(self.root, None)
        finally:
            self.root.deiconify()
            self.root.lift()
        if not region:
            return
        ratio = self._to_ratio(region)
        for key, src in (("t_x", "x"), ("t_y", "y"), ("t_w", "w"), ("t_h", "h")):
            self.vars[key].set(f"{float(ratio.get(src, 0)):.3f}")
        self._save()
        self.test_time_ocr()

    def _time_region(self) -> dict:
        v = self.vars
        return {
            "mode": "ratio",
            "x": max(0.0, self._f(v["t_x"].get(), 0.02)),
            "y": max(0.0, self._f(v["t_y"].get(), 0.22)),
            "w": min(1.0, max(0.005, self._f(v["t_w"].get(), 0.14))),
            "h": min(1.0, max(0.005, self._f(v["t_h"].get(), 0.10))),
        }

    def test_time_ocr(self) -> None:
        self._collect()
        threading.Thread(target=self._run_time_ocr, daemon=True).start()

    def _run_time_ocr(self) -> None:
        try:
            self.monitor.prepare(self.snapshot())
            capture, ocr = self.monitor.capture, self.monitor._ocr
            region = self.snapshot()["rules"][0]["emergency"]["time_region"]
            img, src = capture.grab_region_auto(region)
            _lines, text = ocr.recognize(img)
            from .numbers import format_clock, parse_clock

            seconds = parse_clock(text)
            if seconds is None:
                msg = f"时间区域（通道 {src}）识别到 {text!r}，但没解析出 mm:ss"
                self.events.put({"type": "error", "message": msg})
            else:
                self.events.put({"type": "time", "seconds": seconds, "text": text})
                self.events.put({"type": "notify",
                                 "message": f"时间识别：{text!r} → {format_clock(seconds)}"})
        except Exception as exc:
            self.events.put({"type": "error", "message": f"时间识别失败: {exc}"})

    def emergency_dry_run(self) -> None:
        self._collect()
        threading.Thread(target=self._run_emergency_dry, daemon=True).start()

    def _run_emergency_dry(self) -> None:
        try:
            self.monitor.prepare(self.snapshot())
            rule = self.snapshot()["rules"][0]
            stopper = self.monitor.emergency
            reads = []
            gap = max(0.1, self._f(self.vars["emg_gap"].get(), 0.6))
            times = max(1, self._i(self.vars["emg_reads"].get(), 5))
            for i in range(times):
                reads.append(stopper._read_value(rule) if stopper else None)
                if i < times - 1:
                    time.sleep(gap)
            good = sum(1 for r in reads if r is not None)
            self.events.put({"type": "notify",
                             "message": f"演练：{times} 次识别，{good} 次读到数字 → "
                                        f"{'急停算成功' if good == 0 else '急停算未确认'}"})
        except Exception as exc:
            self.events.put({"type": "error", "message": f"演练失败: {exc}"})

    def release_emergency(self) -> None:
        try:
            if self.monitor.emergency:
                self.monitor.emergency.reset()
            self.events.put({"type": "notify", "message": "急停锁已解除"})
        except Exception as exc:
            self.events.put({"type": "error", "message": f"解除失败: {exc}"})

    def emergency_diagnose(self) -> None:
        self._collect()
        threading.Thread(target=self._run_emergency_diag, daemon=True).start()

    def _run_emergency_diag(self) -> None:
        try:
            from .wininput import (foreground_hwnd, is_elevated, self_elevated,
                                   window_pid, window_title)

            self.monitor.prepare(self.snapshot())
            hwnd = self.monitor.capture._hwnd()
            pid = window_pid(hwnd) if hwnd else 0
            game_admin = is_elevated(pid) if pid else None
            me_admin = self_elevated()
            lines = [
                f"目标窗口：{window_title(hwnd) if hwnd else '未找到（检查窗口标题/进程名）'}",
                f"当前前台：{window_title(foreground_hwnd()) or '（空）'}",
                f"游戏是否管理员：{game_admin}",
                f"本程序是否管理员：{me_admin}",
            ]
            if game_admin and not me_admin:
                lines.append("")
                lines.append("权限不匹配：游戏以管理员运行，本程序没有。")
                lines.append("Windows 会阻止切前台与按键注入，请以管理员身份重新打开本程序。")
            if not hwnd:
                lines.append("")
                lines.append("没找到游戏窗口：确认游戏在运行，且高级设置里的窗口标题/进程名正确。")
            text = "\n".join(lines)
            self.events.put({"type": "notify", "message": text.replace("\n", " | ")})
            self.root.after(0, lambda: messagebox.showinfo("急停诊断", text))
        except Exception as exc:
            self.events.put({"type": "error", "message": f"诊断失败: {exc}"})

    def emergency_run_now(self) -> None:
        if not messagebox.askyesno(
                "立即执行急停",
                "会真的把游戏切到前台并按 ESC，然后连续识别 5 次。\n确定现在执行？"):
            return
        self._collect()
        threading.Thread(target=self._run_emergency_now, daemon=True).start()

    def _run_emergency_now(self) -> None:
        try:
            self.monitor.prepare(self.snapshot())
            rule = self.snapshot()["rules"][0]
            stopper = self.monitor.emergency
            if stopper is None:
                self.events.put({"type": "error", "message": "急停组件未初始化"})
                return
            res = stopper.run_now(rule, rule.get("emergency") or {}, "手动测试")
            self.events.put({
                "type": "emergency", "success": res.success, "reason": res.reason,
                "message": (f"手动急停：{'成功' if res.success else '未确认'}"
                            f"（读数 {res.detail}）\n{res.diagnostics}"),
            })
        except Exception as exc:
            self.events.put({"type": "error", "message": f"手动急停失败: {exc}"})

    def _tab_snapshot(self, nb) -> None:
        v = self.vars
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  截图存档  ")
        ttk.Checkbutton(tab, text="启用截图存档（跌破阈值时自动截图）",
                        variable=v["snapshot_enabled"]).grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(tab, text="截图通道").grid(row=1, column=0, sticky="e", pady=(10, 0))
        ttk.Combobox(tab, textvariable=v["backend"], values=list(BACKENDS), width=16,
                     state="readonly").grid(row=1, column=1, sticky="w", padx=(4, 12), pady=(10, 0))
        ttk.Label(tab, text="保留天数").grid(row=1, column=2, sticky="e", pady=(10, 0))
        ttk.Entry(tab, textvariable=v["keep_days"], width=6).grid(row=1, column=3, sticky="w",
                                                                  padx=(4, 0), pady=(10, 0))

        ttk.Label(tab, text="窗口标题").grid(row=2, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(tab, textvariable=v["window_title"], width=30).grid(row=2, column=1, columnspan=2,
                                                                     sticky="w", padx=(4, 8), pady=(8, 0))
        ttk.Button(tab, text="读取前台窗口", command=self.grab_foreground).grid(row=2, column=3,
                                                                               sticky="w", pady=(8, 0))
        ttk.Label(tab, text="进程名").grid(row=3, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(tab, textvariable=v["process_name"], width=30).grid(row=3, column=1, columnspan=2,
                                                                     sticky="w", padx=(4, 8), pady=(8, 0))
        ttk.Button(tab, text="测试截图", command=self.test_snapshot).grid(row=3, column=3,
                                                                         sticky="w", pady=(8, 0))
        ttk.Button(tab, text="打开截图目录", command=self.open_snapshot_dir).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))

        ttk.Label(tab, text=(
            "截图通道选「游戏窗口」时通过 PrintWindow 取窗口画面，"
            "游戏被别的窗口挡住、或显示器自动关闭时通常仍能截到；\n"
            "如果某款游戏是独占全屏截到黑图，改选「自动」或「整屏」。"
            "文件名就是真实时间，报警截图带 _EX 后缀。"
        ), foreground="#666", wraplength=620, justify="left").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(14, 0))

    # ③ 通知
    def _tab_notify(self, nb) -> None:
        v = self.vars
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  钉钉通知  ")
        ttk.Label(tab, text="Webhook").grid(row=0, column=0, sticky="e")
        ttk.Entry(tab, textvariable=v["webhook"], width=62).grid(row=0, column=1, columnspan=3,
                                                                sticky="we", padx=(4, 0))
        ttk.Label(tab, text="加签密钥").grid(row=1, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(tab, textvariable=v["secret"], width=32, show="*").grid(
            row=1, column=1, sticky="w", padx=(4, 12), pady=(8, 0))
        ttk.Label(tab, text="关键词").grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Entry(tab, textvariable=v["keyword"], width=16).grid(row=1, column=3, sticky="w",
                                                                padx=(4, 0), pady=(8, 0))
        ttk.Label(tab, text="@手机号").grid(row=2, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(tab, textvariable=v["ats"], width=32).grid(row=2, column=1, sticky="w",
                                                            padx=(4, 12), pady=(8, 0))
        ttk.Button(tab, text="发送测试消息", command=self.test_dingtalk).grid(row=2, column=2,
                                                                             columnspan=2,
                                                                             sticky="w", pady=(8, 0))
        ttk.Label(tab, text=(
            "钉钉群里加机器人 → 选「自定义」→ 安全设置建议「加签」，把 SEC 开头的串填到加签密钥；\n"
            "若选「自定义关键词」，把关键词填进来即可（程序会自动把关键词补进消息）。"
        ), foreground="#666", wraplength=620, justify="left").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(14, 0))

    # ④ 运行
    def _tab_runtime(self, nb) -> None:
        v = self.vars
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  运行与日志  ")
        ttk.Checkbutton(tab, text="启动后自动开始监控", variable=v["autostart"]).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(tab, text="进程用低于正常优先级", variable=v["low_priority"]).grid(
            row=1, column=0, sticky="w")
        ttk.Checkbutton(tab, text="显示区域边框", variable=v["show_overlay"],
                        command=self._toggle_overlay).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(tab, text="记录采样日志（samples.log：时间 + 数值/fail）",
                        variable=v["sample_log"]).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(tab, text="最小化时显示悬浮窗（置顶/半透明/鼠标穿透，仅可见）",
                        variable=v["hud_enabled"]).grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(tab, text="位置").grid(row=5, column=0, sticky="e", pady=(6, 0))
        ttk.Combobox(tab, textvariable=v["hud_pos"], values=list(HudOverlay.PRESETS),
                     state="readonly", width=10).grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Label(tab, text="透明度").grid(row=5, column=2, sticky="e", pady=(6, 0))
        ttk.Entry(tab, textvariable=v["hud_opacity"], width=6).grid(row=5, column=3, sticky="w",
                                                                   pady=(6, 0))
        ttk.Button(tab, text="恢复默认配置", command=self.reset_config).grid(
            row=2, column=1, padx=8)
        ttk.Button(tab, text="导入配置", command=self.import_config).grid(row=0, column=1, padx=8)
        ttk.Button(tab, text="导出配置", command=self.export_config).grid(row=0, column=2)
        ttk.Button(tab, text="打开配置目录", command=self.open_config_dir).grid(row=1, column=1, padx=8)
        ttk.Button(tab, text="查看日志（独立窗口）", command=self.open_log_viewer).grid(
            row=1, column=2, padx=8)
        ttk.Button(tab, text="导出诊断包（发给作者）", command=self.export_diagnostics).grid(
            row=2, column=2, padx=8, pady=(6, 0))

        self.log_text = tk.Text(tab, height=14, wrap="word", state="disabled",
                                font=("Consolas", 9), background="#11161a", foreground="#cfe3dc")
        sb = ttk.Scrollbar(tab, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        sb.grid(row=3, column=3, sticky="ns", pady=(10, 0))
        tab.rowconfigure(3, weight=1)
        tab.columnconfigure(2, weight=1)

    def _bind_traces(self) -> None:
        for var in self.vars.values():
            var.trace_add("write", lambda *_: self._schedule_save())

    # ---------------- 配置读写 ----------------
    def _schedule_save(self, *_):
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(600, self._save)

    @staticmethod
    def _f(text: str, default: float) -> float:
        try:
            return float(str(text).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _i(text: str, default: int) -> int:
        try:
            return int(float(str(text).strip()))
        except (TypeError, ValueError):
            return default

    def _collect(self) -> None:
        v = self.vars
        with self._lock:
            rule = self._rule()
            mode = REGION_MODES.get(v["region_mode"].get(), "ratio")
            if mode == "ratio":
                rule["region"] = {
                    "mode": "ratio",
                    "x": max(0.0, self._f(v["x"].get(), 0.0)),
                    "y": max(0.0, self._f(v["y"].get(), 0.0)),
                    "w": min(1.0, max(0.005, self._f(v["w"].get(), 0.3))),
                    "h": min(1.0, max(0.005, self._f(v["h"].get(), 0.3))),
                }
            else:
                rule["region"] = {
                    "mode": "absolute",
                    "x": self._i(v["x"].get(), rule["region"].get("x", 0)),
                    "y": self._i(v["y"].get(), rule["region"].get("y", 0)),
                    "w": max(8, self._i(v["w"].get(), rule["region"].get("w", 200))),
                    "h": max(8, self._i(v["h"].get(), rule["region"].get("h", 60))),
                }
            num = rule.setdefault("number", {})
            num["mode"] = DIRECTIONS.get(v["mode"].get(), "first")
            num["require_percent"] = bool(v["require_percent"].get())
            rng_text = v["range"].get().strip().replace("~", "-").replace(",", "-")
            if "-" in rng_text:
                lo_s, _, hi_s = rng_text.partition("-")
                lo, hi = self._f(lo_s, 0.0), self._f(hi_s, 100.0)
                num["valid_range"] = [min(lo, hi), max(lo, hi)]
            else:
                num.pop("valid_range", None)

            rule.setdefault("warn", {})["value"] = self._f(v["warn"].get(), 50.0)
            alert = rule.setdefault("alert", {})
            alert["value"] = self._f(v["alert"].get(), 35.0)
            alert["cooldown_sec"] = max(0, self._i(v["cooldown"].get(), 60))
            alert["realert_sec"] = max(0, self._i(v["realert"].get(), 1800))
            alert["notify"] = bool(v["notify_enabled"].get())
            alert["log"] = bool(v["log_enabled"].get())
            rule.setdefault("warn", {})["log"] = bool(v["log_enabled"].get())
            rule.setdefault("recover", {})["notify"] = bool(v["recover_notify"].get())

            emg = rule.setdefault("emergency", {})
            emg["enabled"] = bool(v["emg_enabled"].get())
            emg["triggers"] = {"zero_percent": bool(v["emg_zero"].get()),
                               "elapsed_minutes": bool(v["emg_time"].get())}
            emg["elapsed_minutes"] = max(0.1, self._f(v["emg_minutes"].get(), 10.0))
            emg["verify_reads"] = max(1, self._i(v["emg_reads"].get(), 5))
            emg["verify_interval_sec"] = max(0.1, self._f(v["emg_gap"].get(), 0.6))
            emg["time_region"] = self._time_region()
            emg["auto_rearm"] = bool(v["emg_auto_rearm"].get())
            emg["click_before_key"] = bool(v["emg_click"].get())
            emg["click_ratio"] = [
                min(1.0, max(0.0, self._f(v["emg_click_x"].get(), 0.5))),
                min(1.0, max(0.0, self._f(v["emg_click_y"].get(), 0.5))),
            ]

            sample = self.cfg.setdefault("sample", {})
            sample["interval_sec"] = max(0.5, self._f(v["interval"].get(), 1.5))
            sample["retry_count"] = max(0, self._i(v["retry_count"].get(), 2))
            sample["retry_gap_sec"] = max(0.0, self._f(v["retry_gap"].get(), 0.35))
            sample["log_enabled"] = bool(v["sample_log"].get())

            guard = self.cfg.setdefault("guard", {})
            guard["max_drop_per_1_5s"] = self._f(v["max_drop"].get(), 1.0)
            guard["drop_tolerance"] = self._f(v["drop_tol"].get(), 0.5)
            guard["min_confidence"] = max(0.0, min(1.0, self._f(v["min_conf"].get(), 0.45)))

            snap = self.cfg.setdefault("snapshot", {})
            snap["enabled"] = bool(v["snapshot_enabled"].get())
            snap["backend"] = BACKENDS.get(v["backend"].get(), "window")
            snap["window_title"] = v["window_title"].get().strip()
            snap["process_name"] = v["process_name"].get().strip()
            snap["keep_days"] = max(0, self._i(v["keep_days"].get(), 30))

            ding = self.cfg.setdefault("dingtalk", {})
            ding["webhook"] = v["webhook"].get().strip()
            ding["secret"] = v["secret"].get().strip()
            ding["keyword"] = v["keyword"].get().strip()
            ding["at_mobiles"] = [m.strip() for m in v["ats"].get().split(",") if m.strip()]

            app = self.cfg.setdefault("app", {})
            app["start_monitoring"] = bool(v["autostart"].get())
            app["low_priority"] = bool(v["low_priority"].get())
            app["show_overlay"] = bool(v["show_overlay"].get())
            app["hud"] = {
                "enabled": bool(v["hud_enabled"].get()),
                "position": v["hud_pos"].get(),
                "opacity": min(1.0, max(0.2, self._f(v["hud_opacity"].get(), 0.78))),
                "font_size": max(12, min(60, self._i(v["hud_font"].get(), 20))),
            }

    def _save(self) -> None:
        self._save_job = None
        try:
            self._collect()
            errors = cfgmod.validate_rule(self._rule())
            if errors:
                self.status_var.set("配置有问题：" + "；".join(errors))
            with self._lock:
                cfgmod.save(self.cfg, self.cfg_path)
        except Exception as exc:
            self.log.error("保存配置失败: %s", exc)

    # ---------------- 区域 ----------------
    def pick(self) -> None:
        self.root.withdraw()
        self.root.update_idletasks()
        try:
            region = pick_region(self.root, self.snapshot()["rules"][0].get("region"))
        finally:
            self.root.deiconify()
            self.root.lift()
        if not region:
            return
        mode = REGION_MODES.get(self.vars["region_mode"].get(), "ratio")
        if mode == "ratio":
            region = self._to_ratio(region)
        self._apply_region(region)
        self._save()
        self.log.info("已框选区域 %s", region)
        self.detect_once()

    def _to_ratio(self, region: dict) -> dict:
        """把屏幕像素选中的区域换算成相对游戏窗口客户区的比例。"""
        from .capture import find_window, virtual_screen_rect, window_rects

        snap = self.snapshot().get("snapshot") or {}
        hwnd = find_window(snap.get("window_title", ""), snap.get("process_name", ""))
        base = window_rects(hwnd)[1] if hwnd else virtual_screen_rect()
        bx, by, bw, bh = base
        if bw <= 0 or bh <= 0:
            return region
        return {
            "mode": "ratio",
            "x": round(max(0.0, (region["x"] - bx) / bw), 4),
            "y": round(max(0.0, (region["y"] - by) / bh), 4),
            "w": round(min(1.0, region["w"] / bw), 4),
            "h": round(min(1.0, region["h"] / bh), 4),
        }

    def _apply_region(self, region: dict) -> None:
        mode = str(region.get("mode") or "ratio")
        self.vars["region_mode"].set(REGION_MODES_REV.get(mode, "比例（相对游戏窗口）"))
        fmt = (lambda x: f"{float(x):.3f}") if mode == "ratio" else (lambda x: str(int(float(x))))
        for key in ("x", "y", "w", "h"):
            self.vars[key].set(fmt(region.get(key, 0)))
        if self.overlay and self.monitor.capture is not None:
            try:
                self.overlay.show(self.monitor.capture.region_pixels(region))
            except Exception:
                self.overlay.show(region)

    def calibrate(self) -> None:
        """在现有区域里找到那行百分比，把区域收紧到文字周围。"""
        self._collect()
        rule = copy.deepcopy(self.snapshot()["rules"][0])
        self.status_var.set("校准中…")
        threading.Thread(target=self._run_calibrate, args=(rule,), daemon=True).start()

    def _run_calibrate(self, rule: dict) -> None:
        try:
            region = self.monitor.calibrate(rule)
        except Exception as exc:
            self.log.error("自动校准失败: %s", exc)
            self.events.put({"type": "error", "message": f"校准失败: {exc}"})
            return
        if not region:
            self.events.put({"type": "error",
                             "message": "校准没找到带 % 的文字，先手动框一个大概范围再试"})
            return
        self.events.put({"type": "calibrated", "region": region})

    def _on_overlay_change(self, region: dict) -> None:
        mode = REGION_MODES.get(self.vars["region_mode"].get(), "ratio")
        if mode == "ratio":
            region = self._to_ratio(region)
        self._apply_region(region)
        self._save()

    def _show_overlay(self) -> None:
        if self.overlay is None:
            return
        region = self.snapshot()["rules"][0].get("region") or {}
        if self.monitor.capture is not None:
            try:
                region = self.monitor.capture.region_pixels(region)
            except Exception:
                pass
        self.overlay.show(region)

    def _hide_overlay(self) -> None:
        if self.overlay:
            self.overlay.hide()

    def _follow_region(self) -> None:
        """窗口被拖动/改大小时，让绿框跟着走（不依赖是否在监控）。"""
        if self._closing:
            return
        try:
            if self.overlay is not None and self.overlay.visible() and self.monitor.capture:
                region = self.snapshot()["rules"][0].get("region") or {}
                self.overlay.update_region(self.monitor.capture.region_pixels(region))
        except Exception:
            self.log.debug("跟随区域失败", exc_info=True)
        self.root.after(1200, self._follow_region)

    def _toggle_overlay(self) -> None:
        if self.vars["show_overlay"].get():
            self._show_overlay()
        else:
            self._hide_overlay()

    # ---------------- 识别 ----------------
    def detect_once(self) -> None:
        self._collect()
        rule = copy.deepcopy(self.snapshot()["rules"][0])
        self.status_var.set("识别中…")
        threading.Thread(target=self._run_detect, args=(rule,), daemon=True).start()

    def _run_detect(self, rule: dict) -> None:
        self.monitor.run_now(rule)

    # ---------------- 截图 ----------------
    def grab_foreground(self) -> None:
        title, name = foreground_window_info()
        if title:
            self.vars["window_title"].set(title)
        if name:
            self.vars["process_name"].set(name)
        self.log.info("读取前台窗口：%s / %s", title, name)
        self._save()

    def test_snapshot(self) -> None:
        self._collect()
        threading.Thread(target=self._run_test_snapshot, daemon=True).start()

    def _run_test_snapshot(self) -> None:
        try:
            self.monitor.prepare(self.snapshot())
            path = self.monitor.recorder.snapshot(self._rule(), "_TEST", reason="手动测试截图")
            self.events.put({"type": "notify", "message": f"测试截图已保存：{path}" if path
                             else "截图失败，详见日志"})
        except Exception as exc:
            self.log.error("测试截图失败: %s", exc)
            self.events.put({"type": "error", "message": f"截图失败: {exc}"})

    @staticmethod
    @staticmethod
    def _shell_execute(path: Path, verb: str = "open") -> int:
        """直接调 ShellExecuteW 并返回真实结果：>32 表示成功，否则是错误码。

        用 Popen 起 explorer.exe 是"假成功"——进程起来了但窗口可能被吞掉，
        以 ShellExecuteW 返回码判断结果，失败时回显错误码。
        """
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        try:
            result = shell32.ShellExecuteW(None, verb, str(path), None, str(path.parent), 1)
        except Exception:
            return 0
        return int(result or 0)

    def _open_in_explorer(path: Path, select: bool = False) -> None:
        """打开文件夹/定位文件；失败时抛出带真实错误码的异常。"""
        import subprocess

        path = path.resolve()
        errors: list[str] = []

        code = AppUI._shell_execute(path)
        if code > 32:
            return
        errors.append(f"ShellExecuteW 返回 {code}" + ("（常见：5=拒绝访问）" if code == 5 else ""))

        if select:
            args = ["explorer.exe", "/select,", str(path)]
        else:
            args = ["explorer.exe", str(path)]
        try:
            subprocess.Popen(args)
        except OSError as exc:
            errors.append(f"explorer: {exc}")
            raise OSError("；".join(errors)) from exc
        # explorer 是"假成功"：起来了不等于窗口出来，只能告知已尝试
        raise OSError("；".join(errors) + "；explorer.exe 已启动但系统未弹出窗口")

    def _snapshot_root(self) -> Path:
        from . import config as cfgmod

        sub = str((self.snapshot().get("snapshot") or {}).get("dir", "snapshots"))
        return (cfgmod.data_dir() / sub).resolve()

    def open_snapshot_dir(self) -> None:
        """软件内打开截图列表；不依赖系统外壳（ShellExecute 被拦时也能用）。"""
        self._snapshot_browser = SnapshotBrowser(self, self._snapshot_root())

    def reset_config(self) -> None:
        """恢复出厂默认设置（不动截图和日志文件）。"""
        if not messagebox.askyesno(
            "恢复默认配置",
            "会把所有设置恢复为默认值（阈值 50/35、区域、钉钉地址会清空）。\n"
            "已有的截图和日志文件不会删除。\n\n确定继续？",
        ):
            return
        self.cfg = copy.deepcopy(cfgmod.DEFAULT_CONFIG)
        self.cfg["app"]["wizard_done"] = True   # 别再弹向导，用户自己会点「设置向导」
        self._reload_vars()
        self._save()
        self._show_overlay()
        self.status_var.set("已恢复默认配置")
        self.last_event_var.set("已恢复默认配置，记得重新填钉钉地址、再点一次自动校准。")
        self.log.info("配置已恢复默认")

    def export_diagnostics(self) -> None:
        """把日志、脱敏配置、系统信息打包，方便发作者排查（完全本地）。"""
        try:
            from . import diagnostics

            path = diagnostics.export("用户手动导出")
        except Exception as exc:
            self.log.error("导出诊断包失败", exc_info=True)
            messagebox.showerror("导出失败", f"生成诊断包时出错：\n{exc}")
            return
        self.events.put({"type": "notify", "message": f"诊断包已保存：{path}"})
        messagebox.showinfo(
            "诊断包已生成",
            "已保存到：\n%s\n\n把该文件发给作者（QQ 494735622）即可。\n"
            "包内 Webhook/密钥已打码，不含任何上传。" % path)

    def _on_tk_callback_error(self, exc_type, exc, tb) -> None:
        """界面回调里的异常：写日志，并自动存一次诊断包（每个进程只导一次）。"""
        import traceback

        self.log.error("界面回调异常：\n%s",
                       "".join(traceback.format_exception(exc_type, exc, tb)))
        if getattr(self, "_tk_error_dumped", False):
            return
        self._tk_error_dumped = True
        try:
            from . import diagnostics

            path = diagnostics.export("界面回调异常")
            self.events.put({"type": "error", "message": f"界面异常，诊断包：{path}"})
        except Exception:
            pass

    def open_log_viewer(self) -> None:
        from . import config as cfgmod

        self._log_viewer = LogViewer(self, cfgmod.log_dir() / "app.log")

    # ---------------- 钉钉 ----------------
    def test_dingtalk(self) -> None:
        self._collect()
        if not self.snapshot()["dingtalk"].get("webhook"):
            messagebox.showwarning("缺少配置", "请先填写钉钉机器人的 Webhook 地址。")
            return
        threading.Thread(target=self._run_test_ding, daemon=True).start()

    def _run_test_ding(self) -> None:
        try:
            msg = DingTalk(self.snapshot()["dingtalk"]).test()
            self.events.put({"type": "notify", "message": f"钉钉测试消息{msg}"})
        except DingTalkError as exc:
            self.events.put({"type": "error", "message": f"钉钉发送失败: {exc}"})

    # ---------------- 启停 ----------------
    def _set_running_ui(self, running: bool) -> None:
        for b1, b2 in self._run_buttons:
            try:
                b1.configure(state="disabled" if running else "normal")
                b2.configure(state="normal" if running else "disabled")
            except tk.TclError:
                pass

    def start_monitor(self) -> None:
        self._collect()
        self._save()
        errors = cfgmod.validate_rule(self._rule())
        if errors:
            messagebox.showerror("配置有问题", "\n".join(errors))
            return
        if self.monitor.is_alive():
            self.monitor.stop(join=True)
            self.monitor = MonitorService(self.snapshot, self._on_event, self.log,
                                          states=self.states)
        self._cancel_rearm_watch()
        self._check_elevation_once()
        self.monitor = MonitorService(self.snapshot, self._on_event, self.log,
                                      states=self.states)
        self.monitor.start()
        self._set_running_ui(True)
        self.status_var.set("监控中")
        self._set_light("● 监控中", "#2f8f6a")
        self.log.info("开始监控，采样间隔 %ss，预警线 %s / 报警线 %s",
                      self.cfg["sample"]["interval_sec"],
                      self._rule()["warn"]["value"], self._rule()["alert"]["value"])
        if self.cfg["app"].get("show_overlay", True):
            self._show_overlay()

    def _check_elevation_once(self) -> None:
        """游戏以管理员运行时，非管理员进程无法注入按键：启动监控前提示一次。"""
        if getattr(self, "_elevation_checked", False):
            return
        self._elevation_checked = True
        emg = self._rule().get("emergency") or {}
        if not emg.get("enabled"):
            return
        try:
            from .capture import find_window
            from .wininput import is_elevated, self_elevated, window_pid

            snap = self.cfg.get("snapshot") or {}
            hwnd = find_window(snap.get("window_title", ""), snap.get("process_name", ""))
            if not hwnd:
                return
            pid = window_pid(hwnd)
            if pid and is_elevated(pid) and not self_elevated():
                messagebox.showwarning(
                    "权限不一致，急停可能无效",
                    "游戏正在以管理员身份运行，而本程序不是。\n"
                    "这种情况下程序无法向游戏发送点击和按键，急停会失败。\n\n"
                    "解决方式：关闭本程序，右键以「管理员身份运行」后重新开始监控。")
        except Exception:
            self.log.debug("权限检查失败", exc_info=True)

    def stop_monitor(self) -> None:
        self._cancel_rearm_watch()
        if self.monitor.is_alive():
            self.monitor.stop(join=True)
            self.monitor.stop()
            self.log.info("已请求停止监控")
        self._set_running_ui(False)
        self.status_var.set("已停止")
        self._set_light("● 已停止", "#777")

    # ---------------- 急停后等待下一局 ----------------
    def _cancel_rearm_watch(self) -> None:
        self._await_new_round = False
        if self._rearm_job is not None:
            try:
                self.root.after_cancel(self._rearm_job)
            except Exception:
                pass
            self._rearm_job = None

    def _start_rearm_watch(self) -> None:
        emg = self._rule().get("emergency") or {}
        if not emg.get("auto_rearm", True):
            return
        self._cancel_rearm_watch()
        self._await_new_round = True
        self._rearm_fp = None
        self.log.info("急停完成，等待下一局（计时器画面变化后自动恢复监控）")
        self._rearm_job = self.root.after(4000, self._watch_new_round)

    def _watch_new_round(self) -> None:
        self._rearm_job = None
        if not self._await_new_round or self._closing:
            return
        try:
            from .capture import ScreenCapture
            from .emergency import looks_like_new_round

            stopper = self.monitor.emergency
            if stopper is None:
                self._cancel_rearm_watch()
                return
            prev = getattr(stopper, "_last_time", None)
            emg_cfg = self._rule().get("emergency") or {}
            region = emg_cfg.get("time_region") or {}
            if not region:
                self._cancel_rearm_watch()
                return

            # 先比画面指纹：计时器停住时画面不变，直接跳过 OCR（几乎零开销）
            img, _src = self.monitor.capture.grab_region_auto(region)
            fingerprint = ScreenCapture.fingerprint(img)
            changed = ScreenCapture.diff(self._rearm_fp, fingerprint) > 1.5
            self._rearm_fp = fingerprint
            if not changed:
                if self._await_new_round:
                    self._rearm_job = self.root.after(4000, self._watch_new_round)
                return

            seconds, _text = stopper.read_time(self._rule(), emg_cfg)
            if looks_like_new_round(prev, seconds):
                self.log.info("检测到新一局，恢复监控")
                for st in self.states.values():
                    st.zone = "unknown"
                    st.value = None
                    st.pending_value = None
                stopper.reset()
                self._cancel_rearm_watch()
                self.start_monitor()
                self.last_event_var.set("检测到新一局，已恢复监控")
                return
        except Exception:
            self.log.debug("等待新一局检查失败", exc_info=True)
        if self._await_new_round:
            self._rearm_job = self.root.after(4000, self._watch_new_round)

    # ---------------- 事件泵 ----------------
    def _on_event(self, evt: dict) -> None:
        self.events.put(evt)

    def _pump(self) -> None:
        if self._closing:
            return
        for _ in range(50):
            try:
                self._handle_event(self.events.get_nowait())
            except queue.Empty:
                break
        for _ in range(200):
            try:
                self._append_log(self.log_queue.get_nowait())
            except queue.Empty:
                break
        try:
            while True:
                self._handle_command(self.commands.get_nowait())
        except queue.Empty:
            pass
        self._sync_hud()
        self.root.after(250, self._pump)

    def _sync_hud(self) -> None:
        """主窗口最小化且正在监控时，才显示悬浮窗。"""
        cfg = (self.snapshot().get("app") or {}).get("hud") or {}
        enabled = bool(cfg.get("enabled", True))
        try:
            minimized = self.root.state() == "iconic"
        except tk.TclError:
            minimized = False
        if not (enabled and self.monitor.is_alive() and minimized):
            if self._hud is not None:
                self._hud.hide()
            return
        sig = (str(cfg.get("position")), float(cfg.get("opacity", 0.78)),
               int(cfg.get("font_size", 20)))
        if self._hud is None or self._hud_sig != sig:
            if self._hud is not None:
                self._hud.destroy()
            self._hud = HudOverlay(self)
            self._hud_sig = sig
            self._hud_last = None
        current = (self._last_value_text, self._last_time_text, self._last_alert)
        if getattr(self, "_hud_last", None) != current:
            self._hud.set_values(self._last_value_text, self._last_time_text, self._last_alert)
            self._hud_last = current
        self._hud.show()

    def _display_value(self, value) -> str:
        if value is None:
            return "--"
        num = self._rule().get("number") or {}
        text = format_value(value)
        return f"{text}%" if num.get("require_percent") else text

    def _set_light(self, text: str, color: str) -> None:
        self.light_var.set(text)
        try:
            self.light_label.configure(fg=color)
            if hasattr(self, "big_label"):
                self.big_label.configure(fg=color)
        except (tk.TclError, AttributeError):
            pass

    @staticmethod
    def _friendly(message: str) -> str:
        """把技术错误翻成人话。"""
        text = message or ""
        table = [
            ("未找到游戏窗口", "未找到游戏窗口：请确认游戏正在运行，并在「高级设置 → 截图存档」中填写正确的窗口标题或进程名。"),
            ("截图失败", "无法截取游戏画面：若游戏为独占全屏模式，请在「高级设置 → 截图存档」将截图通道改为「整屏」。"),
            ("独占全屏", "当前游戏为独占全屏模式，无法截取画面：请改为无边框窗口，或将截图通道设为「整屏」。"),
            ("画面全黑", "画面全黑：显示器可能已关闭或游戏窗口被遮挡，程序会等待画面恢复后继续。"),
            ("未配置钉钉", "尚未配置钉钉机器人：请在「设置向导」第 4 步填写 Webhook 地址。"),
            ("连续", "连续多次未识别到数值：请确认游戏未处于菜单或加载画面。"),
        ]
        for key, human in table:
            if key in text:
                return human
        return text

    def _handle_event(self, evt: dict) -> None:
        kind = evt.get("type")
        if kind == "sample":
            value = evt.get("value")
            zone = evt.get("zone") or ""
            self.big_var.set(self._display_value(value))
            self._last_value_text = self._display_value(value)
            self._last_alert = zone == "alert"
            if value is None:
                self._set_light("● 这次没读到数字", "#b26a00")
                self.small_var.set(f"原因：{evt.get('text', '')}（会自动重试）")
            else:
                label = {"alert": ("● 低于报警线", "#c62828"),
                         "warn": ("● 低于预警线", "#b26a00"),
                         "normal": ("● 正常", "#2f8f6a")}.get(zone, ("● 正常", "#2f8f6a"))
                self._set_light(*label)
                text = (evt.get("text") or "").replace("\n", " ")
                self.small_var.set(f"识别到：{text}（置信度 {evt.get('score', 0):.2f}）")
            if hasattr(self, "result_var"):
                self.result_var.set(self.small_var.get())
            self._sync_overlay(value, zone)
        elif kind == "region":
            if self.overlay and self.overlay.visible():
                self.overlay.update_region(evt.get("rect") or {})
        elif kind == "calibrated":
            region = evt.get("region") or {}
            self._apply_region(region)
            self._save()
            self.status_var.set("校准完成")
            self.log.info("自动校准完成：%s", region)
            if self._wizard_after_region:
                cb, self._wizard_after_region = self._wizard_after_region, None
                self.root.after(300, cb)
            self.detect_once()
        elif kind == "notify":
            first = (evt.get("message") or "").splitlines()[0]
            self.last_event_var.set(f"最近：{first}")
        elif kind == "emergency":
            msg = evt.get("message", "")
            self.last_event_var.set(msg)
            self._set_light("● 急停" if evt.get("success") else "● 急停未确认",
                            "#c62828" if evt.get("success") else "#b26a00")
            if evt.get("success"):
                self._last_value_text = "已急停"
                self._start_rearm_watch()
        elif kind == "stopped":
            self._set_running_ui(False)
            self.status_var.set("已停止（急停）")
            self._set_light("● 已停止", "#777")
            self.last_event_var.set(evt.get("message", "已停止监控"))
        elif kind == "time":
            from .numbers import format_clock

            self.time_var.set(f"时间 {format_clock(evt.get('seconds'))}")
            self._last_time_text = format_clock(evt.get("seconds"))
        elif kind == "emergency_stage":
            self.last_event_var.set(f"急停：{evt.get('message', '')}")
            if evt.get("running"):
                self._set_light("● 急停执行中…", "#c62828")
        elif kind == "error":
            msg = self._friendly(evt.get("message", ""))
            self.last_event_var.set(msg)
            if "时间区域" in (evt.get("message") or ""):
                self.time_var.set("时间 读取失败")

    def _sync_overlay(self, value, zone: str) -> None:
        if self.overlay and self.overlay.visible():
            label = format_value(value) if value is not None else "--"
            self.overlay.set_status(label, alert=(zone == "alert"))

    def _handle_command(self, cmd: str) -> None:
        if cmd == "show":
            self.root.deiconify()
            self.root.lift()
        elif cmd == "start":
            self.start_monitor()
        elif cmd == "stop":
            self.stop_monitor()
        elif cmd == "quit":
            self._quit()

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "120.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------- 杂项 ----------------
    def _start_tray(self) -> None:
        try:
            from .tray import Tray

            self.tray = Tray(self.commands)
            self.tray.start()
        except Exception as exc:
            self.log.warning("托盘图标不可用（不影响主功能）: %s", exc)
            self.tray = None

    def import_config(self) -> None:
        path = filedialog.askopenfilename(title="选择配置文件", filetypes=[("JSON", "*.json")])
        if not path:
            return
        cfg = cfgmod.load(Path(path))
        self.cfg = cfg
        self._reload_vars()
        self._save()
        self._show_overlay()
        self.log.info("已导入配置: %s", path)

    def export_config(self) -> None:
        self._collect()
        path = filedialog.asksaveasfilename(
            title="导出配置", defaultextension=".json",
            initialfile="screen_ocr_alert_config.json", filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            cfgmod.save(self.snapshot(), Path(path))
            messagebox.showinfo("已导出", f"配置已保存到：\n{path}\n\n"
                                          "对方导入后填自己的钉钉地址即可。")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def open_config_dir(self) -> None:
        try:
            os.startfile(str(self.cfg_path.parent))
        except Exception as exc:
            messagebox.showerror("打不开目录", str(exc))

    def _reload_vars(self) -> None:
        rule = self._rule()
        v = self.vars
        self._apply_region(rule["region"])
        v["warn"].set(format_value(rule["warn"]["value"]))
        v["alert"].set(format_value(rule["alert"]["value"]))
        v["cooldown"].set(str((rule.get("alert") or {}).get("cooldown_sec", 60)))
        v["realert"].set(str((rule.get("alert") or {}).get("realert_sec", 1800)))
        v["webhook"].set(self.cfg["dingtalk"].get("webhook", ""))
        v["secret"].set(self.cfg["dingtalk"].get("secret", ""))
        v["keyword"].set(self.cfg["dingtalk"].get("keyword", ""))
        v["window_title"].set((self.cfg.get("snapshot") or {}).get("window_title", ""))
        v["process_name"].set((self.cfg.get("snapshot") or {}).get("process_name", ""))
        v["interval"].set(str((self.cfg.get("sample") or {}).get("interval_sec", 1.5)))
        emg = rule.get("emergency") or {}
        trig = emg.get("triggers") or {}
        v["emg_enabled"].set(bool(emg.get("enabled", False)))
        v["emg_zero"].set(bool(trig.get("zero_percent", True)))
        v["emg_time"].set(bool(trig.get("elapsed_minutes", False)))
        v["emg_minutes"].set(format_value(emg.get("elapsed_minutes", 10)))
        v["emg_reads"].set(str(emg.get("verify_reads", 5)))
        v["emg_auto_rearm"].set(bool(emg.get("auto_rearm", True)))
        v["emg_gap"].set(str(emg.get("verify_interval_sec", 0.6)))
        v["emg_click"].set(bool(emg.get("click_before_key", True)))
        click_ratio = emg.get("click_ratio") or [0.5, 0.5]
        v["emg_click_x"].set(f"{float(click_ratio[0]):.2f}")
        v["emg_click_y"].set(f"{float(click_ratio[1]):.2f}")
        v["notify_enabled"].set(bool((rule.get("alert") or {}).get("notify", True)))
        v["log_enabled"].set(bool((rule.get("alert") or {}).get("log", True)))
        v["sample_log"].set(bool((self.cfg.get("sample") or {}).get("log_enabled", True)))
        hud_cfg = (self.cfg.get("app") or {}).get("hud") or {}
        v["hud_enabled"].set(bool(hud_cfg.get("enabled", True)))
        v["hud_pos"].set(str(hud_cfg.get("position", "顶部居中")))
        v["hud_opacity"].set(str(hud_cfg.get("opacity", 0.78)))
        v["hud_font"].set(str(hud_cfg.get("font_size", 20)))

    # ---------------- 向导 ----------------
    def open_wizard(self, step: int = 0) -> None:
        if self._wizard is not None:
            try:
                self._wizard.top.deiconify()
                self._wizard.top.lift()
                self._wizard.top.notebook.select(step)
                return
            except tk.TclError:
                self._wizard = None
        self._wizard = SetupWizard(self, step)

    def pick_and_calibrate(self, after=None) -> None:
        """向导里用：先框选，再自动收紧。"""
        self._wizard_after_region = after
        self.root.after(120, self._do_pick_calibrate)

    def _do_pick_calibrate(self) -> None:
        self.pick()
        try:
            width = float(self.vars["w"].get())
        except (TypeError, ValueError):
            width = 0
        if width <= 0:
            self._wizard_after_region = None
            return
        self.calibrate()

    def finish_wizard(self) -> None:
        self._collect()
        self.cfg["app"]["wizard_done"] = True
        self._save()
        self._wizard = None
        self.start_monitor()
        self.status_var.set("已开始监控")
        self._show_simple()
        self.last_event_var.set("已经开始了。窗口最小化也没关系，出问题会自动提醒。")

    def _bring_to_front(self) -> None:
        """启动时把窗口提到最前并居中，避免用户以为"没打开"（窗口其实在游戏后面）。"""
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width() or 620
            h = self.root.winfo_height() or 660
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.after(900, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self._cancel_rearm_watch()
        except Exception:
            pass
        try:
            self._collect()
            cfgmod.save(self.cfg, self.cfg_path)
        except Exception:
            pass
        try:
            if self.monitor.is_alive():
                self.monitor.stop()
                self.monitor.join(timeout=3)
        except Exception:
            pass
        if self.overlay:
            self.overlay.destroy()
        if self._hud is not None:
            self._hud.destroy()
            self._hud = None
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        if self.cfg["app"].get("low_priority"):
            from .singleton import lower_priority

            self.root.after(300, lambda: lower_priority(self.log))
            self.root.after(400, lambda: self.status_var.set(
                "监控中" if self.monitor.is_alive() else "待机中"))
        self.root.mainloop()

class SetupWizard:
    """四步向导：选程序 -> 框区域(自动校准) -> 填阈值 -> 填钉钉。

    面向"不想看配置项"的人：每一步只问一件事，填完直接开始监控。
    """

    STEPS = ("① 选程序", "② 框区域", "③ 设阈值", "④ 填钉钉")

    def __init__(self, app: "AppUI", step: int = 0) -> None:
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title("设置向导 · 四步")
        self.top.geometry("660x470")
        self.top.minsize(620, 430)
        self.top.transient(app.root)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self._win_items: list[tuple[str, str]] = []

        outer = ttk.Frame(self.top, padding=14)
        outer.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self._tab_window()
        self._tab_region()
        self._tab_threshold()
        self._tab_dingtalk()

        nav = ttk.Frame(outer)
        nav.pack(fill="x", pady=(10, 0))
        ttk.Button(nav, text="上一步", command=self.prev).pack(side="left")
        ttk.Button(nav, text="先关闭", command=self.close).pack(side="right", padx=(6, 0))
        self.done_btn = ttk.Button(nav, text="完成，开始监控", command=self.finish)
        self.done_btn.pack(side="right", padx=6)
        self.next_btn = ttk.Button(nav, text="下一步", command=self.next)
        self.next_btn.pack(side="right")

        self.notebook.select(max(0, min(step, len(self.STEPS) - 1)))
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._update_nav())
        self._update_nav()

    # ---------- ① 选程序 ----------
    def _tab_window(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(tab, text=self.STEPS[0])
        ttk.Label(tab, text="要盯的是哪个程序的窗口？",
                  font=("Microsoft YaHei UI", 12)).pack(anchor="w")
        ttk.Label(tab, text="下面列出当前开着的窗口。选游戏那一行就行（一般已经帮你选好 Warframe）。",
                  foreground="#666", wraplength=580, justify="left").pack(anchor="w", pady=(4, 10))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.win_var = tk.StringVar()
        self.combo = ttk.Combobox(row, textvariable=self.win_var, state="readonly", width=52)
        self.combo.pack(side="left")
        ttk.Button(row, text="刷新", command=self.refresh_windows).pack(side="left", padx=6)
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self._on_window())
        ttk.Label(tab, text="没看到游戏？先把游戏窗口切到前台，再点「刷新」。",
                  foreground="#999", wraplength=580).pack(anchor="w", pady=(10, 0))
        self.refresh_windows()

    def refresh_windows(self) -> None:
        from .capture import list_windows

        self._win_items = list_windows()
        values = [f"{t}（{p or '未知进程'}）" for t, p in self._win_items]
        self.combo["values"] = values
        cfg = self.app.snapshot().get("snapshot") or {}
        want_title = (cfg.get("window_title") or "").lower()
        want_proc = (cfg.get("process_name") or "").lower()
        pick = None
        for i, (title, proc) in enumerate(self._win_items):
            if want_proc and want_proc == proc.lower():
                pick = i
                break
            if want_title and title.lower() == want_title:
                pick = i
                break
        if pick is None:
            for i, (_title, proc) in enumerate(self._win_items):
                if "warframe" in proc.lower():
                    pick = i
                    break
        # 找不到匹配项时宁可不选：随便选第一个窗口会导致"区域不跟游戏走"
        if pick is not None:
            self.combo.current(pick)
            self._on_window()
        else:
            self.combo.set("")

    def _on_window(self) -> None:
        idx = self.combo.current()
        if idx < 0 or idx >= len(self._win_items):
            return
        title, proc = self._win_items[idx]
        self.app.vars["window_title"].set(title)
        self.app.vars["process_name"].set(proc)
        self.app._save()

    # ---------- ② 框区域 ----------
    def _tab_region(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(tab, text=self.STEPS[1])
        ttk.Label(tab, text="把那个百分比数字框进去",
                  font=("Microsoft YaHei UI", 12)).pack(anchor="w")
        ttk.Label(tab, text="点下面的按钮，屏幕会变暗；按住鼠标左键拖一个框，框住「86%」这种数字。\n"
                           "松手后程序会自动收紧到刚好包住文字，并且不用管分辨率。",
                  foreground="#666", justify="left").pack(anchor="w", pady=(4, 12))
        ttk.Button(tab, text="框选并自动校准", command=self.pick_region).pack(anchor="w")
        self.region_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.region_var, foreground="#2f8f6a",
                  wraplength=580, justify="left").pack(anchor="w", pady=(14, 0))
        self._refresh_region_text()

    def pick_region(self) -> None:
        self.top.withdraw()
        self.app.pick_and_calibrate(after=self._back_from_picking)

    def _back_from_picking(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.notebook.select(1)
        self._refresh_region_text()

    def _refresh_region_text(self) -> None:
        rule = self.app.snapshot()["rules"][0]
        region = rule.get("region") or {}
        if region.get("mode") == "ratio":
            text = (f"当前区域：窗口的 {region.get('x', 0)*100:.1f}% ~ "
                    f"{(region.get('x', 0)+region.get('w', 0))*100:.1f}% 宽、"
                    f"{region.get('y', 0)*100:.1f}% ~ "
                    f"{(region.get('y', 0)+region.get('h', 0))*100:.1f}% 高")
        else:
            text = f"当前区域：屏幕坐标 {region}"
        self.region_var.set(text)

    # ---------- ③ 阈值 ----------
    def _tab_threshold(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(tab, text=self.STEPS[2])
        ttk.Label(tab, text="数值掉到多少要提醒？",
                  font=("Microsoft YaHei UI", 12)).pack(anchor="w")
        body = ttk.Frame(tab)
        body.pack(anchor="w", pady=(12, 0))
        ttk.Label(body, text="低于这个数，记一条日志并截图：").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(body, textvariable=self.app.vars["warn"], width=8).grid(row=0, column=1, padx=8)
        ttk.Label(body, text="（比如 50）", foreground="#999").grid(row=0, column=2, sticky="w")
        ttk.Label(body, text="低于这个数，发钉钉提醒：").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(body, textvariable=self.app.vars["alert"], width=8).grid(row=1, column=1, padx=8)
        ttk.Label(body, text="（比如 35）", foreground="#999").grid(row=1, column=2, sticky="w")
        ttk.Label(tab, text="规则：预警线要大于报警线。改完这两处，其余参数都由程序自己算。",
                  foreground="#999", wraplength=580, justify="left").pack(anchor="w", pady=(16, 0))

    # ---------- ④ 钉钉 ----------
    def _tab_dingtalk(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(tab, text=self.STEPS[3])
        ttk.Label(tab, text="最后一步：把钉钉机器人的地址粘进来",
                  font=("Microsoft YaHei UI", 12)).pack(anchor="w")
        ttk.Label(tab, text="钉钉群 → 群设置 → 机器人 → 添加「自定义」→ 复制 Webhook 地址。\n"
                           "安全设置选「加签」的话，把 SEC 开头那串也填到下面。",
                  foreground="#666", justify="left").pack(anchor="w", pady=(4, 12))
        form = ttk.Frame(tab)
        form.pack(fill="x", anchor="w")
        ttk.Label(form, text="Webhook").grid(row=0, column=0, sticky="e")
        ttk.Entry(form, textvariable=self.app.vars["webhook"], width=62).grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(form, text="加签密钥").grid(row=1, column=0, sticky="e", pady=(8, 0))
        ttk.Entry(form, textvariable=self.app.vars["secret"], width=42, show="*").grid(
            row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Button(tab, text="发送一条测试消息", command=self.app.test_dingtalk).pack(
            anchor="w", pady=(14, 0))
        ttk.Label(tab, text="没有钉钉也能用：先跳过，截图和日志照常记录。",
                  foreground="#999").pack(anchor="w", pady=(12, 0))

    # ---------- 导航 ----------
    def prev(self) -> None:
        idx = self.notebook.index("current")
        self.notebook.select(max(0, idx - 1))

    def next(self) -> None:
        idx = self.notebook.index("current")
        self.notebook.select(min(len(self.STEPS) - 1, idx + 1))

    def _update_nav(self) -> None:
        try:
            idx = self.notebook.index("current")
        except tk.TclError:
            return
        last = idx >= len(self.STEPS) - 1
        self.next_btn.configure(state="disabled" if last else "normal")
        self.done_btn.configure(text="完成，开始监控" if last else "直接完成并开始")

    def finish(self) -> None:
        self.app.finish_wizard()
        self.close()

    def close(self) -> None:
        self.app._wizard = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass

class _TechLogViewer:
    """软件内直接看日志：边看边跟随写入，不锁文件，不用开文件管理器。"""

    def __init__(self, app: "AppUI", path: Path) -> None:
        self.app = app
        self.path = Path(path)
        self._pos = 0
        self.top = tk.Toplevel(app.root)
        self.top.title("运行日志")
        self.top.geometry("940x560")
        self.top.transient(app.root)

        bar = ttk.Frame(self.top, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text=str(self.path), foreground="#666").pack(side="left")
        ttk.Button(bar, text="打开所在文件夹", command=self._open_dir).pack(side="right", padx=4)
        ttk.Button(bar, text="清屏（只清显示）", command=self._clear).pack(side="right", padx=4)
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动跟随", variable=self.auto).pack(side="right", padx=8)

        body = ttk.Frame(self.top, padding=(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="none", font=("Consolas", 9), background="#11161a",
                            foreground="#cfe3dc", state="disabled")
        ysb = ttk.Scrollbar(body, command=self.text.yview)
        xsb = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="we")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self._load_all()
        self.top.after(800, self._tick)

    # ---------- 内部 ----------
    def _load_all(self) -> None:
        try:
            if not self.path.exists():
                self._append(f"[还没产生日志] 期望路径：{self.path}")
                return
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                self._append(fh.read())
                self._pos = fh.tell()
        except OSError as exc:
            self._append(f"[读取日志失败] {exc}")

    def _append(self, data: str) -> None:
        if not data:
            return
        at_bottom = self.text.yview()[1] > 0.985
        self.text.configure(state="normal")
        self.text.insert("end", data)
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 3000:
            self.text.delete("1.0", f"{lines - 2500}.0")
        self.text.configure(state="disabled")
        if self.auto.get() and at_bottom:
            self.text.see("end")

    def _tick(self) -> None:
        try:
            if not self.top.winfo_exists():
                return
            if self.path.exists():
                size = self.path.stat().st_size
                if size < self._pos:      # 日志轮转/被清空
                    self._pos = 0
                    self.text.configure(state="normal")
                    self.text.delete("1.0", "end")
                    self.text.configure(state="disabled")
                if size > self._pos:
                    with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(self._pos)
                        self._append(fh.read())
                        self._pos = fh.tell()
        except OSError:
            pass
        except tk.TclError:
            return
        try:
            self.top.after(1000, self._tick)
        except tk.TclError:
            pass

    def _clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _open_dir(self) -> None:
        try:
            AppUI._open_in_explorer(self.path.parent)
        except Exception as exc:
            messagebox.showerror("打不开目录", f"{self.path.parent}\n\n{exc}")

class _Tail:
    """按文件增量读取，处理日志轮转/清空，不做任何锁。"""

    def __init__(self, path: Path, formatter=None) -> None:
        self.path = Path(path)
        self.formatter = formatter or (lambda s: s)
        self.pos = 0

    def read_new(self) -> str:
        if not self.path.exists():
            return ""
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self.pos:
            self.pos = 0
        if size <= self.pos:
            return ""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.pos)
                data = fh.read()
                self.pos = fh.tell()
        except OSError:
            return ""
        return self.formatter(data)


class LogViewer:
    """软件内日志：默认只看"跌破阈值的记录"，另一页才是技术日志。"""

    LABELS = {
        "warn_cross": "跌破预警线",
        "alert_cross": "跌破报警线",
        "alert_repeat": "持续低于报警线",
        "recover": "恢复正常",
    }

    def __init__(self, app: "AppUI", log_path: Path) -> None:
        from . import config as cfgmod

        self.app = app
        self.log_path = Path(log_path)
        self.events_path = cfgmod.log_dir() / "events.jsonl"
        self.samples_path = cfgmod.log_dir() / "samples.log"
        self.ev_tail = _Tail(self.events_path, self._format_events)
        self.tech_tail = _Tail(self.log_path)
        self.sample_tail = _Tail(self.samples_path)

        self.top = tk.Toplevel(app.root)
        self.top.title("记录")
        self.top.geometry("980x560")
        self.top.transient(app.root)

        bar = ttk.Frame(self.top, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="报警记录只保留跌破阈值的条目；技术细节在另一页。",
                  foreground="#666").pack(side="left")
        ttk.Button(bar, text="打开日志文件夹", command=self._open_dir).pack(side="right", padx=4)
        ttk.Button(bar, text="清屏（只清显示）", command=self._clear).pack(side="right", padx=4)

        self.nb = ttk.Notebook(self.top)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.ev_text = self._make_tab("报警记录")
        self.sample_text = self._make_tab("采样记录")
        self.tech_text = self._make_tab("技术日志")

        self._fill_initial()
        self.top.after(800, self._tick)

    def _make_tab(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=title)
        text = tk.Text(frame, wrap="none", font=("Consolas", 9), background="#11161a",
                       foreground="#cfe3dc", state="disabled")
        ysb = ttk.Scrollbar(frame, command=text.yview)
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="we")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return text

    # ---------- 业务记录格式化 ----------
    def _format_events(self, data: str) -> str:
        import json

        out: list[str] = []
        for raw in data.splitlines():
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except Exception:
                out.append(raw)
                continue
            label = self.LABELS.get(str(e.get("event")), str(e.get("event")))
            parts = [str(e.get("time", ""))[:19], label, str(e.get("rule", ""))]
            value = e.get("value")
            prev = e.get("prev")
            if isinstance(value, (int, float)):
                piece = f"当前 {value:g}"
                if isinstance(prev, (int, float)):
                    piece += f"（上一 {prev:g}）"
                parts.append(piece)
            if isinstance(e.get("alert"), (int, float)):
                parts.append(f"报警线 {e['alert']:g}")
            if e.get("snapshot"):
                parts.append(f"截图 {e['snapshot']}")
            out.append("  |  ".join(p for p in parts if p))
        return ("\n".join(out) + "\n") if out else ""

    # ---------- 填充与跟随 ----------
    def _append(self, widget: tk.Text, text: str) -> None:
        if not text:
            return
        at_bottom = widget.yview()[1] > 0.985
        widget.configure(state="normal")
        widget.insert("end", text)
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > 3000:
            widget.delete("1.0", f"{lines - 2500}.0")
        widget.configure(state="disabled")
        if at_bottom:
            widget.see("end")

    def _fill_initial(self) -> None:
        ev = self.ev_tail.read_new()
        if ev:
            self._append(self.ev_text, ev)
        else:
            self._append(self.ev_text, "（还没有跌破阈值的记录）\n")
        tech = self.tech_tail.read_new()
        self._append(self.tech_text, tech or "（暂无技术日志）\n")
        samples = self.sample_tail.read_new()
        self._append(self.sample_text, samples or "（还没有采样记录）\n")

    def _tick(self) -> None:
        try:
            if not self.top.winfo_exists():
                return
            self._append(self.ev_text, self.ev_tail.read_new())
            self._append(self.sample_text, self.sample_tail.read_new())
            self._append(self.tech_text, self.tech_tail.read_new())
        except tk.TclError:
            return
        try:
            self.top.after(1000, self._tick)
        except tk.TclError:
            pass

    def _clear(self) -> None:
        current = self.nb.index("current")
        widget = self.ev_text if current == 0 else self.tech_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _open_dir(self) -> None:
        try:
            AppUI._open_in_explorer(self.log_path.parent)
        except Exception as exc:
            messagebox.showerror("打不开目录", f"{self.log_path.parent}\n\n{exc}")

class SamplesViewer:
    """紧凑采样记录：一行一个值或 fail，用来回看数值走势。"""

    def __init__(self, app: "AppUI", path: Path) -> None:
        self.path = Path(path)
        self.tail = _Tail(self.path)
        self.top = tk.Toplevel(app.root)
        self.top.title("采样记录")
        self.top.geometry("420x560")
        self.top.transient(app.root)

        bar = ttk.Frame(self.top, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text=str(self.path), foreground="#666").pack(side="left")
        body = ttk.Frame(self.top, padding=(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, wrap="none", font=("Consolas", 10), background="#11161a",
                            foreground="#cfe3dc", state="disabled")
        sb = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        data = self.tail.read_new()
        self._append(data or "（还没有采样记录）\n")
        self.top.after(1000, self._tick)

    def _append(self, data: str) -> None:
        if not data:
            return
        at_bottom = self.text.yview()[1] > 0.985
        self.text.configure(state="normal")
        self.text.insert("end", data)
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 4000:
            self.text.delete("1.0", f"{lines - 3000}.0")
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _tick(self) -> None:
        try:
            if not self.top.winfo_exists():
                return
            self._append(self.tail.read_new())
        except tk.TclError:
            return
        try:
            self.top.after(1000, self._tick)
        except tk.TclError:
            pass

class SnapshotBrowser:
    """软件内看截图：双击用默认看图程序打开，选中后用资源管理器定位。"""

    def __init__(self, app: "AppUI", root_dir: Path) -> None:
        self.app = app
        self.root_dir = Path(root_dir)
        self.top = tk.Toplevel(app.root)
        self.top.title("截图")
        self.top.geometry("860x540")
        self.top.transient(app.root)

        bar = ttk.Frame(self.top, padding=(10, 8))
        bar.pack(fill="x")
        self.info_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.info_var, foreground="#666").pack(side="left")

        body = ttk.Frame(self.top, padding=(10, 0, 10, 6))
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("time", "name", "size"), show="headings",
                                 selectmode="browse")
        for key, title, width in (("time", "时间", 170), ("name", "文件", 520), ("size", "大小", 90)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        ysb = ttk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())
        self.tree.bind("<Return>", lambda _e: self.open_selected())

        self.hint_var = tk.StringVar(value="")
        ttk.Label(self.top, textvariable=self.hint_var, foreground="#a15c00",
                  padding=(12, 0)).pack(fill="x")

        foot = ttk.Frame(self.top, padding=(10, 6, 10, 10))
        foot.pack(fill="x")
        ttk.Button(foot, text="打开选中的截图", command=self.open_selected).pack(side="left")
        ttk.Button(foot, text="立即截一张", command=self.capture_now).pack(side="left", padx=6)
        ttk.Button(foot, text="在资源管理器里定位", command=self.reveal_selected).pack(
            side="left", padx=6)
        ttk.Button(foot, text="刷新", command=self.refresh).pack(side="right")
        ttk.Button(foot, text="关闭", command=self.top.destroy).pack(side="right", padx=6)

        self._files: dict[str, Path] = {}
        self.refresh()

    # ---------- 列表 ----------
    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._files.clear()
        rows = []
        if self.root_dir.exists():
            for path in self.root_dir.rglob("*.png"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rows.append((stat.st_mtime, path, stat.st_size))
        rows.sort(key=lambda item: item[0], reverse=True)
        for mtime, path, size in rows[:500]:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
            iid = str(path)
            self._files[iid] = path
            self.tree.insert("", "end", iid=iid, values=(stamp, path.name, f"{size / 1024:.0f} KB"))
        self.info_var.set(f"{self.root_dir}    共 {len(rows)} 张")
        if not rows:
            self.hint_var.set("还没有截图：只有跌破预警线 / 报警线时才会自动截图（也可以点「立即看一次」后的截图测试）。")
        else:
            self.hint_var.set("")

    def _selected(self) -> Path | None:
        sel = self.tree.selection()
        return self._files.get(sel[0]) if sel else None

    # ---------- 打开 ----------
    def open_selected(self) -> None:
        """软件内预览，不再调外部看图程序。"""
        if self._selected() is None:
            children = self.tree.get_children()
            if not children:
                self.hint_var.set("还没有截图。点「立即截一张」先试一下通道。")
                return
            # 没选中就默认第一张，保证按钮"按了就有东西"
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
        path = self._selected()
        order = [self._files[iid] for iid in self.tree.get_children() if iid in self._files]
        index = order.index(path) if path in order else 0
        self._image_viewer = ImageViewer(self.app, order, index)

    def capture_now(self) -> None:
        """立刻截一张，验证截图通道是否正常。"""
        try:
            self.app.monitor.prepare(self.app.snapshot())
            rule = self.app.snapshot()["rules"][0]
            path = self.app.monitor.recorder.snapshot(rule, "_TEST", reason="手动截图")
        except Exception as exc:
            self.hint_var.set(f"截图失败：{exc}")
            return
        self.refresh()
        if path:
            self.hint_var.set(f"已截图：{path}")
            if self.tree.get_children():
                self.tree.selection_set(self.tree.get_children()[0])
                self.open_selected()
        else:
            self.hint_var.set("截图失败，详情看「记录 → 技术日志」。")

    def reveal_selected(self) -> None:
        path = self._selected() or self.root_dir
        try:
            self.root_dir.mkdir(parents=True, exist_ok=True)
            AppUI._open_in_explorer(path, select=path.is_file())
            self.hint_var.set(f"已尝试打开资源管理器；如果没弹出来，路径是：{path}")
        except Exception as exc:
            self.hint_var.set(f"资源管理器没打开（{exc}）。路径：{path}")

class ImageViewer:
    """软件内看图：不调用任何外部程序，PIL 画到 Canvas 上。"""

    def __init__(self, app: "AppUI", paths: list[Path], index: int = 0) -> None:
        self.app = app
        self.paths = list(paths)
        self.index = max(0, min(index, len(self.paths) - 1)) if self.paths else 0
        self._photo = None
        self._src_size = (0, 0)
        self._fit = True

        self.top = tk.Toplevel(app.root)
        self.top.title("查看截图")
        self.top.geometry("1000x720")
        self.top.transient(app.root)
        self.top.configure(bg="#0d1216")

        self.canvas = tk.Canvas(self.top, bg="#0d1216", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._render())

        bar = ttk.Frame(self.top, padding=(10, 6))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="适应窗口 / 实际大小", command=self.toggle_fit).pack(side="left")
        ttk.Button(bar, text="上一张", command=lambda: self.step(-1)).pack(side="left", padx=6)
        ttk.Button(bar, text="下一张", command=lambda: self.step(1)).pack(side="left")
        ttk.Button(bar, text="关闭", command=self.top.destroy).pack(side="right")

        self.info_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.info_var, foreground="#8aa").pack(side="left", padx=12)

        self.top.bind("<Left>", lambda _e: self.step(-1))
        self.top.bind("<Right>", lambda _e: self.step(1))
        self._render()

    # ---------- 内部 ----------
    def _render(self) -> None:
        try:
            from PIL import Image, ImageTk
        except Exception as exc:
            self.info_var.set(f"缺少 PIL，无法预览：{exc}")
            return
        if not self.paths:
            self.info_var.set("没有图片")
            return
        path = self.paths[self.index]
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            self.info_var.set(f"读不了这张图：{exc}")
            return
        self._src_size = img.size
        width = max(64, self.canvas.winfo_width() or 900)
        height = max(64, self.canvas.winfo_height() or 600)
        if self._fit:
            ratio = min(width / img.width, height / img.height, 1.0)
        else:
            ratio = 1.0
        if ratio < 1.0:
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                             Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(width // 2, height // 2, image=self._photo, anchor="center")
        try:
            size_kb = path.stat().st_size / 1024
        except OSError:
            size_kb = 0
        self.top.title(f"{path.name}  （{self.index + 1}/{len(self.paths)}）")
        self.info_var.set(f"{path.name}   {self._src_size[0]}×{self._src_size[1]}   "
                          f"{size_kb:.0f} KB")

    def toggle_fit(self) -> None:
        self._fit = not self._fit
        self._render()

    def step(self, delta: int) -> None:
        if not self.paths:
            return
        self.index = (self.index + delta) % len(self.paths)
        self._render()

class HudOverlay:
    """最小化时显示的悬浮窗：置顶 + 半透明 + 鼠标穿透（仅可见，不影响其他操作）。"""

    PRESETS = ("顶部居中", "左上", "右上", "左下", "右下")

    def __init__(self, app: "AppUI") -> None:
        from .wininput import ensure_topmost, make_click_through

        self.app = app
        cfg = (app.snapshot().get("app") or {}).get("hud") or {}
        self.opacity = float(cfg.get("opacity", 0.78))
        self.font_size = int(cfg.get("font_size", 20))
        self.position = str(cfg.get("position", "顶部居中"))

        self.top = tk.Toplevel(app.root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass
        self.top.configure(bg="#0b1210")

        box = tk.Frame(self.top, bg="#0b1210", padx=14, pady=8)
        box.pack()
        self.value_var = tk.StringVar(value="--")
        self.time_var = tk.StringVar(value="时间 --:--")
        self.value_label = tk.Label(box, textvariable=self.value_var, bg="#0b1210",
                                    fg="#7ef0c4",
                                    font=("Microsoft YaHei UI", self.font_size, "bold"))
        self.value_label.pack(anchor="center")
        tk.Label(box, textvariable=self.time_var, bg="#0b1210", fg="#b8d8cf",
                 font=("Consolas", max(10, self.font_size - 8))).pack(anchor="center")

        self._hwnd = int(self.top.winfo_id())
        if not make_click_through(self._hwnd):
            app.log.warning("悬浮窗未能设为鼠标穿透（仍然可用，只是会挡住点击）")
        try:                     # 改过扩展样式后再设一次透明度，防止被清成 0
            self.top.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass
        ensure_topmost(self._hwnd)
        self.top.withdraw()

    # ---------- 对外 ----------
    def set_values(self, value_text: str, time_text: str, alert: bool = False) -> None:
        self.value_var.set(value_text)
        self.time_var.set(f"时间 {time_text}")
        try:
            self.value_label.configure(fg="#ff7a7a" if alert else "#7ef0c4")
        except tk.TclError:
            pass

    def show(self) -> None:
        try:
            self._place()
            self.top.deiconify()
            from .wininput import ensure_topmost, make_click_through

            # 每次显示都重设一遍：Tk 在 deiconify 时可能重建包装窗口，样式会丢
            make_click_through(self._hwnd)
            try:
                self.top.attributes("-alpha", self.opacity)
            except tk.TclError:
                pass
            ensure_topmost(self._hwnd)
            self.top.attributes("-topmost", True)
        except tk.TclError:
            pass

    def hide(self) -> None:
        try:
            self.top.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    # ---------- 内部 ----------
    def _place(self) -> None:
        try:
            self.top.update_idletasks()
            w = max(80, self.top.winfo_reqwidth())
            h = max(40, self.top.winfo_reqheight())
            sw = self.top.winfo_screenwidth()
            sh = self.top.winfo_screenheight()
            pos = self.position
            if pos == "左上":
                x, y = 24, 24
            elif pos == "右上":
                x, y = sw - w - 24, 24
            elif pos == "左下":
                x, y = 24, sh - h - 90
            elif pos == "右下":
                x, y = sw - w - 24, sh - h - 90
            else:
                x, y = (sw - w) // 2, 40
            self.top.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        except tk.TclError:
            pass
