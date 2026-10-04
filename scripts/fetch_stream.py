#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒综合广播 (FM98.9) 直播源自动提取脚本
================================================================
供 GitHub Actions 定时调用，自动刷新 xinyi_radio_local.m3u

为什么必须用真实浏览器：
  该平台（广电云 guangdianyun.tv）的 CDN 播放签名与「浏览器播放会话」绑定。
  直接调用接口 getPlayAddress 拿到的地址会被 CDN 拒绝（HTTP 403），
  只有页面里 hls.js 真正在播放的那个地址才有效。
  因此用无头 Chromium 打开播放页 → 触发播放 → 读取网络请求中的真实地址。

输出：
  重写仓库根目录的 xinyi_radio_local.m3u（播放地址固定，内容自动更新）

退出码：0=成功  1=失败（Actions 中会标红，便于发现问题）
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))          # 北京时间

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_M3U = os.path.join(ROOT, "xinyi_radio_local.m3u")

PAGE_URL = "https://live.xytv.cc/radio/223?uin=1629&refererId=0"
CHANNEL_NAME = "信宜融媒综合广播 (FM98.9)"
CHANNEL_GROUP = "广东"
CHANNEL_LOGO = ("https://static-pro.guangdianyun.tv/1629/program/"
                "20190102/b782f17ee2606a6df9f4de06aa988a6e.jpeg")

PLAY_BUTTONS = (".xwH5Player-play,.svg-play,.play-bill-btn,"
                "[class*=play-btn],[class*=playBtn]")


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + str(msg), flush=True)


def extract_key_meta(url):
    """从 auth_key 解析过期时间戳（格式：<expire>-0-0-<sign>）"""
    m = re.search(r"auth_key=(\d+)-", url)
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)), CST)
    except Exception:
        return None


def fetch_playlist(page, url):
    """在页面上下文里拉取播放列表（与真实播放同源，避免 CDN 校验差异）"""
    js = """
    (async () => {
      try {
        const r = await fetch(%s, {cache: 'no-store'});
        const t = await r.text();
        return r.status + '\\n' + t.slice(0, 4000);
      } catch (e) { return 'ERR\\n' + e.message; }
    })()
    """ % json.dumps(url)
    try:
        v = page.evaluate(js) or ""
    except Exception as e:
        return -1, str(e)
    head, _, body = v.partition("\n")
    if head == "ERR":
        return -1, body
    return int(head or -1), body


def capture(page, deadline_s=150):
    """打开播放页，等待并抓取 hls.js 正在使用的 m3u8 地址"""
    log(f"打开播放页: {PAGE_URL}")
    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)

    js_url = ("(function(){var e=performance.getEntriesByType('resource')"
              ".map(function(x){return x.name}).filter(function(n){"
              "return n.indexOf('.m3u8')>-1});"
              "return e.length?e[e.length-1]:''})()")

    end = time.time() + deadline_s
    clicked = False
    while time.time() < end:
        try:
            # 播放按钮出现后点击一次
            if not clicked:
                has_btn = page.evaluate(
                    "(function(){return document.querySelectorAll(%s).length})()"
                    % json.dumps(PLAY_BUTTONS))
                if has_btn:
                    page.evaluate(
                        "(function(){var e=document.querySelectorAll(%s);"
                        "if(e.length){e[0].click();return 1}return 0})()"
                        % json.dumps(PLAY_BUTTONS))
                    log("已点击播放按钮")
                    clicked = True
            # 兜底：直接调用 media.play()
            page.evaluate(
                '(function(){var a=document.querySelector("audio,video");'
                'if(a&&a.paused){var p=a.play();if(p&&p.catch)p.catch(function(){})}'
                'return 1})()')

            url = page.evaluate(js_url) or ""
            if url:
                status, body = fetch_playlist(page, url)
                if status == 200 and body.startswith("#EXTM3U"):
                    log(f"成功捕获播放地址 (HTTP {status})")
                    return url, body
                log(f"地址已出现但暂不可用: HTTP {status}，继续等待…")
        except Exception as e:
            log(f"等待中: {e}")
        time.sleep(3)

    return None, None


def build_m3u(url):
    now = datetime.now(CST)
    exp = extract_key_meta(url)
    exp_txt = exp.strftime("%Y-%m-%d %H:%M") if exp else "未知"
    lines = [
        "#EXTM3U",
        f"# 频道：{CHANNEL_NAME}",
        f"# 更新时间：{now.strftime('%Y-%m-%d %H:%M')}（北京时间，由 GitHub Actions 自动刷新）",
        f"# 有效期至：{exp_txt}（北京时间）",
        "# 说明：本文件地址固定，内容每日 07:30 / 17:30 自动更新，播放器无需改动",
        (f'#EXTINF:-1 tvg-id="xinyi-radio" tvg-name="{CHANNEL_NAME}" '
         f'tvg-logo="{CHANNEL_LOGO}" group-title="{CHANNEL_GROUP}",{CHANNEL_NAME}'),
        url,
        "",
    ]
    return "\n".join(lines)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺少 playwright，请先 pip install playwright && playwright install chromium")
        return 1

    ok = False
    with sync_playwright() as p:
        kwargs = {
            "headless": True,
            "args": ["--autoplay-policy=no-user-gesture-required",
                     "--mute-audio", "--no-sandbox",
                     "--disable-dev-shm-usage"],
        }
        # 支持指定浏览器可执行文件（本地调试用）
        if os.environ.get("CHROME_PATH"):
            kwargs["executable_path"] = os.environ["CHROME_PATH"]
        elif os.environ.get("PW_CHANNEL"):
            kwargs["channel"] = os.environ["PW_CHANNEL"]

        browser = p.chromium.launch(**kwargs)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/154.0.0.0 Safari/537.36"),
            viewport={"width": 480, "height": 860},
            locale="zh-CN",
        )
        page = ctx.new_page()
        try:
            url, _ = capture(page)
            if url:
                content = build_m3u(url)
                with open(OUT_M3U, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
                log(f"已写入 {OUT_M3U}")
                log("播放地址: " + url[:120])
                ok = True
            else:
                log("抓取失败：未能取得可用播放地址")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # 输出给 Actions 使用
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"success={'true' if ok else 'false'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
