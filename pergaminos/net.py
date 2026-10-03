"""文件名: net.py
摘要: 全部 HTTP 出网点的唯一权威。重试策略 + 跨进程节流 + 代理 + 两级超时。
     所有源都必须走 request()，不得自行 httpx 直连——否则策略必然漂移
     （上一代 29 个出网点各自配参的历史就是这个教训）。
依赖: httpx（socks5 需 socksio；ai-services 已装）
      ./config.py

═══════════════════════════════════════════════════════════════════════════
  重试策略的来源与语义（**改动前必读**）
═══════════════════════════════════════════════════════════════════════════
数值取自 2026-09-28「papersearch SSL 重试」那次标定（原为 urllib3 口径）：
    total=5   other=5   backoff_factor=0.5
    allowed_methods={GET, HEAD}   respect_retry_after_header=True
    raise_on_status=False

【发现一】「other=0 是毒药」——urllib3 把异常分成 connect/read/redirect/status
之外还有一类 `other`，而 `SSLEOFError`（TLS 记录中途截断）正好落 `other`。
`other=0`（requests 官方示例的默认）会让它在**第 1 次即放弃**，total 给多大都无效。
  ⚠️ 本项目改用 httpx，**这个坑在结构上不存在**：httpx 的异常谱系是一层干净兜底
     TransportError ├ TimeoutException ├ NetworkError ├ ProtocolError
                    ├ ProxyError       └ UnsupportedProtocol
     没有"5 个独立计数器"，一个 `except httpx.TransportError` 就是天然 catch-all。
  ⇒ **不要按 urllib3 的思路去"补"一个 other 参数**——httpx 没有这个概念。

【发现二】匿名 429 不该重试（2026-09-02 限流排障的裁决）：
匿名共享池限流是**持续状态**，5s/10s 退避救不回来；当时 S2 每次搜索白等 15 秒。
  ⇒ `429` **显式排除**在可重试状态之外，立即返回让调用方降级该源。

【等价性】urllib3 的 total=5 = 5 次重试（共 6 次尝试）；退避序列
0/1/2/4/8 秒（累计 15s）；最坏 15 + 6×20s = **135s**。
本项目照搬该序列（见 retry_delay），但**加一道每源总预算闸门**——
上一代的另一个教训是 `asyncio.gather` **等最慢协程 ⇒ 每次搜索白等 15 秒**。

【raise_on_status=False 的语义】重试耗尽后**返回响应而不是抛异常**。
上游大量调用点靠 status_code 分支处理 4xx/5xx（如 Unpaywall 的 404/422），
抛异常会破坏既有语义 ⇒ 本项目同样**耗尽即返回响应**，由调用方按状态码分支。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Final

import httpx

from . import config, xlock

logger = logging.getLogger("pergaminos.net")

# ── 重试策略（照搬标定值；语义见文件头）──────────────────────────────
RETRY_TOTAL: Final = 5                 # 重试次数（共 6 次尝试）
BACKOFF_FACTOR: Final = 0.5            # 退避序列 0/1/2/4/8
RETRY_METHODS: Final = frozenset({"GET", "HEAD"})

# 可重试的状态码：**仅瞬时服务端故障**。
#   ★ 429 被显式排除（见文件头【发现二】）——它是持续状态，退避无意义。
#   ★ 其余 4xx 一律不重试（客户端错误，重试不会变好）。
RETRY_STATUS: Final = frozenset({502, 503, 504})

# ── 各源官方限速（ms）。0 = 不节流，只靠额度 ──────────────────────────
MIN_INTERVAL_MS: Final[dict[str, int]] = {
    "arxiv": 3000,      # 官方：每 3 秒 1 次、单连接、**全机器合计**
    "crossref": 1000,   # Public 5/s 并发 1；进 Polite 后 10/s 并发 3
    "biorxiv": 1000,    # 官方未公布，保守取值（待核实）
    "medrxiv": 1000,    # 同上
    "unpaywall": 0,     # 100k/天
    "openalex": 0,      # 100 req/s + 日预算
}

_CACHE_HOME: Final = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
THROTTLE_DIR: Final = _CACHE_HOME / "pergaminos" / "throttle"


def retry_delay(attempt: int) -> float:
    """第 `attempt` 次重试前的退避秒数，等价 urllib3 的 backoff_factor=0.5。

    attempt = 1..5 → 0 / 1 / 2 / 4 / 8（累计 15s）
    """
    if attempt <= 1:
        return 0.0
    return BACKOFF_FACTOR * (2 ** (attempt - 1))


# ── 代理 ────────────────────────────────────────────────────────────
class ProxyConfigError(RuntimeError):
    """代理配置不可满足——**首次调用即崩，不静默回退直连**。"""


def proxies_for(source: str) -> str | None:
    raw = config.get("PROXY").strip()
    if not raw:
        return None
    if source.lower() in {s.lower() for s in config.get_list("PROXY_BYPASS")}:
        return None
    if raw.split("://", 1)[0].lower().startswith("socks"):
        try:
            import socksio  # noqa: F401
        except ImportError:
            raise ProxyConfigError(
                f"{config.resolve_name('PROXY')}={raw} 需要 socksio（httpx 的 socks 支持）。"
                f" 请 `pip install 'httpx[socks]'`，或改用 http:// 代理。"
            ) from None
    return raw


# ── 出网点（唯一）───────────────────────────────────────────────────
class RetryExhausted(RuntimeError):
    """传输类失败且重试预算用尽——**没有拿到任何响应**。"""

    def __init__(self, source: str, attempts: int, last: BaseException | None) -> None:
        super().__init__(
            f"[{source}] 重试 {attempts} 次仍无响应（策略已耗尽，不再重试）：{last!r}"
        )
        self.source = source
        self.attempts = attempts
        self.last = last


def request(
    source: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    budget_ms: int | None = None,
    waited: list[float] | None = None,
) -> httpx.Response:
    """唯一 HTTP 出网点。**所有源必须走这里。**

    两级超时：
      · `TIMEOUT_MS`            —— 单次请求
      · `PER_SOURCE_TIMEOUT_MS` —— 该源**含全部重试**的总预算；用完即放弃该源
    第二级是必须的：上一代的教训是 gather **等最慢协程 ⇒ 每次搜索白等 15 秒**。

    重试条件（**仅两类**）：
      · `httpx.TransportError`  —— 连接/读/写/TLS/协议/代理，唯一兜底
      · 状态码 ∈ `RETRY_STATUS`（502/503/504，瞬时服务端故障），并尊重 `Retry-After`
    **429 不重试**，立即返回由调用方降级。

    重试耗尽且从未拿到响应 ⇒ 抛 `RetryExhausted`；拿到过响应 ⇒ 返回该响应
    （等价 `raise_on_status=False`）。
    """
    method = method.upper()
    if method not in RETRY_METHODS:
        raise ValueError(
            f"{method} 不在可重试方法集 {sorted(RETRY_METHODS)} 内——"
            f"非幂等请求不许重试（防重复写入）"
        )

    timeout_s = config.get_int("TIMEOUT_MS") / 1000.0
    budget_ms = (budget_ms if budget_ms is not None
                 else config.get_int("PER_SOURCE_TIMEOUT_MS"))
    deadline = time.monotonic() + budget_ms / 1000.0

    hdrs = {"User-Agent": config.user_agent()}
    if headers:
        hdrs.update(headers)

    last_exc: BaseException | None = None
    last_resp: httpx.Response | None = None
    attempts = 0

    with httpx.Client(
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
        proxy=proxies_for(source),
        headers=hdrs,
    ) as client:
        for attempt in range(RETRY_TOTAL + 1):
            if attempt > 0:
                delay = _retry_after_seconds(last_resp, retry_delay(attempt))
                remaining = deadline - time.monotonic()
                if delay + timeout_s > remaining:
                    logger.warning(
                        "[%s] 预算不足以再重试（剩 %.1fs < 退避 %.1fs + 超时 %.1fs），"
                        "放弃该源", source, remaining, delay, timeout_s,
                    )
                    break
                if delay > 0:
                    time.sleep(delay)

            attempts += 1
            try:
                # 跨进程节流（POSIX flock / Windows 有名互斥体）。
                # 等待秒数累加进 waited_sink，供调用方回报 waited_ms——
                # 契约要求它必须回报：arXiv 那 3 秒要让模型知道"在等什么"，
                # 否则它会以为工具挂了，然后重试，更慢。
                with xlock.throttle(source, MIN_INTERVAL_MS.get(source, 0)) as w:
                    if waited is not None and w:
                        waited.append(w)
                    resp = client.request(method, url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("[%s] 第 %d 次尝试传输失败：%r", source, attempts, exc)
                continue

            last_exc = None
            last_resp = resp
            if resp.status_code not in RETRY_STATUS:
                return resp                       # ← 429 也走这条：立即返回
            logger.warning(
                "[%s] 第 %d 次尝试得到 %d（可重试状态）",
                source, attempts, resp.status_code,
            )

    if last_resp is not None:
        return last_resp                          # 等价 raise_on_status=False
    raise RetryExhausted(source, attempts, last_exc)


def _retry_after_seconds(resp: httpx.Response | None, fallback: float) -> float:
    """服务端给了 Retry-After 就照它等（`respect_retry_after_header` 等价物）。"""
    if resp is None:
        return fallback
    raw = resp.headers.get("Retry-After")
    if not raw:
        return fallback
    try:
        return max(fallback, float(raw))
    except ValueError:
        return fallback     # HTTP-date 形式暂不支持，退回退避序列
