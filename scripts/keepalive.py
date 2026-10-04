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
  把当前有效地址写入 current.json（供 Cloudflare Worker 中转服务实时读取），
  并按需生成 m3u。配合 workflow 每 3 小时触发一次，多个任务互相重叠，使直链长期可用。

三个产出文件：
  current.json          —— 当前有效直链（中转服务 / 网页读取，随会话自动刷新）
  xinyi_radio_local.m3u —— 直连版
                            · 未配置 RELAY_BASE 时：写真实 m3u8 地址（动态刷新）
                            · 配置了 RELAY_BASE 后：写中转的【静态地址】，永久不变
  xinyi_web.m3u         —— 网页版：webview:// 打开网页播放（完全免维护）

中转服务（让 m3u 里的播放地址永久不变）：
  部署在 Cloudflare Pages 上（见 pages/_worker.js 与 pages/deploy.py）。
  为什么是 Pages 而不是 Workers：*.workers.dev 在国内被 DNS 污染 + SNI 阻断，
  而 *.pages.dev 国内直连正常（实测 HTTP 200）。Pages 免费、无需域名。

环境变量：
  RUN_MINUTES   本次任务运行时长（分钟），默认 330
  RELAY_BASE    中转服务地址（如 https://xinyi-relay.pages.dev）；
                配置后直连版 m3u 转为静态地址，不再随会话刷新
  PW_CHANNEL    可选，指定浏览器通道（本地调试用 chrome，Actions 留空）
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 输出文件
M3U_DIRECT = os.path.join(ROOT, "xinyi_radio_local.m3u")   # 直连版
M3U_WEB = os.path.join(ROOT, "xinyi_web.m3u")              # 网页版
JSON_FILE = os.path.join(ROOT, "current.json")             # 当前有效直链（中转服务用）

# 中转服务地址；配置后「直连版」转为永久不变的静态地址
RELAY_BASE = (os.environ.get("RELAY_BASE") or "").rstrip("/")

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
        "relay": "/xinyi/radio.m3u8",
    },
    {
        "id": "xinyi-tv",
        "page": "https://live.xytv.cc/tv/643?uin=1629&refererId=0",
        "logo": ("https://static-pro.guangdianyun.tv/1629/mss/20241219/"
                 "3b3aa73530526d817a02349a3a323f37.jpeg"),
        "name": "信宜综合电视台",
        "label": "信宜综合电视台",
        "web": WEB_BASE + "tv.html",
        "relay": "/xinyi/tv.m3u8",
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
    """从现有直连版 m3u 读取各频道直链（tvg-id -> url），用于启动兜底，避免刷新空档"""
    out = {}
    if not os.path.exists(M3U_DIRECT):
        return out
    with open(M3U_DIRECT, encoding="utf-8") as f:
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


def _head(kind, note):
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    return [
        "#EXTM3U",
        "# 频道：信宜融媒综合广播 (FM98.9) ＋ 信宜综合电视台",
        "# 类型：%s" % kind,
        "# 更新：%s (北京时间，由 GitHub Actions 自动刷新)" % now,
        note,
    ]


def build_direct_m3u():
    """直连版：中转静态地址（已配置 RELAY_BASE）或真实 m3u8 地址（未配置）"""
    if RELAY_BASE:
        lines = [
            "#EXTM3U",
            "# 频道：信宜融媒综合广播 (FM98.9) ＋ 信宜综合电视台",
            "# 类型：静态直连（地址永久不变，由中转服务实时解析当前有效流）",
            "# 说明：以下地址写一次终身可用，无需随直播会话刷新；"
            "网页版见 xinyi_web.m3u。",
        ]
        for ch in CHANNELS:
            lines += [
                '#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" '
                'group-title="广东",%s' % (ch["id"], ch["name"], ch["logo"],
                                            ch["label"]),
                RELAY_BASE + ch["relay"],
            ]
        lines.append("")
        return "\n".join(lines)

    lines = _head("直连（播放器直接播放，云端会话自动保活）",
                  "# 说明：本文件只含「直连」；网页版见 xinyi_web.m3u。")
    for ch in CHANNELS:
        key = STATE.get(ch["id"])
        if not key:
            continue
        lines += [
            '#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" group-title="广东",'
            '%s' % (ch["id"], ch["name"], ch["logo"], ch["label"]),
            key,
        ]
    lines.append("")
    return "\n".join(lines)


def build_current_json():
    """当前有效直链（供中转服务实时读取）"""
    data = {ch["id"]: STATE[ch["id"]] for ch in CHANNELS if ch["id"] in STATE}
    data["time"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def build_web_m3u():
    """网页版：webview:// 打开网页播放（完全免维护）"""
    lines = _head("网页版（播放器内置浏览器打开，无需维护）",
                  "# 说明：本文件只含「网页版」；直连版见 xinyi_radio_local.m3u。")
    for ch in CHANNELS:
        lines += [
            '#EXTINF:-1 tvg-id="%s-web" tvg-name="%s(网页版)" tvg-logo="%s" '
            'group-title="广东",%s' % (ch["id"], ch["name"], ch["logo"],
                                        ch["label"]),
            "webview://" + ch["web"],
        ]
    lines.append("")
    return "\n".join(lines)


def publish():
    """写出各产出文件并推送；只有内容真正变化（忽略时间戳）才提交"""
    with PUB_LOCK:
        changed = []

        # 1) 两个 m3u：忽略「更新：」行的差异，只在频道/地址真正变化时提交
        for path, content in ((M3U_DIRECT, build_direct_m3u()),
                              (M3U_WEB, build_web_m3u())):
            old = _read(path)
            if _strip_stamp(old) == _strip_stamp(content):
                continue
            _write(path, content)
            changed.append(os.path.basename(path))

        # 2) current.json：中转服务读它拿当前有效地址
        jcontent = build_current_json()
        jold = _read(JSON_FILE)
        if _strip_json_time(jold) != _strip_json_time(jcontent):
            _write(JSON_FILE, jcontent)
            changed.append(os.path.basename(JSON_FILE))
        elif jold != jcontent:
            # 仅时间变化：就地刷新，不提交
            _write(JSON_FILE, jcontent)

        if not changed:
            return False

    with GIT_LOCK:
        sh("git", "config", "user.name", "github-actions[bot]", quiet=True)
        sh("git", "config", "user.email",
           "41898282+github-actions[bot]@users.noreply.github.com", quiet=True)
        sh("git", "add", *changed, quiet=True)
        if sh("git", "diff", "--cached", "--quiet", quiet=True) == 0:
            return False
        stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        sh("git", "commit", "-m",
           "chore: 更新信宜融媒直播源（%s） %s CST"
           % ("/".join(changed), stamp), quiet=True)
        for attempt in range(3):
            sh("git", "pull", "--rebase", "--autostash", quiet=True)
            if sh("git", "push", quiet=True) == 0:
                return True
            log("推送失败，重试 %d/3" % (attempt + 1))
            time.sleep(5)
        log("推送最终失败（本地文件已更新）")
        return True


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, content):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _strip_json_time(text):
    """忽略 current.json 里的 time 字段，用于内容比对"""
    try:
        d = json.loads(text)
    except Exception:
        return text
    d.pop("time", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _strip_stamp(text):
    """去掉更新时间行，用于内容比对"""
    return "\n".join(l for l in text.splitlines()
                     if not l.startswith("# 更新："))


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
