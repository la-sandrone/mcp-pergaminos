#!/usr/bin/env python3
"""文件名: sources/crossref.py
摘要: Crossref 源 —— 检索主力 + DOI 元数据权威。
     官方口径（2026-10-03 核实）：
       · 端点 https://api.crossref.org/v1/works
       · 认证：无 / Polite(带 mailto) / Plus(API key)
       · 限速：Public 5 req/s **并发 1**；Polite 10 req/s 并发 3；Plus 150
       · 官方建议用 select= 只取需要的字段（省流量省时间）
       · rows 默认 20，最大 1000；翻页用 cursor=*
     本源**不提供 OA 信息**——OA 状态以 Unpaywall 为权威（契约 §5）。
依赖: ./base, ../net, ../config
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from .. import config
from ..net import RetryExhausted, request
from .base import Location, Provenance, SourceOutcome, Work, clean_text, truncate

logger = logging.getLogger("pergaminos.sources.crossref")

SOURCE = "crossref"
ENDPOINT = "https://api.crossref.org/v1/works"

# 官方建议：只要需要的字段。省流量，也省模型 token。
SELECT = ",".join([
    "DOI", "title", "author", "issued", "published", "container-title",
    "type", "abstract", "URL", "license", "subject", "is-referenced-by-count",
])

_TYPE_MAP = {
    "journal-article": "article",
    "proceedings-article": "article",
    "posted-content": "preprint",
    "book-chapter": "other",
    "book": "other",
    "dataset": "dataset",
    "report": "other",
    "dissertation": "other",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _err(resp: httpx.Response) -> dict[str, str] | None:
    """把状态码翻译成契约 §8 的错误码。**不许静默返回空。**"""
    if resp.status_code == 404:
        return {"code": "NOT_FOUND", "detail": "Crossref 无此记录"}
    if resp.status_code == 429:
        # 官方：超过限速返回 429；重试无意义（net.py 也不会重试它）
        return {"code": "RATE_LIMITED",
                "detail": f"Crossref 限速（429）；Retry-After={resp.headers.get('Retry-After', '-')}"}
    if resp.status_code in (401, 403):
        return {"code": "AUTH_REQUIRED",
                "detail": f"Crossref 拒绝（{resp.status_code}）——"
                          f"检查 {config.resolve_name('PROXY')} 是否被共享出口拖累"}
    if resp.status_code >= 500:
        return {"code": "UPSTREAM", "detail": f"Crossref 服务端 {resp.status_code}"}
    return None


def _year(item: dict) -> int | None:
    for key in ("issued", "published"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            return parts[0][0]
    return None


def _authors(item: dict) -> list[str]:
    out = []
    for a in item.get("author") or []:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or " ".join(
            p for p in (a.get("given"), a.get("family")) if p
        )
        if name:
            out.append(name.strip())
    return out


def _to_work(item: dict, query: str, abstract_max: int) -> Work | None:
    doi = (item.get("DOI") or "").strip().lower()
    titles = item.get("title") or []
    title = clean_text(titles[0]) if titles else ""
    if not (doi or title):
        return None                      # 既无 DOI 又无题名 —— 无法标识，丢弃

    abstract, cut = truncate(clean_text(item.get("abstract")), abstract_max)
    venues = item.get("container-title") or []

    # Crossref 的 license[] 是**出版方声明的许可**，不是 OA 判定。
    # 如实记为 publisher 位置，但不据此设置 oa（OA 归 Unpaywall 管）。
    locations = []
    for lic in item.get("license") or []:
        if not isinstance(lic, dict):
            continue
        url = lic.get("URL") or ""
        if not url:
            continue
        locations.append(Location(
            source="Crossref(license)",
            host_type="publisher",
            version={"vor": "publishedVersion", "am": "acceptedVersion",
                     "tdm": "text-mining"}.get(lic.get("content-version", ""), ""),
            url=url,
            license=url,
        ))

    return Work(
        ids={"doi": doi} if doi else {},
        title=title,
        authors=_authors(item),
        year=_year(item),
        venue=clean_text(venues[0]) if venues else "",
        type=_TYPE_MAP.get(item.get("type", ""), "other"),
        abstract=abstract,
        abstract_truncated=cut,
        doi_url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
        oa=None,                          # ← Crossref 不答 OA
        locations=locations,
        provenance=Provenance(
            source=SOURCE,
            endpoint=f"GET {ENDPOINT}?query.bibliographic={query!r}"
                     f"&select=<{len(SELECT.split(','))} fields>",
            fetched_at=_now(),
            raw_ref="",                   # 缓存落地后回填
        ),
    )


def search(query: str, limit: int = 20, year: str = "",
           abstract_max: int = 600) -> SourceOutcome:
    """检索。返回 SourceOutcome——**失败显式**，不返回空列表冒充"没找到"。"""
    params: dict[str, str] = {
        "query.bibliographic": query,
        "rows": str(max(1, min(limit, 1000))),
        "select": SELECT,
    }
    if year:
        f = _year_filter(year)
        if f:
            params["filter"] = f
    mailto = config.get("CROSSREF_MAILTO")
    if mailto:
        params["mailto"] = mailto       # ← 进 Polite 池：10/s 并发 3（否则 5/s 并发 1）

    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", ENDPOINT, params=params, waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, error={
            "code": "NETWORK", "detail": str(exc)})

    err = _err(resp)
    if err:
        return SourceOutcome(source=SOURCE, error=err)

    try:
        payload = resp.json()
        items = payload["message"]["items"]
        if not isinstance(items, list):
            raise TypeError("message.items 不是 list")
    except Exception as exc:
        # ★ 上游结构变了 —— 必须报错，绝不静默返回空。
        #   Unpaywall 的 /v2/search 在 2026-09-18 刚 410 过，上游一定会变。
        return SourceOutcome(source=SOURCE, error={
            "code": "UPSTREAM_CHANGED",
            "detail": f"Crossref 响应结构不符预期：{exc!r}",
        })

    works = [w for w in (_to_work(i, query, abstract_max) for i in items) if w]
    total = (payload.get("message") or {}).get("total-results")
    logger.info("[crossref] %r -> %d/%s results", query, len(works), total)
    return SourceOutcome(source=SOURCE, results=works,
                         waited_ms=int(sum(waited) * 1000))


def _year_filter(year: str) -> str:
    """官方过滤语法：/works?filter=from-pub-date:2024-01-01,until-pub-date:…
    接受 "2020" / "2016-2020" / "-2015" / "2016-"。
    """
    y = year.strip()
    if not y:
        return ""
    if "-" not in y:
        return f"from-pub-date:{y}-01-01,until-pub-date:{y}-12-31"
    lo, _, hi = y.partition("-")
    parts = []
    if lo.strip():
        parts.append(f"from-pub-date:{lo.strip()}-01-01")
    if hi.strip():
        parts.append(f"until-pub-date:{hi.strip()}-12-31")
    return ",".join(parts)


def get_by_doi(doi: str, abstract_max: int = 600) -> SourceOutcome:
    """按 DOI 取单条（权威元数据）。用于 paper_oa 的元数据底座。"""
    url = f"{ENDPOINT}/{doi.strip().lower()}"
    mailto = config.get("CROSSREF_MAILTO")
    params = {"mailto": mailto} if mailto else None

    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", url, params=params, waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, error={"code": "NETWORK", "detail": str(exc)})

    err = _err(resp)
    if err:
        return SourceOutcome(source=SOURCE, error=err)

    try:
        item = resp.json()["message"]
        if not isinstance(item, dict):
            raise TypeError("message 不是 dict")
    except Exception as exc:
        return SourceOutcome(source=SOURCE, error={
            "code": "UPSTREAM_CHANGED",
            "detail": f"Crossref 单条响应结构不符预期：{exc!r}",
        })

    w = _to_work(item, doi, abstract_max)
    return SourceOutcome(source=SOURCE, results=[w] if w else [],
                         waited_ms=int(sum(waited) * 1000),
                         error=None if w else {"code": "NOT_FOUND",
                                               "detail": "响应无法映射为 Work"})
