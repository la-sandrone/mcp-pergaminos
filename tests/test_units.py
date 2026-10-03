#!/usr/bin/env python3
"""文件名: tests/test_units.py
摘要: 离线单元测试。**全部用合成 fixture，不含任何真实检索痕迹**——
     真实 DOI 列表与摘要既是隐私也可能是版权问题，公开仓库不提交它们。
依赖: pytest；被测模块（不需要网络，不需要 API key）
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pergaminos import config, net, xlock                       # noqa: E402
from pergaminos.sources.base import Work, clean_text, truncate  # noqa: E402


# ── 重试策略：数值有出处，改动即破坏标定 ──────────────────────────────
class TestRetryPolicy:
    def test_backoff_sequence_matches_calibration(self):
        """2026-09-28 标定：backoff_factor=0.5 → 0/1/2/4/8，累计 15s。"""
        got = [net.retry_delay(i) for i in range(1, 6)]
        assert got == [0.0, 1.0, 2.0, 4.0, 8.0]
        assert sum(got) == 15.0

    def test_retry_total_is_five(self):
        assert net.RETRY_TOTAL == 5          # 5 次重试 = 共 6 次尝试

    def test_429_is_not_retryable(self):
        """2026-09-02 裁决：匿名共享池限流是持续状态，退避救不回来。"""
        assert 429 not in net.RETRY_STATUS
        assert net.RETRY_STATUS == frozenset({502, 503, 504})

    def test_only_idempotent_methods(self):
        assert net.RETRY_METHODS == frozenset({"GET", "HEAD"})
        with pytest.raises(ValueError, match="可重试方法集"):
            net.request("crossref", "POST", "https://example.invalid/")

    def test_arxiv_interval_is_official_three_seconds(self):
        assert net.MIN_INTERVAL_MS["arxiv"] == 3000


# ── 跨进程节流 ────────────────────────────────────────────────────────
class TestThrottle:
    def test_enforces_interval(self):
        name = "_test_throttle_unit"
        xlock.reset(name)
        t0 = time.monotonic()
        with xlock.throttle(name, 300):
            pass
        first = time.monotonic() - t0
        t1 = time.monotonic()
        with xlock.throttle(name, 300) as waited:
            pass
        second = time.monotonic() - t1
        xlock.reset(name)
        assert first < 0.1, "首次不该等"
        assert second >= 0.25, f"第二次应等到间隔（实测 {second:.3f}s）"
        assert waited > 0

    def test_zero_interval_is_free(self):
        t0 = time.monotonic()
        with xlock.throttle("_test_zero", 0):
            pass
        assert time.monotonic() - t0 < 0.05


# ── 文本清洗（Crossref 摘要是 JATS XML）──────────────────────────────
class TestText:
    def test_strips_jats_markup(self):
        raw = "<jats:p>Helium <jats:italic>n</jats:italic>=3 excitation</jats:p>"
        assert clean_text(raw) == "Helium n =3 excitation"

    def test_unescapes_entities(self):
        assert clean_text("a &amp; b &lt;c&gt;") == "a & b <c>"

    def test_truncate_marks_and_reports(self):
        text, cut = truncate("x" * 100, 10)
        assert cut is True and len(text) <= 12 and text.endswith("…")

    def test_truncate_zero_means_no_limit(self):
        text, cut = truncate("y" * 500, 0)
        assert cut is False and len(text) == 500

    def test_empty_input(self):
        assert clean_text(None) == ""
        assert clean_text("") == ""


# ── 合并键 ────────────────────────────────────────────────────────────
class TestWorkKey:
    def test_prefers_doi_and_lowercases(self):
        a = Work(ids={"doi": "10.1/ABC"}, title="t")
        b = Work(ids={"doi": "10.1/abc"}, title="other")
        assert a.key() == b.key()

    def test_falls_back_to_title_hash(self):
        a = Work(title="Mass-Radius  Relations")
        b = Work(title="mass-radius relations")
        assert a.key() == b.key()
        assert a.key().startswith("title:")

    def test_distinct_works_differ(self):
        assert Work(ids={"doi": "10.1/a"}).key() != Work(ids={"doi": "10.1/b"}).key()

    def test_empty_ids_dropped_from_output(self):
        d = Work(ids={"doi": "10.1/a", "pmid": ""}).as_dict()
        assert d["ids"] == {"doi": "10.1/a"}


# ── 配置 ──────────────────────────────────────────────────────────────
class TestConfig:
    def test_real_env_beats_default(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_TIMEOUT_MS", "1234")
        assert config.get("TIMEOUT_MS") == "1234"

    def test_required_missing_is_fatal(self, monkeypatch):
        monkeypatch.delenv("PERGAMINOS_UNPAYWALL_EMAIL", raising=False)
        monkeypatch.setattr(config, "DEFAULTS",
                            {**config.DEFAULTS, "UNPAYWALL_EMAIL": ""})
        with pytest.raises(SystemExit, match="UNPAYWALL_EMAIL"):
            config.require("UNPAYWALL_EMAIL")

    def test_int_rejects_garbage(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_TIMEOUT_MS", "abc")
        with pytest.raises(SystemExit, match="必须是整数"):
            config.get_int("TIMEOUT_MS")

    def test_ua_is_ascii_only(self, monkeypatch):
        """RFC tchar 不含非 ASCII；且 Windows 控制台是 GBK，
        非 ASCII 会乱码甚至 UnicodeEncodeError。"""
        monkeypatch.setenv("PERGAMINOS_CONTACT", "someone@example.com")
        ua = config.user_agent()
        ua.encode("ascii")                      # 不抛即通过
        assert ua.startswith("Los-pergaminos-de-Melquiades/")
        assert "Mozilla" not in ua              # 不伪装浏览器

    def test_describe_is_ascii_only(self, monkeypatch):
        """启动打印必须能在 GBK 控制台输出——实测单个 emoji 就能让它崩。"""
        monkeypatch.setenv("PERGAMINOS_UNPAYWALL_EMAIL", "a@example.com")
        config.describe().encode("ascii")       # 不抛即通过

    def test_describe_reports_openalex_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PERGAMINOS_OPENALEX_KEY", raising=False)
        assert "DISABLED" in config.describe()

    def test_contact_falls_back_to_unpaywall_email(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_UNPAYWALL_EMAIL", "z@example.com")
        monkeypatch.delenv("PERGAMINOS_CONTACT", raising=False)
        assert config.contact() == "z@example.com"

    def test_namespace_is_own_not_legacy(self):
        """命名空间必须是本项目自己的——上一代叫 PaperSearchMCP。"""
        assert config.ENV_PREFIX == "PERGAMINOS_"
        assert not config.ENV_PREFIX.startswith(config._LEGACY_PREFIX)

    def test_defaults_keys_are_full_var_names(self):
        """DEFAULTS 的键就是 .env 里必须写的字符串——不许再有隐式拼接。

        这条是关键回归闸：曾经 DEFAULTS 写 OPENALEX_KEY、.env 得写
        PAPERSEARCH_OPENALEX_KEY，两处不一致 → 配置静默失效。
        """
        assert config.DEFAULTS, "DEFAULTS 不该为空"
        for key in config.DEFAULTS:
            assert key.startswith(config.ENV_PREFIX), f"{key} 不是全名"
        # 复用来源也必须是全名（否则 get() 里的 _REUSE.get(full) 永远查不中）
        for key, src in config._REUSE.items():
            assert key in config.DEFAULTS, f"_REUSE 的键 {key} 不在 DEFAULTS 里"
            assert src in config.DEFAULTS, f"_REUSE 的值 {src} 不在 DEFAULTS 里"

    def test_get_accepts_short_and_full_name(self, monkeypatch):
        """短名与全名等价——调用点用短名，.env 写全名，两者必须通。"""
        monkeypatch.setenv("PERGAMINOS_TIMEOUT_MS", "4321")
        assert config.get("TIMEOUT_MS") == "4321"
        assert config.get("PERGAMINOS_TIMEOUT_MS") == "4321"
        assert config.resolve_name("TIMEOUT_MS") == "PERGAMINOS_TIMEOUT_MS"
        assert config.resolve_name("PERGAMINOS_TIMEOUT_MS") == "PERGAMINOS_TIMEOUT_MS"

    def test_unread_keys_flags_missing_prefix(self):
        """漏写前缀 = 静默失效，必须被指名。

        实测事故：.env 写 OPENALEX_KEY=… → 启动打印 openalex DISABLED (no key)，
        而 describe() 只报期望名，看不出「你写的那个没人读」。
        """
        out = config._unread_keys({"OPENALEX_KEY": "x"})
        assert out == [("OPENALEX_KEY", "missing prefix; use PERGAMINOS_OPENALEX_KEY")]

    def test_unread_keys_flags_legacy_prefix(self):
        """上一代前缀要提示改名，而不是含糊地说「未知」。"""
        out = config._unread_keys({"PAPERSEARCH_PROXY": "http://127.0.0.1:8994"})
        assert len(out) == 1
        assert "PERGAMINOS_PROXY" in out[0][1]

    def test_unread_keys_flags_unknown_prefixed(self):
        out = config._unread_keys({"PERGAMINOS_TYPOO": "x"})
        assert len(out) == 1
        assert "no code reads it" in out[0][1]

    def test_unread_keys_silent_on_good_input(self):
        assert config._unread_keys({"PERGAMINOS_OPENALEX_KEY": "k"}) == []
        assert config._unread_keys({"PERGAMINOS_ENV_FILE": "/tmp/x"}) == []   # 特殊变量
        assert config._unread_keys({}) == []

    def test_describe_surfaces_unread_keys(self, monkeypatch):
        """静默失效的东西必须出现在启动打印里，且不破坏 ASCII 约束。"""
        monkeypatch.setattr(config, "_unread_env_keys",
                            [("OPENALEX_KEY", "missing prefix; use PERGAMINOS_OPENALEX_KEY")])
        monkeypatch.setenv("PERGAMINOS_UNPAYWALL_EMAIL", "a@example.com")
        out = config.describe()
        assert "UNREAD" in out
        assert "OPENALEX_KEY" in out
        out.encode("ascii")                     # 不抛即通过

    def test_describe_unread_stays_ascii_for_non_ascii_key(self, monkeypatch):
        """键名可能是非 ASCII（手抖输入法）——ASCII 约束不能被它打破。"""
        monkeypatch.setattr(config, "_unread_env_keys", [("键名", "missing prefix and unknown name")])
        monkeypatch.setenv("PERGAMINOS_UNPAYWALL_EMAIL", "a@example.com")
        config.describe().encode("ascii")       # 不抛即通过


# ── 代理 ──────────────────────────────────────────────────────────────
class TestProxy:
    def test_empty_means_direct(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_PROXY", "")
        assert net.proxies_for("crossref") is None

    def test_http_proxy_passes_through(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_PROXY", "http://127.0.0.1:8994")
        monkeypatch.setenv("PERGAMINOS_PROXY_BYPASS", "arxiv")
        assert net.proxies_for("crossref") == "http://127.0.0.1:8994"
        assert net.proxies_for("arxiv") is None      # 按源豁免

    def test_socks5_fails_loud_without_socksio(self, monkeypatch):
        monkeypatch.setenv("PERGAMINOS_PROXY", "socks5://127.0.0.1:1080")
        monkeypatch.setitem(sys.modules, "socksio", None)
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name == "socksio":
                raise ImportError("no socksio")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake)
        with pytest.raises(net.ProxyConfigError, match="socksio"):
            net.proxies_for("crossref")


# ── 日志脱敏（UA 必须带真邮箱，但**打印**不许带）────────────────────────
class TestRedaction:
    def test_masks_local_part(self):
        assert config.redact_email("alice@example.com") == "a****@example.com"

    def test_one_char_local_part_still_masked(self):
        """单字符 local part 若不补 * 就等于原样输出，反而完全泄露。"""
        assert config.redact_email("a@example.com") == "a*@example.com"

    def test_empty(self):
        assert config.redact_email("") == "(empty)"
        assert config.redact_email("not-an-email") == "set"

    def test_describe_never_prints_full_email(self, monkeypatch):
        """describe() 会被写进日志；真邮箱留在日志里就是泄露。"""
        addr = "secretuser@example.com"
        monkeypatch.setenv("PERGAMINOS_UNPAYWALL_EMAIL", addr)
        out = config.describe()
        assert addr not in out, "启动打印泄露了完整邮箱"
        assert "secretuser" not in out

    def test_user_agent_still_carries_real_email(self, monkeypatch):
        """但 UA 本体必须带真邮箱——这是 Crossref/arXiv 的礼貌要求。"""
        addr = "real@example.org"
        monkeypatch.setenv("PERGAMINOS_UNPAYWALL_EMAIL", addr)
        assert addr in config.user_agent()


# ── ASCII 路径（Windows 中文用户名的健壮性）────────────────────────────
class TestAsciiPath:
    def test_plain_path_untouched(self):
        # 占位路径（/home/u/ 是明显的单字符占位）
        assert config.ascii_path("/home/u/.env") == "/home/u/.env"

    def test_non_ascii_escaped(self):
        # 合成路径，不是任何真实机器位置。逐行放行标注：scan-paths-allow
        out = config.ascii_path("/mnt/c/Users/张三/.env")
        assert out.isascii(), "路径必须 ASCII 化"
        assert "\\u" in out

    def test_describe_with_non_ascii_envfile_stays_ascii(self, monkeypatch, tmp_path):
        d = tmp_path / "张三"
        d.mkdir()
        envf = d / ".env"
        envf.write_text("PERGAMINOS_UNPAYWALL_EMAIL=x@example.com\n", encoding="utf-8")
        monkeypatch.setenv("PERGAMINOS_ENV_FILE", str(envf))
        monkeypatch.setattr(config, "_loaded_env_files", [])
        out = config.describe()
        out.encode("ascii")          # 不抛即通过
