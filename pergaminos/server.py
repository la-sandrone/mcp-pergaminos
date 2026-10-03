#!/usr/bin/env python3
"""文件名: server.py
摘要: MCP 服务器（mcp 2.x）。对外**只有两个工具**：paper_search / paper_oa。
     特别地：**不存在任何下载工具**——靠 schema 而非 prompt 实现，
     模型调不到不存在的工具。
依赖: mcp>=2.2, ./config, ./net, ./sources

运行: python -m pergaminos.server    （stdio）
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from . import __version__, config
from .sources import (
    DEFAULT_OA_SOURCES,
    DEFAULT_SEARCH_SOURCES,
    OA_SOURCES,
    SEARCHABLE,
    SourceOutcome,
    Work,
)

logger = logging.getLogger("pergaminos.server")

SERVER_NAME = "pergaminos"

# ── 工具描述（自文档：把等待、边界、不下载理由都写进去）───────────────
_DESC_SEARCH = """检索学术论文，返回**元数据**（题名/作者/年份/摘要/DOI），**不下载全文**。

⚠️ **arXiv 的官方限速是「每 3 秒 1 次、单连接、且对你控制的所有机器合计」**，
因此包含 arXiv 的检索会等待数秒——这是 arXiv 的硬约束，不是本工具的问题。
返回体里每组的 `waited_ms` 会如实告诉你实际等了多久。

各源的覆盖边界不同，所以默认**按源分组**返回，不做推断式合并：
  · Crossref —— 期刊论文元数据主力；**搜不到 arXiv 预印本**
    （arXiv 的 DOI 由 DataCite 注册，不在 Crossref）
  · arXiv —— 预印本；其 DOI 不在 Crossref
  · bioRxiv / medRxiv —— 预印本，无全文检索（仅日期区间/DOI 单查）
若确要合并，用 `merge=true`——它只按 **DOI 精确**合并，绝不按标题瞎猜。

不下载全文的理由见 README（版权 / 限速 / 静默降级 / 成本）。拿到 DOI 与路径后由人决定怎么取。
"""

_DESC_OA = """对账：给定 DOI / arXiv id / 标题，报告其**开放获取（OA）状态与收录位置**。

数据源分工：
  · **Unpaywall** 是 OA 权威 —— 给出 `oa_status`（gold/hybrid/bronze/green/closed）、
    全部 OA 位置、是否存在仓库副本（`has_repository_copy`，即预印本收录判据）、
    以及期刊是否在 DOAJ（`journal_is_in_doaj`）。
  · **Crossref** 提供元数据底座。**它不提供 OA 信息**——这不是缺数据，是它本来就不答这个。

两源不一致时**两个值都返回**（在 `conflicts` 里），不替你做判断。

按标题查询是**模糊匹配**：会返回候选与相似度，并在 `match.kind` 里自报
"这不是精确匹配"。绝不在多条候选时静默取第一条。

