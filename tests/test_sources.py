#!/usr/bin/env python3
"""文件名: tests/test_sources.py
摘要: 源的**离线解析测试**。全部用合成 fixture —— 不含任何真实检索痕迹
     （真实 DOI 列表与摘要既是隐私也可能是版权问题，公开仓库不提交）。
     **不需要网络，不需要任何 API key。**
依赖: pytest；pergaminos.sources.{arxiv,biorxiv,openalex,crossref,base}
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pergaminos.sources import arxiv, biorxiv, openalex             # noqa: E402
from pergaminos.sources.base import Work, clean_text                # noqa: E402

# ── 合成 Atom fixture（结构照真实响应，内容全是编的）──────────────────
ATOM_FIXTURE = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults>123456</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/9901.00001v2</id>
    <title>A synthetic   title with $x_{1}$ markup</title>
    <updated>1999-01-02T00:00:00Z</updated>
    <link href="https://arxiv.org/abs/9901.00001v2" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/9901.00001v2" rel="related" type="application/pdf" title="pdf"/>
    <summary>An abstract about &lt;jats&gt;nothing&lt;/jats&gt; in particular.</summary>
    <category term="astro-ph.EP" scheme="http://arxiv.org/schemas/atom"/>
    <published>1999-01-01T00:00:00Z</published>
    <arxiv:comment>accepted to a journal</arxiv:comment>
    <arxiv:primary_category term="astro-ph.EP"/>
    <arxiv:journal_ref>J. Synthetic, 1:1, 1999</arxiv:journal_ref>
    <arxiv:doi>10.1234/synthetic.1999</arxiv:doi>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/9901.00002v1</id>
    <title>Second synthetic entry</title>
    <published>1999-02-01T00:00:00Z</published>
    <summary>Second abstract.</summary>
    <author><name>Grace Hopper</name></author>
  </entry>
</feed>
"""


class TestArxivQueryConstruction:
    """⚠️ 这组测试钉住一个**实测出来的反直觉行为**，不要"修"它。

    arXiv 把 search_query 里空格分隔的词按 OR 解释。实测对比：
        原样（OR） total=478,546  前 5 条全部高度相关 ✅
        显式 AND   total=0        ❌
    所以本源**原样透传**，只做 all: 包装。
    """

    def test_bare_query_wrapped_in_all(self):
        assert arxiv.build_query("helium auroral") == "all:helium auroral"

    def test_query_with_field_prefix_passed_through_untouched(self):
        q = "ti:helium AND abs:auroral"
        assert arxiv.build_query(q) == q

    def test_query_with_boolean_operator_passed_through(self):
        q = "helium OR auroral"
        assert arxiv.build_query(q) == q

    def test_does_not_inject_AND(self):
        """如果有人把这里改成 AND，线上会从 47 万条变 0 条。"""
        out = arxiv.build_query("a b c d")
        assert " AND " not in out


class TestArxivParsing:
    def test_parses_entries(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_FIXTURE)
        works = [w for w in (arxiv._entry_to_work(e, "q", 600)
                             for e in root.findall("a:entry", arxiv.NS)) if w]
        assert len(works) == 2

        w = works[0]
        assert w.ids["arxiv"] == "9901.00001"          # 版本后缀已剥离
        assert w.ids["doi"] == "10.1234/synthetic.1999"  # ← arxiv:doi 映到正式版
        assert w.year == 1999
        assert w.type == "preprint"
        assert w.authors == ["Ada Lovelace", "Alan Turing"]
        assert "markup" in w.title
        assert w.venue == "J. Synthetic, 1:1, 1999"
        assert w.preprint["version"] == "2"
        assert w.preprint["published_doi"] == "10.1234/synthetic.1999"

    def test_doi_absent_is_not_invented(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_FIXTURE)
        second = root.findall("a:entry", arxiv.NS)[1]
        w = arxiv._entry_to_work(second, "q", 600)
        assert "doi" not in w.ids, "没有 arxiv:doi 就不许编一个出来"
        assert w.preprint["published_doi"] == ""

    def test_abstract_truncation(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_FIXTURE)
        w = arxiv._entry_to_work(root.findall("a:entry", arxiv.NS)[0], "q", 10)
        assert w.abstract_truncated is True
        assert len(w.abstract) <= 12

    def test_oa_is_green_repository_copy(self):
        """arXiv 是仓库副本 ⇒ oa_status=green，in_repository=True。"""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ATOM_FIXTURE)
        w = arxiv._entry_to_work(root.findall("a:entry", arxiv.NS)[0], "q", 600)
        assert w.oa.status == "green"
        assert w.oa.in_repository is True


