#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地产物一次性推送到 GitHub 仓库（走 REST API，不依赖 git 命令）

背景：本机 `github.com` 的 DNS 时常被拒，git push/clone 会失败，但 `api.github.com` 正常。
      因此统一用 Git Data API：blobs → tree → commit → PATCH ref，一次提交完成，只触发一轮 workflow。

用法：
    GITHUB_TOKEN=xxx python scripts/publish_github.py [文件1 文件2 ...]
    不带参数则推送默认清单。
"""

import base64
import json
import os
import sys
import time

import requests

TOKEN = os.environ.get("GITHUB_TOKEN") or ""
OWNER = os.environ.get("REPO_OWNER", "lzw20201111")
REPO = os.environ.get("REPO_NAME", "webSourceM3U8")
BRANCH = os.environ.get("REPO_BRANCH", "main")
API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

H = {
    "Authorization": "Bearer " + TOKEN,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "wb-publish",
}

DEFAULT_FILES = [
    "channels.json",
    "webview_static.m3u",
    "harvest_result.json",
    "webview.txt",
    "scripts/harvest_webview.py",
    "scripts/local_relay_test.mjs",
    "pages/_worker.js",
    "pages/deploy.py",
    ".github/workflows/webview-harvest.yml",
]


def api(method, path, **kw):
    r = requests.request(method, API + path, headers=H, timeout=90, **kw)
    if r.status_code >= 400:
        raise RuntimeError("%s %s -> %s %s" % (method, path, r.status_code, r.text[:300]))
    return r.json() if r.text.strip() else {}


def main():
    if not TOKEN:
        print("请设置环境变量 GITHUB_TOKEN")
        sys.exit(1)
    names = sys.argv[1:] or DEFAULT_FILES

    ref = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, BRANCH))
    base_sha = ref["object"]["sha"]
    base_commit = api("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_sha))
    base_tree = base_commit["tree"]["sha"]
    print("当前分支 %s -> %s" % (BRANCH, base_sha[:8]))

    tree = []
    for name in names:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            print("  跳过（不存在）：%s" % name)
            continue
        data = open(path, "rb").read()
        blob = api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO),
                   json={"content": base64.b64encode(data).decode(),
                         "encoding": "base64"})
        tree.append({"path": name, "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
        print("  已上传 %-42s %7d 字节" % (name, len(data)))

    if not tree:
        print("没有可推送的文件")
        return

    new_tree = api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO),
                   json={"base_tree": base_tree, "tree": tree})
    # 基于 UTC 再加 8 小时得到北京时间；不能用 localtime（本机已含 +8 偏移会重复相加）
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))
    msg = os.environ.get("COMMIT_MSG") or ("chore: 更新静态播放源（%s CST）" % stamp)
    commit = api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO),
                 json={"message": msg, "tree": new_tree["sha"], "parents": [base_sha]})
    api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, BRANCH),
        json={"sha": commit["sha"]})
    print("\n已提交：%s  %s" % (commit["sha"][:8], msg))
    print("https://github.com/%s/%s/commit/%s" % (OWNER, REPO, commit["sha"]))


if __name__ == "__main__":
    main()
