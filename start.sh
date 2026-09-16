#!/usr/bin/env bash
# 小马AI工坊 - 一键启动（macOS / Linux）
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
ENV_CHECK="$ROOT/backend/tools/env_report.py"
VENV_PY="$ROOT/backend/venv/bin/python"
export PYTHONIOENCODING=utf-8

echo "================================================"
echo "  小马AI工坊 - 一键启动"
echo "================================================"
echo

die() {
  echo
  echo "[错误] $1"
  if [ -n "${2:-}" ]; then echo "        $2"; fi
  echo "        看不懂的话，跑下面这条，把整段输出发给作者："
  echo "            python3 \"$ENV_CHECK\" --report"
  exit 1
}

# ---------- 前置：必须有一个能用的 python3 ----------
if ! command -v python3 >/dev/null 2>&1; then
  die "未检测到 python3" "请先安装 Python 3.11+：https://www.python.org/downloads/"
fi
if ! python3 -c "import sys" >/dev/null 2>&1; then
  die "python3 命令存在但无法正常执行" "请确认安装是否完整。"
fi

# ---------- 第 0 步：环境体检 ----------
echo "[0/4] 环境体检..."
if ! python3 "$ENV_CHECK"; then
  die "环境体检没通过，已中止。" "请按上面「→」的提示处理后重试。"
fi
echo

# ---------- 第 1 步：虚拟环境 ----------
if [ ! -x "$VENV_PY" ]; then
  echo "[1/4] 正在创建 Python 虚拟环境..."
  if ! python3 -m venv "$ROOT/backend/venv"; then
    die "创建虚拟环境失败" "常见原因：python3 是精简版/嵌入式版（缺少 venv 模块），或当前目录没有写入权限。"
  fi
else
  echo "[1/4] 虚拟环境已存在，跳过创建。"
fi

# 创建命令返回 0 也不代表真的建成了，所以再确认一次。
if [ ! -x "$VENV_PY" ]; then
  die "虚拟环境看起来没建成功：找不到 $VENV_PY"
fi

# ---------- 第 2 步：后端依赖 ----------
echo "[2/4] 正在安装后端依赖..."
if ! "$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip; then
  die "升级 pip 失败" "多半是网络问题；可手动执行看看报错： \"$VENV_PY\" -m pip install --upgrade pip"
fi
if ! "$VENV_PY" -m pip install --quiet --disable-pip-version-check -r "$ROOT/backend/requirements.txt"; then
  die "安装后端依赖失败" "上面 pip 的报错就是原因；Python 版本太新（依赖没有现成包）时换 3.11 / 3.12 最稳。想看完整报错就去掉 --quiet 手动跑一次。"
fi

# ---------- 第 3 步：前端依赖 ----------
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "[3/4] 正在安装前端依赖（仅首次需要）..."
  if ! command -v npm >/dev/null 2>&1; then
    die "未检测到 npm" "它通常随 Node.js 一起安装，请安装 Node.js 20+：https://nodejs.org/"
  fi
  if ! (cd "$ROOT/frontend" && npm install); then
    die "安装前端依赖失败" "上面 npm 的报错就是原因。国内网络可试：npm config set registry https://registry.npmmirror.com"
  fi
else
  echo "[3/4] 前端依赖已存在，跳过安装。"
fi

# ---------- 第 4 步：构建前端 ----------
echo "[4/4] 正在构建前端界面..."
if ! (cd "$ROOT/frontend" && npm run build); then
  die "构建前端界面失败" "需要 Node.js 20+（版本太低会构建失败）：node --version"
fi

echo
echo "================================================"
echo "  启动成功后请用浏览器打开： http://127.0.0.1:8787"
echo "  按 Ctrl+C 可停止服务。"
echo "================================================"
echo

export FRONTEND_DIST="$ROOT/frontend/webroot"
exec "$VENV_PY" -m uvicorn app.main:app --app-dir "$ROOT/backend" --host 127.0.0.1 --port 8787
