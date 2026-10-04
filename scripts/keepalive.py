#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒（综合广播 FM98.9 + 综合电视台）· 直播直链保活
=========================================================
实测结论（本项目的关键发现）：
  1. 该平台（广电云）的播放地址与「活跃播放会话」绑定：
       - 会话存活期间，地址在任意网络/设备（含国内）都能正常拉流；
       - 会话一断（浏览器退出），约 30~70 秒后地址即 403；
       - 用纯 HTTP 反复请求播放列表【无法】续命，必须有真实播放会话。
  2. 多个播放会话可以并存、互不影响：广播与电视可同时保活，也可重叠冗余。
  3. 因此：只要每个频道各有一个持续运行的浏览器在播放，其地址就长期可用。

本脚本做的事：
  在一个长任务（默认约 5.5 小时）里，为每个频道各维持一个无头 Chrome 播放会话，
  启动时把当前有效地址写入 xinyi_radio_local.m3u，期间地址变化则同步更新。
  配合 workflow 每 3 小时触发一次，多个任务互相重叠，使直链长期可用。

实现说明：各频道用 asyncio 并发（Playwright 同步 API 不支持多线程，
  见 https://playwright.dev/python/docs/library#threading ），每个频道一个独立浏览器。

环境变量：
  RUN_MINUTES   本次任务运行时长（分钟），默认 330
  PW_CHANNEL    可选，指定浏览器通道（本地调试用 chrome，Actions 留空）
"""

import asyncio
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M3U_FILE = os.path.join(ROOT, "xinyi_radio_local.m3u")

WEB_BASE = "https://lzw20201111.github.io/webSourceM3U8/"

# 频道清单：新增频道只需在这里加一项，其余环节（m3u / 保活 / 巡检）自动覆盖
CHANNELS = [
    {
        "id": "xinyi-radio",
        "page": "https://live.xytv.cc/radio/223?uin=1629&refererId=0",
        "logo": ("https://static-pro.guangdianyun.tv/1629/program/20190102/"
                 "b782f17ee2606a6df9f4de06aa988a6e.jpeg"),
        "name": "信宜融媒综合广播",
        "label": "信宜融媒综合广播 (FM98.9)",
        "web": WEB_BASE,
    },
    {
        "id": "xinyi-tv",
        "page": "https://live.xytv.cc/tv/643?uin=1629&refererId=0",
        "logo": ("https://static-pro.guangdianyun.tv/1629/mss/20241219/"
                 "3b3aa73530526d817a02349a3a323f37.jpeg"),
        "name": "信宜综合电视台",
        "label": "信宜综合电视台",
        "web": WEB_BASE + "tv.html",
    },
]

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "330"))
POLL_SECONDS = 60
PW_CHANNEL = os.environ.get("PW_CHANNEL") or None

STATE = {}          # tvg-id -> 当前有效直链
PUB_LOCK = threading.Lock()
GIT_LOCK = threading.Lock()

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--mute-audio",
]

CLICK_JS = """(function(){
  var e=document.querySelectorAll('.xwH5Player-play,.svg-play,.play-bill-btn,[class*=play]');
  if(e.length){ e[0].click(); }
  var a=document.querySelector('audio,video');
  if(a){ try{ a.play(); }catch(x){} }
  return e.length;
})()"""

GRAB_JS = """(function(){
  var r=performance.getEntriesByType('resource').map(function(e){return e.name})
       .filter(function(n){return n.indexOf('m3u8')>-1});
  return r.length? r[r.length-1] : '';
})()"""


def log(msg):
    print("[%s] %s" % (datetime.now(CST).strftime("%H:%M:%S"), msg), flush=True)


def sh(*args, quiet=False):
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if not quiet:
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if out:
            log("  $ " + out.splitlines()[0][:200])
        if r.returncode != 0 and err:
            log("  ! " + err.splitlines()[-1][:200])
    return r.returncode


def read_existing():
    """从现有 m3u 读取各频道直链（tvg-id -> url），用于启动兜底，避免刷新空档"""
    out = {}
    if not os.path.exists(M3U_FILE):
        return out
    with open(M3U_FILE, encoding="utf-8") as f:
        cur = None
        for raw in f:
            line = raw.strip()
            if line.startswith("#EXTINF") and 'tvg-id="' in line:
                cur = line.split('tvg-id="', 1)[1].split('"', 1)[0]
            elif line and not line.startswith("#") and cur:
                if line.lower().startswith(("http://", "https://")):
                    out[cur] = line
                cur = None
    return out


def build_m3u():
    """生成 m3u：每个频道两项 —— 直连（自动保活）+ 网页版（免维护）"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    lines = [
        "#EXTM3U",
        "# 频道：信宜融媒综合广播 (FM98.9) ＋ 信宜综合电视台",
        "# 更新：%s (北京时间，由 GitHub Actions 自动刷新)" % now,
        "# 说明：每组第 1 项「直连」为当前有效播放地址，云端会话自动保活，",
        "#       播放器直接播放即可；第 2 项「网页版」不需要任何维护。",
    ]
    for ch in CHANNELS:
        key = STATE.get(ch["id"])
        if key:
            lines += [
                '#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" '
                'group-title="广东",%s · 直连'
                % (ch["id"], ch["name"], ch["logo"], ch["label"]),
                key,
            ]
        # 网页版为免维护项，始终保留（即使直链暂时未就绪）
        lines += [
            '#EXTINF:-1 tvg-id="%s-web" tvg-name="%s(网页版)" tvg-logo="%s" '
            'group-title="广东",%s · 网页版' % (ch["id"], ch["name"], ch["logo"],
                                                 ch["label"]),
            "webview://" + ch["web"],
        ]
    lines.append("")
    return "\n".join(lines)


