# -*- coding: utf-8 -*-
"""临时验证脚本（GitHub Actions 上运行）
目的：在真实数据中心网络下抓取播放地址，并保持会话存活，
      供国内实测其可用性 —— 判定"云端抓取的直链能否在国内直接播放"。
触发方式：变更本文件后推送（push paths: probe/**）。
"""
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "https://live.xytv.cc/radio/223?uin=1629&refererId=0"

CLICK_JS = """(function(){
  var e=document.querySelectorAll('.xwH5Player-play,.svg-play,.play-bill-btn,[class*=play]');
  if(e.length)e[0].click();
  var a=document.querySelector('audio,video'); if(a){try{a.play()}catch(x){}}
})()"""

GRAB_JS = """(function(){
  var r=performance.getEntriesByType('resource').map(function(e){return e.name})
       .filter(function(n){return n.indexOf('m3u8')>-1});
  return r.length? r[r.length-1] : '';
})()"""


def sh(*args):
    subprocess.run(args, cwd=ROOT, check=False)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
    ctx = b.new_context(locale="zh-CN")
    pg = ctx.new_page()
    pg.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(12000)
    pg.evaluate(CLICK_JS)
    pg.wait_for_timeout(15000)
    key = pg.evaluate(GRAB_JS)
    print("KEY =", key, flush=True)

    with open(os.path.join(ROOT, "probe_key.txt"), "w", encoding="utf-8") as f:
        f.write(key + "\n")
        f.write("at=" + datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " (Beijing)\n")

    sh("git", "config", "user.name", "github-actions[bot]")
    sh("git", "config", "user.email",
       "41898282+github-actions[bot]@users.noreply.github.com")
    sh("git", "add", "probe_key.txt")
    sh("git", "commit", "-m", "probe: 抓取播放地址快照 [skip ci]")
    sh("git", "pull", "--rebase", "--autostash")
    sh("git", "push")
    print("已提交 probe_key.txt", flush=True)

    for i in range(10):
        time.sleep(60)
        k2 = pg.evaluate(GRAB_JS)
        print("t=%ds  key不变=%s" % ((i + 1) * 60, k2 == key), flush=True)

    b.close()
print("probe done", flush=True)

# trigger 1791086460
