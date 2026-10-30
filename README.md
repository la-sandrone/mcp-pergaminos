# mcp-pergaminos

**学术论文检索与 OA 对账 MCP 服务器。只报告元数据与获取路径，不下载全文。**

> **TL;DR** —— 两个工具：`paper_search` 跨源检索（Crossref 主力、arXiv 预印本、bioRxiv/medRxiv），`paper_oa` 按 DOI / arXiv id / 标题对账 OA 状态与收录位置（以 Unpaywall 为权威）。各源覆盖不同，所以默认按源分组返回、只做 DOI 精确合并，不做推断式拼接。

名字取自加西亚·马尔克斯《百年孤独》里的**墨尔基阿德斯**（Melquíades）——
那个把布恩迪亚家族百年史写在羊皮卷上、并且
「**没有按人类惯常的时间顺序来排列这些事件**」的人。本项目与同为该出处的
[mcp-melquiades]（会话档案）共享这一母题：**身份稳定，顺序易变**。

---

## 它做什么

两个工具，只有两个：

| 工具 | 用途 |
|---|---|
| `paper_search` | **检索**：给关键词，跨源找论文，返回元数据 |
| `paper_oa` | **对账**：给 DOI / arXiv id / 标题，报告 OA 状态与**收录位置** |

## 它**不**做什么

**不下载全文。没有、也不会有下载工具。** 四条理由：

1. **版权** —— arXiv 使用条款「不得做的事」明文禁止：*"Store and serve arXiv e-prints
   (PDFs, source files, or other content) from your servers, **unless you have the
   permission of the copyright holder**"*，并且明确 *"arXiv is not the copyright holder"*。
   我们既不是版权持有人，也没有获得许可。
2. **限速** —— arXiv 官方要求「**每 3 秒不超过 1 次请求，且限于单连接**」，
   而且这个限制适用于「**你控制的所有机器合计**」。一次全文抓取就吃掉 3 秒额度，
   对账 20 篇就是 60 秒纯等待。
3. **静默降级** —— 出版商站点常有反爬/登录墙；失败常表现为
   **HTTP 200 但内容是一个 PDF.js 空壳**。模型会以为拿到了论文，
   然后基于一页 JavaScript 编出结论。**这比报错更危险**，所以从接口层消除。
4. **成本** —— 若启用 OpenAlex，官方定价把全文下载定为检索的 **10 倍**（$10 vs $1 / 1000 次）。

拿到 DOI 与获取路径之后，由**人**决定怎么取。

---

## 数据源与覆盖边界

**各源覆盖不同，所以默认按源分组返回，不做推断式合并。** 这是有意的：
合并会掩盖「这条来自哪儿」，而不同源的可靠性并不相同。

| 源 | 角色 | 认证 | 官方限速 |
|---|---|---|---|
| **Crossref** | 检索主力 + DOI 元数据权威 | 无 / Polite(带 `mailto`) | Public 5 req/s **并发 1**；Polite 10 req/s 并发 3 |
| **Unpaywall** | **OA 权威**（但**不能检索**） | **email 必填** | 100,000 次/天 |
| arXiv | 预印本检索 | 无 | **每 3 秒 1 次，单连接，全机器合计** |
| bioRxiv / medRxiv | 预印本 | 无 | 未公布 |
| OpenAlex | *可选*（默认关闭） | 免费 key | 免费 $1/天，**无法在不付款下提高** |

### 已知的边界（不知道这些会误判）

- **Unpaywall 不能检索。** 它的 `/v2/search` 已于 **2026-09-18 废止**（返回 `410 Gone`）。
  现在只能按 DOI 查。
- **Crossref 搜不到 arXiv 预印本。** arXiv 的 DOI 由 **DataCite** 注册，不在 Crossref。
- **Crossref 的摘要有很大空缺**（实测常见 `abstract` 为空）；arXiv 则稳定提供摘要。
- **同一篇论文可能有两个 DOI**（预印本 + 正式版）。所以合并只做 **DOI 精确匹配**，
  **绝不按标题猜测**——`merge=true` 也只做精确匹配。
