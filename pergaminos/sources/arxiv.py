#!/usr/bin/env python3
"""文件名: sources/arxiv.py
摘要: arXiv 源 —— 预印本检索。**摘要稳定提供**（这正是 Crossref 缺的那块）。
     官方口径（2026-10-03 核实，来源 info.arxiv.org/help/api）：
       · 端点 http://export.arxiv.org/api/query   、返回 **Atom 1.0 XML**
       · 限速 **每 3 秒 1 次、单连接，且对「你控制的所有机器合计」**（硬约束）
       · 元数据以 **CC0 1.0** 释出；e-print 正文版权归作者/出版方
       · 重分发 e-print 需版权持有人许可 ⇒ 本源**只取元数据，不取全文**

⚠️ **一个实测出来的反直觉行为（改动前必读）**
   arXiv 把 search_query 里空格分隔的词按 **OR** 解释。所以
   `all:helium 587.6 nm auroral emission` 会返回 **47 万条**。
   **但这是对的** —— 实测对比：
       原样（OR）  total=478,546   前 5 条全部高度相关 ✅
       显式 AND    total=0         ❌（8 个具体术语不可能同时出现在题名/摘要里）
   ⇒ **不要"修"成 AND**。真正的排序工作由 `sortBy=relevance` 完成。
   本源因此**原样透传查询**，只做 `all:` 包装（用户已写字段前缀时不包装）。
依赖: ./base, ../net, ../config
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from ..net import RetryExhausted, request
from .base import Location, OaInfo, Provenance, SourceOutcome, Work, clean_text, truncate

logger = logging.getLogger("pergaminos.sources.arxiv")

SOURCE = "arxiv"
ENDPOINT = "https://export.arxiv.org/api/query"

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}

_ID_RE = re.compile(r"/abs/([^v/\s]+)(v\d+)?$")
_FIELD_RE = re.compile(r"\b(ti|abs|au|co|jr|cat|rn|id|all|doi):", re.I)
_OP_RE = re.compile(r"\s(AND|OR|ANDNOT)\s")
_WS_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_query(raw: str) -> str:
    """把用户查询转成 arXiv 的 search_query。

    已含字段前缀（`ti:` 等）或布尔算符时**原样透传**——用户既然写了，
    就说明他知道自己在干什么，不该替他改写。
    否则包一层 `all:`，此时 arXiv 会对空格分词做 **OR**（见文件头）。
    """
    q = raw.strip()
    if _FIELD_RE.search(q) or _OP_RE.search(q):
        return q
    return f"all:{q}"


def _entry_to_work(e: ET.Element, query: str, abstract_max: int) -> Work | None:
    aid_raw = (e.findtext("a:id", "", NS) or "").strip()
    m = _ID_RE.search(aid_raw)
    arxiv_id = m.group(1) if m else aid_raw.rsplit("/", 1)[-1]
    version = (m.group(2) or "").lstrip("v") if m else ""

    titles = e.findtext("a:title", "", NS)
    title = clean_text(titles)
    if not (arxiv_id or title):
        return None

    abstract, cut = truncate(clean_text(e.findtext("a:summary", "", NS)), abstract_max)

    authors = [clean_text(n.text or "") for n in e.findall("a:author/a:name", NS)]
    authors = [a for a in authors if a]

    published = (e.findtext("a:published", "", NS) or "")
    year = None
    if len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])

    # 分类：primary_category 优先，其余作为附加
    cats = [c.get("term", "") for c in e.findall("a:category", NS)]
    primary = ""
    pc = e.find("arxiv:primary_category", NS)
    if pc is not None:
        primary = pc.get("term", "")

    # ⭐ 这两个字段是**预印本 → 正式版**的映射，arXiv 独有
    pub_doi = (e.findtext("arxiv:doi", "", NS) or "").strip().lower()
    journal_ref = clean_text(e.findtext("arxiv:journal_ref", "", NS))
    comment = clean_text(e.findtext("arxiv:comment", "", NS))

    ids: dict[str, str] = {"arxiv": arxiv_id}
    if pub_doi:
        ids["doi"] = pub_doi          # 作者登记的正式版 DOI

    # 位置：abs 页 + PDF。注意**这是元数据里的链接，不是我们下载的**
    locations = []
    for ln in e.findall("a:link", NS):
        href, rel, typ = ln.get("href", ""), ln.get("rel", ""), ln.get("type", "")
        if rel == "related" and "pdf" in typ:
            locations.append(Location(source="arXiv", host_type="repository",
                                      version="submittedVersion", url=href))
        elif rel == "alternate":
            locations.append(Location(source="arXiv", host_type="repository",
                                      version="submittedVersion", url=href))
    if pub_doi:
        locations.append(Location(source="Crossref(published)", host_type="publisher",
                                  version="publishedVersion",
                                  url=f"https://doi.org/{pub_doi}"))

    return Work(
        ids=ids,
        title=title,
        authors=authors,
        year=year,
        venue=journal_ref or f"arXiv:{primary}" if (journal_ref or primary) else "",
        type="preprint",
        abstract=abstract,
        abstract_truncated=cut,
        doi_url=f"https://arxiv.org/abs/{arxiv_id}",
        oa=OaInfo(is_oa=True, status="green",          # arXiv 是仓库副本 ⇒ green
                  url=f"https://arxiv.org/abs/{arxiv_id}",
                  in_repository=True),
        locations=locations,
        preprint={"server": "arXiv", "version": version, "comment": comment,
                  "primary_category": primary, "categories": cats,
                  "published_doi": pub_doi, "journal_ref": journal_ref},
        provenance=Provenance(
            source=SOURCE,
            endpoint=f"GET {ENDPOINT}?search_query={build_query(query)[:60]}…",
            fetched_at=_now(),
        ),
    )


def _err(resp: httpx.Response) -> dict[str, str] | None:
    if resp.status_code == 429:
        return {"code": "RATE_LIMITED",
                "detail": "arXiv 限速（官方：每 3 秒 1 次）"}
    if resp.status_code >= 500:
        return {"code": "UPSTREAM", "detail": f"arXiv 服务端 {resp.status_code}"}
    return None


def _parse(resp: httpx.Response, query: str, abstract_max: int,
           ) -> tuple[list[Work], int | None]:
    """解析 Atom。返回 (works, totalResults)。"""
    root = ET.fromstring(resp.text)
    if root.tag != f"{{{NS['a']}}}feed":
        raise ValueError(f"根元素不是 atom:feed，而是 {root.tag!r}")

    total_el = root.find("os:totalResults", NS)
    total = int(total_el.text) if total_el is not None and total_el.text else None

    works = [w for w in (_entry_to_work(e, query, abstract_max)
                         for e in root.findall("a:entry", NS)) if w]
    return works, total


def search(query: str, limit: int = 20, year: str = "", abstract_max: int = 600,
           ) -> SourceOutcome:
    """检索。**含 arXiv 的调用会等待数秒**——官方限速 3 秒/次且全机器合计，
    等待秒数由 net.py 的跨进程节流器保证，并回报在 waited_ms 里。"""
    params: dict[str, str] = {
        "search_query": build_query(query),
        "start": "0",
        "max_results": str(max(1, min(limit, 2000))),
        "sortBy": "relevance",          # ← 排序才是真正在干活的（见文件头）
        "sortOrder": "descending",
    }

    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", ENDPOINT, params=params, waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "NETWORK", "detail": str(exc)})

    err = _err(resp)
    if err:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000), error=err)

    try:
        works, total = _parse(resp, query, abstract_max)
    except Exception as exc:
        # ★ 上游结构变了必须报错。Unpaywall 的 /v2/search 刚 410 过。
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "UPSTREAM_CHANGED",
                                    "detail": f"arXiv Atom 结构不符预期：{exc!r}"})

    logger.info("[arxiv] %r -> %d/%s results", query, len(works), total)
    out = SourceOutcome(source=SOURCE, results=works,
                        waited_ms=int(sum(waited) * 1000))
    # totalResults 是重要信号：OR 语义下它常常是几十万，说明命中的是"模糊池"
    out.total_available = total
    return out


def get_by_id(arxiv_id: str, abstract_max: int = 600) -> SourceOutcome:
    """按 arXiv ID 取单条。供 paper_oa 处理 arXiv id 输入。

    注意：arXiv 的 DOI 由 **DataCite** 注册，不在 Crossref，
    所以 Unpaywall 查不到它——本源是这条路径上的唯一信息源。
    """
    aid = arxiv_id.strip()
    for pfx in ("arxiv:", "https://arxiv.org/abs/", "http://arxiv.org/abs/"):
        if aid.lower().startswith(pfx):
            aid = aid[len(pfx):]
    aid = re.sub(r"v\d+$", "", aid)

    waited: list[float] = []
    try:
        resp = request(SOURCE, "GET", ENDPOINT,
                       params={"id_list": aid, "max_results": "1"}, waited=waited)
    except RetryExhausted as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "NETWORK", "detail": str(exc)})

    err = _err(resp)
    if err:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000), error=err)

    try:
        works, _ = _parse(resp, aid, abstract_max)
    except Exception as exc:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "UPSTREAM_CHANGED",
                                    "detail": f"arXiv Atom 结构不符预期：{exc!r}"})

    if not works:
        return SourceOutcome(source=SOURCE, waited_ms=int(sum(waited) * 1000),
                             error={"code": "NOT_FOUND",
                                    "detail": f"arXiv 无此 ID：{aid}"})
    return SourceOutcome(source=SOURCE, results=works,
                         waited_ms=int(sum(waited) * 1000))
