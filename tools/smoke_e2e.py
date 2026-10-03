#!/usr/bin/env python3
"""端到端实测：用真实查询与真实 DOI 跑通 paper_search / paper_oa。

不伪造数据——短时无网就会看到显式的 NETWORK 错误码，而不是"0 条结果"，
这本身就是契约 §8「不许静默返回空」的验证。

用法: python tools/smoke_e2e.py ["自定义查询"]
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _borrow_email() -> str:
    """借用既有 email（不打印其值）。

    两个来源都能读：本项目的 .env 用 PERGAMINOS_*；
    上一代 paper-search-mcp 的 .env 用 PAPERSEARCH_* / UNPAYWALL_EMAIL 等
    —— 那是**别人的命名空间**，这里只读不写，故保留其原名。
    """
    envs = [
        Path.home() / "ai-services" / "paper-search-mcp" / ".env",
        ROOT / ".env",
    ]
    accepted = (
        "PERGAMINOS_UNPAYWALL_EMAIL",          # 本项目
        "PAPERSEARCH_UNPAYWALL_EMAIL",         # 上一代 paper-search-mcp
        "UNPAYWALL_EMAIL",
        "PAPER_SEARCH_MCP_UNPAYWALL_EMAIL",
    )
    for p in envs:
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in accepted and v.strip():
                return v.strip()
    return ""


def main() -> int:
    from pergaminos.server import do_oa, do_search

    email = _borrow_email()
    if email:
        os.environ["PERGAMINOS_UNPAYWALL_EMAIL"] = email
        print(f"email: 已借用（长度 {len(email)}）")
    else:
        print("email: 未找到 —— paper_oa 会走 CONFIG_MISSING（这也是正确行为）")

    query = sys.argv[1] if len(sys.argv) > 1 else (
        "gas giant planet mass-radius relation radius insensitive Jupiter")
    doi = "10.1038/nature12373"      # Unpaywall 官方文档示例 DOI

    print("\n" + "=" * 72)
    print(f"[1] paper_search({query!r})")
    print("=" * 72)
    t0 = time.monotonic()
    r = do_search({"query": query, "per_page": 3})
    print(f"耗时 {time.monotonic() - t0:.2f}s")
    print(f"  sources_asked    {r.get('sources_asked')}")
    print(f"  sources_answered {r.get('sources_answered')}")
    print(f"  sources_failed   {r.get('sources_failed')}")
    print(f"  counts           {r.get('counts')}")
    for g in r.get("groups", []):
        print(f"  ── [{g['source']}] {g['count']} 条 · waited={g['waited_ms']}ms")
        for w in g["results"][:3]:
            au = ", ".join(w["authors"][:2]) or "(无作者)"
            print(f"     · {w['year']}  {w['title'][:70]}")
            print(f"       {au} | {w['venue'][:40]} | doi={w['ids'].get('doi', '-')}")
            print(f"       abstract {len(w['abstract'])} 字符"
                  f"{' (已截断)' if w['abstract_truncated'] else ''}"
                  f" | provenance={w['provenance']['source']}")

    print("\n" + "=" * 72)
    print(f"[2] paper_oa({doi!r})")
    print("=" * 72)
    t0 = time.monotonic()
    r2 = do_oa({"identifiers": [doi]})
    print(f"耗时 {time.monotonic() - t0:.2f}s")
    for it in r2.get("items", []):
        print(f"  input  {it['input']}")
        print(f"  match  {it['match']}")
        w = it.get("work")
        if w:
            oa = w.get("oa") or {}
            print(f"  title  {w['title'][:70]}")
            print(f"  year   {w['year']}")
            print(f"  oa     status={oa.get('status')} is_oa={oa.get('is_oa')} "
                  f"repo={oa.get('in_repository')} doaj={oa.get('journal_is_in_doaj')}")
            print(f"  url    {(oa.get('url') or '-')[:80]}")
            print(f"  locs   {len(w.get('locations') or [])} 处")
            for loc in (w.get("locations") or [])[:4]:
                print(f"         · {loc['host_type']:<11}{loc['version']:<18}"
                      f"{(loc['url'] or '-')[:60]}")
            print(f"  prov   {w['provenance']['source']} | {w['provenance']['endpoint'][:70]}")
        if it.get("conflicts"):
            print(f"  conflicts {it['conflicts']}")
        if it.get("source_errors"):
            print(f"  source_errors {it['source_errors']}")

    print("\n" + "=" * 72)
    print("[3] 工具面自检")
    print("=" * 72)
    from pergaminos.server import _TOOLS
    names = [t.name for t in _TOOLS]
    print(f"  工具数 {len(names)}: {names}")
    bad = [n for n in names if "download" in n.lower() or "read_pdf" in n.lower()]
    print(f"  含下载类工具: {bad if bad else '无 ✅（靠 schema 而非 prompt）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
