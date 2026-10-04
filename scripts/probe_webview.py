# -*- coding: utf-8 -*-
"""快速探测：用无头浏览器访问 webview 页面，捕获真实 HLS 流地址（可行性评估）"""
import asyncio, re, sys, json
from playwright.async_api import async_playwright

TEST = [
    ("CCTV1-央视网", "https://tv.cctv.com/live/cctv1/"),
    ("广东卫视", "https://www.gdtv.cn/tvChannelDetail/43"),
    ("浙江卫视", "https://www.cztv.com/liveTV/101"),
    ("深圳卫视", "https://www.sztv.com.cn/dianshi.shtml?id=7867"),
    ("杭州综合", "https://tv.hoolo.tv/hzzh/"),
    ("成都新闻综合", "https://www.cditv.cn/show/4845-563.html"),
    ("北京卫视-央视频", "https://yangshipin.cn/tv/home?pid=600002309"),
    ("湖南-金鹰卡通", "https://www.mgtv.com/live"),
    ("江苏卫视", "https://live.jstv.com/?tag=%E6%B1%9F%E8%8B%8F%E5%8D%AB%E8%A7%86"),
    ("宁波-1", "https://www.ncmc.nbtv.cn/gbds/folder8458/NBTV1/index.shtml"),
]

HLS_RE = re.compile(r"\.m3u8(\?|$)", re.I)


async def probe(ctx, name, url):
    found = []
    page = await ctx.new_page()

    def on_req(req):
        u = req.url
        if HLS_RE.search(u.split("#")[0]) and u not in found:
            found.append(u)

    page.on("request", on_req)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        await page.close()
        return name, url, found, f"goto:{type(e).__name__}"
    # 尝试点击常见播放按钮
    for sel in ["video", ".vjs-big-play-button", ".xgplayer-start", "text=播放",
                ".play-btn", "#player", "[class*=play]"]:
        try:
            el = await page.query_selector(sel)
            if el:
                try:
                    await el.click(timeout=1200)
                except Exception:
                    pass
                break
        except Exception:
            pass
    for _ in range(24):
        await asyncio.sleep(0.5)
        if found:
            break
    try:
        srcs = await page.eval_on_selector_all(
            "video,source", "els=>els.map(e=>e.src||e.currentSrc||'').filter(Boolean)")
    except Exception:
        srcs = []
    for s in srcs:
        if HLS_RE.search(s.split("#")[0]) and s not in found:
            found.append(s)
    await page.close()
    return name, url, found, ""


async def main():
    out = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, channel="chrome",
                                    args=["--no-sandbox", "--mute-audio",
                                          "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}, ignore_https_errors=True)
        sem = asyncio.Semaphore(3)

        async def run(n, u):
            async with sem:
                r = await probe(ctx, n, u)
                out.append(r)
                tag = "OK " if r[2] else "MISS"
                print(f"[{tag}] {n}  {r[3]}  {r[2][:1]}", flush=True)

        await asyncio.gather(*[run(n, u) for n, u in TEST])
        await b.close()
    ok = sum(1 for r in out if r[2])
    print(f"\n命中 {ok}/{len(TEST)}")
    with open("/tmp/probe_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


asyncio.run(main())
