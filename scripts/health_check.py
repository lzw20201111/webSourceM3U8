#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒（综合广播 + 综合电视台）· 直播链路巡检
==============================================================
每天 07:30 / 17:30（北京时间）检查以下环节，结果写入 status.json 供网页展示：

  page     —— 广播直播页是否可访问
  tvpage   —— 电视直播页是否可访问
  pages    —— GitHub Pages 上的网页版是否正常
  m3u      —— 直连版文件（xinyi_radio_local.m3u）是否存在、格式是否正确
  webm3u   —— 网页版文件（xinyi_web.m3u）是否存在、是否含 webview 项
  direct   —— 广播「直连」当前能否真正拉到 HLS 播放列表（最关键）
  tvdirect —— 电视「直连」当前能否真正拉到 HLS 播放列表（最关键）

背景：该平台播放地址与「活跃播放会话」绑定。会话由 keepalive 工作流维持，
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
TV_PAGE_URL = "https://live.xytv.cc/tv/643?uin=1629&refererId=0"
BASE = "https://lzw20201111.github.io/webSourceM3U8/"
DIRECT_M3U_URL = BASE + "xinyi_radio_local.m3u"
WEB_M3U_URL = BASE + "xinyi_web.m3u"

# status.json 中的直连字段 -> m3u 里的 tvg-id
DIRECT_KEYS = [("direct", "xinyi-radio"), ("tvdirect", "xinyi-tv")]


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


def check(url, expect_text):
    status, body = fetch(url)
    if status != "ok":
        return status
    if expect_text and expect_text not in body:
        return "fail:内容异常(%r)" % body[:40]
    return "ok"


def parse_m3u(text):
    """解析出 tvg-id -> 直链（只取 http(s) 行，跳过 webview://）"""
    out = {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF") and 'tvg-id="' in line:
            cur = line.split('tvg-id="', 1)[1].split('"', 1)[0]
        elif line and not line.startswith("#") and cur:
            if line.lower().startswith(("http://", "https://")):
                out[cur] = line
            cur = None
    return out


def probe_stream(url):
    """真正拉一次播放列表，确认地址可用（失败重试一次，避免会话切换瞬间误报）"""
    s, body = fetch(url, timeout=20)
    if not (s == "ok" and "#EXTM3U" in body):
        time.sleep(6)
        s, body = fetch(url, timeout=20)
    if s == "ok" and "#EXTM3U" in body:
        return "ok"
    return s if s != "ok" else "fail:返回内容异常"


def main():
    results = {}

    results["page"] = check(PAGE_URL, "<div")
    results["tvpage"] = check(TV_PAGE_URL, "<div")
    results["pages"] = check(BASE, "信宜")

    stamp = "?t=%d" % int(time.time())

    # 直连版文件 + 两条直链的可播性（加时间戳绕过 Pages/CDN 缓存）
    status, m3u_text = fetch(DIRECT_M3U_URL + stamp)
    if status != "ok":
        results["m3u"] = status
        for key, _ in DIRECT_KEYS:
            results[key] = "fail:无法读取源文件"
    elif "#EXTM3U" not in m3u_text:
        results["m3u"] = "fail:格式异常"
        for key, _ in DIRECT_KEYS:
            results[key] = "fail:无有效直链"
    else:
        results["m3u"] = "ok"
        srcs = parse_m3u(m3u_text)
        for key, cid in DIRECT_KEYS:
            url = srcs.get(cid)
            results[key] = probe_stream(url) if url else "fail:未找到直链"

    # 网页版文件：存在且含 webview 项即可（不需要额外保活）
    wstatus, wtext = fetch(WEB_M3U_URL + stamp)
    if wstatus != "ok":
        results["webm3u"] = wstatus
    elif "#EXTM3U" not in wtext:
        results["webm3u"] = "fail:格式异常"
    elif wtext.count("webview://") < 2:
        results["webm3u"] = "fail:webview 项不足"
    else:
        results["webm3u"] = "ok"

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
