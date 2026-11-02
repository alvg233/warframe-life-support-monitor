"""配置读写：JSON + 与默认值深合并 + 原子写入。

结构上分四层，互不耦合，方便单独改：
  sample  采样节奏（多快问一次屏幕、画面没变要不要跳过）
  guard   识别校验（合理区间、降速上限、重读次数）
  snapshot 截图存档（通道、目录、保留天数）
  rules[] 规则（区域 + 数值解析 + 警戒线/报警线 + 各自的通知与截图策略）
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

DEFAULT_RULE = {
    "id": "rule-1",
    "name": "维生系统",
    "enabled": True,
    "region": {
        "mode": "ratio",          # ratio = 相对游戏窗口客户区的比例；absolute = 屏幕像素
        "x": 0.0, "y": 0.05, "w": 0.30, "h": 0.38,
    },
    # 小框读失败时，在这块"搜索区"里找带 % 的那一行，并自动把位置纠正过来
    "search_zone": {"mode": "ratio", "x": 0.0, "y": 0.08, "w": 0.35, "h": 0.30},
    "number": {
        "mode": "first",          # first / last / max / min / sum
        "require_percent": True,  # 只认带 % 的数字
        "valid_range": [0, 100],  # 合理区间，超出视为误读（null 关闭）
    },
    # 预警线：从上方跌破这条线 -> 记日志 + 截图（不打扰）
    "warn": {"value": 50.0, "log": True, "snapshot": True, "suffix": ""},
    # 报警线：跌破这条线 -> 记日志 + 截图（文件名带 _EX）+ 发钉钉
    "alert": {
        "value": 35.0,
        "log": True,
        "snapshot": True,
        "suffix": "_EX",
        "notify": True,
        "cooldown_sec": 60,
        "realert_sec": 1800,
        "template": (
            "【{name}】当前值 {value}，已低于报警线 {alert}。\n"
            "上一读数：{prev}\n时间：{time}\n主机：{host}\n截图：{snapshot}"
        ),
    },
    "recover": {"notify": True, "snapshot": False},
    # 急停：满足触发条件后把游戏调前台、按 ESC，再连续识别若干次确认已停
    "emergency": {
        "enabled": False,
        "triggers": {"zero_percent": True, "elapsed_minutes": False},
        "zero_threshold": 0.5,          # 数值 <= 这个值即视为"归零"
        "elapsed_minutes": 10.0,        # 时间到达多少分钟触发
        "time_read_every_n": 2,         # 每几轮读一次时间（计时器变化慢，省 CPU）
        "time_region": {"mode": "ratio", "x": 0.02, "y": 0.22, "w": 0.14, "h": 0.10},
        "activate_window": True,        # 先把游戏切前台
        "click_before_key": True,       # 很多游戏要真实鼠标点击后才接收按键
        "click_ratio": [0.05, 0.15],    # 点击位置（相对窗口客户区；默认落在左上角「生存」附近）
        "press_key": "esc",
        "press_count": 1,               # 按几次（有些游戏第一下只是"聚焦"）
        "verify_reads": 5,              # 连续识别次数
        "verify_interval_sec": 0.6,
        "auto_rearm": True,             # 急停后自动等待下一局并恢复监控
        "notify": True,
        "template": (
            "【{name}】急停已触发（{reason}）。\n"
            "连续 {reads} 次识别：{detail}\n结果：{result}\n时间：{time}"
        ),
    },
    # 连续识别失败是否发钉钉：0 = 不发（默认）。失败只写技术日志，不打扰。
    "fail_alert_after": 0,
}

DEFAULT_CONFIG = {
    "version": 2,
    "app": {
        "low_priority": True,
        "start_monitoring": False,
        "show_overlay": True,
        "ui_mode": "simple",       # simple / advanced
        "wizard_done": False,      # 首次启动是否已完成四步向导
        # 主窗口最小化时显示的悬浮窗：置顶、半透明、鼠标穿透（仅可见）
        "hud": {"enabled": True, "position": "顶部居中", "opacity": 0.78, "font_size": 20},
    },
    "sample": {
        "interval_sec": 1.5,       # 基础采样间隔，也是降速判定的时间基准
        "change_detection": True,  # 画面没变就跳过 OCR
        "diff_threshold": 1.5,
        "force_ocr_sec": 600,      # 静止太久也强制识别一次
        "retry_count": 2,          # 可疑/不合法时快速重读次数
        "retry_gap_sec": 0.35,     # 重读间隔（比基础间隔更短）
        "log_enabled": True,       # 是否记录 samples.log（时间 + 数值/fail）
    },
    "guard": {
        "max_drop_per_1_5s": 1.0,  # 每 1.5 秒最多下降 1 个百分点
        "drop_tolerance": 1.0,     # 容差，吸收 OCR 的 ±1 级抖动
        "agree_tolerance": 1.0,    # 重读之间差多少算"读数一致"
        "min_confidence": 0.45,
    },
    "snapshot": {
        "enabled": True,
        "backend": "window",       # window / desktop / auto
        "window_title": "",        # 游戏窗口标题（子串匹配）
        "process_name": "",        # 或按进程名匹配，例如 GenshinImpact.exe
        "dir": "snapshots",
        "keep_days": 30,
    },
    "dingtalk": {
        "webhook": "",
        "secret": "",
        "keyword": "",
        "at_mobiles": [],
        "at_all": False,
        "timeout_sec": 8,
        "max_per_minute": 18,
    },
    "rules": [DEFAULT_RULE],
}


def data_dir() -> Path:
    """配置、日志、截图所在目录。

    优先级：
      1. 环境变量 SCREEN_OCR_ALERT_HOME（调试/测试用）
      2. 便携模式：exe 同目录放一个 portable.txt，就用 exe 目录
      3. 默认 %APPDATA%\\ScreenOcrAlert —— 放在程序外面，
         这样重新打包覆盖程序目录时，配置和截图不会被清掉。
    """
    env = os.environ.get("SCREEN_OCR_ALERT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.txt").exists():
            return exe_dir
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        target = (Path(appdata) / "ScreenOcrAlert") if appdata else exe_dir
        _migrate_from(exe_dir, target)
        return target
    return Path(__file__).resolve().parent.parent


def _migrate_from(old_dir: Path, new_dir: Path) -> None:
    """旧版配置位于程序目录，迁移到数据目录。"""
    try:
        old_cfg = old_dir / "config.json"
        new_cfg = new_dir / "config.json"
        if old_cfg.exists() and not new_cfg.exists():
            new_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_cfg, new_cfg)
    except OSError:
        pass


def config_path() -> Path:
    return data_dir() / "config.json"


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base，但保留 base 里 override 没有的键。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = deep_merge(DEFAULT_CONFIG, raw)
        except Exception:  # 配置坏了不阻塞启动，尝试用 .bak 恢复
            bak = path.with_suffix(path.suffix + ".bak")
            recovered = False
            if bak.exists():
                try:
                    raw = json.loads(bak.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        cfg = deep_merge(DEFAULT_CONFIG, raw)
                        recovered = True
                except Exception:
                    pass
            try:
                logging.getLogger(__name__).warning(
                    "配置文件损坏，已%s", "用 .bak 备份恢复" if recovered else "临时使用默认配置")
            except Exception:
                pass
    if not cfg.get("rules"):
        cfg["rules"] = [copy.deepcopy(DEFAULT_RULE)]
    else:
        # 老配置里没有新加的字段（比如 emergency），按默认值补齐，但不覆盖用户已有值
        cfg["rules"] = [deep_merge(DEFAULT_RULE, r) if isinstance(r, dict) else
                        copy.deepcopy(DEFAULT_RULE) for r in cfg["rules"]]
    return cfg


def save(cfg: dict, path: Path | None = None) -> None:
    """原子写入：先写临时文件再替换，避免断电写坏配置。

    覆盖前保留一份 .bak，防止误清空（比如 Webhook 这种手填的东西）。
    """
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def primary_rule(cfg: dict) -> dict:
    rules = cfg.get("rules") or [DEFAULT_RULE]
    return rules[0]


def validate_rule(rule: dict) -> list[str]:
    """返回配置错误列表（空表示没问题），UI 和监控启动前都会调。"""
    errors: list[str] = []
    alert_v = float((rule.get("alert") or {}).get("value", 35))
    warn_v = float((rule.get("warn") or {}).get("value", 50))
    if warn_v <= alert_v:
        errors.append("预警线必须高于报警线")
    region = rule.get("region") or {}
    try:
        rw, rh = float(region.get("w", 0)), float(region.get("h", 0))
    except (TypeError, ValueError):
        rw, rh = 0.0, 0.0
    if rw <= 0 or rh <= 0:
        errors.append("区域未设置")
    rng = (rule.get("number") or {}).get("valid_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2 and float(rng[0]) > float(rng[1]):
        errors.append("有效范围上下限写反了")
    return errors
