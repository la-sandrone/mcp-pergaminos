#!/usr/bin/env bash
# 文件名: export_to_public.sh
# 摘要: 白名单导出 + 泄露闸门。从自用工作目录导出到公开发布目录。
#       **不是 cp -r**：白名单之外的东西永远不会被带出去，
#       而且每次导出都是一次泄露检测（闸门命中即拒绝，不静默放行）。
# 依赖: 标准库 + 可选 gitleaks / trivy / trufflehog（未装则走降级 grep）

set -uo pipefail

# WARNING: 本脚本**自己也在导出版里**，所以它不能含任何机器相关路径——
#          否则就把作者机器的目录布局一起发布出去了（被本脚本自己的闸门抓过一次）。
#          SRC 默认取脚本自身所在仓库根；DST 必须显式给，不给就报错。
SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DST="${2:-${PERGAMINOS_PUBLIC_DST:-}}"

if [ -z "$DST" ]; then
  printf '%s\n' "用法: $0 [源目录] <导出版目录>"
  printf '%s\n' "  或设 PERGAMINOS_PUBLIC_DST=<导出版目录>"
  printf '%s\n' "  （导出版目录**故意不给默认值**——避免把本机布局写进发布件）"
  exit 2
fi

# ── 白名单：只有这些进得去 ────────────────────────────────────────────
ALLOW_FILES=(
  README.md LICENSE .env.example .gitignore
  pyproject.toml
)
ALLOW_DIRS=(
  pergaminos tests tools
)

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
fail=0
note() { printf '%s\n' "$*"; }
ok()   { printf "%s  ok  %s%s\n" "$GRN" "$*" "$RST"; }
warn() { printf "%s warn %s%s\n" "$YEL" "$*" "$RST"; }
bad()  { printf "%s FAIL %s%s\n" "$RED" "$*" "$RST"; fail=1; }

# ── 0. 前置 ───────────────────────────────────────────────────────────
[ -d "$SRC" ] || { bad "源目录不存在: $SRC"; exit 1; }
mkdir -p "$DST"
note "src: $SRC"
note "dst: $DST"
note ""