def publish():
    """把当前地址写入 m3u 并推送；内容无变化则跳过（加锁，避免并发写冲突）"""
    with PUB_LOCK:
        content = build_m3u()
        old = ""
        if os.path.exists(M3U_FILE):
            with open(M3U_FILE, encoding="utf-8") as f:
                old = f.read()
        if old == content:
            return False
        with open(M3U_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    with GIT_LOCK:
        sh("git", "config", "user.name", "github-actions[bot]", quiet=True)
        sh("git", "config", "user.email",
           "41898282+github-actions[bot]@users.noreply.github.com", quiet=True)
        sh("git", "add", "xinyi_radio_local.m3u", quiet=True)
        if sh("git", "diff", "--cached", "--quiet", quiet=True) == 0:
            return False
        stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        sh("git", "commit", "-m",
           "chore: 更新信宜融媒直播直链 %s CST" % stamp, quiet=True)
        for attempt in range(3):
            sh("git", "pull", "--rebase", "--autostash", quiet=True)
            if sh("git", "push", quiet=True) == 0:
                return True
            log("推送失败，重试 %d/3" % (attempt + 1))
            time.sleep(5)
        log("推送最终失败（本地文件已更新）")
        return True


async def start_session(p, url):
    """启动浏览器并开始播放，返回 (browser, page, key)"""
    kwargs = {"headless": True, "args": LAUNCH_ARGS}
    if PW_CHANNEL:
        kwargs["channel"] = PW_CHANNEL
    b = await p.chromium.launch(**kwargs)
    ctx = await b.new_context(locale="zh-CN")
    pg = await ctx.new_page()
    await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    await pg.wait_for_timeout(12000)
    await pg.evaluate(CLICK_JS)
    await pg.wait_for_timeout(15000)
    return b, pg, await pg.evaluate(GRAB_JS)


async def monitor(p, ch, deadline):
    """单个频道的保活循环：会话断开即重建，地址变化即发布"""
    cid, name = ch["id"], ch["name"]
    while time.time() < deadline - 300:
        try:
            b, pg, key = await start_session(p, ch["page"])
        except Exception as e:
            log("[%s] 建立播放会话失败：%r，30 秒后重试" % (name, e))
            await asyncio.sleep(30)
            continue

        try:
            if key:
                if STATE.get(cid) != key:
                    STATE[cid] = key
                    publish()
                log("[%s] 当前直链：%s" % (name, key[:100]))
            else:
                log("[%s] 未取到播放地址，稍后重试" % name)

            while time.time() < deadline - 300:
                await asyncio.sleep(POLL_SECONDS)
                try:
                    k = await pg.evaluate(GRAB_JS)
                except Exception as e:
                    log("[%s] 页面异常：%r，重建会话" % (name, e))
                    break
                if not k:
                    log("[%s] 会话疑似失效，重建" % name)
                    break
                if k != STATE.get(cid):
                    STATE[cid] = k
                    publish()
                    log("[%s] 地址已更新：%s" % (name, k[:100]))
        finally:
            try:
                await b.close()
            except Exception:
                pass


async def run():
    deadline = time.time() + RUN_MINUTES * 60
    for cid, url in read_existing().items():
        STATE[cid] = url

    log("任务启动：计划运行 %d 分钟（截止 %s CST），共 %d 个频道"
        % (RUN_MINUTES, datetime.fromtimestamp(deadline, CST).strftime("%H:%M"),
           len(CHANNELS)))

    async with async_playwright() as p:
        await asyncio.gather(*[monitor(p, ch, deadline) for ch in CHANNELS])

    log("任务正常结束")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