- **OA 分类法的值集两源不同**：Unpaywall 是 5 值
  （`gold`/`hybrid`/`bronze`/`green`/`closed`），OpenAlex 多一个 `diamond`。
  本工具以 Unpaywall 为准，若启用 OpenAlex 会在 `conflicts` 里显式记差异。

---

## 配置

优先级：**真实环境变量 > `.env` > 代码内兜底**。
代码内的兜底常量**只放可公开的安全默认值**——本项目要发布，
任何"为了跑得通向里塞一下"的值都会随发布泄露。

```sh
cp .env.example .env
$EDITOR .env
```

必填一项：

```ini
PERGAMINOS_UNPAYWALL_EMAIL=you@example.com
```

常用项见 `.env.example`。**代理**支持 `http://` 与 `socks5://`
（后者需要 `socksio`，即 `pip install 'httpx[socks]'`），并可按源豁免：

```ini
PERGAMINOS_PROXY=http://127.0.0.1:8994
PERGAMINOS_PROXY_BYPASS=arxiv
```

**启动时会把每个源的生效配置打印到 stderr**——代理、池、超时、节流值。
这是故意的：配置配错了却看不出来（「静默指错」）比崩溃难查得多。

---

## 运行

依赖：`mcp>=2.2`、`httpx`（含 `socksio` 可选）。

```sh
python -m pergaminos.server          # stdio
```

MCP 客户端（如 DSH）配置示例：

```yaml
- name: pergaminos
  command: bash
  args: ["-l", "/path/to/mcp-stdio.sh", "pergaminos"]
```

---

## 设计要点（给要改代码的人）

- **所有 HTTP 出网必须走 `pergaminos/net.py` 的 `request()`。**
  上一代有 29 个各自为政的出网点，策略必然漂移。这里只有一个权威。
- **重试策略的数值有出处**（`net.py` 文件头有完整交代）：`total=5`、`backoff=0.5`
  （退避序列 0/1/2/4/8，累计 15s）。特别注意
  **`429` 被显式排除**在可重试状态之外——匿名共享池的限流是持续状态，退避救不回来。
- **跨进程节流**（`xlock.py`）：arXiv 的限速是"全机器合计"，
  而本机可能同时跑多个实例，所以互斥必须落到内核对象上。
  POSIX 用 `flock`，Windows 用 `CreateMutexW`（真·有名互斥体，ctypes，无需 pywin32）。
  **双平台均有自检脚本**：`tools/verify_throttle_crossproc.py`。
- **每源有总预算**（`PER_SOURCE_TIMEOUT_MS`）：上一代用 `asyncio.gather` 无预算，
  结果是「等最慢协程 ⇒ 每次搜索白等 15 秒」。现在是并行 + 每源硬预算，
  总耗时 ≈ max(各源) 而非求和。
- **上游结构变化必须显式报错**（`UPSTREAM_CHANGED`），**绝不静默返回空**。
  这条不是洁癖：Unpaywall 的 `/v2/search` 在 2026-09-18 刚变成 `410 Gone`。
- **控制台输出一律 ASCII。** Windows 控制台默认 GBK(CP936)，
  中文与 emoji 会乱码甚至 `UnicodeEncodeError` 崩溃——实测**单一个 ✅ 就足以让脚本挂掉**。
  协议输出走 `json.dumps(..., ensure_ascii=True)`，任何编码都安全。

## 自检

```sh
python tools/verify_throttle_crossproc.py 4 3000   # 跨进程节流（双平台）
python tools/smoke_e2e.py                          # 端到端（需联网）
```

---

## 许可

**代码**：AGPL-3.0（见 `LICENSE`）。

**数据**：不适用本仓库的许可。本工具只是**转发**，数据的权利归各来源：

- **arXiv** 的**描述性元数据**以 **CC0 1.0** 释出（官方明文）；e-print 正文的版权归作者或出版方。
- **Crossref**、**Unpaywall**、**OpenAlex** 的数据适用其各自的服务条款。

**本工具不存储、不代持、不镜像任何论文全文。**
