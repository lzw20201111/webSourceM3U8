/**
 * 静态播放源中转服务（Cloudflare Pages · 高级模式）
 * ================================================
 * 解决两个问题：
 *   ① 信宜融媒（广电云）：播放地址与「活跃播放会话」绑定，地址会轮换 → 需要固定地址；
 *   ② webview.txt 里的 400 多个「网页源」：普通播放器打不开网页 → 需要真正的流地址，
 *      而抓出来的地址普遍带时效签名（auth_key / txTime / ysign…），会过期。
 *
 * 统一解法：本 Worker 提供【永久不变】的地址，每次请求时实时读取
 * 由定时任务刷新的 channels.json / current.json，拿到当前有效流并转发播放列表。
 *
 * 对外地址（永久固定，写一次终身可用）：
 *   GET  /webview.m3u                全部网页源频道 → 静态播放源清单（订阅用，内容自动刷新）
 *   GET  /ch/<id>.m3u8               单个频道的静态播放地址
 *   GET  /ch/<id>.m3u8?r=1           直接 302 跳转到当前真实地址（少数播放器更兼容）
 *   GET  /xinyi/radio.m3u8           信宜融媒综合广播 (FM98.9)
 *   GET  /xinyi/tv.m3u8              信宜综合电视台
 *   GET  /xinyi/all.m3u              信宜两条频道的静态汇总清单
 *   GET  /health                     健康检查
 *
 * 工作原理：拉取上游 HLS 播放列表 → 把分片地址改写成【CDN 绝对地址】后返回。
 *   → 只有播放列表（几 KB）经过 Worker，音视频分片由播放器直连 CDN，
 *     流量不经 Cloudflare，性能与直连一致。
 */

const REPO = "lzw20201111/webSourceM3U8";
const BRANCH = "main";

/** 数据源，按顺序尝试（GitHub 原生 raw 优先，jsDelivr 兜底） */
const DATA_BASES = [
  "https://raw.githubusercontent.com/" + REPO + "/" + BRANCH,
  "https://cdn.jsdelivr.net/gh/" + REPO + "@" + BRANCH,
  "https://raw.githack.com/" + REPO + "/" + BRANCH,
];

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         + "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36";

/** 信宜融媒两条频道（数据在 current.json） */
const ROUTES = {
  "/xinyi/radio.m3u8": "xinyi-radio",
  "/xinyi/tv.m3u8": "xinyi-tv",
};

const XINYI_CHANNELS = [
  {
    key: "xinyi-radio", path: "/xinyi/radio.m3u8", id: "xinyi-radio",
    name: "信宜融媒综合广播", label: "信宜融媒综合广播 (FM98.9)", group: "广东",
    logo: "https://static-pro.guangdianyun.tv/1629/program/20190102/b782f17ee2606a6df9f4de06aa988a6e.jpeg",
  },
  {
    key: "xinyi-tv", path: "/xinyi/tv.m3u8", id: "xinyi-tv",
    name: "信宜综合电视台", label: "信宜综合电视台", group: "广东",
    logo: "https://static-pro.guangdianyun.tv/1629/mss/20241219/3b3aa73530526d817a02349a3a323f37.jpeg",
  },
];

/** 部署时注入的「最后已知可用」快照（部署脚本会把 channels.json 塞进来）。
 *  作用：万一在线数据源（GitHub raw / jsDelivr）都取不到，仍能靠快照继续服务。 */
const EMBEDDED = /*__CHANNELS_SNAPSHOT__*/ null;

/** 永远允许中转的 CDN（信宜用） */
const ALLOW_SUFFIX = ["aodianyun.com"];

