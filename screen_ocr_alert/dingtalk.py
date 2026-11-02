"""钉钉自定义群机器人：加签 + 限流 + 重试。

用法（官方文档的核心逻辑）：
    timestamp = 当前毫秒
    string_to_sign = f"{timestamp}\n{secret}"
    sign = base64(hmac_sha256(secret, string_to_sign))
    webhook + "&timestamp=...&sign=" + quote_plus(sign)

消息里必须包含机器人安全设置里的自定义关键词（若配置了"自定义关键词"），
本模块在内容缺少关键词时自动补一行，并写一条警告到日志。
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import logging
import socket
import time
import urllib.parse

import requests


class DingTalkError(RuntimeError):
    pass


def _default_logger() -> logging.Logger:
    return logging.getLogger(__name__)


class DingTalk:
    def __init__(self, conf: dict, logger: logging.Logger | None = None) -> None:
        conf = conf or {}
        self.log = logger or _default_logger()
        self.webhook = (conf.get("webhook") or "").strip()
        self.secret = (conf.get("secret") or "").strip()
        self.keyword = (conf.get("keyword") or "").strip()
        self.at_mobiles = [str(m) for m in (conf.get("at_mobiles") or []) if str(m).strip()]
        self.at_all = bool(conf.get("at_all"))
        self.timeout = float(conf.get("timeout_sec") or 8)
        self.max_per_minute = max(1, int(conf.get("max_per_minute") or 18))
        self._sent: collections.deque[float] = collections.deque()

    # ---------- 基础 ----------
    def configured(self) -> bool:
        return self.webhook.startswith("http")

    def _signed_url(self) -> str:
        url = self.webhook
        if not self.secret:
            return url
        timestamp = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={timestamp}&sign={sign}"

    def _throttle(self) -> None:
        now = time.time()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_minute:
            raise DingTalkError(
                f"本机发送频率超限（{self.max_per_minute} 条/分钟），本次提醒已丢弃"
            )

    def _post(self, payload: dict) -> dict:
        if not self.configured():
            raise DingTalkError("未配置钉钉 webhook，请先在面板里填写并在群里添加机器人")
        self._throttle()
        url = self._signed_url()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if int(data.get("errcode", 0)) != 0:
                    raise DingTalkError(
                        f"钉钉返回错误 errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
                    )
                self._sent.append(time.time())
                return data
            except DingTalkError:
                raise
            except (requests.RequestException, ValueError, socket.error) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise DingTalkError(f"发送失败: {last_exc}")

    def _finalize(self, content: str) -> str:
        text = content
        if self.keyword and self.keyword not in text:
            text = f"{text}\n{self.keyword}"
            self.log.warning("消息未包含关键词「%s」，已自动补充以满足钉钉安全设置", self.keyword)
        return text

    # ---------- 对外 ----------
    def send_text(
        self,
        content: str,
        at_mobiles: list[str] | None = None,
        at_all: bool | None = None,
    ) -> dict:
        content = self._finalize(content)
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {
                "atMobiles": list(at_mobiles if at_mobiles is not None else self.at_mobiles),
                "isAtAll": bool(self.at_all if at_all is None else at_all),
            },
        }
        return self._post(payload)

    def send_markdown(self, title: str, text: str) -> dict:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": self._finalize(text)},
            "at": {"atMobiles": list(self.at_mobiles), "isAtAll": self.at_all},
        }
        return self._post(payload)

    def test(self) -> str:
        data = self.send_text("【屏幕数值监控】测试消息：钉钉机器人配置成功 ✓")
        return f"发送成功 errcode={data.get('errcode', 0)}"
