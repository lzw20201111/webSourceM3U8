#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webview.txt → 静态播放源 · 全量抓取器
=====================================
webview.txt 里 377 条是「网页源」（webview:// 后跟一个播放页地址），
普通播放器放不了。本脚本用无头浏览器逐条打开播放页、点击播放，
从真实网络请求里抓出该频道实际的 HLS（m3u8）地址，并逐条验证可拉流。

产出：
  channels.json        —— 抓取结果（供 Cloudflare Pages 中转读取，生成静态地址）
  webview_static.m3u   —— 静态播放源清单（每条的地址都指向上面的中转，永久不变）
  harvest_result.json  —— 原始抓取结果（含失败原因，便于复查）

环境变量：
  CONCURRENCY   并发浏览器数，默认 5
  ONLY_GROUP    只跑指定分类（调试用）
  LIMIT         只跑前 N 条（调试用）
  PW_CHANNEL    浏览器通道（本机调试用 chrome；Actions 留空）
  REGEN=1       不重抓，只按最新规则重算 id 并重写产物
  RESUME=1      沿用上次成功结果，只重跑失败的
  KEEP_PREV=1   本轮抓失败的条目，若上轮成功过则沿用上轮地址（防止抓取环境变差时丢频道）
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SRC_TXT = os.path.join(ROOT, "webview.txt")
OUT_JSON = os.path.join(ROOT, "channels.json")
OUT_M3U = os.path.join(ROOT, "webview_static.m3u")
RAW_JSON = os.path.join(ROOT, "harvest_result.json")

# 中转服务地址：静态播放源全部指向它（永久不变）
RELAY_BASE = (os.environ.get("RELAY_BASE")
              or "https://xinyi-relay.pages.dev").rstrip("/")

CONCURRENCY = int(os.environ.get("CONCURRENCY") or 5)
ONLY_GROUP = os.environ.get("ONLY_GROUP") or ""
LIMIT = int(os.environ.get("LIMIT") or 0)
PW_CHANNEL = os.environ.get("PW_CHANNEL") or None
KEEP_PREV = bool(os.environ.get("KEEP_PREV"))

# 上一轮的成功结果（KEEP_PREV=1 时用于兜底）
PREV_MAP = {}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

HLS_RE = re.compile(r"\.m3u8(?:[?#]|$)", re.I)
# 带这些参数的地址是临时签名地址（会过期），优先级最低
TOKEN_HINT = re.compile(
    r"(auth_key|auth_key=|token=|txsecret=|txtime=|ysign=|ytime=|"
    r"expire|expires|sign=|signature|_t=|e=|es=|key=|wskey|md5|hmac)",
    re.I)

CLICK_SELS = [
    "video", "source",
    ".vjs-big-play-button", ".xgplayer-start", ".dplayer-play-icon",
    "[class*=play-btn]", "[class*=playBtn]", "[class*=play_btn]",
    "[class*=start-play]", "[class*=startPlay]", "#player",
    "[class*=player]", "[class*=Player]",
]


# ------------------------------------------------------------------ 解析

def parse_webview(path):
    """解析 webview.txt → [(group, name, page_url)]"""
    items = []
    group = "未分类"
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if line.endswith("#genre#"):
                group = line[:-len("#genre#")].rstrip(", ").strip() or group
                continue
            if "," in line:
                name, url = line.split(",", 1)
            else:
                # 形如「郫都新闻综合https://www.cditv.cn/show/4844-580.html」（缺逗号）
                m = re.search(r"https?://", line)
                if not m:
                    continue
                name, url = line[:m.start()], line[m.start():]
            name = name.strip().rstrip(",，")
            url = url.strip().strip('"').strip("'")
            if url.lower().startswith("webview://"):
                url = url[len("webview://"):]
            url = url.rstrip("#")
            if not re.match(r"^https?://", url, re.I):
                continue
            if not name:
                continue
            items.append((group, name, url))
    return items


