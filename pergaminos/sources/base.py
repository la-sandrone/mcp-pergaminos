#!/usr/bin/env python3
"""文件名: sources/base.py
摘要: Work 数据契约与源结果类型的**唯一真相源**。所有源产出 Work，
     所有对账/合并逻辑消费 Work。字段语义见 契约-mcp-pergaminos-v0.1.md §4。
依赖: 标准库
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

OaStatus = Literal["gold", "hybrid", "bronze", "green", "closed"]
MatchKind = Literal["doi_exact", "arxiv_id", "title_fuzzy", "not_found"]

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(raw: str | None) -> str:
    """去 JATS/HTML 标签并压缩空白。

    Crossref 的 abstract 是 **JATS XML**（`<jats:p>…</jats:p>`），
    直接塞给模型会带一坨标签——必须剥。
    """
    if not raw:
        return ""
    text = _HTML_TAG.sub(" ", raw)
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&").replace("&quot;", '"')
                .replace("&apos;", "'").replace("&#x2010;", "-"))
    return _WS.sub(" ", text).strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """按 abstract_max_chars 截断。返回 (文本, 是否截断)。limit<=0 表示不截。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


@dataclass
class Provenance:
    """**逐字段来源** —— 契约 §4.1 强制要求。

    理由：本项目已有「数据巴别塔」与「静默指错」两个故障类。
    每个字段都必须能回答"这是谁说的"，`raw_ref` 使争议可回查原始响应。
    """

    source: str
    endpoint: str
    fetched_at: str
    raw_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OaInfo:
    """OA 状态。值集采用 Unpaywall 五值（契约 §4.2）。"""

    is_oa: bool
    status: OaStatus | None = None
    url: str = ""
    in_repository: bool = False
    journal_is_oa: bool = False
    journal_is_in_doaj: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Location:
    """一处收录位置。`locations[]` 即「这篇论文活在世界上的每一个地方」。"""

    source: str = ""
    host_type: str = ""          # repository | publisher
    version: str = ""            # publishedVersion | acceptedVersion | submittedVersion
    url: str = ""
    license: str = ""
    oa_date: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Work:
    """唯一的数据契约。见 契约 §4。"""

    ids: dict[str, str] = field(default_factory=dict)
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    type: str = "other"
    abstract: str = ""
    abstract_truncated: bool = False
    doi_url: str = ""

    oa: OaInfo | None = None
    locations: list[Location] = field(default_factory=list)
    preprint: dict[str, Any] = field(default_factory=dict)

    provenance: Provenance | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # ids 里去掉空值，避免 "pmid": "" 这类噪声
        d["ids"] = {k: v for k, v in d["ids"].items() if v}
        return d

    def key(self) -> str:
        """合并键：优先 DOI；无 DOI 时退化为规范化标题的哈希。

        契约 §4.3 已声明：arXiv 预印本的 DOI 由 DataCite 注册、不在 Crossref，
        且同一作品可能有**两个 DOI**（预印本 + 正式版）——
        所以这里只用 DOI 做**精确合并**，不做推断式合并。
        """
        doi = (self.ids.get("doi") or "").lower().strip()
        if doi:
            return f"doi:{doi}"
        norm = _WS.sub(" ", (self.title or "").lower()).strip()
        return "title:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


@dataclass
class SourceOutcome:
    """单源的结果信封。**失败必须显式**，不许静默返回空。"""

    source: str
    results: list[Work] = field(default_factory=list)
    waited_ms: int = 0
    error: dict[str, str] | None = None    # {"code": ..., "detail": ...}
    # 上游报告的"总共命中多少条"。重要信号：arXiv 的 OR 语义下它常是几十万，
    # 说明命中的是模糊池、真正做排序的是 relevance。None = 该源不报。
    total_available: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
