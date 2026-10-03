#!/usr/bin/env python3
"""MCP stdio 握手自检：起服务 → initialize → tools/list → tools/call。

验证的是协议层，不是业务逻辑：服务能不能起、工具面声明对不对、
工具描述里有没有把「等待」和「不下载」写清楚。

用法: python tools/verify_mcp_stdio.py [--call]
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    py = sys.executable
    env = dict(os.environ)
    # 借一下 email，避免 paper_oa 因 CONFIG_MISSING 失败。
    # 读取时认两个命名空间（本项目 PERGAMINOS_*，上一代 paper-search-mcp 用
    # PAPERSEARCH_*）；写入只用本项目自己的名字。
    for p in (Path.home() / "ai-services" / "paper-search-mcp" / ".env", ROOT / ".env"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(("PERGAMINOS_UNPAYWALL_EMAIL",
                                            "PAPERSEARCH_UNPAYWALL_EMAIL",
                                            "UNPAYWALL_EMAIL")):
                    k, _, v = line.partition("=")
                    if v.strip():
                        env["PERGAMINOS_UNPAYWALL_EMAIL"] = v.strip()
                    break

    proc = subprocess.Popen(
        [py, "-m", "pergaminos.server"],
        cwd=str(ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18",
                         "capabilities": {},
                         "clientInfo": {"name": "selfcheck", "version": "1"}}})
        r = recv()
        if not r or "result" not in r:
            print(f"❌ initialize 失败: {r}")
            return 1
        si = r["result"].get("serverInfo", {})
        print(f"  ✅ initialize  serverInfo={si}")
        caps = r["result"].get("capabilities", {})
        print(f"     capabilities={sorted(caps)}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r = recv()
        tools = r["result"]["tools"]
        names = [t["name"] for t in tools]
        print(f"\n  ✅ tools/list  {len(tools)} 个工具: {names}")

        bad = [n for n in names if any(w in n.lower()
               for w in ("download", "pdf", "read_paper", "fetch_fulltext"))]
        print(f"     下载类工具: {bad if bad else '无 ✅（靠 schema 而非 prompt）'}")

        for t in tools:
            d = t.get("description", "")
            keys = [k for k, kw in (("等arXiv", "3 秒"), ("不下载", "不下载全文"),
                                    ("waited", "waited_ms"), ("边界", "DataCite"))
                    if kw in d]
            print(f"     [{t['name']}] desc {len(d)} 字符 · 自文档要点 {keys}")
            assert "inputSchema" in t, "缺 inputSchema"

        if "--call" in sys.argv:
            print("\n  --- tools/call paper_search ---")
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "paper_search",
                             "arguments": {"query": "helium triplet excitation cross section",
                                           "per_page": 2}}})
            r = recv()
            body = json.loads(r["result"]["content"][0]["text"])
            print(f"     isError={r['result'].get('isError')}")
            print(f"     sources_answered={body.get('sources_answered')} "
                  f"failed={body.get('sources_failed')}")
            for g in body.get("groups", []):
                print(f"     [{g['source']}] {g['count']} 条 waited={g['waited_ms']}ms")
                for w in g["results"][:2]:
                    print(f"        · {w['year']} {w['title'][:62]}")

            print("\n  --- 出站 JSON 是否 ASCII-safe（Windows GBK 控制台）---")
            raw = r["result"]["content"][0]["text"]
            print(f"     非 ASCII 字符数: {sum(1 for c in raw if ord(c) > 127)}"
                  f"  {'✅ 全转义' if all(ord(c) < 128 for c in raw) else '❌ 有裸非 ASCII'}")
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    err = proc.stderr.read()
    conf = [ln for ln in err.splitlines() if "conf" in ln or "ua " in ln]
    print(f"\n  --- 启动打印（防静默指错）---")
    print(f"     共 {len(err.splitlines())} 行 stderr，其中配置行 {len(conf)} 行")
    for ln in conf[:8]:
        print(f"     {ln}")
    nonascii = sum(1 for c in err if ord(c) > 127)
    print(f"     stderr 非 ASCII 字符数: {nonascii}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