def slug(group, name, url, used):
    """生成稳定的频道 id（同一频道每次跑出来要一样）"""
    import hashlib
    base = re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-").lower()
    if not re.search(r"[a-z]", base):
        # 纯数字 / 纯中文名：用名字+地址的短哈希，避免出现 "2"、"3" 这种 id
        h = hashlib.md5((group + "|" + name + "|" + url).encode("utf-8")).hexdigest()[:6]
        base = ("ch-" + base + "-" + h).strip("-") if base else "ch-" + h
    base = base[:40].strip("-") or "ch"
    sid = base
    n = 2
    while sid in used:
        sid = "%s-%d" % (base, n)
        n += 1
    used.add(sid)
    return sid


# ------------------------------------------------------------------ 抓取

def rank(urls):
    """给候选地址排序：优先无签名的、地址更短的"""
    def score(u):
        s = 0
        if TOKEN_HINT.search(u):
            s += 100
        s += min(len(u) // 20, 20)
        # 频道专属路径比通用路径优先
        return s
    return sorted(set(urls), key=score)


async def harvest_one(ctx, page_url, wait_s):
    """打开播放页，抓取 m3u8 候选地址"""
    found = []
    page = await ctx.new_page()

    def on_req(req):
        u = req.url
        if HLS_RE.search(u.split("#")[0]) and u not in found:
            found.append(u)

    page.on("request", on_req)
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        try:
            await page.close()
        except Exception:
            pass
        return found, "goto:%s" % type(e).__name__

    # 点击常见播放按钮
    for sel in CLICK_SELS:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click(timeout=1500)
                break
        except Exception:
            continue

    deadline = time.time() + wait_s
    while time.time() < deadline:
        await asyncio.sleep(0.6)
        if found:
            # 已经抓到就再多等一会儿，收集完整候选（master + variant）
            await asyncio.sleep(2.0)
            break

    # 兜底：直接从 DOM 里找
    try:
        srcs = await page.eval_on_selector_all(
            "video,source,iframe",
            "els=>els.map(e=>e.src||e.currentSrc||'').filter(Boolean)")
        for s in srcs:
            if HLS_RE.search(s.split("#")[0]) and s not in found:
                found.append(s)
    except Exception:
        pass

    # 再兜底：滚动 / 触发一次交互（部分页面需要）
    if not found:
        try:
            await page.mouse.click(640, 400)
        except Exception:
            pass
        left = wait_s * 0.5
        end = time.time() + left
        while time.time() < end and not found:
            await asyncio.sleep(0.6)

    try:
        await page.close()
    except Exception:
        pass
    return found, ""


async def validate(url, referer=None):
    """验证地址能否真正拉到 HLS：200 + #EXTM3U + 首个子地址可达"""
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def http(u, timeout=12, ref=None):
        h = {"User-Agent": UA, "Accept": "*/*",
             "Accept-Language": "zh-CN,zh;q=0.9", "Origin": safe_origin(ref or u)}
        if ref:
            h["Referer"] = ref
        req = urllib.request.Request(u, headers=h)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.getcode(), r.read(262144).decode("utf-8", "ignore")

    try:
        code, text = await asyncio.get_event_loop().run_in_executor(
            None, lambda: http(url, ref=referer))
    except Exception as e:
        return False, "拉流失败:%s" % type(e).__name__

    if code != 200:
        return False, "HTTP %s" % code
    if "#EXTM3U" not in text:
        return False, "非 HLS 列表"

    lines = [l.strip() for l in text.splitlines()]
    subs = [l for l in lines if l and not l.startswith("#")]
    if not subs:
        return False, "列表为空"
    if ".m3u8" in subs[0] or (lines and any("#EXT-X-STREAM-INF" in l for l in lines)):
        # master playlist：验证首个子列表
        try:
            from urllib.parse import urljoin
            sub = urljoin(url, subs[0])
            c2, t2 = await asyncio.get_event_loop().run_in_executor(
                None, lambda: http(sub, ref=referer))
            if c2 != 200 or "#EXTM3U" not in t2:
                return False, "子列表不可用"
        except Exception as e:
            return False, "子列表失败:%s" % type(e).__name__
    return True, ""


def safe_origin(u):
    try:
        from urllib.parse import urlparse
        p = urlparse(u)
        return "%s://%s" % (p.scheme, p.netloc)
    except Exception:
        return ""


async def run_one(sem, ctx, item, wait_s):
    gid, name, group, page = item
    async with sem:
        t0 = time.time()
        cands, err = [], ""

        # 本身就已经是 m3u8 的条目：直接验证，不要当网页打开
        if HLS_RE.search(page.split("#")[0]):
            ok, why = await validate(page, page)
            if ok:
                cands = [page]
            else:
                err = why
                try:
                    cands, err2 = await harvest_one(ctx, page, wait_s)
                    err = err2 or err
                except Exception as e:
                    err = "异常:%s" % type(e).__name__
        else:
            try:
                cands, err = await harvest_one(ctx, page, wait_s)
            except Exception as e:
                cands, err = [], "异常:%s" % type(e).__name__

        best = None
        tried = 0
        for u in rank(cands)[:4]:
            tried += 1
            ok, why = await validate(u, page)
            if ok:
                best = (u, why)
                break
            err = err or why
        cost = time.time() - t0
        if best:
            print("[OK  ] %-16s %-22s %.0fs %s"
                  % (group[:16], name[:22], cost, best[0][:80]), flush=True)
            return {
                "id": gid, "name": name, "group": group, "page": page,
                "url": best[0], "ok": True, "cands": len(cands),
                "signed": bool(TOKEN_HINT.search(best[0])), "cost": round(cost, 1),
            }
        print("[MISS] %-16s %-22s %.0fs %s (候选%d) "
              % (group[:16], name[:22], cost, err or "未捕获", len(cands)),
              flush=True)
        return {
            "id": gid, "name": name, "group": group, "page": page,
            "url": "", "ok": False,
            "reason": err or ("捕获到候选但均不可用" if cands else "页面未产生 m3u8 请求"),
            "cost": round(cost, 1),
        }


# ------------------------------------------------------------------ 主流程

async def main():
    if not os.path.exists(SRC_TXT):
        print("缺少 webview.txt")
        sys.exit(1)

    items = parse_webview(SRC_TXT)
    if ONLY_GROUP:
        items = [i for i in items if ONLY_GROUP in i[0]]
    if LIMIT:
        items = items[:LIMIT]

    used = set()
    tasks = [(slug(g, n, u, used), n, g, u) for (g, n, u) in items]

    # REGEN=1：不重抓，只按最新规则重算 id 并重写产物（改命名规则后用）
    if os.environ.get("REGEN"):
        prev = json.load(open(RAW_JSON, encoding="utf-8"))
        by_key = {(t[3], t[1]): t[0] for t in tasks}
        for r in prev:
            r["id"] = by_key.get((r["page"], r["name"]), r["id"])
        save(prev)
        print("已按新规则重算 id 并重写 channels.json / webview_static.m3u")
        return

    prev, prev_ok = load_prev()

    global PREV_MAP
    if KEEP_PREV:
        PREV_MAP = load_prev_map()
        if PREV_MAP:
            print("已载入上轮成功结果 %d 条作为兜底" % len(PREV_MAP), flush=True)

    results = []
    if prev_ok:
        results = [r for r in prev.values()]
        tasks = [t for t in tasks if t[0] not in prev_ok]
        print("沿用上次成功结果 %d 条，本轮只重跑 %d 条\n" % (len(prev_ok), len(tasks)),
              flush=True)

    total = len(tasks)
    print("共 %d 个频道，并发 %d，开始抓取…\n" % (total, CONCURRENCY), flush=True)

    async with async_playwright() as p:
        kwargs = {"headless": True, "args": LAUNCH_ARGS}
        if PW_CHANNEL:
            kwargs["channel"] = PW_CHANNEL
        browser = await p.chromium.launch(**kwargs)
        ctx = await browser.new_context(
            user_agent=UA, locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True)
        # 拦截弹窗/下载，避免卡住
        sem = asyncio.Semaphore(CONCURRENCY)
        done = 0

        async def wrap(t):
            nonlocal done
            r = await run_one(sem, ctx, t, 22)
            results.append(r)
            done += 1
            if done % 10 == 0 or done == total:
                ok = sum(1 for x in results if x["ok"])
                print("---- 进度 %d/%d  成功 %d ----" % (done, total, ok), flush=True)
                save(results)
            return r

        await asyncio.gather(*[wrap(t) for t in tasks])
        await browser.close()

    save(results)
    print("\n完成：成功 %d / %d" % (sum(1 for r in results if r["ok"]), total))
    print("结果：%s" % OUT_JSON)


def load_prev():
    """RESUME=1 时载入上次结果，成功的直接沿用，只重跑失败的"""
    if not os.environ.get("RESUME"):
        return {}, set()
    if not os.path.exists(RAW_JSON):
        return {}, set()
    try:
        prev = json.load(open(RAW_JSON, encoding="utf-8"))
    except Exception:
        return {}, set()
    keep = {r["id"]: r for r in prev if r.get("ok")}
    return keep, set(keep)


def load_prev_map():
    """载入上一轮全部「成功」条目，供本轮失败时兜底沿用"""
    if not os.path.exists(RAW_JSON):
        return {}
    try:
        prev = json.load(open(RAW_JSON, encoding="utf-8"))
    except Exception:
        return {}
    return {r["id"]: r for r in prev if r.get("ok")}


def rescue(results):
    """本轮抓失败的条目，若上轮成功过则沿用上轮地址（标记 stale）"""
    n = 0
    for r in results:
        if r.get("ok") or r.get("stale"):
            continue
        p = PREV_MAP.get(r.get("id"))
        if not p:
            continue
        r["url"] = p["url"]
        r["signed"] = p.get("signed")
        r["ok"] = True
        r["stale"] = 1
        r["reason"] = ""
        n += 1
    return n


def save(results):
    if KEEP_PREV and PREV_MAP:
        rescue(results)

    ok_all = [r for r in results if r["ok"]]
    order = {r["id"]: i for i, r in enumerate(results)}
    ok_all.sort(key=lambda r: order[r["id"]])

    # 同名同分类只保留第一个成功的（原始清单里有重复项）
    seen, ok = set(), []
    for r in ok_all:
        k = (r["group"], r["name"])
        if k in seen:
            continue
        seen.add(k)
        ok.append(r)

    payload = {
        "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S +0800"),
        "total": len(results), "ok": len(ok),
        "channels": {r["id"]: {"n": r["name"], "g": r["group"],
                               "p": r["page"], "u": r["url"],
                               "s": 1 if r.get("signed") else 0,
                               "st": 1 if r.get("stale") else 0}
                     for r in ok},
        "failed": [{"n": r["name"], "g": r["group"], "p": r["page"],
                    "why": r.get("reason", "")}
                   for r in results if not r["ok"]],
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    # 静态播放源清单
    lines = [
        "#EXTM3U",
        "# 静态播放源 · 由 webview.txt 全量转换（共 %d 条可用 / %d 条）"
        % (len(ok), len(results)),
        "# 更新：%s" % payload["time"],
        "# 每条地址永久不变，由中转实时解析当前有效流，导入一次即可。",
    ]
    last_g = None
    for r in ok:
        if r["group"] != last_g:
            lines.append("")
            last_g = r["group"]
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="%s",%s'
                     % (r["id"], r["name"], r["group"], r["name"]))
        lines.append("%s/ch/%s.m3u8" % (RELAY_BASE, r["id"]))
    lines.append("")
    with open(OUT_M3U, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
