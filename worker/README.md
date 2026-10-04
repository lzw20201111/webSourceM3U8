# 信宜融媒直播 · 静态地址中转（Cloudflare Worker）

把「会随会话轮换的直播直链」封装成**永久不变**的地址，播放器里填一次终身可用。

| 你的播放器里填 | 用途 |
| --- | --- |
| `https://<你的域名>/xinyi/radio.m3u8` | 信宜融媒综合广播 (FM98.9) |
| `https://<你的域名>/xinyi/tv.m3u8` | 信宜综合电视台 |
| `https://<你的域名>/xinyi/all.m3u` | 两条频道的汇总清单（可直接当订阅源） |

## 原理

```
播放器 ──①请求固定地址──▶ Worker ──②读 current.json 拿当前有效直链──▶ GitHub
                              │
                              └──③改写播放列表（分片转成 CDN 绝对地址）──▶ 播放器
播放器 ─────────④分片直连 CDN（不经 Cloudflare）─────────────▶ 广电云 CDN
```

- 只有**播放列表**（几 KB）经过 Worker，音视频分片由播放器直连 CDN，速度和直连一致；
- 地址刚轮换时的偶发 403 会自动重取新地址重试，播放器侧基本无感；
- `current.json` 由保活工作流每 3 小时刷新，Worker 每 15 秒内必拿到最新值。

## 部署（三选一）

### 方式 A：网页操作（不用装任何东西，最省事）

1. 登录 <https://dash.cloudflare.com> → 左侧 **Compute (Workers)** → **Create** → **Start with Hello World!**
2. 名字填 `xinyi-relay`，点 **Deploy**；
3. 点 **Edit code**，把 `worker.js` 的全部内容粘进去覆盖，右上角 **Deploy**；
4. 得到地址形如 `https://xinyi-relay.<你的账号>.workers.dev` —— 这就是你的永久地址。

### 方式 B：命令行（wrangler）

```bash
cd worker
npx wrangler login          # 浏览器授权
npx wrangler deploy
```

### 方式 C：把 API Token 给我，我直接部署

创建 Token：dash.cloudflare.com → 右上头像 → **My Profile** → **API Tokens** →
**Create Token** → 用模板 **Edit Cloudflare Workers** → 生成后把 Token 和
**Account ID**（Workers 页面右侧可复制）发我即可。

## 部署后：告诉保活任务用哪个地址

GitHub 仓库 → **Settings → Secrets and variables → Actions → Variables** →
**New repository variable**：

- Name：`RELAY_BASE`
- Value：`https://xinyi-relay.<你的账号>.workers.dev`（结尾不要带 `/`）

设置后，下一次保活任务会把 `xinyi_radio_local.m3u` 自动改成上面的静态地址，此后**永不再变**。

## 关于 workers.dev 在国内的可用性

`*.workers.dev` 在部分国内网络下不稳定（可能被 DNS 污染）。如果你有域名并已托管在
Cloudflare，建议在 `wrangler.toml` 里打开 `routes` 绑定自定义域名（如
`live.你的域名`），国内访问会明显更稳。

## 免费额度

Workers 免费版每天 10 万次请求。广播的播放列表每 2 秒刷新一次（约 4.3 万次/天），
电视约 9 秒一次（约 1 万次/天），**单个人听看合计约 5~6 万次/天**，够用；
多人同时看建议升级 Workers 付费版（$5/月，1000 万次）。

## 调试

| 请求 | 说明 |
| --- | --- |
| `/xinyi/radio.m3u8` | 正常返回改写后的播放列表 |
| `/xinyi/radio.m3u8?r=1` | 改成 302 跳转（少数播放器更兼容） |
| `/health` | 返回当前解析到的两个真实地址，便于排查 |
| `/` | 一个简单的说明页 |
