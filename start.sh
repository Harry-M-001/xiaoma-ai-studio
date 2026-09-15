#!/usr/bin/env bash
# 小马AI工坊 - 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  小马AI工坊 - 一键启动"
echo "================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未检测到 python3，请先安装 Python 3.11+"
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "[错误] 未检测到 Node.js，请先安装 Node.js 20+"
  exit 1
fi

if [ ! -d backend/venv ]; then
  echo "[1/4] 创建 Python 虚拟环境..."
  python3 -m venv backend/venv
fi

# shellcheck disable=SC1091
source backend/venv/bin/activate
echo "[2/4] 安装后端依赖..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend/requirements.txt

if [ ! -d frontend/node_modules ]; then
  echo "[3/4] 安装前端依赖（仅首次需要）..."
  (cd frontend && npm install)
fi

echo "[4/4] 构建前端界面..."
(cd frontend && npm run build)

export FRONTEND_DIST="$(pwd)/frontend/webroot"
echo
echo "启动成功后请用浏览器打开： http://127.0.0.1:8787"
echo
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787
