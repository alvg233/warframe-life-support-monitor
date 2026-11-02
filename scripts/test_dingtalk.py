r"""钉钉模块离线测试：本地起一个假服务器，验证加签、payload、重试与限流。

    $env:PYTHONPATH=".vendor"; python scripts\test_dingtalk.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

from screen_ocr_alert.dingtalk import DingTalk, DingTalkError  # noqa: E402

SECRET = "SECtest1234567890"
received: list[dict] = []
reply = {"errcode": 0, "errmsg": "ok"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        received.append({"path": self.path, "body": json.loads(body.decode("utf-8"))})
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def verify_sign(query: dict[str, list[str]]) -> bool:
    ts = query["timestamp"][0]
    sign = urllib.parse.unquote_plus(query["sign"][0])
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), f"{ts}\n{SECRET}".encode(), hashlib.sha256).digest()
    ).decode()
    return sign == expected


def main() -> int:
    global reply
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webhook = f"http://127.0.0.1:{port}/robot/send?access_token=demo"
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' -> ' + detail) if detail else ''}")
        ok = ok and cond

    bot = DingTalk({"webhook": webhook, "secret": SECRET, "keyword": "", "timeout_sec": 5})
    bot.send_text("屏幕数值监控：维生系统 35%，低于阈值 40%")
    check("发出 1 条消息", len(received) == 1)
    item = received[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(item["path"]).query)
    check("带 timestamp", "timestamp" in query)
    check("加签正确", "sign" in query and verify_sign(query), str(query.get("sign")))
    check("access_token 保留", query.get("access_token") == ["demo"])
    check("msgtype=text", item["body"].get("msgtype") == "text")
    check("正文正确", "维生系统 35%" in item["body"]["text"]["content"])

    # 关键词补全
    bot2 = DingTalk({"webhook": webhook, "keyword": "监控", "timeout_sec": 5})
    bot2.send_text("库存不足")
    content = received[-1]["body"]["text"]["content"]
    check("自动补关键词", "监控" in content, content.replace("\n", " | "))

    # @人
    bot3 = DingTalk({"webhook": webhook, "at_mobiles": ["13800000000"], "timeout_sec": 5})
    bot3.send_text("hi")
    at = received[-1]["body"].get("at", {})
    check("atMobiles 正确", at.get("atMobiles") == ["13800000000"])

    # 钉钉返回错误码要抛异常
    reply = {"errcode": 310000, "errmsg": "keywords not in content"}
    try:
        bot3.send_text("x")
        check("错误码抛异常", False)
    except DingTalkError as exc:
        check("错误码抛异常", "310000" in str(exc), str(exc))

    # 未配置 webhook
    try:
        DingTalk({"webhook": ""}).send_text("x")
        check("空 webhook 抛异常", False)
    except DingTalkError:
        check("空 webhook 抛异常", True)

    server.shutdown()
    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
