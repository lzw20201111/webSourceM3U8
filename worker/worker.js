/**
 * 信宜融媒（综合广播 FM98.9 ＋ 综合电视台）· 静态地址中转
 * =========================================================
 * 解决的问题：
 *   该平台（广电云）的播放地址与「活跃播放会话」绑定，地址会随会话轮换，
 *   因此播放器里填的直链会过期。本 Worker 提供一个【永久不变】的地址，
 *   每次请求时实时去取当前有效地址并转发播放列表，播放器侧完全无感。
 *
 * 对外地址（永久固定，写一次终身可用）：
 *   GET  /xinyi/radio.m3u8   信宜融媒综合广播 (FM98.9)
 *   GET  /xinyi/tv.m3u8      信宜综合电视台
 *   GET  /xinyi/all.m3u      两条频道的静态汇总清单（可直接给播放器/订阅）
 *   GET  /xinyi/radio.m3u8?r=1   直接 302 跳转到当前地址（部分播放器更兼容）
 *
 * 工作原理：
 *   1. 从 GitHub 仓库的 current.json 读取当前有效直链（由 Actions 保活任务刷新）；
 *   2. 用该地址拉取 HLS 播放列表；
 *   3. 把列表里的分片 / 子列表地址改写成【CDN 绝对地址】后返回。
 *      → 只有播放列表（几 KB）经过 Worker，音视频分片由播放器直连 CDN，
 *        流量不经 Cloudflare，性能与直连一致。
 *   4. 地址刚轮换导致的偶发 403 会自动重取一次新地址重试。
 */

const REPO = "lzw20201111/webSourceM3U8";
const BRANCH = "main";
const RAW = "https://raw.githubusercontent.com/" + REPO + "/" + BRANCH;
const JSON_URL = RAW + "/current.json";

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         + "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36";

/** 静态路径 -> current.json 中的频道键 */
const ROUTES = {
  "/xinyi/radio.m3u8": "xinyi-radio",
  "/xinyi/tv.m3u8": "xinyi-tv",
};

/** 频道的展示信息（用于 /xinyi/all.m3u） */
const CHANNELS = [
  {
    key: "xinyi-radio",
    path: "/xinyi/radio.m3u8",
    id: "xinyi-radio",
    name: "信宜融媒综合广播",
    label: "信宜融媒综合广播 (FM98.9)",
    logo: "https://static-pro.guangdianyun.tv/1629/program/20190102/b782f17ee2606a6df9f4de06aa988a6e.jpeg",
  },
  {
    key: "xinyi-tv",
    path: "/xinyi/tv.m3u8",
    id: "xinyi-tv",
    name: "信宜综合电视台",
    label: "信宜综合电视台",
    logo: "https://static-pro.guangdianyun.tv/1629/mss/20241219/3b3aa73530526d817a02349a3a323f37.jpeg",
  },
];

/** 只允许中转该 CDN，避免被当成开放代理 */
const ALLOW_HOST = /(^|\.)aodianyun\.com$/i;

