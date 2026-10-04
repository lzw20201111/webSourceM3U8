#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒综合广播直播链路巡检脚本（供 GitHub Actions 定时调用）
================================================================
每天 07:30 / 17:30（北京时间）检查一次直播链路各环节是否正常，
并把结果写入 status.json（网页会读取并展示最近巡检状态）。

为什么只做巡检、不再抓取播放地址：
  经实测验证，该平台 CDN 的播放签名与「签发来源 IP/地区」绑定——
  海外服务器签发的地址在国内一律 403，反之亦然。
  因此定时抓取固定地址的方案不可行；网页已改为在访客浏览器内
  直接建立播放会话（永远有效），本脚本只需保证链路健康即可。

退出码：0=全部正常  1=存在异常（Actions 中会标红）
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_FILE = os.path.join(ROOT, "status.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

PAGE_URL = "https://live.xytv.cc/radio/223?uin=1629&refererId=0"
API_URL = ("https://1812501212048408.cn-hangzhou.fc.aliyuncs.com/2016-08-15/"
           "proxy/node-api.online/node-api/radio/channelInfo?id=223&uin=1629")
PAGES_M3U = "https://lzw20201111.github.io/webSourceM3U8/xinyi_radio_local.m3u"


def check(name, url, headers=None, expect_text=None):
    """返回 'ok' 或 'fail:原因'"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   **(headers or {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(4096).decode("utf-8", "ignore")
            if expect_text and expect_text not in body:
                return f"fail:响应内容异常({body[:40]!r})"
            return "ok"
    except urllib.error.HTTPError as e:
        return f"fail:HTTP {e.code}"
    except Exception as e:
        return f"fail:{type(e).__name__}"


def main():
    results = {
        "page":  check("直播页", PAGE_URL, expect_text="<div"),
        "api":   check("频道接口", API_URL,
                       headers={"Origin": "https://live.xytv.cc",
                                "Referer": "https://live.xytv.cc/"},
                       expect_text='"code":200'),
        "pages": check("Pages静态文件", PAGES_M3U, expect_text="#EXTM3U"),
    }
    results["time"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    all_ok = all(v == "ok" for k, v in results.items() if k != "time")

    with open(STATUS_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(json.dumps(results, ensure_ascii=False, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"success={'true' if all_ok else 'false'}\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
