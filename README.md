# 信宜融媒直播源自动更新

基于 GitHub Actions 自动抓取信宜融媒直播流的 m3u8 地址，每 2 小时自动更新一次。

---

## 📁 文件说明

| 文件 | 作用 |
|---|---|
| `fetch_sources.py` | Python 抓取脚本，调用广电云 API 获取最新 m3u8 地址 |
| `.github/workflows/update-sources.yml` | GitHub Actions 定时任务配置 |
| `xinyi.m3u` | 标准 M3U 播放列表（自动更新） |
| `xinyi-m3u8.txt` | TXT 格式播放列表（自动更新） |
| `xinyi-meta.json` | 更新时间和频道元数据（自动更新） |
| `radio.html` / `tv.html` | Webview 代理页面（兼容旧版播放器） |

---

## 🚀 部署步骤

### 第一步：上传文件到 GitHub 仓库

1. 打开你的仓库：https://github.com/lzw20201111/webSourceM3U8
2. 点击 **Add file → Upload files**
3. 把本文件夹里的所有文件拖进去，**注意保留目录结构**：
   - `.github/workflows/update-sources.yml` 必须在 `.github/workflows/` 目录下
   - 其他文件都放在根目录
4. 点击 **Commit changes** 提交

> 💡 目录结构应该是这样的：
> ```
> webSourceM3U8/
> ├── .github/
> │   └── workflows/
> │       └── update-sources.yml
> ├── fetch_sources.py
> ├── xinyi.m3u
> ├── xinyi-m3u8.txt
> ├── xinyi-meta.json
> ├── radio.html
> ├── tv.html
> └── README.md
> ```

### 第二步：启用 GitHub Actions

1. 进入仓库的 **Actions** 标签页
2. 第一次打开会看到提示："Workflows aren't being run on this repository's default branch"
3. 点击绿色按钮 **I understand my workflows, go ahead and enable them**
4. 左边列表里应该能看到 **Update Live Sources** 这个工作流

### 第三步：手动触发一次测试

1. 在 Actions 页面点击左边的 **Update Live Sources**
2. 点击右边的 **Run workflow** 按钮
3. 选择 `main` 分支，点绿色 **Run workflow** 确认
4. 等待 1~2 分钟，刷新页面看是否运行成功（绿色 ✓ 就是成功）

---

## 📺 使用播放源

### M3U 播放列表地址

把下面这个地址导入到你的 IPTV 播放器里（**国内推荐用 jsDelivr CDN，访问更稳定**）：

```
https://cdn.jsdelivr.net/gh/lzw20201111/webSourceM3U8@main/xinyi.m3u
```

> 💡 为什么用 jsDelivr？
> - `raw.githubusercontent.com` 在国内经常访问不稳定/打不开
> - jsDelivr 是 CDN 加速，国内有节点，速度快又稳定
> - 内容和 GitHub 仓库实时同步，更新后自动生效（最多缓存 12 小时）

支持的播放器：
- VLC、PotPlayer
- IPTV Smarters、TiviMate
- 各类电视盒子 IPTV APP

### Webview 代理地址（兼容旧格式）

如果你的播放器只支持 webview:// 格式：

```
webview://https://lzw20201111.github.io/webSourceM3U8/radio.html
webview://https://lzw20201111.github.io/webSourceM3U8/tv.html
```

> ⚠️ Webview 方式需要先在仓库 **Settings → Pages** 里启用 GitHub Pages（Source 选 main 分支 / root）

---

## ⏱️ 修改更新频率

编辑 `.github/workflows/update-sources.yml`，找到 `cron` 那一行：

```yaml
- cron: '0 */2 * * *'   # ← 改这里
```

**常用频率对照表（北京时间 = UTC + 8）：**

| 想要的效果 | cron 表达式 | 说明 |
|---|---|---|
| 每 1 小时更新 | `0 * * * *` | 最频繁，消耗 Actions 分钟数多 |
| 每 2 小时更新 | `0 */2 * * *` | ← 当前默认，平衡时效和额度 |
| 每 4 小时更新 | `0 */4 * * *` | 更省额度 |
| 每 6 小时更新 | `0 */6 * * *` | |
| 每天早上 8 点更新 | `0 0 * * *` | UTC 0:00 = 北京 8:00 |
| 每天早 8 点 + 晚 8 点 | `0 0,12 * * *` | 一天两次 |

> 💡 GitHub 免费版每个月有 2000 分钟的 Actions 额度，每 2 小时跑一次大约每月用 360 分钟，完全够用。

---

## ❓ 常见问题

### Q: Actions 运行失败怎么办？

1. 进 Actions 页面，点进失败的那次运行
2. 看红色报错的步骤，点开会有详细日志
3. 常见原因：
   - 网络问题（API 暂时不通）→ 等下次自动运行就好
   - 权限问题（push 失败）→ 去仓库 **Settings → Actions → General**，拉到下面，选 **Read and write permissions**，保存

### Q: 播放地址多久会失效？

- 广电云的 m3u8 地址带签名时效，一般有效期几小时到一天不等
- 脚本每 2 小时自动刷新一次，只要定时任务正常跑，地址就一直有效

### Q: 怎么知道上次更新是什么时候？

打开 `xinyi-meta.json`，里面有 `update_time` 字段，就是最近一次更新的时间。

### Q: 怎么加更多频道？

编辑 `fetch_sources.py` 里的 `CHANNELS` 列表，照着格式加就行：

```python
CHANNELS = [
    {
        "name": "频道名称",
        "type": "radio",  # radio = 电台，tv = 电视台
        "id": 频道ID,
        "uin": 1629,
        "group": "分组名"
    },
]
```

---

## 📌 频道列表

| 频道名 | 类型 | 频道 ID |
|---|---|---|
| 信宜广播电台 | 电台 (radio) | 223 |
| 信宜综合台 | 电视 (tv) | 643 |