# ── 1. 白名单拷贝 ─────────────────────────────────────────────────────
note "[1] 白名单导出"
rm -rf "$DST"/* 2>/dev/null || true
for f in "${ALLOW_FILES[@]}"; do
  if [ -f "$SRC/$f" ]; then cp -p "$SRC/$f" "$DST/$f"; ok "$f"; else warn "缺 $f"; fi
done
for d in "${ALLOW_DIRS[@]}"; do
  if [ -d "$SRC/$d" ]; then
    # 排除规则**只此一处**。
    # 上一版写了两份：rsync 带 --exclude，降级路径 `|| cp -r` 裸拷不带任何 exclude。
    # 而 rsync **在本机没装**（rc=127，且被 2>/dev/null 吞掉）⇒ 必然走降级 ⇒
    # 排除规则整个失效，__pycache__ / *.pyc 混进了导出件。
    # 教训：降级路径若放松了主路径的保证，那它不是降级，是漏洞——
    #       所以这里不再保留两条路，改用 tar 管道：一处排除表，且 tar 是标准件。
    #       （上游注释"不是 cp -r"的承诺，也是靠这一步才真正成立。）
    if tar -C "$SRC" -cf - \
         --exclude='__pycache__' --exclude='*.pyc' --exclude='*.bak*' \
         --exclude='.env' --exclude='.cache' --exclude='*.log' --exclude='.DS_Store' \
         "$d" | tar -C "$DST" -xf -; then
      ok "$d/"
    else
      bad "导出 $d/ 失败（tar 管道退出码非 0）"
    fi
  else
    warn "缺 $d/"
  fi
done
note ""

# ── 2. 闸门：凭据 ────────────────────────────────────────────────────
# 扫描目标是 **$DST（导出件）**，不是 $SRC —— 两条路（gitleaks / grep）必须一致。
#   为什么不扫 $SRC：源树里**必然**有 .env（它就该待在那儿，且已被 .gitignore 的
#   `.env.*` 覆盖）。拿 gitleaks 扫源树 ⇒ 每次都在 .env 上报 generic-api-key ⇒
#   闸门**永远无法通过**。而"误报会把闸门淹没、最后被人关掉"正是本脚本第 [3] 段
#   特意避开的事——对凭据闸门同样成立。
#   发布件的定义就是 $DST；扫它才等价于"将要发布的东西里没有凭据"。
# 强度对比（实测）：下面那条固定格式 PAT 抓不到 32 字符随机 key；
#   gitleaks 的熵值规则（generic-api-key）抓得到。所以有 gitleaks 就该用。
# 判据与分级见 dsh-skill-pack-security/secret-scan。
note "[2] 凭据闸门"

# 泄露报告里**绝不出现密钥明文**——只留前 6 字符。
redact() { sed -E 's/(AKIA[0-9A-Z]{4})[0-9A-Z]{12}/\1…REDACTED/g;
                   s/(ghp_[A-Za-z0-9]{4})[A-Za-z0-9]{16,}/\1…REDACTED/g;
                   s/(github_pat_[A-Za-z0-9]{4})[A-Za-z0-9_]{18,}/\1…REDACTED/g;
                   s/(sk-[A-Za-z0-9]{4})[A-Za-z0-9]{16,}/\1…REDACTED/g;
                   s/(xox[bap]-[A-Za-z0-9-]{4})[A-Za-z0-9-]{6,}/\1…REDACTED/g;'; }

PAT='AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,}|sk-[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,}|AZURE_STORAGE_[A-Za-z0-9]+=|-----BEGIN (RSA|OPENSSH|EC|PGP) '

if command -v gitleaks >/dev/null 2>&1; then
  note "  gitleaks: $(gitleaks --version 2>&1 | head -1)"
  if gitleaks detect --source "$DST" --no-git \
        --report-format json --report-path /tmp/.perg-gitleaks.json --redact -v 2>/tmp/.perg-gl.err; then
    ok "  凭据扫描（gitleaks --no-git 快照，扫描导出件）"
  else
    if grep -q "leaks found" /tmp/.perg-gl.err 2>/dev/null; then
      bad "  凭据扫描发现泄露——见 /tmp/.perg-gitleaks.json（含打码值）"
    else
      warn "  gitleaks 退出码非 0 但 stderr 无 'leaks found' ⇒ 可能是配置错误，请人工确认"
      sed -n '1,5p' /tmp/.perg-gl.err | sed 's/^/      /'
    fi
  fi
  rm -f /tmp/.perg-gitleaks.json /tmp/.perg-gl.err
else
  warn "  gitleaks 未安装 → 降级 grep（**只覆盖当前树，覆盖不了已删除的历史**）"
fi

HITS=$(grep -rInE "$PAT" "$DST" 2>/dev/null | redact || true)
if [ -n "$HITS" ]; then
  bad "  凭据模式命中："
  printf '%s\n' "$HITS" | sed 's/^/      /'
else
  ok "  凭据模式扫描（当前树）"
fi
note ""

# ── 3. 闸门：绝对路径与个人身份 ──────────────────────────────────────
note "[3] 绝对路径 / 个人身份闸门"

# python 解释器（第 3 与第 4 段都要用）
PY=""
for c in "$HOME/ai-services/bin/python" python3 python; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done

# Python 感知扫描：剥掉注释与 docstring，只看可执行代码里的字符串。
# 用 grep 分不出"示例路径"与"真路径"，误报会把闸门淹没、最后被人关掉。
if [ -n "$PY" ] && [ -f "$DST/tools/scan_paths.py" ]; then
  if "$PY" "$DST/tools/scan_paths.py" "$DST"; then :; else bad "  机器绝对路径（见上）"; fi
else
  warn "  scan_paths.py 不可用，退回 grep（误报会较多）"
  P_HIT=$(grep -rInE '/mnt/[a-z]/|/home/[A-Za-z]|C:\\\\Users' "$DST" 2>/dev/null \
          | grep -vE '^\S+:\s*#' | grep -vE 'github\.com|docs\.|https?://' || true)
  if [ -n "$P_HIT" ]; then
    bad "  发现机器绝对路径："; printf '%s\n' "$P_HIT" | head -20 | sed 's/^/      /'
  else
    ok "  无机器绝对路径"
  fi
fi

# 真实邮箱：占位符放行（级C 判据 = 文件名或值含 example/xxx/placeholder）
E_HIT=$(grep -rInoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$DST" 2>/dev/null \
        | grep -viE 'example\.(com|org)|@example|xxx|placeholder|you@|@your|noreply' || true)
if [ -n "$E_HIT" ]; then
  bad "  发现疑似真实邮箱（若非占位符，请改为 example.com）："
  printf '%s\n' "$E_HIT" | sed 's/^/      /'
else
  ok "  无真实邮箱（占位符已放行）"
fi

# .env 绝不能出现
if [ -f "$DST/.env" ]; then bad "  导出版里出现 .env！"; else ok "  无 .env"; fi
if [ ! -f "$DST/.env.example" ]; then bad "  缺 .env.example"; fi

# 导出件纯净度：白名单之外的东西一律不该在。
# 这是**结果侧**的结构性检查——与第 [1] 段用什么命令拷贝**无关**，
# 所以它才是真正能兜住"降级路径偷偷放款排除规则"的那道网。
# 实测栽过一次：rsync 未装（rc=127 且被 2>/dev/null 吞掉）→ `|| cp -r` 裸拷
# → __pycache__/*.pyc 混进导出件，而当时所有闸门都放行。
# 注：`.git` 要 prune 掉——用户可能在导出后 git init，重跑时不该被它干扰。
NOISE=$(find "$DST" -name '.git' -prune -o \
          \( -name '__pycache__' -o -name '*.pyc' -o -name '*.bak*' \
             -o -name '*.log' -o -name '.DS_Store' -o -name '.cache' \) \
          -print 2>/dev/null || true)
if [ -n "$NOISE" ]; then
  bad "  导出件里混入噪声（白名单之外，不该出现）："
  printf '%s\n' "$NOISE" | sed 's/^/      /'
else
  ok "  导出件纯净（无 __pycache__ / 字节码 / 备份 / 日志）"
fi
note ""

# ── 4. 语法自检 ──────────────────────────────────────────────────────
note "[4] 导出件语法自检"
if [ -n "$PY" ]; then
  while IFS= read -r f; do
    "$PY" -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f" \
      || bad "语法错误: $f"
  done < <(find "$DST" -name '.git' -prune -o -name '*.py' \
             -not -path '*/__pycache__/*' -print)
  ok "  Python 语法（$PY）"
else
  warn "  无 python，跳过"
fi
note ""

# ── 5. 汇总 ──────────────────────────────────────────────────────────
# 清单与体积都必须排除 .git。
# 用户会在导出后 git init；重跑导出时 .git 会留在原地（这是对的——不能把
# 仓库自己删掉），但它会把复核清单灌满 sample hook，并把体积数字从 248K
# 抬到 448K，让人以为出了问题。
# **复核视图失真 = 闸门等于没有**：清单是"这次到底要发布什么"的唯一人工视图，
# 一旦它不反映真实内容，人就只能盲签。
note "[5] 导出清单"
( cd "$DST" && find . -name '.git' -prune -o \
    -type f -not -path '*/__pycache__/*' -print | sort | sed 's/^/  /' )
note ""
SIZE=$(du -sh --exclude=.git "$DST" 2>/dev/null | cut -f1)
note "  体积 $SIZE（不含 .git）"

note ""
if [ "$fail" -eq 0 ]; then
  printf "%s导出通过。%s\n" "$GRN" "$RST"
  # 提示要与现状相符：仓库已存在时不该再让人 git init（那会让人以为要从头来）。
  # 直白的下一步提示错了，和文档撒谎是一回事。
  if [ -d "$DST/.git" ]; then
    note "下一步：cd $DST && git add . && git status   # 仓库已存在；人工复核后再提交"
  else
    note "下一步：cd $DST && git init -b main && git add . && git status   # 人工复核后再提交"
  fi
else
  printf "%s导出被闸门拒绝——**未通过，不要提交**。%s\n" "$RED" "$RST"
fi
exit "$fail"