class TestBiorxiv:
    def test_published_na_is_not_a_doi(self):
        """biorxiv 未发表时 published 是字面量 'NA'，不是空串——
        照抄会把 'NA' 当成 DOI 发出去。"""
        for marker in ("NA", "na", "n/a", "none"):
            row = {"doi": "10.1101/x", "published": marker, "title": "t"}
            w = biorxiv._to_work(row, "biorxiv", "10.1101/x")
            assert w.preprint["published_doi"] == ""
            assert "published_doi" not in w.ids

    def test_real_published_doi_recorded_both_ways(self):
        row = {"doi": "10.1101/x", "published": "10.1234/y", "title": "t", "version": "2"}
        w = biorxiv._to_work(row, "medrxiv", "10.1101/x")
        assert w.preprint["published_doi"] == "10.1234/y"
        assert w.ids["published_doi"] == "10.1234/y"

    def test_author_split_on_semicolon(self):
        row = {"doi": "10.1/x", "published": "NA", "authors": "A B; C D;; E F",
               "title": "t"}
        w = biorxiv._to_work(row, "biorxiv", "10.1/x")
        assert w.authors == ["A B", "C D", "E F"]

    def test_rows_rejects_unexpected_shape(self):
        """结构不符要抛，由调用方翻成 UPSTREAM_CHANGED —— 不许静默返回空。"""
        with pytest.raises(TypeError):
            biorxiv._rows(["not", "a", "dict"])
        with pytest.raises(KeyError):
            biorxiv._rows({"no_collection": []})


class TestOpenAlex:
    def test_diamond_maps_to_gold_but_raw_is_kept(self):
        """契约 §4.2：OpenAlex 有 6 个值（多一个 diamond），Unpaywall 是 5 个。
        映射为 gold，**但原值必须保留** —— 不隐藏源差异。"""
        item = {"doi": "https://doi.org/10.1/x", "display_name": "t",
                "ids": {}, "open_access": {"is_oa": True, "oa_status": "diamond"}}
        w = openalex._to_work(item, 600)
        assert w.oa.status == "gold"
        assert w.preprint["openalex_raw_oa_status"] == "diamond"

    def test_non_diamond_statuses_pass_through(self):
        for s in ("gold", "hybrid", "bronze", "green", "closed"):
            item = {"display_name": "t", "open_access": {"is_oa": s != "closed",
                                                         "oa_status": s}}
            assert openalex._to_work(item, 600).oa.status == s

    def test_abstract_from_inverted_index(self):
        """OpenAlex 只给倒排索引（官方：法律原因不发纯文本）。"""
        inv = {"the": [0], "quick": [1], "fox": [2, 4], "brown": [3]}
        assert openalex._abstract(inv) == "the quick fox brown fox"

    def test_empty_inverted_index(self):
        assert openalex._abstract(None) == ""
        assert openalex._abstract({}) == ""

    def test_disabled_without_key(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_OPENALEX_KEY", "")
        assert openalex.enabled() is False
        out = openalex.get_by_doi("10.1/x")
        assert out.error["code"] == "CONFIG_MISSING"

    def test_ids_stripped_to_bare_identifiers(self):
        item = {"doi": "https://doi.org/10.1/x", "display_name": "t",
                "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/12345"},
                "open_access": {"is_oa": False}}
        w = openalex._to_work(item, 600)
        assert w.ids["doi"] == "10.1/x"
        assert w.ids["pmid"] == "12345"
