#!/usr/bin/env python3
"""文件名: sources/__init__.py
摘要: 源注册表 —— 唯一真相源。新增源只改这里，不改 server。
依赖: 各源模块
"""

from __future__ import annotations

from . import arxiv, biorxiv, crossref, openalex, unpaywall
from .base import Location, OaInfo, Provenance, SourceOutcome, Work

# ── 可检索的源（契约 §5）───────────────────────────────────────────
# 注意 arXiv 官方限速「每 3 秒 1 次、单连接、全机器合计」，
# 因此含它的检索会等待数秒——这是硬约束，不是故障。返回体里的
# waited_ms 会如实回报实际等了多久（用户裁决 A4：默认要打，用文档说明等待）。
SEARCHABLE: dict[str, object] = {
    "crossref": crossref,
    "arxiv": arxiv,
    # OpenAlex **始终已注册**，但不在默认集里（见下方 DEFAULT_SEARCH_SOURCES）。
    # 这样显式点名 sources=["openalex"] 时会得到「需要 key」的明确错误，
    # 而不是含糊的「未知源」——后者会让人以为是拼写问题。
    "openalex": openalex,
}

# 契约 §5 的默认检索集。arxiv / biorxiv / medrxiv 尚未实现，
# server 会自动跳过未注册的源并在 sources_failed 里标明，不静默吞掉。
# ⚠️ **OpenAlex 故意不在默认集里**（用户裁决「方案 C」）：
#    它的免费额度 $1/天是**无法在不付款下提高的硬顶**，撞上要等 UTC 午夜。
#    不启用时 Unpaywall（100k/天）+ Crossref（10 req/s）都没有这种性质。
#    要用？在 .env 里填 PERGAMINOS_OPENALEX_KEY 即可 —— 这是开关，不是架构依赖。
DEFAULT_SEARCH_SOURCES: list[str] = ["crossref", "arxiv"]

# ── 对账源（paper_oa）─────────────────────────────────────────────
# Unpaywall 是 OA 骨干（但**不能检索**）；Crossref 提供元数据底座。
OA_SOURCES: dict[str, object] = {
    "unpaywall": unpaywall,
    "crossref": crossref,
    "arxiv": arxiv,
    # ⚠️ bioRxiv/medRxiv **不进检索集**：官方 API 无全文检索（只有日期区间/DOI 单查）。
    # 它的价值在 paper_oa —— 原生提供预印本 → 正式版的 `published` 映射。
    "biorxiv": biorxiv,
    "openalex": openalex,
}
DEFAULT_OA_SOURCES: list[str] = ["unpaywall", "crossref"]

__all__ = [
    "Location", "OaInfo", "Provenance", "SourceOutcome", "Work",
    "SEARCHABLE", "DEFAULT_SEARCH_SOURCES", "OA_SOURCES", "DEFAULT_OA_SOURCES",
    "arxiv", "crossref", "unpaywall",
]
