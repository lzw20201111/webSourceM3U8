/**
 * 本地自测：直接在 Node 里跑 Cloudflare Pages 的 _worker.js
 * =========================================================
 * 做法：把脚本里的 DATA_BASES 换成指向本机的静态服务（提供 channels.json / current.json），
 * 然后真实调用 worker.fetch，验证 /ch/<id>.m3u8 能否拿到 HLS 列表、分片是否已改写为绝对地址。
 *
 * 用法：node scripts/local_relay_test.mjs [抽样数量]
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")), "..");
const PORT = 8899;
const SAMPLE = Number(process.argv[2] || 12);

/* ① 本机静态服务：提供 channels.json / current.json */
const server = http.createServer((req, res) => {
  const name = req.url.split("?")[0].replace(/^\//, "");
  const file = path.join(ROOT, name);
  if (!fs.existsSync(file)) { res.writeHead(404).end("nf"); return; }
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(fs.readFileSync(file));
});
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

/* ② 生成一份把数据源指向本机的 worker 副本 */
const src = fs.readFileSync(path.join(ROOT, "pages", "_worker.js"), "utf-8");
const patched = src.replace(
  /const DATA_BASES = \[[\s\S]*?\];/,
  `const DATA_BASES = ["http://127.0.0.1:${PORT}"];`
);
if (patched === src) { console.error("没找到 DATA_BASES，替换失败"); process.exit(1); }
const tmp = path.join(os.tmpdir(), "worker_local_" + Date.now() + ".mjs");
fs.writeFileSync(tmp, patched);

/* ③ 加载并真实调用 */
const worker = (await import(pathToFileURL(tmp).href)).default;

const data = JSON.parse(fs.readFileSync(path.join(ROOT, "channels.json"), "utf-8"));
const ids = Object.keys(data.channels);
console.log(`channels.json：${ids.length} 个可用频道（${data.time}）`);

const pick = [];
const step = Math.max(1, Math.floor(ids.length / SAMPLE));
for (let i = 0; i < ids.length && pick.length < SAMPLE; i += step) pick.push(ids[i]);

const ORIGIN = `http://127.0.0.1:${PORT}`;
let ok = 0, bad = 0;

for (const id of pick) {
  const ch = data.channels[id];
  const req = new Request(`${ORIGIN}/ch/${id}.m3u8`);
  let line = `  ${ch.n}（${ch.g}）`;
  try {
    const res = await worker.fetch(req, {}, {});
    const body = await res.text();
    if (res.status !== 200 || !body.includes("#EXTM3U")) {
      console.log(`  [FAIL] ${line} → HTTP ${res.status} ${body.slice(0, 70).replace(/\n/g, "|")}`);
      bad++; continue;
    }
    const segs = body.split(/\r?\n/).filter((l) => l && !l.startsWith("#"));
    const rel = segs.filter((l) => !/^https?:\/\//i.test(l) && !l.startsWith("data:"));
    if (!segs.length) { console.log(`  [FAIL] ${line} → 列表为空`); bad++; continue; }
    if (rel.length) {
      console.log(`  [WARN] ${line} → ${rel.length}/${segs.length} 条仍是相对地址，例：${rel[0]}`);
    }
    console.log(`  [OK  ] ${line} → ${segs.length} 条地址，首条 ${segs[0].slice(0, 70)}`);
    ok++;
  } catch (e) {
    console.log(`  [FAIL] ${line} → ${e.message}`);
    bad++;
  }
}

console.log(`\n自测结果：OK ${ok} / FAIL ${bad}（抽样 ${pick.length} 条）`);

/* ④ 顺带看看汇总清单 */
try {
  const res = await worker.fetch(new Request(`${ORIGIN}/webview.m3u`), {}, {});
  const body = await res.text();
  console.log(`/webview.m3u → HTTP ${res.status}，${body.split("\n").length} 行`);
} catch (e) {
  console.log("/webview.m3u 失败：" + e.message);
}

server.close();
fs.unlinkSync(tmp);
process.exit(0);
