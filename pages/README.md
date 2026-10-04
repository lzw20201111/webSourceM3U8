# 中转服务（Cloudflare Pages · 让播放地址永久不变）

## 为什么需要它

广电云的播放地址与「活跃播放会话」绑定，会随会话轮换而变。把直链直接写进 m3u，
过一会儿就失效。中转服务提供一个**永久不变的地址**，每次请求时实时去取当前有效地址：

```
https://xinyi-relay.pages.dev/xinyi/radio.m3u8   ← 广播，写一次终身可用
https://xinyi-relay.pages.dev/xinyi/tv.m3u8      ← 电视，写一次终身可用
https://xinyi-relay.pages.dev/xinyi/all.m3u      ← 两条频道的汇总订阅清单
https://xinyi-relay.pages.dev/health             ← 健康检查（返回当前直链）
```

地址后加 `?r=1` 可改为 302 直接跳转（兼容少数不解析相对地址的播放器）。

## 为什么用 Pages 而不是 Workers

| 域名 | 国内可达性 |
|---|---|
| `*.workers.dev` | ❌ DNS 污染 + SNI 阻断（换真实 IP 也被重置） |
| `*.pages.dev` | ✅ 实测正常（HTTPS 200） |

Cloudflare Pages 免费、无需自有域名，`_worker.js`（高级模式）与 Workers 是同一套运行时，
中转逻辑可以原样复用。

## 部署

`deploy.py` 直接调 Cloudflare API 做 Direct Upload，不需要装 wrangler。

```bash
CF_TOKEN=<你的 Cloudflare API Token> python pages/deploy.py
```

Token 需要 `Account → Cloudflare Pages → Edit` 权限。

## 关键实现细节（踩过的坑）

Pages 直传时，**高级模式的代码不是以静态文件上传的**。把 `_worker.js` 当成普通文件放进
manifest 上传，API 会接受、部署也显示成功，但 `uses_functions` 为 `false`，访问一律 404。

真正的要求是：把代码放进 multipart 字段 **`_worker.bundle`**，其内容是
**Workers 脚本上传表单的序列化字节**，即一个内层 multipart：

```
--BOUNDARY
Content-Disposition: form-data; name="metadata"

{"main_module":"index.mjs","compatibility_date":"2024-09-23","compatibility_flags":[]}
--BOUNDARY
Content-Disposition: form-data; name="index.mjs"; filename="index.mjs"
Content-Type: application/javascript+module

<JS 源码>
--BOUNDARY--
```

外层请求的字段：`branch`、`commit_message`、`manifest`（JSON 字符串）、
`_worker.bundle`（`application/octet-stream`）。

验证是否成功：部署详情里的 **`uses_functions` 必须为 `true`**。
