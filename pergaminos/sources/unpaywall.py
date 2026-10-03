#!/usr/bin/env python3
"""文件名: sources/unpaywall.py
摘要: Unpaywall 源 —— **OA 骨干**（但**不能检索**）。
     官方口径（2026-10-03 核实）：
       · 端点 https://api.unpaywall.org/v2/{doi}?email=…
       · **email 必填**（URL 参数）；无 key 概念，email 即请求标识
       · 限额 100,000 次/天
       · `/v2/search` 已于 **2026-09-18 废止**（返回 410 Gone），
         官方明示改用 OpenAlex search ⇒ 本源**只做 DOI 查询**
     它自带两个关键判据，因此 DOAJ 与"预印本收录"都不必再挂别的源：
       · journal_is_in_doaj   —— 该 DOI 是否发表在 DOAJ 索引的期刊上
       · has_repository_copy  —— 是否存在仓库副本（预印本/机构库）
依赖: ./base, ../net, ../config
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .. import config
from ..net import RetryExhausted, request
from .base import Location, OaInfo, Provenance, SourceOutcome, Work, clean_text

logger = logging.getLogger("pergaminos.sources.unpaywall")

SOURCE = "unpaywall"
BASE = "https://api.unpaywall.org/v2"

_GENRE_MAP = {
    "journal-article": "article",
    "posted-content": "preprint",
    "proceedings-article": "article",
    "dataset": "dataset",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _err(resp: httpx.Response, doi: str) -> dict[str, str] | None:
    if resp.status_code == 404:
        return {"code": "NOT_FOUND", "detail": f"Unpaywall 无此 DOI：{doi}"}
    if resp.status_code == 422:
        # 官方对格式非法的 DOI 返回 422
        return {"code": "NOT_FOUND", "detail": f"DOI 格式非法或无法解析：{doi}"}
    if resp.status_code == 429:
        return {"code": "RATE_LIMITED",
                "detail": f"Unpaywall 日额度可能用尽（100k/天）；"
                          f"Retry-After={resp.headers.get('Retry-After', '-')}"}
    if resp.status_code in (401, 403):
        return {"code": "CONFIG_MISSING",
                "detail": "Unpaywall 拒绝请求——多半是 email 参数缺失或无效"}
    if resp.status_code >= 500:
        return {"code": "UPSTREAM", "detail": f"Unpaywall 服务端 {resp.status_code}"}
    return None


def _locations(payload: dict) -> list[Location]:
    out = []
    for loc in payload.get("oa_locations") or []:
        if not isinstance(loc, dict):
            continue
        out.append(Location(
            source=clean_text(loc.get("repository_institution")
                              or loc.get("host_type") or ""),
            host_type=loc.get("host_type") or "",
            version=loc.get("version") or "",
            url=loc.get("url_for_pdf") or loc.get("url") or "",
            license=loc.get("license") or "",
            oa_date=loc.get("oa_date") or "",
        ))
    return out


def to_work(payload: dict, doi: str) -> Work:
    best = payload.get("best_oa_location") or {}
    oa = OaInfo(
        is_oa=bool(payload.get("is_oa")),
        status=payload.get("oa_status") or None,
        url=(best.get("url_for_pdf") or best.get("url") or "") if best else "",
        in_repository=bool(payload.get("has_repository_copy")),
        journal_is_oa=bool(payload.get("journal_is_oa")),
        journal_is_in_doaj=bool(payload.get("journal_is_in_doaj")),
    )
    return Work(
        ids={"doi": (payload.get("doi") or doi).lower()},
        title=clean_text(payload.get("title")),
        authors=[],                       # Unpaywall 不提供作者
        year=payload.get("year"),
        venue=clean_text(payload.get("journal_name")),
        type=_GENRE_MAP.get(payload.get("genre") or "", "other"),
        abstract="",                      # Unpaywall 不提供摘要
        doi_url=payload.get("doi_url") or f"https://doi.org/{doi}",
        oa=oa,
        locations=_locations(payload),
        provenance=Provenance(
            source=SOURCE,
            endpoint=f"GET {BASE}/{doi}?email=<redacted>",
            fetched_at=_now(),
            raw_ref="",
        ),
    )


def get_by_doi(doi: str) -> SourceOutcome:
    """按 DOI 查 OA 状态与全部 OA 位置。"""
    doi = doi.strip().lower()
    if not doi:
        return SourceOutcome(source=SOURCE,
                             error={"code": "NOT_FOUND", "detail": "空 DOI"})

    email = config.get("UNPAYWALL_EMAIL")
    if not email:
        return SourceOutcome(source=SOURCE, error={
            "code": "CONFIG_MISSING",
            "detail": f"Unpaywall 要求 email（{config.resolve_name('UNPAYWALL_EMAIL')}）",
        })

    url = f"{BASE}/{doi}"
    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", url, params={"email": email}, waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, error={"code": "NETWORK", "detail": str(exc)})

    err = _err(resp, doi)
    if err:
        return SourceOutcome(source=SOURCE, error=err)

    try:
        payload = resp.json()
        if not isinstance(payload, dict) or "is_oa" not in payload:
            raise TypeError("缺少 is_oa 字段")
    except Exception as exc:
        # ★ 上游变了必须报错。这条不是理论担忧——
        #   Unpaywall 的 /v2/search 在 2026-09-18 刚变成 410 Gone。
        return SourceOutcome(source=SOURCE, error={
            "code": "UPSTREAM_CHANGED",
            "detail": f"Unpaywall 响应结构不符预期：{exc!r}",
        })

    return SourceOutcome(source=SOURCE, results=[to_work(payload, doi)],
                         waited_ms=int(sum(waited) * 1000))
