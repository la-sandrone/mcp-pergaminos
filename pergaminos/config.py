"""文件名: config.py
摘要: 配置解析唯一权威。优先级「真实环境变量 > .env > Python 顶部兜底」。
     命名空间只有一个：变量全名由 _v() 构造，DEFAULTS 的键 = .env 里必须
     写的字符串（逐字一致，不隐式拼接）。.env 里写了但没人读的变量会出声。
     兜底常量必须可公开（不得含凭据/绝对路径/个人身份）——本项目要发布。
依赖: 标准库
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Final

logger = logging.getLogger("pergaminos.config")

# ── 命名空间 ─────────────────────────────────────────────────────────
# 本项目叫 pergaminos，不是上一代的 PaperSearchMCP —— 前缀必须是自己的。
# 上一代用 PAPERSEARCH_*；两个项目共用一个命名空间本身就是隐患
# （同名变量互相覆盖，而且「这个变量归谁」无法从名字上看出来）。
ENV_PREFIX: Final = "PERGAMINOS_"


def _v(suffix: str) -> str:
    """变量**全名**的唯一构造点。

    为什么要这一步：命名空间只能有一个，而且必须是「DEFAULTS 里写的」
    与「.env 里写的」逐字相同的那一个。若靠 f"{ENV_PREFIX}{name}" 在读取处
    隐式拼接，就会出现「字典写 OPENALEX_KEY、.env 得写 PAPERSEARCH_OPENALEX_KEY」
    这种两处不一致——一旦不一致，写错的那一边**完全静默**：get() 精确查全名，
    查不到就落回空兜底。
    实测踩过：.env 写 `OPENALEX_KEY=…` → 启动打印 `openalex DISABLED (no key)`，
    一切看起来"正常"，只是开关没生效。
    """
    return f"{ENV_PREFIX}{suffix}"


def resolve_name(name: str) -> str:
    """把短名（OPENALEX_KEY）或全名（PERGAMINOS_OPENALEX_KEY）归一成全名。"""
    return name if name.startswith(ENV_PREFIX) else _v(name)


# ── 兜底默认值（只放可公开的安全值）────────────────────────────────
# 纪律：这里永远不出现邮箱、路径、key。真值一律走 .env 或环境变量。
# 键 = **变量全名**，与 .env 里必须写的字符串逐字一致。
DEFAULTS: Final[dict[str, str]] = {
    _v("UNPAYWALL_EMAIL"): "",            # 必填 -> 缺失即启动失败
    _v("CONTACT"): "",                    # 空则复用 UNPAYWALL_EMAIL
    _v("CROSSREF_MAILTO"): "",            # 空则复用 CONTACT
    _v("OPENALEX_KEY"): "",               # 空 = 不启用 OpenAlex
    _v("PROXY"): "",                      # 空 = 不挂代理；支持 http:// 与 socks5://
    _v("PROXY_BYPASS"): "",               # 逗号分隔源名
    _v("TIMEOUT_MS"): "20000",             # 单请求超时；给短值，不留 5 分钟
    _v("PER_SOURCE_TIMEOUT_MS"): "90000",  # 每源总预算（含全部重试）。
    #   上一代最坏 135s（15s 退避 + 6×20s）且被 gather 放大；
    #   90s 是"够 4~5 次尝试、但硬性收口"的折中——预算是闸门，不是目标。
    _v("CACHE_TTL_H"): "168",
    _v("LOG_LEVEL"): "INFO",
}

# 全名 -> 复用来源的全名（同样不隐式拼接）
_REUSE: Final[dict[str, str]] = {
    _v("CONTACT"): _v("UNPAYWALL_EMAIL"),
    _v("CROSSREF_MAILTO"): _v("CONTACT"),
}

# 上一代的命名空间。出现在 .env / 环境里要**明说已改名**，不能装看不见。
_LEGACY_PREFIX: Final = "PAPERSEARCH_"

_loaded_env_files: list[Path] = []
_resolved_from: dict[str, str] = {}   # 短名 -> 来源描述（供启动打印）
_unread_env_keys: list[tuple[str, str]] = []   # (键, 提示)：.env 里写了但没人读的变量

# 合法、但不在 DEFAULTS 里的变量（_candidate_env_files 会读它）
_SPECIAL_ENV_KEYS: Final[frozenset[str]] = frozenset({_v("ENV_FILE")})


def _candidate_env_files() -> list[Path]:
    """`.env` 的候选位置，按优先顺序。"""
    explicit = os.environ.get(_v("ENV_FILE"), "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    here = Path(__file__).resolve().parent.parent
    return [
        here / ".env",                                        # 仓库根（开发）
        Path.home() / ".config" / "pergaminos" / ".env",       # 用户级
    ]


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _unread_keys(parsed: dict[str, str]) -> list[tuple[str, str]]:
    """挑出 `.env` 里**没有任何代码会读**的变量，附一条 ASCII 修正提示。

    为什么必须报：这类错误**完全静默**。get() 查的是精确全名，键名一写错就
    落回空兜底——表现成「我明明配了，它却说没配」。而 describe() 天生看不见它：
    它只报「期望的名字取到了什么」，不报「你写的那个没人读」。
    实测踩过：.env 写 `OPENALEX_KEY=…` → 启动打印 `openalex DISABLED (no key)`。

    诊断输出全程 ASCII（Windows 控制台 GBK，非 ASCII 会乱码甚至崩）。
    """
    out: list[tuple[str, str]] = []
    for key in parsed:
        if key in DEFAULTS or key in _SPECIAL_ENV_KEYS:
            continue                                       # 正常，会被读取
        if key.startswith(ENV_PREFIX):
            out.append((key, "unknown name; no code reads it"))
        elif key.startswith(_LEGACY_PREFIX):
            suffix = key[len(_LEGACY_PREFIX):]
            out.append((key, f"legacy project name; use {_v(suffix)}"))
        elif _v(key) in DEFAULTS:
            out.append((key, f"missing prefix; use {_v(key)}"))
        else:
            out.append((key, "missing prefix and unknown name"))
    return out


def load_env_files() -> None:
    """加载 `.env`；**不覆盖**已存在的真实环境变量（即真实环境变量优先）。

    同时登记「写了但没人读」的变量（见 `_unread_keys`）——它们会在启动打印
    和日志里出声，不让配置静默失效。
    """
    if _loaded_env_files:
        return
    for path in _candidate_env_files():
        if not path.is_file():
            continue
        try:
            parsed = _parse_env_file(path)
        except Exception as exc:               # 读不动要吵，不能静默
            logger.warning("读取 .env 失败 %s: %s", path, exc)
            continue
        for key, val in parsed.items():
            # setdefault 语义 = 「真实环境变量优先」
            if key not in os.environ:
                os.environ[key] = val
        _loaded_env_files.append(path)
        for key, hint in _unread_keys(parsed):
            if (key, hint) not in _unread_env_keys:
                _unread_env_keys.append((key, hint))
            logger.warning("%s 里的 %s %s（它不会被读取）", ascii_path(path), key, hint)


def get(name: str, default: str | None = None) -> str:
    """取配置。顺序：真实环境变量 → .env → 复用来源 → DEFAULTS → 传入 default。

    `name` 可写短名（`OPENALEX_KEY`）或全名（`PERGAMINOS_OPENALEX_KEY`），
    两者等价——内部一律先归一成全名，再拿全名去查 DEFAULTS。
    """
    load_env_files()
    full = resolve_name(name)
    short = full[len(ENV_PREFIX):]

    if full in os.environ:
        _resolved_from[short] = "env"
        return os.environ[full]

    src = _REUSE.get(full)
    if src:
        v = get(src)
        if v:
            _resolved_from[short] = f"reuse {src}"
            return v

    if DEFAULTS.get(full, "") != "":
        _resolved_from.setdefault(short, "兜底")
        return DEFAULTS[full]

    return DEFAULTS.get(full, "") if default is None else default


def require(name: str) -> str:
    """必填项缺失时**立即崩溃**，不延迟到第一次调用。"""
    v = get(name)
    if not v:
        raise SystemExit(
            f"[pergaminos] 缺少必填配置 {resolve_name(name)}\n"
            f"  请在 .env 或环境变量中设置；参见 .env.example"
        )
    return v


def get_int(name: str) -> int:
    raw = get(name)
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"[pergaminos] {ENV_PREFIX}{name} 必须是整数，得到 {raw!r}") from None


def get_list(name: str) -> list[str]:
    raw = get(name)
    return [x.strip() for x in raw.split(",") if x.strip()]


def contact() -> str:
    return get("CONTACT") or get("UNPAYWALL_EMAIL")


def ascii_path(p: object) -> str:
    """把路径渲染成**保证 ASCII** 的形式。

    为什么必须：describe() 要能在 GBK 控制台输出（Windows 默认 CP936）。
    而 Windows 上中文用户名极常见（`C:\\Users\\张三\\…`），
    路径一旦含非 ASCII，启动打印就会乱码甚至 UnicodeEncodeError。
    非 ASCII 字符转义为 \\uXXXX —— 可读性略降，但**绝不崩**。
    """
    s = str(p)
    if all(ord(c) < 128 for c in s):
        return s
    return s.encode("unicode_escape").decode("ascii")


def redact_email(addr: str) -> str:
    """把邮箱打码成 a***@domain 形式。

    用途：**日志/诊断输出**。UA 本体里必须带真邮箱（Crossref 官方
    best practice 要求 UA 能标识你与你的脚本；arXiv 的 CoC 同理），
    但**打印到日志**会把联系方式留在各处持久化记录里——
    而那正是本项目导出闸门判定为泄露的东西。
    """
    if not addr or "@" not in addr:
        return "set" if addr else "(empty)"
    local, _, domain = addr.partition("@")
    keep = local[:1]
    return f"{keep}{'*' * max(1, len(local) - 1)}@{domain}"


def user_agent() -> str:
    """UA：只允许 ASCII（RFC tchar 不含非 ASCII），不伪装浏览器。

    形态：<product>/<version> (<comment>)
    """
    from . import __version__ as ver

    parts = ["papersearch"]
    repo = "https://github.com/la-sandrone/mcp-pergaminos"
    parts.append(f"+{repo}")
    mail = contact()
    if mail:
        parts.append(f"mailto:{mail}")
    return f"Los-pergaminos-de-Melquiades/{ver} ({'; '.join(parts)})"


def describe() -> str:
    """启动时打印**生效配置**——防「静默指错」。

    ⚠️ **全 ASCII，不许出现中文**。Windows 控制台默认 GBK(CP936)，
    中文与 emoji 会乱码、甚至 UnicodeEncodeError 直接崩
    （实测：单一个 ✅ 就足以让 py.exe 下的脚本挂掉）。
    诊断输出是给"人扫一眼"的，用 ASCII 标签就够。
    """
    load_env_files()
    lines: list[str] = []
    lines.append(f"  ua          {user_agent().replace(contact(), redact_email(contact()))}"
                 if contact() else f"  ua          {user_agent()}")
    if _loaded_env_files:
        lines.append("  env files   " + ", ".join(ascii_path(p) for p in _loaded_env_files))
    else:
        lines.append("  env files   (none; env vars + fallbacks only)")

    # 你写了、但没有任何代码会读的变量。**必须显眼**：它们静默失效，
    # 而下面那段 fields 只报「期望名取到了什么」，天生看不见这一类。
    if _unread_env_keys:
        lines.append("  ---------- .env keys that NO code reads ----------")
        for key, hint in _unread_env_keys:
            lines.append(f"  UNREAD      {ascii_path(key)}  ({hint})")
        lines.append("  ---------- fix or drop them; they have no effect ----------")

    fields = [
        ("unpaywall", "UNPAYWALL_EMAIL", lambda v: "set" if v else "**MISSING**"),
        ("contact", "CONTACT", lambda v: "set" if v else "(empty)"),
        ("crossref", "CROSSREF_MAILTO", lambda v: "set" if v else "(empty -> Public pool)"),
        ("openalex", "OPENALEX_KEY", lambda v: "ENABLED" if v else "DISABLED (no key)"),
        ("proxy", "PROXY", lambda v: v if v else "(none)"),
        ("bypass", "PROXY_BYPASS", lambda v: v if v else "(none)"),
    ]
    for label, name, fmt in fields:
        val = get(name)
        src = _resolved_from.get(name, "-")
        lines.append(f"  {label:<11} {fmt(val):<26} [{src}]")
    lines.append(
        f"  timeout     {get('TIMEOUT_MS')}ms/single, "
        f"{get('PER_SOURCE_TIMEOUT_MS')}ms/budget"
    )
    lines.append(f"  cache ttl   {get('CACHE_TTL_H')}h")
    return "\n".join(lines)


def log_prologue() -> None:
    """把生效配置打到 stderr。

    在 Windows 上先尝试把 stderr 切到 UTF-8；切不动就退化为 ASCII 输出
    （describe() 本身已保证全 ASCII，故退化后仍可读）。
    """
    stream = sys.stderr
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    try:
        print("[pergaminos] effective config:", file=stream)
        print(describe(), file=stream)
        stream.flush()
    except UnicodeEncodeError:      # 极端兜底：绝不因为打印配置而崩
        print("[pergaminos] effective config: (encoding-limited console; "
              "set PYTHONIOENCODING=utf-8)", file=stream)


# 导入即加载（保持与旧 config.py 一致的行为）
load_env_files()
