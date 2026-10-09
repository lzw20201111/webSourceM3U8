#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信宜融媒直播源自动抓取脚本
每2小时通过 GitHub Actions 运行，获取最新的 m3u8 播放地址
"""

import requests
import uuid
import json
from datetime import datetime

# 频道配置
CHANNELS = [
    {
        "name": "信宜广播电台",
        "type": "radio",
        "id": 223,
        "uin": 1629,
        "group": "广东频道"
    },
    {
        "name": "信宜综合台",
        "type": "tv",
        "id": 643,
        "uin": 1629,
        "group": "广东频道"
    }
]

# API 基础地址
API_BASE = "https://1812501212048408.cn-hangzhou.fc.aliyuncs.com/2016-08-15/proxy/node-api.online/node-api"

# 请求头（必须带 Referer，否则会被拦截）
HEADERS = {
    "Referer": "https://live.xytv.cc/",
    "Origin": "https://live.xytv.cc",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def get_play_url(channel):
    """调用 API 获取单个频道的 m3u8 播放地址"""
    client_id = str(uuid.uuid4())
    url = f"{API_BASE}/{channel['type']}/getPlayAddress?id={channel['id']}&uin={channel['uin']}&clientId={client_id}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        data = resp.json()

        if data.get("errorCode") == 0 and data.get("data", {}).get("hlsUrl"):
            return data["data"]["hlsUrl"]
        else:
            print(f"[WARN] {channel['name']} API返回异常: {data.get('errMessage', 'unknown')}")
            return None
    except Exception as e:
        print(f"[ERROR] {channel['name']} 请求失败: {e}")
        return None


def generate_m3u(channels_data):
    """生成标准 m3u 播放列表"""
    lines = ["#EXTM3U"]
    for ch in channels_data:
        if ch["url"]:
            lines.append(
                f'#EXTINF:-1 tvg-name="{ch["name"]}" group-title="{ch["group"]}",{ch["name"]}'
            )
            lines.append(ch["url"])
    return "\n".join(lines) + "\n"


def generate_txt(channels_data):
    """生成 webview 格式的 txt 播放列表（兼容原有格式）"""
    lines = ["广东频道,#genre#"]
    for ch in channels_data:
        if ch["url"]:
            lines.append(f'{ch["name"]},{ch["url"]}')
    return "\n".join(lines) + "\n"


def main():
    print(f"=== 开始抓取直播源 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    channels_data = []
    for ch in CHANNELS:
        print(f"正在获取 {ch['name']} ...")
        play_url = get_play_url(ch)
        channels_data.append({
            "name": ch["name"],
            "type": ch["type"],
            "group": ch["group"],
            "url": play_url
        })
        if play_url:
            print(f"  ✓ 成功: {play_url[:80]}...")
        else:
            print(f"  ✗ 失败")

    # 生成 m3u 文件
    m3u_content = generate_m3u(channels_data)
    with open("xinyi.m3u", "w", encoding="utf-8") as f:
        f.write(m3u_content)
    print(f"\n已生成 xinyi.m3u ({len([c for c in channels_data if c['url']])} 个有效频道)")

    # 生成 txt 格式（兼容原 webview 格式的纯 m3u8 版本）
    txt_content = generate_txt(channels_data)
    with open("xinyi-m3u8.txt", "w", encoding="utf-8") as f:
        f.write(txt_content)
    print("已生成 xinyi-m3u8.txt")

    # 生成元数据 JSON（方便调试和查看）
    meta = {
        "update_time": datetime.now().isoformat(),
        "channels": channels_data
    }
    with open("xinyi-meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("已生成 xinyi-meta.json")

    print("=== 抓取完成 ===")


if __name__ == "__main__":
    main()
