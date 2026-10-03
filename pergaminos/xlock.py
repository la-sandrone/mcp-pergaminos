#!/usr/bin/env python3
r"""文件名: xlock.py
摘要: 跨进程互斥 + 状态时间戳。**零第三方依赖**（仅标准库），
     因此 Windows 侧也能单独导入与测试。
     POSIX  → fcntl.flock
     Windows→ kernel32.CreateMutexW（真·有名互斥体，ctypes 调用，无需 pywin32）
依赖: 标准库（ctypes / fcntl / os / json）

═══════════════════════════════════════════════════════════════════════════
  为什么必须是跨进程的
═══════════════════════════════════════════════════════════════════════════
arXiv 的限速是「每 3 秒 1 次、单连接，且对**你控制的所有机器合计**」。
而本机确实会同时跑多个实例（配置注释即有
「⚠️ 并行测试注意：现役 9007 同时跑着同一套 MCP」）。
进程内的 asyncio.Lock 各管各的 ⇒ 实际速率翻倍 ⇒ 违约、可能被封。

⇒ 互斥必须落到**内核对象**上：
     Windows: 有名互斥体（CreateMutexW）——正是"名字"让别的进程能找来
     POSIX  : flock —— 用文件路径充当那个"名字"
   两者语义等价（内核级、跨进程、天然 FIFO、持有者崩溃后自动释放）。

Windows 细节：
  · 名字空间用 `Local\` 前缀（会话内）。**不用 `Global\`**——它需要
    SeCreateGlobalPrivilege，普通用户进程通常没有。
  · 互斥体所有权**绑定线程**：必须由取得的线程释放。本模块按"取用即释放
    在同一线程内"使用，不要跨线程传递。
  · 持有者崩溃 ⇒ 下一个等到 `WAIT_ABANDONED`，照常取得所有权（不卡死）。
  · 名字里不能含 `\`（那是名字空间分隔符）——构造时已清洗。
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("pergaminos.xlock")

IS_WINDOWS = os.name == "nt"

_CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
THROTTLE_DIR = _CACHE_HOME / "pergaminos" / "throttle"


# ══════════════════════════════════════════════════════════════════════════
#  平台后端：只暴露 _acquire() / _release()
# ══════════════════════════════════════════════════════════════════════════
class _PosixBackend:
    """fcntl.flock —— 文件路径即互斥体名字。"""

    def __init__(self, name: str, state_path: Path) -> None:
        import fcntl                      # 仅在 POSIX 分支导入（Windows 无此模块）
        self._fcntl = fcntl
        self.state_path = state_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.state_path, os.O_RDWR | os.O_CREAT, 0o600)
        self._fcntl.flock(self._fd, self._fcntl.LOCK_EX)      # 阻塞 → 天然 FIFO

    def release(self) -> None:
        if self._fd is not None:
            try:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def read_state(self) -> dict:
        try:
            raw = self.state_path.read_bytes()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_bytes(json.dumps(state).encode("utf-8"))


class _WindowsBackend:
    """CreateMutexW —— 真·有名互斥体；状态另存文件。"""

    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_ABANDONED = 0x00000080       # 前任持有者崩了，但所有权归我们
    _INFINITE = 0xFFFFFFFF

    def __init__(self, name: str, state_path: Path) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self.state_path = state_path
        self._handle = None

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32

        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        self.mutex_name = f"Local\\pergaminos-throttle-{safe}"

    def acquire(self) -> None:
        h = self._k32.CreateMutexW(None, False, self.mutex_name)
        if not h:
            raise OSError(
                f"CreateMutexW 失败: {self._ctypes.WinError(self._ctypes.get_last_error())}"
            )
        rc = self._k32.WaitForSingleObject(h, self._INFINITE)
        if rc not in (self._WAIT_OBJECT_0, self._WAIT_ABANDONED):
            self._k32.CloseHandle(h)
            raise OSError(f"WaitForSingleObject 返回 0x{rc:08X}")
        if rc == self._WAIT_ABANDONED:
            logger.warning("互斥体 %s 前任持有者异常退出，已接管", self.mutex_name)
        self._handle = h

    def release(self) -> None:
        if self._handle is not None:
            self._k32.ReleaseMutex(self._handle)
            self._k32.CloseHandle(self._handle)
            self._handle = None

    def read_state(self) -> dict:
        try:
            raw = self.state_path.read_bytes()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_bytes(json.dumps(state).encode("utf-8"))


def _backend(name: str, state_path: Path):
    return _WindowsBackend(name, state_path) if IS_WINDOWS else _PosixBackend(name, state_path)


# ══════════════════════════════════════════════════════════════════════════
#  节流器
# ══════════════════════════════════════════════════════════════════════════
@contextmanager
def throttle(name: str, min_interval_ms: int) -> Iterator[float]:
    """跨进程节流。进入时保证距上次取用至少 `min_interval_ms`。

    产出实际等待的秒数（供上报——arXiv 那 3 秒要让模型知道在等什么）。

    用法:
        with throttle("arxiv", 3000) as waited:
            ... 出站请求 ...
    """
    if min_interval_ms <= 0:
        yield 0.0
        return

    be = _backend(name, THROTTLE_DIR / f"{name}.json")
    be.acquire()
    try:
        state = be.read_state()
        last_ms = int(state.get("last_ms", 0))
        now_ms = int(time.time() * 1000)
        wait_s = max(0.0, (min_interval_ms - (now_ms - last_ms)) / 1000.0)
        if wait_s > 0:
            logger.debug("[%s] 节流等待 %.2fs（跨进程）", name, wait_s)
            time.sleep(wait_s)
        be.write_state({"last_ms": int(time.time() * 1000), "pid": os.getpid()})
        yield wait_s
    finally:
        be.release()


def reset(name: str) -> None:
    """清掉某个源的节流状态（测试与排障用）。"""
    p = THROTTLE_DIR / f"{name}.json"
    if p.exists():
        p.unlink()
