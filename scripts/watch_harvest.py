#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询 GitHub Actions 抓取任务直到全部跑完，再拉最终 channels.json 做统计。

用法：
    GITHUB_TOKEN=xxx python scripts/watch_harvest.py
"""

import json
import os
import time
from collections import Counter

import requests

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ.get("REPO_OWNER_NAME", "lzw20201111/webSourceM3U8")
API = "https://api.github.com/repos/" + REPO
KEY = "静态播放源"
RAW = "https://raw.githubusercontent.com/%s/main" % REPO

H = {
    "Authorization": "Bearer " + TOKEN,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "wb-watch",
}


def runs():
    r = requests.get(API + "/actions/runs", headers=H,
                     params={"per_page": 10}, timeout=30)
    r.raise_for_status()
    return [x for x in r.json()["workflow_runs"] if KEY in x["name"]]


def main():
    last = None
    for i in range(120):
        rs = runs()
        act = [x for x in rs if x["status"] != "completed"]
        line = " ".join("#%s(%s)" % (x["run_number"], x["status"]) for x in rs[:3])
        if line != last:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), line), flush=True)
            last = line
        if not act:
            print("全部结束", flush=True)
            break
        time.sleep(60)

    time.sleep(8)
    for name in ("harvest_result.json", "channels.json"):
        try:
            d = requests.get(RAW + "/" + name + "?t=%d" % time.time(),
                             timeout=40).json()
        except Exception as e:
            print("读取 %s 失败：%s" % (name, e))
            continue
        if name == "channels.json":
            stale = sum(1 for v in d["channels"].values() if v.get("st"))
            print("\n== channels.json ==")
            print("更新：%s" % d["time"])
            print("可用：%d / %d（沿用上轮 %d）" % (d["ok"], d["total"], stale))
            c = Counter(v["g"] for v in d["channels"].values())
            print("分类：" + "、".join("%s%d" % kv for kv in c.most_common(10)))
            print("前 12 条失败原因：")
            for f in d.get("failed", [])[:12]:
                print("   %-16s %-6s %s" % (f["n"], f["g"], f["why"][:44]))
        else:
            ok = [r for r in d if r.get("ok")]
            print("\n== harvest_result.json ==")
            print("原始条数 %d，本轮成功 %d（含沿用 %d）"
                  % (len(d), len(ok), sum(1 for r in ok if r.get("stale"))))


if __name__ == "__main__":
    main()
