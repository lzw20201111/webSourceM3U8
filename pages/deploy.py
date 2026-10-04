# -*- coding: utf-8 -*-
"""把中转脚本以 Pages「高级模式」部署：正确构造 _worker.bundle

Pages 直传时，Functions / 高级模式的代码不是以静态文件上传的，
而是要放在 multipart 字段 _worker.bundle 里，内容是【Workers 上传表单的序列化字节】。
本脚本手工构造这个内部 multipart，不依赖 wrangler。
"""
import hashlib
import json
import os
import sys

import requests

TOKEN = os.environ["CF_TOKEN"]
ACCOUNT = os.environ.get("CF_ACCOUNT", "d519224859f2c66983423e26db5fb620")
PROJECT = os.environ.get("PROJECT_NAME", "xinyi-relay")
API = "https://api.cloudflare.com/client/v4"
H = {"Authorization": "Bearer " + TOKEN, "User-Agent": "wb-deploy"}

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "webSourceM3U8", "pages", "_worker.js")

COMPAT_DATE = "2024-09-23"
MODULE_NAME = "index.mjs"
MODULE_MIME = "application/javascript+module"


def build_inner_multipart(js_source: bytes) -> bytes:
    """构造 Workers 上传表单的原始 multipart 字节（即 _worker.bundle 的内容）"""
    boundary = "----wbpagesbundle" + hashlib.md5(js_source).hexdigest()[:16]
    metadata = {
        "main_module": MODULE_NAME,
        "compatibility_date": COMPAT_DATE,
        "compatibility_flags": [],
    }
    parts = []
    # ① metadata（普通字段）
    parts.append(
        ('--%s\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n' % boundary).encode()
        + json.dumps(metadata).encode() + b"\r\n"
    )
    # ② 主模块（文件字段）
    parts.append(
        ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
         'Content-Type: %s\r\n\r\n' % (boundary, MODULE_NAME, MODULE_NAME, MODULE_MIME)).encode()
        + js_source + b"\r\n"
    )
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts)


def deploy():
    js = open(SRC, "rb").read()
    bundle = build_inner_multipart(js)
    print("模块 %s  %d 字节  →  _worker.bundle %d 字节" % (MODULE_NAME, len(js), len(bundle)))

    files = [
        ("branch", (None, "main")),
        ("commit_message", (None, "advanced mode deploy")),
        ("manifest", (None, json.dumps({}), "application/json")),
        ("_worker.bundle", ("_worker.bundle", bundle, "application/octet-stream")),
    ]
    r = requests.post("%s/accounts/%s/pages/projects/%s/deployments" % (API, ACCOUNT, PROJECT),
                      headers=H, files=files, timeout=180)
    print("上传 ->", r.status_code)
    j = r.json()
    if not j.get("success"):
        print("错误:", json.dumps(j.get("errors"), ensure_ascii=False)[:400])
        return None
    did = j["result"]["id"]
    d = requests.get("%s/accounts/%s/pages/projects/%s/deployments/%s" % (API, ACCOUNT, PROJECT, did),
                     headers=H, timeout=40).json()["result"]
    print("uses_functions =", d.get("uses_functions"))
    return did


if __name__ == "__main__":
    if not deploy():
        sys.exit(1)
    print("完成")
