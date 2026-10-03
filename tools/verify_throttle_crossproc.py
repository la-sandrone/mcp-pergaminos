#!/usr/bin/env python3
"""验证 xlock.throttle 的**跨进程**节流真的生效——Linux 与 Windows 双平台。

方法：同时拉起 N 个独立进程，每个都去抢同一个源名的节流锁，
记录各自「拿到锁并写回时间戳」的时刻。若跨进程成立，相邻时刻差应 ≥ min_interval。

这是本项目最关键的一条声称：
  · 进程内 asyncio.Lock 做不到它（各进程各管各的 → 速率翻倍）
  · 而 arXiv 的限速是「对你控制的**所有机器**合计」
后端按平台自动分派：
  · POSIX  → fcntl.flock（文件路径充当互斥体名）
  · Windows→ kernel32.CreateMutexW（**真·有名互斥体**，ctypes，无需 pywin32）

只依赖 pergaminos.xlock（零第三方依赖），因此 Windows 侧无需装任何东西即可运行。

用法: python verify_throttle_crossproc.py [并发数] [间隔ms]
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = "_probe_throttle"

CHILD = """
import sys, time
sys.path.insert(0, {root!r})
from pergaminos import xlock
with xlock.throttle({src!r}, {interval}) as w:
    print(time.time())
"""


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 3000

    sys.path.insert(0, str(ROOT))
    from pergaminos import xlock

    xlock.reset(SRC)

    code = CHILD.format(root=str(ROOT), src=SRC, interval=interval)
    procs = [
        subprocess.Popen([sys.executable, "-c", code],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(n)
    ]
    stamps: list[float] = []
    for i, p in enumerate(procs):
        out, err = p.communicate(timeout=300)
        if p.returncode != 0:
            print(f"  child {i + 1} failed rc={p.returncode}\n{err}")
            return 2
        stamps.append(float(out.strip()))
    stamps.sort()

    backend = "CreateMutexW(named mutex)" if xlock.IS_WINDOWS else "fcntl.flock"
    print(f"platform    {'Windows' if xlock.IS_WINDOWS else 'POSIX'} / backend {backend}")
    print(f"concurrency {n} procs / interval {interval}ms")
    print(f"state file  {xlock.THROTTLE_DIR / (SRC + '.json')}")
    print()

    gaps: list[float] = []
    for i, t in enumerate(stamps):
        if i == 0:
            print(f"  proc {i + 1}: {t:.3f}  (first, no wait)")
        else:
            gap = (t - stamps[i - 1]) * 1000
            gaps.append(gap)
            print(f"  proc {i + 1}: {t:.3f}   gap vs prev {gap:>8.1f}ms  "
                  f"{'OK  ' if gap >= interval * 0.9 else 'FAIL'}")

    print()
    if not gaps:
        print("single process, nothing to compare")
        return 0
    worst = min(gaps)
    total = (stamps[-1] - stamps[0]) * 1000
    print(f"min gap     {worst:.1f}ms (required >={interval * 0.9:.0f}ms)")
    print(f"total span  {total:.1f}ms (theoretical {interval * (n - 1)}ms)")
    if worst >= interval * 0.9:
        print("\n[PASS] cross-process throttle holds; multiple instances will NOT double-hit the upstream")
        xlock.reset(SRC)
        return 0
    print("\n[FAIL] cross-process throttle does NOT hold; processes have no shared state")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
