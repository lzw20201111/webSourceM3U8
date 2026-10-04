#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比本地目录与 GitHub 仓库，列出「不一致 / 仅本地有 / 仅远程有」的文件。

用 Git blob sha（sha1("blob <len>\\0" + data)）与仓库 tree 里的 sha 直接比对，精确且省流量。

用法：
    GITHUB_TOKEN=xxx python scripts/diff_remote.py [子目录 ...]
"""

import hashlib
import os
import sys

import requests

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ.get("REPO_OWNER_NAME", "lzw20201111/webSourceM3U8")
BRANCH = "main"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

H = {"Authorization": "Bearer " + TOKEN,
     "Accept": "application/vnd.github+json",
     "X-GitHub-Api-Version": "2022-11-28",
     "User-Agent": "wb-diff"}

# 不参与比对
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".workbuddy"}
SKIP_EXT = {".log", ".zip", ".pyc"}


def blob_sha(data):
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def tree_files():
    """一次性递归取仓库全部文件 path -> sha"""
    r = requests.get(
        "https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (REPO, BRANCH),
        headers=H, timeout=60)
    r.raise_for_status()
    j = r.json()
    if j.get("truncated"):
        print("警告：仓库文件过多，tree 被截断")
    return {e["path"]: e["sha"] for e in j["tree"] if e["type"] == "blob"}


def main():
    subs = sys.argv[1:] or [""]
    remote = tree_files()
    print("仓库文件数：%d\n" % len(remote))

    seen = set()
    diff, only_local = [], []
    for sub in subs:
        base = os.path.join(ROOT, sub) if sub else ROOT
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1] in SKIP_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                if rel in seen:
                    continue
                seen.add(rel)
                data = open(full, "rb").read()
                s = blob_sha(data)
                if rel not in remote:
                    only_local.append((rel, len(data)))
                elif remote[rel] != s:
                    diff.append((rel, len(data)))

    only_remote = [p for p in remote if p not in seen]

    print("== 内容不一致（需推送）==")
    for p, n in sorted(diff):
        print("  M %-46s %8d B" % (p, n))
    print("\n== 仅本地有 ==")
    for p, n in sorted(only_local):
        print("  + %-46s %8d B" % (p, n))
    print("\n== 仅远程有 ==")
    for p in sorted(only_remote):
        print("  - %s" % p)
    print("\n合计：%d 处不一致 / %d 仅本地 / %d 仅远程"
          % (len(diff), len(only_local), len(only_remote)))


if __name__ == "__main__":
    main()
