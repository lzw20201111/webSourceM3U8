#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽样验证中转服务：/ch/<id>.m3u8 能否返回播放列表、首个分片能否真正拉到。

用法：
    python scripts/verify_relay.py [抽样条数，默认 20]
"""

import random
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://xinyi-relay.pages.dev"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20

S = requests.Session()
S.trust_env = False          # 不走系统代理


def get(url, timeout=20, **kw):
    return S.get(url, timeout=timeout, **kw)


def check(cid):
    try:
        r = get("%s/ch/%s.m3u8" % (BASE, cid))
        if r.status_code != 200 or "#EXTM3U" not in r.text:
            return cid, "列表 %s" % r.status_code, ""
        seg = next((l.strip() for l in r.text.splitlines()
                    if l.strip().startswith("http")), "")
        if not seg:
            return cid, "无分片地址", ""
        host = seg.split("//")[-1].split("/")[0][:32]
        r2 = get(seg, timeout=25, headers={"Referer": seg.split("?")[0]})
        n = len(r2.content) if r2.content else 0
        if r2.status_code == 200 and n > 1000:
            return cid, "可播(%dKB)" % (n // 1024), host
        return cid, "分片 %s" % r2.status_code, host
    except Exception as e:
        return cid, "异常:" + type(e).__name__, ""


def main():
    allc = get(BASE + "/webview.m3u", timeout=30)
    ids = [l.strip().split("/ch/")[1].replace(".m3u8", "")
           for l in allc.text.splitlines() if "/ch/" in l]
    print("清单共 %d 条，抽样 %d 条验证…\n" % (len(ids), min(N, len(ids))))
    sample = random.sample(ids, min(N, len(ids)))

    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for cid, why, host in ex.map(check, sample):
            mark = "✓" if why.startswith("可播") else "✗"
            if mark == "✓":
                ok += 1
            print("  %s %-22s %-14s %s" % (mark, cid, why, host))
    print("\n可播 %d / %d" % (ok, len(sample)))


if __name__ == "__main__":
    main()
