#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增量刷新：先快速校验现有频道地址，只对【失效的】重新跑浏览器抓取。

相比全量重抓（417 条 / 30-40 分钟），日常只重抓几十条，几分钟就能完成。
这是【必须在国内 IP 上跑】的刷新入口 —— 海外 IP 会拿到海外 CDN 线路，国内放不了。

用法：
    PW_CHANNEL=chrome python scripts/refresh_webview.py            # 校验 + 重抓失效的
    PW_CHANNEL=chrome python scripts/refresh_webview.py --dry      # 只校验，不重抓
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests
from requests.utils import requote_uri

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH = os.path.join(ROOT, "channels.json")
PY = sys.executable or "python"

DRY = "--dry" in sys.argv
WORKERS = int(os.environ.get("CHECK_WORKERS") or 16)

S = requests.Session()
S.trust_env = False
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


def get(url, referer="", timeout=15):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    # 部分源地址带中文/非法字符，直接请求会抛 UnicodeEncodeError
    return S.get(requote_uri(url), headers=h, timeout=timeout)


def first_uri(text):
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def verify_one(item):
    cid, url, page = item
    try:
        cur = url
        for _ in range(3):
            r = get(cur, referer=page)
            if r.status_code != 200 or "#EXTM3U" not in r.text:
                return cid, False, "列表%d" % r.status_code
            nxt = first_uri(r.text)
            if not nxt:
                return cid, False, "无子项"
            nxt = urljoin(cur, nxt)
            if "#EXT-X-STREAM-INF" in r.text:
                cur = nxt          # master：继续下钻子列表
                continue
            r2 = get(nxt, referer=page, timeout=20)
            n = len(r2.content) if r2.content else 0
            if r2.status_code == 200 and n > 800:
                return cid, True, ""
            return cid, False, "分片%d/%dB" % (r2.status_code, n)
        return cid, False, "层级过深"
    except Exception as e:
        return cid, False, "异常:" + type(e).__name__


def main():
    d = json.load(open(CH, encoding="utf-8"))
    chans = d["channels"]
    items = [(cid, v["u"], v.get("p") or "") for cid, v in chans.items()]
    print("现有 %d 条，并发 %d 校验中…\n" % (len(items), WORKERS), flush=True)

    t0 = time.time()
    bad = []
    okn = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for cid, good, why in ex.map(verify_one, items):
            if good:
                okn += 1
            else:
                bad.append(cid)
                print("  ✗ %-22s %s" % (cid, why), flush=True)

    print("\n有效 %d / %d，失效 %d（耗时 %.0fs）"
          % (okn, len(items), len(bad), time.time() - t0), flush=True)

    if not bad:
        print("全部有效，无需重抓")
        return
    if DRY:
        print("--dry 模式：不重抓。失效 id：")
        print(",".join(bad))
        return

    env = dict(os.environ)
    env["ONLY_IDS"] = ",".join(bad)
    env["KEEP_PREV"] = "1"
    env.setdefault("CONCURRENCY", "6")
    print("\n开始重抓这 %d 条…\n" % len(bad), flush=True)
    r = subprocess.run([PY, os.path.join(ROOT, "scripts", "harvest_webview.py")],
                       cwd=ROOT, env=env)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
