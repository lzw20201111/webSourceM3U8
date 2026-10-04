#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒综合广播 · 直播直链保活（GitHub Actions 长任务）
=========================================================
实测结论（本项目的关键发现）：
  1. 该台 CDN 的播放地址与「活跃播放会话」绑定：
       - 会话存活期间，地址在任意网络/设备（含国内）都能正常拉流；
       - 会话一断（浏览器退出），约 30 秒后地址即 403；
       - 用纯 HTTP 反复请求播放列表【无法】续命，必须有真实播放会话。
  2. 多个播放会话可以并存，互不影响（可重叠运行做冗余）。
  3. 因此：只要有一个持续运行的浏览器在播放，其地址就长期可用。

本脚本做的事：
  在一个长任务（默认约 5.5 小时）里持续维持无头 Chrome 播放，
  启动时把当前有效地址写入 xinyi_radio_local.m3u（第 1 项「直连」），
  期间若地址变化则同步更新。配合 workflow 每 3 小时触发一次，
  多个任务互相重叠，从而让 m3u 中的直连地址长期保持可用。

环境变量：
  RUN_MINUTES  本次任务运行时长（分钟），默认 330
"""

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M3U_FILE = os.path.join(ROOT, "xinyi_radio_local.m3u")

PAGE_URL = "https://live.xytv.cc/radio/223?uin=1629&refererId=0"
WEBVIEW_URL = "webview://https://lzw20201111.github.io/webSourceM3U8/"
LOGO = ("https://static-pro.guangdianyun.tv/1629/program/20190102/"
        "b782f17ee2606a6df9f4de06aa988a6e.jpeg")

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "330"))
POLL_SECONDS = 60

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


def build_m3u(key):
    """生成 m3u：第 1 项直连（自动保活），第 2 项网页版（免维护）"""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        "#EXTM3U",
        "# 频道：信宜融媒综合广播 (FM98.9)",
        "# 更新：%s (北京时间，由 GitHub Actions 自动刷新)" % now,
        "# 说明：第 1 项「直连」为当前有效播放地址，云端会话自动保活，",
        "#       播放器直接播放即可；第 2 项「网页版」不需要任何维护。",
        '#EXTINF:-1 tvg-id="xinyi-radio" tvg-name="信宜融媒综合广播" '
        'tvg-logo="%s" group-title="广东",'
        '信宜融媒综合广播 (FM98.9) · 直连' % LOGO,
        key,
        '#EXTINF:-1 tvg-id="xinyi-radio-web" tvg-name="信宜融媒综合广播(网页版)" '
        'tvg-logo="%s" group-title="广东",'
        '信宜融媒综合广播 (FM98.9) · 网页版' % LOGO,
        WEBVIEW_URL,
        "",
    ])


def publish(key):
    """把地址写入 m3u 并推送到仓库；内容无变化则跳过"""
    content = build_m3u(key)
    old = ""
    if os.path.exists(M3U_FILE):
        with open(M3U_FILE, encoding="utf-8") as f:
            old = f.read()
    if old == content:
        return False

    with open(M3U_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    sh("git", "config", "user.name", "github-actions[bot]", quiet=True)
    sh("git", "config", "user.email",
       "41898282+github-actions[bot]@users.noreply.github.com", quiet=True)
    sh("git", "add", "xinyi_radio_local.m3u", quiet=True)
    if sh("git", "diff", "--cached", "--quiet", quiet=True) == 0:
        return False

    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    sh("git", "commit", "-m",
       "chore: 更新信宜广播直播直链 %s CST" % stamp, quiet=True)
    for attempt in range(3):
        sh("git", "pull", "--rebase", "--autostash", quiet=True)
        if sh("git", "push", quiet=True) == 0:
            return True
        log("推送失败，重试 %d/3" % (attempt + 1))
        time.sleep(5)
    log("推送最终失败（本地文件已更新）")
    return True


def start_session(p):
    """启动浏览器并开始播放，返回 (browser, page, key)"""
    b = p.chromium.launch(headless=True, args=[
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--autoplay-policy=no-user-gesture-required",
        "--mute-audio",
    ])
    ctx = b.new_context(locale="zh-CN")
    pg = ctx.new_page()
    pg.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(12000)
    pg.evaluate(CLICK_JS)
    pg.wait_for_timeout(15000)
    return b, pg, pg.evaluate(GRAB_JS)


def main():
    deadline = time.time() + RUN_MINUTES * 60
    log("任务启动：计划运行 %d 分钟（截止 %s CST）"
        % (RUN_MINUTES, datetime.fromtimestamp(deadline, CST).strftime("%H:%M")))

    with sync_playwright() as p:
        while time.time() < deadline:
            if time.time() + 300 >= deadline:
                break
            try:
                b, pg, key = start_session(p)
            except Exception as e:
                log("建立播放会话失败：%r，30 秒后重试" % (e,))
                time.sleep(30)
                continue

            try:
                if key:
                    publish(key)
                    log("当前直链：" + key[:110])
                else:
                    log("未取到播放地址，稍后重试")

                while time.time() < deadline - 300:
                    time.sleep(POLL_SECONDS)
                    try:
                        k = pg.evaluate(GRAB_JS)
                    except Exception as e:
                        log("页面异常：%r，重建会话" % (e,))
                        break
                    if not k:
                        log("会话疑似失效，重建")
                        break
                    if k != key:
                        key = k
                        publish(key)
                        log("地址已更新：" + key[:110])
            finally:
                try:
                    b.close()
                except Exception:
                    pass

    log("任务正常结束")


if __name__ == "__main__":
    main()
