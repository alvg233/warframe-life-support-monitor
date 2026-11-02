"""创建 GitHub / Gitee 发行版并上传安装包。

令牌来源（按优先级）：
  1. 环境变量 GITHUB_TOKEN / GITEE_TOKEN
  2. 本机文件 %USERPROFILE%\\.dsh_tokens.json  或  <项目根>/.tokens.json
     {"github": "...", "gitee": "..."}

令牌只在内存中使用，不打印、不写入仓库。

用法：
    python tools/publish_release.py --tag v1.9.0 --asset release/Warframe-LifeSupport-Monitor-Setup-1.9.0.exe
    python tools/publish_release.py --tag v1.9.0 --asset ... --only github
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
GITHUB_REPO = "alvg233/warframe-life-support-monitor"
GITEE_REPO = "alvg233/warframe-life-support-monitor"
API_GH = "https://api.github.com"
API_GT = "https://gitee.com/api/v5"


def load_tokens() -> dict:
    tokens = {}
    if os.environ.get("GITHUB_TOKEN"):
        tokens["github"] = os.environ["GITHUB_TOKEN"]
    if os.environ.get("GITEE_TOKEN"):
        tokens["gitee"] = os.environ["GITEE_TOKEN"]
    if "github" in tokens and "gitee" in tokens:
        return tokens
    for path in (pathlib.Path.home() / ".dsh_tokens.json", ROOT / ".tokens.json"):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for key in ("github", "gitee"):
                    if data.get(key) and key not in tokens:
                        tokens[key] = str(data[key]).strip()
            except Exception as exc:
                print(f"读取 {path} 失败：{exc}")
    return tokens


def read_body(path: pathlib.Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def github_release(token: str, tag: str, name: str, body: str,
                   asset: pathlib.Path | None) -> str:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    resp = requests.get(f"{API_GH}/repos/{GITHUB_REPO}/releases/tags/{tag}",
                        headers=headers, timeout=30)
    if resp.status_code == 200:
        release = resp.json()
        print(f"GitHub 已存在发行版：{release['html_url']}")
    else:
        payload = {"tag_name": tag, "name": name, "body": body,
                   "draft": False, "prerelease": False}
        resp = requests.post(f"{API_GH}/repos/{GITHUB_REPO}/releases",
                             headers=headers, json=payload, timeout=60)
        if resp.status_code >= 300:
            raise SystemExit(f"GitHub 创建失败 {resp.status_code}: {resp.text[:300]}")
        release = resp.json()
        print(f"GitHub 已创建发行版：{release['html_url']}")

    if asset and asset.exists():
        upload_url = release["upload_url"].split("{")[0]
        with asset.open("rb") as fh:
            resp = requests.post(
                f"{upload_url}?name={requests.utils.quote(asset.name)}",
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=fh, timeout=600)
        if resp.status_code >= 300:
            print(f"GitHub 上传附件失败 {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"GitHub 已上传附件：{asset.name}")
    return release["html_url"]


def gitee_release(token: str, tag: str, name: str, body: str,
                  asset: pathlib.Path | None) -> str:
    auth = {"access_token": token}
    resp = requests.get(f"{API_GT}/repos/{GITEE_REPO}/releases/tags/{tag}",
                        params=auth, timeout=30)
    if resp.status_code == 200:
        release = resp.json()
        print(f"Gitee 已存在发行版：{release.get('html_url')}")
    else:
        payload = {"tag_name": tag, "name": name, "body": body,
                   "prerelease": False, **auth}
        resp = requests.post(f"{API_GT}/repos/{GITEE_REPO}/releases",
                             data=payload, timeout=60)
        if resp.status_code >= 300:
            raise SystemExit(f"Gitee 创建失败 {resp.status_code}: {resp.text[:300]}")
        release = resp.json()
        print(f"Gitee 已创建发行版：{release.get('html_url')}")

    release_id = release.get("id")
    if asset and asset.exists() and release_id:
        with asset.open("rb") as fh:
            resp = requests.post(
                f"{API_GT}/repos/{GITEE_REPO}/releases/{release_id}/attach_files",
                params=auth, files={"file": (asset.name, fh, "application/octet-stream")},
                timeout=600)
        if resp.status_code >= 300:
            print(f"Gitee 上传附件失败 {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"Gitee 已上传附件：{asset.name}")
    return str(release.get("html_url") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v1.9.0")
    parser.add_argument("--name", default=None)
    parser.add_argument("--body", default="docs/RELEASE_NOTES_1.9.0.md")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--only", choices=["github", "gitee"], default=None)
    args = parser.parse_args()

    tokens = load_tokens()
    name = args.name or f"Warframe 生存任务监测 {args.tag}"
    body = read_body(ROOT / args.body)
    asset = (ROOT / args.asset) if args.asset else None

    if asset and not asset.exists():
        print(f"附件不存在：{asset}")
        return 2

    done = 0
    if args.only in (None, "github"):
        if tokens.get("github"):
            github_release(tokens["github"], args.tag, name, body, asset)
            done += 1
        else:
            print("缺少 GitHub 令牌：设置环境变量 GITHUB_TOKEN 或写入 .tokens.json")
    if args.only in (None, "gitee"):
        if tokens.get("gitee"):
            gitee_release(tokens["gitee"], args.tag, name, body, asset)
            done += 1
        else:
            print("缺少 Gitee 令牌：设置环境变量 GITEE_TOKEN 或写入 .tokens.json")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
