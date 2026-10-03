#!/usr/bin/env python3
"""文件名: sources/biorxiv.py
摘要: bioRxiv / medRxiv 源 —— 预印本，**只做 DOI 单查，不做检索**。
     官方口径（2026-10-03 核实，来源 api.biorxiv.org）：
       · 单篇 https://api.biorxiv.org/details/{server}/{doi}/na/{format}
       · 正式版 https://api.biorxiv.org/pubs/{server}/{doi}/na/{format}
       · 区间  https://api.biorxiv.org/details/{server}/{interval}/{cursor}/{format}
       · 分页  30 篇/次（details）、100 篇/次（pubs）
       · **官方不提供全文检索**（只有日期区间 / DOI 单查）

⚠️ 因此本源**不进检索默认集**（契约 §5 把它列进去是错的，已修正）。
   它的价值在 paper_oa：返回里有两个关键字段——
       · `published`  —— 该预印本对应的**正式版 DOI**（预印本 → 正式版映射）
       · `license`    —— 预印本自己的许可
   这正好补上 arXiv 那侧 `arxiv:doi` 字段稀疏的缺口。

返回字段：doi, title, authors, author_corresponding,
         author_corresponding_institution, date, version, type, license,
         category, jats xml path, abstract, published, server
依赖: ./base, ../net, ../config
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from ..net import RetryExhausted, request
from .base import Location, OaInfo, Provenance, SourceOutcome, Work, clean_text

logger = logging.getLogger("pergaminos.sources.biorxiv")

SOURCE = "biorxiv"
BASE = "https://api.biorxiv.org"

# DOI 前缀 → 服务器名。**这是判据，不是猜测**：
#   10.1101/ 是 Cold Spring Harbor 给 bioRxiv/medRxiv 分配的
#   两者的 DOI 前缀相同，所以**光看 DOI 分不出是哪个 server**，
#   必须两个都试（本源的做法），或由调用方显式指定。
SERVER_HINTS = {
    "biorxiv": ("biorxiv",),
    "medrxiv": ("medrxiv",),
}
_ALL_SERVERS = ("biorxiv", "medrxiv")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _err(resp: httpx.Response, what: str) -> dict[str, str] | None:
    if resp.status_code == 404:
        return {"code": "NOT_FOUND", "detail": f"{what} 无此记录"}
    if resp.status_code == 429:
        return {"code": "RATE_LIMITED", "detail": f"{what} 限速（官方未公布具体值）"}
    if resp.status_code >= 500:
        return {"code": "UPSTREAM", "detail": f"{what} 服务端 {resp.status_code}"}
    return None


def _rows(payload: object) -> list[dict]:
    """biorxiv 的响应恒为 {"messages": [...], "collection": [...]}。
    结构不符即抛（由调用方翻成 UPSTREAM_CHANGED）。"""
    if not isinstance(payload, dict):
        raise TypeError(f"顶层不是 dict：{type(payload).__name__}")
    if "collection" not in payload:
        raise KeyError("缺 collection 字段")
    rows = payload["collection"]
    if not isinstance(rows, list):
        raise TypeError("collection 不是 list")
    return [r for r in rows if isinstance(r, dict)]


def _fetch(path: str, waited: list[float]) -> tuple[httpx.Response | None, dict | None]:
    try:
        resp = request(SOURCE, "GET", f"{BASE}{path}", waited=waited)
    except RetryExhausted as exc:
        return None, {"code": "NETWORK", "detail": str(exc)}
    err = _err(resp, "bioRxiv/medRxiv")
    if err:
        return None, err
    return resp, None


def _to_work(row: dict, server: str, doi: str) -> Work:
    pub_doi = (row.get("published") or "").strip().lower()
    # biorxiv 的 published 字段在未发表时是 "NA"（字面量），不是空串
    if pub_doi in ("na", "n/a", "none"):
        pub_doi = ""

    doi = (row.get("doi") or doi).strip().lower()
    ids = {"doi": doi, "biorxiv": doi}
    if pub_doi and pub_doi != doi:
        # 正式版 DOI 与预印本 DOI 不同 —— 两个都记（官方明示一篇作品可有多个 DOI）
        ids["published_doi"] = pub_doi

    authors = [a.strip() for a in (row.get("authors") or "").split(";") if a.strip()]

    date = (row.get("date") or "").strip()
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None

    locations = [Location(
        source=f"{server}", host_type="repository", version="submittedVersion",
        url=f"https://www.{server}.org/content/{doi}v{row.get('version', '')}",
        license=row.get("license") or "",
    )]
    if pub_doi:
        locations.append(Location(source="published", host_type="publisher",
                                  version="publishedVersion",
                                  url=f"https://doi.org/{pub_doi}"))

    corr = clean_text(row.get("author_corresponding"))
    inst = clean_text(row.get("author_corresponding_institution"))

    return Work(
        ids=ids,
        title=clean_text(row.get("title")),
        authors=authors,
        year=year,
        venue=clean_text(row.get("category")),
        type="preprint",
        abstract=clean_text(row.get("abstract")),
        doi_url=f"https://doi.org/{doi}",
        oa=OaInfo(is_oa=True, status="green",
                  url=f"https://www.{server}.org/content/{doi}v{row.get('version', '')}",
                  in_repository=True),
        locations=locations,
        preprint={"server": server, "version": str(row.get("version") or ""),
                  "published_doi": pub_doi, "license": row.get("license") or "",
                  "date": date, "corresponding": corr, "institution": inst},
        provenance=Provenance(
            source=SOURCE,
            endpoint=f"GET {BASE}/details/{server}/{doi}/na/json",
            fetched_at=_now(),
        ),
    )


def get_by_doi(doi: str, abstract_max: int = 600, server: str = "") -> SourceOutcome:
    """按 DOI 查 bioRxiv / medRxiv。

    ⚠️ 两者的 DOI 前缀**都是 10.1101/**，光看 DOI 分不出来 ——
    所以默认依次试两个 server，命中即停。这是判据缺失导致的必要尝试，
    不是"猜"：我们报的是**实际命中的那个 server**，并写在 provenance 里。
    """
    doi = doi.strip().lower()
    if not doi:
        return SourceOutcome(source=SOURCE, error={"code": "NOT_FOUND",
                                                   "detail": "空 DOI"})
    servers = SERVER_HINTS.get(server, _ALL_SERVERS) if server else _ALL_SERVERS

    waited: list[float] = []
    tried: list[str] = []
    for srv in servers:
        tried.append(srv)
        resp, err = _fetch(f"/details/{srv}/{doi}/na/json", waited)
        if err:
            return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                                 error=err)
        try:
            rows = _rows(resp.json())
        except Exception as exc:
            return SourceOutcome(
                source=SOURCE, waited_ms=int(sum(waited) * 1000),
                error={"code": "UPSTREAM_CHANGED",
                       "detail": f"bioRxiv 响应结构不符预期：{exc!r}"})

        if rows:
            logger.info("[biorxiv] %s -> %s (server=%s)", doi, len(rows), srv)
            return SourceOutcome(
                source=SOURCE, waited_ms=int(sum(waited) * 1000),
                results=[_to_work(r, srv, doi) for r in rows],
                total_available=len(rows))

    return SourceOutcome(
        source=SOURCE, waited_ms=int(sum(waited) * 1000),
        error={"code": "NOT_FOUND",
               "detail": f"bioRxiv/medRxiv 均无此 DOI（已试 {tried}）"})
