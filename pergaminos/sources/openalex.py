#!/usr/bin/env python3
"""文件名: sources/openalex.py
摘要: OpenAlex 源 —— **可选开关**，配了 key 才启用（裁决：方案 C）。
     官方口径（2026-10-03 核实，来源 docs.openalex.org）：
       · 端点 https://api.openalex.org/works
       · 无需 key 即可基础使用；**免费 key 把日预算提高 10 倍**
       · 免费层**不需要任何支付方式**；需要美元渠道的只是"超额后补款"
       · 绝对上限 100 req/s；另有**日预算**（有 key $1/天，无 key $0.10/天）
       · 按操作计价：**按 DOI/ID 取单实体 = 免费且无限**；
         list+filter $0.10/1000；search $1/1000；**全文下载 $10/1000**
       · `per_page` 上限 100；OR 过滤一次最多 **100 个值**
       · 日预算 UTC 午夜重置（= 北京时间 08:00）

⚠️ 为什么不默认启用（用户裁决，理由见 README）：
   免费额度 $1/天是**无法在不付款的前提下提高的硬顶**，撞上要等到 UTC 午夜。
   而不启用时，Unpaywall（100k/天）+ Crossref（10 req/s）都没有这种"撞墙就停"的性质。

⚠️ 两个口径陷阱（改动前必读）：
   ① `open_access.oa_status` 有 **6 个值**（多一个 `diamond`），而 Unpaywall 是 5 个。
      契约 §4.2 定的是「映射为 gold + 保留原值 + 记冲突」，故 `raw_status` 字段
      保留原始值，不静默丢失。
   ② 官网 **openalex.org 的网页浏览吃同一个预算**（匿名 $0.10/天）。
      官方原文：网站上搜一次约 18 credits，而程序化 /works?search= 是 10 credits。
      ⇒ 在浏览器里翻 OpenAlex 会从 API 额度里扣。
依赖: ./base, ../net, ../config
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .. import config
from ..net import RetryExhausted, request
from .base import Location, OaInfo, Provenance, SourceOutcome, Work, clean_text, truncate

logger = logging.getLogger("pergaminos.sources.openalex")

SOURCE = "openalex"
BASE = "https://api.openalex.org/works"

# 官方建议用 select= 省流量——而这里还多一层理由：按操作计价，少传就少算。
SELECT = ",".join([
    "id", "doi", "ids", "display_name", "title", "publication_year",
    "publication_date", "type", "authorships", "primary_location",
    "best_oa_location", "locations", "locations_count", "open_access",
    "biblio", "abstract_inverted_index", "apc_list", "apc_paid",
])

# OpenAlex 的 6 值 → 契约的 5 值（契约 §4.2）
_STATUS_MAP = {"diamond": "gold"}


def enabled() -> bool:
    """开关：**只有配了 key 才启用**。空 = DISABLED。"""
    return bool(config.get("OPENALEX_KEY").strip())


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _params(extra: dict | None = None) -> dict[str, str]:
    p: dict[str, str] = {"select": SELECT, "per_page": "50"}
    key = config.get("OPENALEX_KEY").strip()
    if key:
        p["api_key"] = key
    # 礼貌池：OpenAlex 接受 mailto 作为联系标识
    mail = config.get("CROSSREF_MAILTO")
    if mail:
        p["mailto"] = mail
    if extra:
        p.update(extra)
    return p


def _err(resp: httpx.Response) -> dict[str, str] | None:
    if resp.status_code == 404:
        return {"code": "NOT_FOUND", "detail": "OpenAlex 无此记录"}
    if resp.status_code == 429:
        # 官方：超日预算或超过 100 req/s 都返回 429
        return {"code": "RATE_LIMITED",
                "detail": "OpenAlex 日预算用尽或超 100 req/s；"
                          f"额度 UTC 午夜重置（北京 08:00）；"
                          f"剩余={resp.headers.get('X-RateLimit-Remaining', '-')}"}
    if resp.status_code in (401, 403):
        return {"code": "AUTH_REQUIRED",
                "detail": f"OpenAlex 拒绝（{resp.status_code}）——"
                          f"检查 {config.resolve_name('OPENALEX_KEY')}"}
    if resp.status_code >= 500:
        return {"code": "UPSTREAM", "detail": f"OpenAlex 服务端 {resp.status_code}"}
    return None


def _abstract(inv: dict | None) -> str:
    """OpenAlex 只提供**倒排索引**（官方原文：出于法律原因不发纯文本摘要）。

    `{"Despite": [0], "growing": [1, 57]}` ⇒ 按位置还原成句子。
    """
    if not isinstance(inv, dict) or not inv:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inv.items():
        if not isinstance(positions, list):
            continue
        for p in positions:
            if isinstance(p, int):
                slots[p] = word
    if not slots:
        return ""
    return " ".join(slots[i] for i in sorted(slots))


def _location(loc: dict | None, default_source: str = "") -> Location | None:
    if not isinstance(loc, dict):
        return None
    src = loc.get("source") or {}
    return Location(
        source=clean_text(src.get("display_name") if isinstance(src, dict) else "")
               or default_source,
        host_type="publisher" if (isinstance(src, dict)
                                  and src.get("type") == "journal") else "repository",
        version=loc.get("version") or "",
        url=loc.get("pdf_url") or loc.get("landing_page_url") or "",
        license=loc.get("license") or "",
    )


def _to_work(item: dict, abstract_max: int) -> Work | None:
    if not isinstance(item, dict):
        return None
    doi = (item.get("doi") or "").replace("https://doi.org/", "").strip().lower()
    title = clean_text(item.get("display_name") or item.get("title"))
    if not (doi or title):
        return None

    # ids：官方给的是 URI 形式，转成裸标识符
    raw_ids = item.get("ids") or {}
    ids: dict[str, str] = {}
    if doi:
        ids["doi"] = doi
    for k, v in (("openalex", "openalex"), ("pmid", "pmid"), ("pmcid", "pmcid"),
                 ("mag", "mag")):
        val = raw_ids.get(k)
        if isinstance(val, str) and val:
            ids[v] = val.rsplit("/", 1)[-1]

    oa_raw = item.get("open_access") or {}
    raw_status = (oa_raw.get("oa_status") or "").strip() or None
    mapped = _STATUS_MAP.get(raw_status, raw_status)

    authors = []
    for a in item.get("authorships") or []:
        if isinstance(a, dict):
            nm = ((a.get("author") or {}).get("display_name") or "").strip()
            if nm:
                authors.append(nm)

    locs = []
    for loc in item.get("locations") or []:
        L = _location(loc)
        if L:
            locs.append(L)

    abstract, cut = truncate(_abstract(item.get("abstract_inverted_index")), abstract_max)

    w = Work(
        ids=ids,
        title=title,
        authors=authors,
        year=item.get("publication_year"),
        venue=clean_text(((item.get("primary_location") or {}).get("source") or {})
                         .get("display_name") if isinstance(
                             (item.get("primary_location") or {}).get("source"), dict) else ""),
        type="preprint" if item.get("type") == "preprint" else (
            "article" if item.get("type") == "article" else "other"),
        abstract=abstract,
        abstract_truncated=cut,
        doi_url=f"https://doi.org/{doi}" if doi else "",
        oa=OaInfo(
            is_oa=bool(oa_raw.get("is_oa")),
            status=mapped,                                  # ← 6 值映射到 5 值
            url=oa_raw.get("oa_url") or "",
            in_repository=bool(oa_raw.get("any_repository_has_fulltext")),
        ),
        locations=locs,
        provenance=Provenance(
            source=SOURCE,
            endpoint="GET /works (select=…)",
            fetched_at=_now(),
        ),
    )
    if raw_status != mapped:
        # 契约 §4.2：**不隐藏源差异** —— 原值保留，由上层记 conflicts
        w.preprint["openalex_raw_oa_status"] = raw_status
    return w


def get_by_doi(doi: str, abstract_max: int = 600) -> SourceOutcome:
    """按 DOI 取单实体。**OpenAlex 官方定价里这是免费且无限的操作** ——
    比 search 便宜，所以能按 DOI 走就按 DOI 走。"""
    if not enabled():
        return SourceOutcome(source=SOURCE, error={
            "code": "CONFIG_MISSING",
            "detail": f"OpenAlex 未启用（{config.resolve_name('OPENALEX_KEY')} 为空）"})

    doi = doi.strip().lower()
    if not doi:
        return SourceOutcome(source=SOURCE, error={"code": "NOT_FOUND", "detail": "空 DOI"})

    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", f"{BASE}/https://doi.org/{doi}",
                       params=_params(), waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "NETWORK", "detail": str(exc)})

    err = _err(resp)
    if err:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000), error=err)

    try:
        item = resp.json()
        if not isinstance(item, dict) or "open_access" not in item:
            raise TypeError("缺 open_access 字段")
    except Exception as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "UPSTREAM_CHANGED",
                                    "detail": f"OpenAlex 响应结构不符预期：{exc!r}"})

    w = _to_work(item, abstract_max)
    return SourceOutcome(source=SOURCE, results=[w] if w else [],
                         waited_ms=int(sum(waited) * 1000),
                         error=None if w else {"code": "NOT_FOUND",
                                               "detail": "响应无法映射为 Work"})


def search(query: str, limit: int = 20, year: str = "", abstract_max: int = 600,
           ) -> SourceOutcome:
    """检索。**注意计价**：官方 search = $1/1000 次（单实体是免费）。
    有 key 时 $1/天 ⇒ 1000 次检索/天。"""
    if not enabled():
        return SourceOutcome(source=SOURCE, error={
            "code": "CONFIG_MISSING",
            "detail": f"OpenAlex 未启用（{config.resolve_name('OPENALEX_KEY')} 为空）"})

    params = _params({"search": query,
                      "per_page": str(max(1, min(limit, 100)))})
    if year:
        f = _year_filter(year)
        if f:
            params["filter"] = f

    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", BASE, params=params, waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "NETWORK", "detail": str(exc)})

    err = _err(resp)
    if err:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000), error=err)

    try:
        payload = resp.json()
        items = payload["results"]
        if not isinstance(items, list):
            raise TypeError("results 不是 list")
    except Exception as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "UPSTREAM_CHANGED",
                                    "detail": f"OpenAlex 响应结构不符预期：{exc!r}"})

    works = [w for w in (_to_work(i, abstract_max) for i in items) if w]
    total = ((payload.get("meta") or {}).get("count"))
    logger.info("[openalex] %r -> %d/%s results", query, len(works), total)
    return SourceOutcome(source=SOURCE, results=works,
                         waited_ms=int(sum(waited) * 1000), total_available=total)


def _year_filter(year: str) -> str:
    y = year.strip()
    if not y:
        return ""
    if "-" not in y:
        return f"publication_year:{y}"
    lo, _, hi = y.partition("-")
    parts = []
    if lo.strip():
        parts.append(f"from_publication_date:{lo.strip()}-01-01")
    if hi.strip():
        parts.append(f"to_publication_date:{hi.strip()}-12-31")
    return ",".join(parts)