/** 音视频分片 / 子播放列表的判定 */
const IS_PLAYLIST = /\.m3u8(\?|#|$)/i;

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = normPath(url.pathname);
    const head = request.method === "HEAD";
    const base = corsHeaders();

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: base });
    }
    if (!head && request.method !== "GET") {
      return plain("# 只支持 GET / HEAD", 405, base);
    }

    try {
      // ① 通用子播放列表代理（master playlist 里的 variant 会指回这里）
      if (path === "/p") {
        const src = url.searchParams.get("src") || "";
        if (!/^https?:\/\//i.test(src) || !hostAllowed(src)) {
          return plain("# 不允许的 src 参数", 400, base);
        }
        return await proxyPlaylist(src, base, head);
      }

      // ② 静态频道地址
      const chKey = ROUTES[path] || channelFromQuery(url.searchParams.get("ch"));
      if (chKey) {
        return await serveChannel(chKey, url, base, head);
      }

      // ③ 静态汇总清单（内容也是永远不变的，可直接当订阅源）
      if (path === "/xinyi/all.m3u" || path === "/all.m3u") {
        return new Response(buildAllM3U(url.origin), {
          status: 200,
          headers: Object.assign({}, base, { "Content-Type": "audio/x-mpegurl" }),
        });
      }

      // ④ 首页说明
      if (path === "/" || path === "/xinyi" || path === "/index.html") {
        return new Response(helpPage(url.origin), {
          status: 200,
          headers: Object.assign({}, base, {
            "Content-Type": "text/html; charset=utf-8",
          }),
        });
      }

      // ⑤ 健康检查：/health
      if (path === "/health") {
        const out = {};
        for (const ch of CHANNELS) {
          try {
            out[ch.key] = await readCurrent(ch.key);
          } catch (e) {
            out[ch.key] = "ERR: " + e.message;
          }
        }
        return new Response(JSON.stringify(out, null, 2), {
          status: 200,
          headers: Object.assign({}, base, {
            "Content-Type": "application/json; charset=utf-8",
          }),
        });
      }

      return plain("# 未找到：可用地址为 /xinyi/radio.m3u8 、 /xinyi/tv.m3u8", 404, base);
    } catch (e) {
      return plain("# 中转异常：" + (e && e.message ? e.message : e), 502, base);
    }
  },
};

/* ------------------------------------------------------------------ 核心 */

async function serveChannel(chKey, url, base, head) {
  let target;
  try {
    target = await readCurrent(chKey, false);
  } catch (e) {
    return plain("# 无法获取当前有效地址：" + e.message, 502, base);
  }

  // 直接 302 跳转到真实地址（播放器在国内，直接访问 CDN 不会被拦截）
  return new Response(null, {
    status: 302,
    headers: Object.assign({}, base, { Location: target }),
  });
}

/** 拉取上游播放列表并把相对地址改写为绝对地址 */
async function proxyPlaylist(src, base, head) {
  const r = await fetch(src, {
    method: head ? "HEAD" : "GET",
    headers: {
      "User-Agent": UA,
      "Accept": "*/*",
      "Referer": "https://live.xytv.cc/",
      "Origin": "https://live.xytv.cc"
    },
    cf: { cacheTtl: 0 },
  });

  if (!r.ok) {
    return plain("# 上游返回 HTTP " + r.status + "（" + hostOf(src) + "）", 502, base);
  }
  if (head) {
    return new Response(null, { status: 200, headers: withType(base) });
  }

  const text = await r.text();
  if (text.indexOf("#EXTM3U") < 0) {
    return plain("# 上游返回的不是 HLS 播放列表", 502, base);
  }

  return new Response(rewritePlaylist(text, src), {
    status: 200,
    headers: withType(base),
  });
}

/**
 * 改写播放列表：
 *   - 非注释行（分片）→ CDN 绝对地址
 *   - 注释行里的 URI="..."（子列表 / 密钥 / 初始化分片）→ 子列表继续走中转，
 *     其余同样转成绝对地址
 *   相对地址按播放列表自身的 URL 解析（与 hls.js 等播放器行为一致）
 */
function rewritePlaylist(body, base) {
  const baseUrl = new URL(base);
  const self = baseUrl.origin + "/p";
  const out = [];

  for (const raw of body.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) {
      out.push(raw);
      continue;
    }
    if (line.charAt(0) === "#") {
      out.push(line.replace(/URI="([^"]*)"/g, (_m, u) => 'URI="' + absolutize(u, baseUrl, self) + '"'));
    } else {
      out.push(absolutize(line, baseUrl, self));
    }
  }
  return out.join("\n");
}

function absolutize(ref, baseUrl, self) {
  if (/^data:/i.test(ref)) return ref;
  let abs;
  try {
    abs = new URL(ref, baseUrl).href;
  } catch (e) {
    return ref;
  }
  return IS_PLAYLIST.test(abs) ? self + "?src=" + encodeURIComponent(abs) : abs;
}

