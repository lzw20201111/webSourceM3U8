#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒综合广播 · 直播链路巡检（供 GitHub Actions 定时调用）
==============================================================
每天 07:30 / 17:30（北京时间）检查以下环节，结果写入 status.json 供网页展示：

  1. page   —— 原始直播页是否可访问
  2. pages  —— GitHub Pages 上的收听页是否正常
  3. m3u    —— 直播源文件是否存在、格式是否正确（含直链项）
  4. direct —— 文件中的「直链」当前能否真正拉到 HLS 播放列表（最关键）

背景：该台播放地址与「活跃播放会话」绑定。会话由 keepalive 工作流维持，
本巡检用于第一时间发现链路异常（例如会话中断、平台改版等）。

退出码：0=全部正常  1=存在异常（Actions 中会标红）
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_FILE = os.path.join(ROOT, "status.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

PAGE_URL = "https://live.xytv.cc/radio/223?uin=1629&refererId=0"
INDEX_URL = "https://lzw20201111.github.io/webSourceM3U8/"
M3U_URL = "https://lzw20201111.github.io/webSourceM3U8/xinyi_radio_local.m3u"


def fetch(url, timeout=20, extra_headers=None):
    """返回 (状态, 文本)；异常时状态为 'fail:xxx'"""
    headers = {"User-Agent": UA, "Cache-Control": "no-cache"}
    headers.update(extra_headers or {})
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok", r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return "fail:HTTP %s" % e.code, ""
    except Exception as e:
        return "fail:%s" % type(e).__name__, ""


def check(url, expect_text, name, extra_headers=None):
    status, body = fetch(url, extra_headers=extra_headers)
    if status != "ok":
        return status
    if expect_text and expect_text not in body:
        return "fail:内容异常(%r)" % body[:40]
    return "ok"


def main():
    results = {}

    results["page"] = check(PAGE_URL, "<div", "直播页")
    results["pages"] = check(INDEX_URL, "信宜", "收听页")

    # 直播源文件 + 直链可播性（加时间戳绕过 Pages/CDN 缓存）
    status, m3u_text = fetch(M3U_URL + "?t=%d" % int(time.time()))
    if status != "ok":
        results["m3u"] = status
        results["direct"] = "fail:无法读取源文件"
    elif "#EXTM3U" not in m3u_text:
        results["m3u"] = "fail:格式异常"
        results["direct"] = "fail:无有效直链"
    else:
        results["m3u"] = "ok"
        direct = ""
        for line in m3u_text.splitlines():
            line = line.strip()
            if line.lower().startswith(("http://", "https://")):
                direct = line
                break
        if not direct:
            results["direct"] = "fail:未找到直链"
        else:
            # 真正拉一次播放列表，确认地址可用（失败重试一次，避免会话切换瞬间误报）
            s, body = fetch(direct, timeout=20)
            if not (s == "ok" and "#EXTM3U" in body):
                time.sleep(6)
                s, body = fetch(direct, timeout=20)
            results["direct"] = "ok" if (s == "ok" and "#EXTM3U" in body) else (
                s if s != "ok" else "fail:返回内容异常")

    results["time"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    keys = [k for k in results if k != "time"]
    all_ok = all(results[k] == "ok" for k in keys)

    with open(STATUS_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(json.dumps(results, ensure_ascii=False, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("success=%s\n" % ("true" if all_ok else "false"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
