"""从 OCR 文本里取数字。

屏幕上的数值经常带千分位、空格、全角字符、百分号，OCR 偶尔把逗号读成点，
统一清洗后再抽取，避免 "1,234" 被拆成两个数。
"""
from __future__ import annotations

import re

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９．，－＋％",
    "0123456789.,-+%",
)

# 数字：可带千分位、可带小数、可带正负号
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

# 带百分号的数字，例如 "86%"、"12.5 %"
_PERCENT_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*%")

# 中文数量词，例如 "1.2万"
_UNIT_RE = re.compile(r"^\s*(万|亿|k|K|w)")


def normalize(text: str) -> str:
    """全角转半角、去掉数字之间的空格、各种破折号统一成减号。"""
    if not text:
        return ""
    s = text.translate(_FULLWIDTH)
    s = s.replace("—", "-").replace("–", "-").replace("−", "-").replace("·", ".")
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)      # "1 234" -> "1234"
    s = re.sub(r"(?<=\d)\s*,\s*(?=\d)", ",", s)  # "1 , 234" -> "1,234"
    s = re.sub(r"(?<=\d)\s+(?=\.)", "", s)       # "12 .5" -> "12.5"
    return s


def to_float(token: str) -> float | None:
    token = token.replace(",", "").strip()
    try:
        return float(token)
    except ValueError:
        return None


def extract_numbers(text: str) -> list[float]:
    """按出现顺序返回所有数字（已处理千分位）；无法解析的跳过。"""
    s = normalize(text)
    out: list[float] = []
    for m in _NUM_RE.finditer(s):
        value = to_float(m.group(0))
        if value is None:
            continue
        # "1.2万" 这类单位换算
        tail = s[m.end(): m.end() + 2]
        um = _UNIT_RE.match(tail)
        if um:
            unit = um.group(1).lower()
            factor = {"万": 1e4, "亿": 1e8, "k": 1e3, "w": 1e4}.get(unit, 1.0)
            value *= factor
        out.append(value)
    return out


def choose(values: list[float], mode: str = "first") -> float | None:
    if not values:
        return None
    mode = (mode or "first").lower()
    if mode == "last":
        return values[-1]
    if mode == "max":
        return max(values)
    if mode == "min":
        return min(values)
    if mode == "sum":
        return float(sum(values))
    return values[0]


def extract_percent_values(text: str) -> list[float]:
    """只取"带百分号"的数字。

    游戏里飘出来的加分项（+300、+1,200）没有百分号，天然被排除；
    带显式正号的数值（如 +300%）不参与取值。
    """
    s = normalize(text)
    out: list[float] = []
    for m in _PERCENT_RE.finditer(s):
        token = m.group(1)
        start = m.start(1)
        prefix = s[max(0, start - 1): start]
        if token.startswith("+") or prefix == "+":
            continue
        value = to_float(token.lstrip("+"))
        if value is not None:
            out.append(value)
    return out


_CLOCK_RE = re.compile(r"(\d{1,4})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?")
_MIN_SEC_RE = re.compile(r"(\d{1,3})\s*分(?:钟)?\s*(?:(\d{1,2})\s*秒)?")


def parse_clock(text: str) -> float | None:
    """把 "43:27" / "191:31" / "1:02:33" / "12分30秒" 解析成秒数。取不到返回 None。

    分钟可能超过 99（长时间计时器），因此不限定 1~2 位，
    否则 "191:31" 会被误解析成 "91:31"。
    """
    if not text:
        return None
    s = normalize(text)
    m = _CLOCK_RE.search(s)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c is not None:                       # hh:mm:ss
            return int(a) * 3600 + int(b) * 60 + int(c)
        return int(a) * 60 + int(b)             # mm:ss（分钟可 > 59）
    m = _MIN_SEC_RE.search(s)
    if m:
        minutes = int(m.group(1))
        seconds = int(m.group(2) or 0)
        return minutes * 60 + seconds
    return None


def format_clock(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_value(text: str, mode: str = "first", require_percent: bool = False) -> float | None:
    """文本 -> 目标数值。取不到返回 None。

    require_percent=True 时只认带 % 的数字，避免把区域里的其它数字
    （时间、飘字加分、水印版本号）当成本次读数。
    """
    s = normalize(text)
    if require_percent:
        return choose(extract_percent_values(s), mode)
    return choose(extract_numbers(s), mode)


def format_value(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")