const IS_PLAYLIST = /\.m3u8(\?|#|$)/i;

/* ================================================================= 入口 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = normPath(url.pathname);
    const head = request.method === "HEAD";
    const base = corsHeaders();

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: base });
    if (!head && request.method !== "GET") return plain("# 只支持 GET / HEAD", 405, base);

    try {
      // ① 单个频道：/ch/<id>.m3u8
      let m = path.match(/^\/ch\/(.+?)\.m3u8$/i) || path.match(/^\/ch\/(.+)$/i);
      if (m) return await serveWebview(decodeURIComponent(m[1]), url, base, head);

      // ② 全部网页源频道清单（内容自动刷新，地址永久不变）
      if (path === "/webview.m3u" || path === "/all.m3u" || path === "/xinyi/all.m3u") {
        return new Response(await buildAllM3U(url.origin), {
          status: 200,
          headers: withType(base),
        });
      }

      // ③ 信宜融媒
      const chKey = ROUTES[path] || xinyiFromQuery(url.searchParams.get("ch"));
      if (chKey) return await serveXinyi(chKey, url, base, head);

      // ④ 通用子播放列表代理
      if (path === "/p") {
        const src = url.searchParams.get("src") || "";
        if (!/^https?:\/\//i.test(src)) return plain("# 缺少 src 参数", 400, base);
        return await proxyPlaylist(src, base, head, await hostAllowSet(url));
      }

      // ⑤ 首页
      if (path === "/" || path === "/index.html" || path === "/xinyi") {
        return new Response(await helpPage(url.origin), {
          status: 200,
          headers: Object.assign({}, base, { "Content-Type": "text/html; charset=utf-8" }),
        });
      }

      // ⑥ 健康检查
      if (path === "/health") return await health(url.origin, base);

      return plain("# 未找到。可用：/webview.m3u 、/ch/<频道id>.m3u8 、/xinyi/radio.m3u8", 404, base);
    } catch (e) {
      return plain("# 中转异常：" + (e && e.message ? e.message : e), 502, base);
    }
  },
};

/* ============================================================ 网页源频道 */

/** 读 channels.json（带短缓存 + 多源兜底 + 内置快照兜底） */
async function readChannels(force) {
  const stamp = force ? Date.now() : Math.floor(Date.now() / 60000);
  let lastErr = "无可用数据源";
  for (const b of DATA_BASES) {
    try {
      const r = await fetch(b + "/channels.json?t=" + stamp, {
        headers: { "User-Agent": UA, "Cache-Control": "no-cache" },
        cf: { cacheTtl: 60 },
      });
      if (!r.ok) { lastErr = b + " HTTP " + r.status; continue; }
      const j = await r.json();
      if (j && j.channels && Object.keys(j.channels).length) return j;
      lastErr = b + " 内容为空";
    } catch (e) {
      lastErr = b + " " + (e && e.message ? e.message : e);
    }
  }
  if (EMBEDDED && EMBEDDED.channels && Object.keys(EMBEDDED.channels).length) {
    return EMBEDDED;
  }
  throw new Error(lastErr);
}

async function serveWebview(id, url, base, head) {
  // 在线数据优先，内置快照兜底；两者都试，谁先拉通就用谁
  const cands = [];
  try {
    const d = await readChannels(false);
    if (d.channels && d.channels[id] && d.channels[id].u) cands.push(d.channels[id]);
  } catch (e) { /* 只看快照 */ }
  if (EMBEDDED && EMBEDDED.channels && EMBEDDED.channels[id] &&
      EMBEDDED.channels[id].u && cands.length === 0) {
    cands.push(EMBEDDED.channels[id]);
  }
  if (!cands.length) return plain("# 没有这个频道：" + id, 404, base);

  if (url.searchParams.get("r") === "1") {
    return new Response(null, {
      status: 302,
      headers: Object.assign({}, base, { Location: cands[0].u }),
    });
  }

  const allow = await hostAllowSet(url);
  let last = null;
  for (const ch of cands) {
    const res = await proxyPlaylist(ch.u, base, head, allow);
    if (res.status === 200) return res;
    last = res;
  }
  return last;
}

/** 汇总清单：地址永久不变，内容随 channels.json 自动刷新 */
async function buildAllM3U(origin) {
  const out = [
    "#EXTM3U",
    "# 静态播放源 · 全部网页源频道（地址永久不变，内容由中转实时刷新）",
  ];
  try {
    const data = await readChannels(false);
    out.push("# 共 " + Object.keys(data.channels).length + " 个可用频道");
    out.push("# 更新：" + (data.time || ""));
    let lastG = null;
    for (const [id, ch] of Object.entries(data.channels)) {
      if (ch.g && ch.g !== lastG) { out.push(""); lastG = ch.g; }
      out.push('#EXTINF:-1 tvg-id="' + id + '" tvg-name="' + esc(ch.n) +
               '" group-title="' + esc(ch.g || "其它") + '",' + esc(ch.n));
      out.push(origin + "/ch/" + id + ".m3u8");
    }
  } catch (e) {
    out.push("# 取频道数据失败：" + (e && e.message ? e.message : e));
  }
  // 信宜融媒两条也一并附上
  out.push("");
  out.push("# 信宜融媒");
  for (const ch of XINYI_CHANNELS) {
    out.push('#EXTINF:-1 tvg-id="' + ch.id + '" tvg-name="' + ch.name +
             '" tvg-logo="' + ch.logo + '" group-title="广东",' + ch.label);
    out.push(origin + ch.path);
  }
  out.push("");
  return out.join("\n");
}

async function helpPage(origin) {
  let rows = "";
  let count = 0;
  try {
    const data = await readChannels(false);
    const arr = Object.entries(data.channels);
    count = arr.length;
    rows = arr.map(function (kv) {
      const id = kv[0], ch = kv[1];
      return '<tr><td class="g">' + esc(ch.g || "") + '</td><td>' + esc(ch.n) +
             '</td><td><a href="' + origin + "/ch/" + id + '.m3u8">' +
             origin + "/ch/" + id + ".m3u8</a></td></tr>";
    }).join("");
  } catch (e) {
    rows = '<tr><td colspan="3">取频道数据失败：' + esc(e.message) + "</td></tr>";
  }
  const xy = XINYI_CHANNELS.map(function (ch) {
    return '<tr><td class="g">广东</td><td>' + esc(ch.label) +
           '</td><td><a href="' + origin + ch.path + '">' + origin + ch.path + "</a></td></tr>";
  }).join("");

  return '<!doctype html><html lang="zh-CN"><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    "<title>静态播放源 · 中转服务</title><style>" +
    "body{font:15px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC'," +
    "'Microsoft YaHei',sans-serif;max-width:1000px;margin:5vh auto;padding:0 20px;color:#1a1d21;" +
    "background:#f6f7f9}h1{font-size:21px;margin-bottom:.2em}" +
    "p.sub{color:#5b6470;font-size:14px;margin-top:0}" +
    "table{border-collapse:collapse;width:100%;background:#fff;border-radius:12px;overflow:hidden;" +
    "box-shadow:0 1px 3px rgba(0,0,0,.07);font-size:13.5px}" +
    "td,th{padding:8px 12px;border-bottom:1px solid #eceef1;text-align:left}" +
    "th{background:#fafbfc;font-weight:600}td.g{color:#5b6470;white-space:nowrap}" +
    "a{color:#0b6bcb;text-decoration:none;word-break:break-all}" +
    "code{background:#f1f3f5;padding:2px 6px;border-radius:5px;font-size:13px;word-break:break-all}" +
    "tr:hover td{background:#fafbfc}" +
    "@media(prefers-color-scheme:dark){body{background:#14171a;color:#e8eaed}" +
    "table{background:#1e2226;box-shadow:none}th{background:#232a30}td,th{border-color:#2b3036}" +
    "code{background:#2b3036}a{color:#6cb2ff}}" +
    "</style><h1>静态播放源 · 中转服务</h1>" +
    "<p class=\"sub\">下面每条地址都<b>永久不变</b>，由中转实时解析当前有效直播流。" +
    "订阅全部频道：" +
    "<code>" + origin + "/webview.m3u</code><br>" +
    "共 " + count + " 个网页源频道。</p>" +
    "<table><tr><th>分类</th><th>频道</th><th>永久播放地址</th></tr>" +
    xy + rows + "</table></html>";
}

async function health(origin, base) {
  const out = {};
  out.embedded = EMBEDDED && EMBEDDED.channels ? Object.keys(EMBEDDED.channels).length : 0;
  out.embeddedTime = EMBEDDED ? EMBEDDED.time : null;
  try {
    const data = await readChannels(true);
    out.channelsJson = { time: data.time, ok: (data.channels ? Object.keys(data.channels).length : 0) };
  } catch (e) {
    out.channelsJson = "ERR: " + e.message;
  }
  for (const ch of XINYI_CHANNELS) {
    try {
      out[ch.key] = await readCurrent(ch.key, false);
    } catch (e) {
      out[ch.key] = "ERR: " + e.message;
    }
  }
  out.webviewM3U = origin + "/webview.m3u";
  return new Response(JSON.stringify(out, null, 2), {
    status: 200,
    headers: Object.assign({}, base, { "Content-Type": "application/json; charset=utf-8" }),
  });
}

/* ============================================================== 信宜融媒 */

async function serveXinyi(chKey, url, base, head) {
  let target;
  try {
    target = await readCurrent(chKey, false);
  } catch (e) {
    return plain("# 无法获取当前有效地址：" + e.message, 502, base);
  }
  if (url.searchParams.get("r") === "1") {
    return new Response(null, { status: 302, headers: Object.assign({}, base, { Location: target }) });
  }
  let res = await proxyPlaylist(target, base, head, await hostAllowSet(url));
  if (res.status !== 200) {
    try {
      const fresh = await readCurrent(chKey, true);
      if (fresh !== target) res = await proxyPlaylist(fresh, base, head, await hostAllowSet(url));
    } catch (e) { /* 拿不到新地址就返回原来的错误 */ }
  }
  return res;
}

async function readCurrent(chKey, force) {
  const stamp = force ? Date.now() : Math.floor(Date.now() / 15000);
  let lastErr = "无可用数据源";
  for (const b of DATA_BASES) {
    try {
      const r = await fetch(b + "/current.json?t=" + stamp, {
        headers: { "User-Agent": UA, "Cache-Control": "no-cache" },
        cf: { cacheTtl: 15 },
      });
      if (!r.ok) { lastErr = "源文件 HTTP " + r.status; continue; }
      const j = await r.json();
      const u = j[chKey] || (j.channels && j.channels[chKey]);
      if (u && /^https?:\/\//i.test(u)) return u;
      lastErr = "源文件中没有 " + chKey;
    } catch (e) { lastErr = String(e && e.message ? e.message : e); }
  }
  throw new Error(lastErr);
}

/* ================================================================ 核心 */

/** 中转白名单：channels.json 里出现过的所有主机 + 固定 CDN 后缀 */
let _allowCache = null, _allowAt = 0;
async function hostAllowSet(url) {
  const now = Date.now();
  if (!_allowCache || now - _allowAt > 120000) {
    const set = new Set();
    ALLOW_SUFFIX.forEach(function (s) { set.add("." + s); });
    try {
      const data = await readChannels(false);
      for (const id in data.channels) {
        const ch = data.channels[id];
        [ch.u, ch.p].forEach(function (v) {
          if (!v) return;
          try { set.add(new URL(v).hostname.toLowerCase()); } catch (e) {}
        });
      }
    } catch (e) { /* 拿不到就只靠固定后缀 */ }
    _allowCache = set; _allowAt = now;
  }
  return { set: _allowCache, origin: url.origin };
}

async function proxyPlaylist(src, base, head, allow) {
  const r = await fetch(src, {
    method: head ? "HEAD" : "GET",
    headers: { "User-Agent": UA, Accept: "*/*", Referer: safeOrigin(src) },
    cf: { cacheTtl: 0 },
  });

  if (!r.ok) return plain("# 上游返回 HTTP " + r.status + "（" + hostOf(src) + "）", 502, base);
  if (head) return new Response(null, { status: 200, headers: withType(base) });

  const text = await r.text();
  if (text.indexOf("#EXTM3U") < 0) return plain("# 上游返回的不是 HLS 播放列表", 502, base);
  return new Response(rewritePlaylist(text, src, allow), { status: 200, headers: withType(base) });
}

function rewritePlaylist(body, base, allow) {
  const baseUrl = new URL(base);
  const self = allow ? allow.origin + "/p" : null;
  const hostOk = function (abs) {
    if (!allow) return false;
    try {
      return allow.set.has(new URL(abs).hostname.toLowerCase());
    } catch (e) { return false; }
  };
  const absolutize = function (ref) {
    if (/^data:/i.test(ref)) return ref;
    let abs;
    try { abs = new URL(ref, baseUrl).href; } catch (e) { return ref; }
    // 子播放列表：若主机在白名单内则继续走中转，否则直接给绝对地址
    if (self && IS_PLAYLIST.test(abs) && hostOk(abs)) {
      return self + "?src=" + encodeURIComponent(abs);
    }
    return abs;
  };
  return body.split(/\r?\n/).map(function (raw) {
    const line = raw.trim();
    if (!line) return raw;
    if (line.charAt(0) === "#") {
      return line.replace(/URI="([^"]*)"/g, function (_m, u) { return 'URI="' + absolutize(u) + '"'; });
    }
    return absolutize(line);
  }).join("\n");
}

/* ================================================================ 工具 */

function xinyiFromQuery(v) {
  if (!v) return null;
  const s = String(v).toLowerCase();
  if (s === "radio" || s === "xinyi-radio") return "xinyi-radio";
  if (s === "tv" || s === "xinyi-tv") return "xinyi-tv";
  return null;
}

function normPath(p) {
  let s = (p || "/").replace(/\/{2,}/g, "/");
  if (s.length > 1) s = s.replace(/\/+$/, "");
  return s || "/";
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function hostOf(u) {
  try { return new URL(u).hostname; } catch (e) { return "?"; }
}

function safeOrigin(u) {
  try { return new URL(u).origin + "/"; } catch (e) { return undefined; }
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "no-store",
    "X-Relay": "static-source",
  };
}

function withType(h) {
  return Object.assign({}, h, { "Content-Type": "application/vnd.apple.mpegurl" });
}

function plain(msg, status, h) {
  return new Response(msg + "\n", {
    status,
    headers: Object.assign({}, h, { "Content-Type": "text/plain; charset=utf-8" }),
  });
}