/** 读取 current.json，返回该频道当前有效直链 */
async function readCurrent(chKey, force) {
  // 普通请求用 15 秒一档的时间戳，兼顾新鲜度与对 GitHub 的请求量；
  // force=true 时用毫秒时间戳，确保绕过一切缓存
  const stamp = force ? Date.now() : Math.floor(Date.now() / 15000);
  const r = await fetch(JSON_URL + "?t=" + stamp, {
    headers: { "User-Agent": UA, "Cache-Control": "no-cache" },
    cf: { cacheTtl: 15 },
  });
  if (!r.ok) throw new Error("源文件 HTTP " + r.status);

  const j = await r.json();
  const u = j[chKey] || (j.channels && j.channels[chKey]);
  if (!u || !/^https?:\/\//i.test(u)) throw new Error("源文件中没有 " + chKey);
  return u;
}

/* -------------------------------------------------------------- 静态清单 */

function buildAllM3U(origin) {
  const head = [
    "#EXTM3U",
    "# 频道：信宜融媒综合广播 (FM98.9) ＋ 信宜综合电视台",
    "# 说明：永久固定地址，由中转实时跳转到当前有效直播流，无需维护。",
  ];
  for (const ch of CHANNELS) {
    head.push(
      '#EXTINF:-1 tvg-id="' + ch.id + '" tvg-name="' + ch.name +
      '" tvg-logo="' + ch.logo + '" group-title="广东",' + ch.label
    );
    head.push(origin + ch.path);
  }
  head.push("");
  return head.join("\n");
}

function helpPage(origin) {
  const rows = CHANNELS.map(
    (ch) =>
      '<tr><td>' + ch.label + '</td><td><code>' + origin + ch.path + "</code></td></tr>"
  ).join("");
  return (
    "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">" +
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
    "<title>信宜融媒 · 静态直播地址</title>" +
    "<style>body{font:15px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto," +
    "'PingFang SC','Microsoft YaHei',sans-serif;max-width:720px;margin:6vh auto;padding:0 20px;" +
    "color:#1a1d21;background:#f6f7f9}h1{font-size:20px}table{border-collapse:collapse;width:100%;" +
    "background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.07)}" +
    "td,th{padding:10px 14px;border-bottom:1px solid #eceef1;text-align:left;font-size:14px}" +
    "code{background:#f1f3f5;padding:2px 6px;border-radius:5px;font-size:13px;word-break:break-all}" +
    "p.note{color:#5b6470;font-size:13px}@media(prefers-color-scheme:dark){body{background:#14171a;color:#e8eaed}" +
    "table{background:#1e2226;box-shadow:none}td,th{border-color:#2b3036}code{background:#2b3036}}</style>" +
    "<h1>信宜融媒 · 静态直播地址</h1>" +
    "<table><tr><th>频道</th><th>永久播放地址</th></tr>" + rows + "</table>" +
    "<p class=\"note\">以上地址永久不变，可直接填进电视盒子 / 影视仓 / TVBox / VLC 等。" +
    "也可以订阅汇总清单：<code>" + origin + "/xinyi/all.m3u</code></p>" +
    "</html>"
  );
}

/* ---------------------------------------------------------------- 工具 */

function normPath(p) {
  let s = (p || "/").replace(/\/{2,}/g, "/");
  if (s.length > 1) s = s.replace(/\/+$/, "");
  return s || "/";
}

function channelFromQuery(v) {
  if (!v) return null;
  const s = String(v).toLowerCase();
  if (s === "radio" || s === "xinyi-radio") return "xinyi-radio";
  if (s === "tv" || s === "xinyi-tv") return "xinyi-tv";
  return null;
}

function hostAllowed(u) {
  try {
    return ALLOW_HOST.test(new URL(u).hostname);
  } catch (e) {
    return false;
  }
}

function hostOf(u) {
  try {
    return new URL(u).hostname;
  } catch (e) {
    return "?";
  }
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,HEAD,OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "no-store",
    "X-Relay": "xinyi-static",
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