本工具只报告路径，**不下载全文**。
"""


def _json(obj: Any) -> str:
    """出站 JSON。

    ⚠️ `ensure_ascii=True`（默认）是纪律，不是巧合：它把非 ASCII 转义为
    \\uXXXX，因此任何控制台编码（含 Windows 的 GBK/CP936）都不会乱码或崩。
    """
    return json.dumps(obj, ensure_ascii=True, indent=1, default=str)


def _run_sources(fn, names: list[str], registry: dict[str, Any]) -> list[SourceOutcome]:
    """并行跑各源。**每源自带预算**，所以总耗时 ≈ max(各源) 而非求和。

    为什么并行：上一代用 `asyncio.gather` 且无每源预算，
    结果是「等最慢协程 ⇒ 每次搜索白等 15 秒」。
    并行 + 每源预算，慢源只拖慢自己。
    """
    jobs: list[tuple[str, Any]] = []
    out: list[SourceOutcome] = []
    for n in names:
        mod = registry.get(n)
        if mod is None:
            out.append(SourceOutcome(source=n, error={
                "code": "UNKNOWN_SOURCE",
                "detail": f"未知或未实现的源 {n!r}；已实现：{sorted(registry)}",
            }))
            continue
        jobs.append((n, mod))

    if not jobs:
        return out

    with ThreadPoolExecutor(max_workers=min(len(jobs), 8)) as pool:
        futs = {pool.submit(fn, mod, n): n for n, mod in jobs}
        for fut, n in futs.items():
            try:
                out.append(fut.result())
            except Exception as exc:                 # 源内部炸了也要显式落账
                logger.exception("[%s] 未捕获异常", n)
                out.append(SourceOutcome(source=n, error={
                    "code": "INTERNAL", "detail": repr(exc)}))
    return out


def _search_one(mod: Any, name: str, query: str, per_page: int,
                year: str, abstract_max: int) -> SourceOutcome:
    return mod.search(query, limit=per_page, year=year, abstract_max=abstract_max)


def _oa_one(mod: Any, name: str, ident: str) -> SourceOutcome:
    if name == "arxiv":
        return mod.get_by_id(ident)
    if name in ("unpaywall", "crossref"):
        return mod.get_by_doi(ident)
    return SourceOutcome(source=name, error={
        "code": "UNKNOWN_SOURCE", "detail": f"{name} 无 DOI 查询能力"})


# ── paper_search ───────────────────────────────────────────────────
def do_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": {"code": "BAD_REQUEST", "detail": "query 不能为空"}}

    sources = args.get("sources") or DEFAULT_SEARCH_SOURCES
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    per_page = int(args.get("per_page") or 20)
    year = (args.get("year") or "").strip()
    abstract_max = int(args.get("abstract_max_chars") or 600)
    merge = bool(args.get("merge"))

    outcomes = _run_sources(
        lambda mod, n: _search_one(mod, n, query, per_page, year, abstract_max),
        list(sources), SEARCHABLE,
    )

    asked, answered, failed, groups = list(sources), [], [], []
    for o in outcomes:
        if o.ok:
            answered.append(o.source)
            groups.append({
                "source": o.source,
                "count": len(o.results),
                "waited_ms": o.waited_ms,
                # 上游报告的命中总数。arXiv 的 OR 语义下常是几十万 ——
                # 这不是"结果很多"，而是"命中的是模糊池，真正排序靠 relevance"。
                "total_available": o.total_available,
                "results": [w.as_dict() for w in o.results],
            })
        else:
            failed.append({"source": o.source, **(o.error or {})})

    merged = None
    if merge:
        by_key: dict[str, dict] = {}
        for o in outcomes:
            for w in o.results:
                k = w.key()
                if k not in by_key:
                    d = w.as_dict()
                    d["sources"] = [o.source]
                    by_key[k] = d
                else:
                    by_key[k]["sources"].append(o.source)
        merged = list(by_key.values())

    return {
        "query": query,
        "sources_asked": asked,
        "sources_answered": answered,
        "sources_failed": failed,
        "groups": groups,
        "merged": merged,
        "counts": {
            "groups": len(groups),
            "unique_by_doi": len(merged) if merged is not None else None,
            "total": sum(g["count"] for g in groups),
        },
    }


# ── paper_oa ───────────────────────────────────────────────────────
def _title_candidates(title: str, limit: int = 5) -> dict[str, Any]:
    """标题 → DOI 的**模糊**匹配。**自报家门**（契约 §3.2 / 裁决 A5）。

    绝不静默取第一条：候选全部列出，并标 confidence。
    """
    from .sources import crossref as cr

    out = cr.search(title, limit=limit)
    if not out.ok:
        return {"kind": "not_found", "confidence": 0.0, "candidates": [],
                "error": out.error}

    q_tokens = {t for t in title.lower().split() if t}
    cands = []
    for w in out.results:
        t_tokens = {t for t in (w.title or "").lower().split() if t}
        score = len(q_tokens & t_tokens) / len(q_tokens | t_tokens) if q_tokens | t_tokens else 0.0
        cands.append({
            "title": w.title,
            "doi": w.ids.get("doi", ""),
            "year": w.year,
            "score": round(score, 3),
        })
    cands.sort(key=lambda c: -c["score"])
    best = cands[0] if cands else None
    return {
        "kind": "title_fuzzy",
        "confidence": best["score"] if best else 0.0,
        "candidates": cands if len(cands) > 1 else [],
        "resolved_doi": best["doi"] if best else "",
    }


def _classify(ident: str) -> str:
    s = ident.strip()
    low = s.lower()
    if low.startswith("10.") and "/" in s:
        return "doi"
    if low.startswith("arxiv:") or low.startswith("arxiv "):
        return "arxiv_id"
    if s.count(".") == 1 and s.replace(".", "").isdigit():
        return "arxiv_id"
    return "title"


def _conflicts(unp: Work | None, cr: Work | None) -> list[dict[str, Any]]:
    """两源不一致时**两个都报**（契约 §3.2 / 裁决 Q6）。"""
    if unp is None or cr is None:
        return []
    out: list[dict[str, Any]] = []

    def cmp(field: str, a: Any, b: Any) -> None:
        if a and b and str(a).strip() != str(b).strip():
            out.append({
                "field": field,
                "values_by_source": {"unpaywall": a, "crossref": b}
                                    if field != "oa_status" else
                                    {"unpaywall": a, "crossref": None},
                "note": "" if field != "oa_status"
                        else "Crossref 不提供 OA 状态——这不是分歧，是它不答这个字段",
            })

    cmp("title", unp.title if unp else "", cr.title if cr else "")
    cmp("year", unp.year if unp else None, cr.year if cr else None)
    cmp("venue", unp.venue if unp else "", cr.venue if cr else "")
    return out


def do_oa(args: dict[str, Any]) -> dict[str, Any]:
    idents = args.get("identifiers") or []
    if isinstance(idents, str):
        idents = [s.strip() for s in idents.split(",") if s.strip()]
    if not idents:
        return {"error": {"code": "BAD_REQUEST", "detail": "identifiers 不能为空"}}

    sources = args.get("sources") or DEFAULT_OA_SOURCES
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]

    items = []
    for ident in idents:
        kind = _classify(ident)
        match: dict[str, Any] = {"kind": "doi_exact", "confidence": 1.0}
        doi = ident.strip().lower()

        if kind == "title":
            m = _title_candidates(ident)
            match = {k: v for k, v in m.items() if k != "resolved_doi"}
            doi = m.get("resolved_doi", "")
            if not doi:
                items.append({"input": ident, "match": match, "work": None,
                              "conflicts": []})
                continue
            match = dict(match, kind="title_fuzzy")
        elif kind == "arxiv_id":
            # ⚠️ arXiv 的 DOI 由 **DataCite** 注册，不在 Crossref ⇒ Unpaywall/Crossref
            # 都查不到它。但 arXiv 源上有 arxiv:doi / arxiv:journal_ref 字段，
            # 那是**预印本 → 正式版**的映射，所以先查 arXiv 拿到正式版 DOI，
            # 再拿这个 DOI 去查 Unpaywall。两步，但都是真的。
            r0 = _run_sources(lambda mod, n: _oa_one(mod, n, ident),
                              ["arxiv"], OA_SOURCES)
            aw = (r0[0].results[0]
                  if r0 and r0[0].ok and r0[0].results else None)
            pub_doi = ((aw.preprint or {}).get("published_doi") if aw else "") or ""
            matched = {"kind": "arxiv_id", "confidence": 1.0}
            item: dict[str, Any] = {"input": ident, "resolved_doi": pub_doi,
                                    "match": matched,
                                    "work": aw.as_dict() if aw else None,
                                    "conflicts": []}
            if not aw:
                item["source_errors"] = [{"source": "arxiv",
                                          **(r0[0].error if r0 else {})}]
            elif pub_doi:
                # 有正式版 DOI —— 顺带把 OA 也对掉，让这条记录可用
                more = _run_sources(lambda mod, n: _oa_one(mod, n, pub_doi),
                                    ["unpaywall"], OA_SOURCES)
                if more and more[0].ok and more[0].results:
                    item["published_version"] = more[0].results[0].as_dict()
                item["match"]["note"] = (
                    "arXiv 记录给出正式版 DOI，已一并查其 OA 状态")
            else:
                item["match"]["note"] = (
                    "该预印本未登记正式版 DOI（arXiv 的 arxiv:doi 字段为空）"
                    "——即作者提交后未回填，或尚未正式发表")
            items.append(item)
            continue

        outcomes = _run_sources(
            lambda mod, n: _oa_one(mod, n, doi), list(sources), OA_SOURCES)

        by: dict[str, SourceOutcome] = {o.source: o for o in outcomes}
        unp_w = (by.get("unpaywall").results[0]
                 if by.get("unpaywall") and by["unpaywall"].ok
                 and by["unpaywall"].results else None)
        cr_w = (by.get("crossref").results[0]
                if by.get("crossref") and by["crossref"].ok
                and by["crossref"].results else None)

        # 主记录优先取 Unpaywall（OA 权威）；它没有则退回 Crossref（元数据）
        primary = unp_w or cr_w
        item: dict[str, Any] = {
            "input": ident,
            "resolved_doi": doi,
            "match": match,
            "work": primary.as_dict() if primary else None,
            "conflicts": _conflicts(unp_w, cr_w),
        }
        errs = [{"source": o.source, **(o.error or {})}
                for o in outcomes if not o.ok]
        if errs:
            item["source_errors"] = errs
        items.append(item)

    return {"items": items}


# ── MCP 装配 ───────────────────────────────────────────────────────
_TOOLS = [
    Tool(
        name="paper_search",
        description=_DESC_SEARCH,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词（支持多词描述性长查询）"},
                "sources": {
                    "type": "array", "items": {"type": "string"},
                    "description": f"要查的源，默认 {DEFAULT_SEARCH_SOURCES}；"
                                   f"已实现: {sorted(SEARCHABLE)}",
                },
                "per_page": {"type": "integer", "description": "每源条数，默认 20（上限 100）"},
                "year": {"type": "string", "description": '年份过滤，如 "2020" / "2016-2020" / "-2015"'},
                "merge": {"type": "boolean", "description": "true = 按 DOI 精确合并（默认 false，按源分组）"},
                "abstract_max_chars": {"type": "integer",
                                       "description": "摘要截断字符数，默认 600，0 = 不截断"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="paper_oa",
        description=_DESC_OA,
        input_schema={
            "type": "object",
            "properties": {
                "identifiers": {
                    "type": "array", "items": {"type": "string"},
                    "description": "DOI / arXiv id / 标题，可混用",
                },
                "sources": {
                    "type": "array", "items": {"type": "string"},
                    "description": f"默认 {DEFAULT_OA_SOURCES}；已实现: {sorted(OA_SOURCES)}",
                },
            },
            "required": ["identifiers"],
        },
    ),
]


async def _on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
    return ListToolsResult(tools=_TOOLS)


async def _on_call_tool(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    args = dict(params.arguments or {})
    try:
        if name == "paper_search":
            payload = await asyncio.to_thread(do_search, args)
        elif name == "paper_oa":
            payload = await asyncio.to_thread(do_oa, args)
        else:
            payload = {"error": {"code": "BAD_REQUEST",
                                 "detail": f"未知工具 {name!r}"}}
    except Exception as exc:
        logger.exception("tool %s 未捕获异常", name)
        payload = {"error": {"code": "INTERNAL", "detail": repr(exc)}}

    is_err = "error" in payload
    return CallToolResult(
        content=[TextContent(type="text", text=_json(payload))],
        is_error=is_err,
    )


server = Server(
    SERVER_NAME,
    version=__version__,
    title="pergaminos",
    description="学术论文检索与 OA 对账（只报路径，不下载全文）",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main() -> None:
    logging.basicConfig(
        level=config.get("LOG_LEVEL") or "INFO",
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config.log_prologue()          # ← 启动打印生效配置（防「静默指错」）
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
