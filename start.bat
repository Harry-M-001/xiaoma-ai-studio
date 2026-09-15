@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo   小马AI工坊 - 一键启动（Windows）
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.11+：https://www.python.org/downloads/
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Node.js，请先安装 Node.js 20+：https://nodejs.org/
  pause
  exit /b 1
)

if not exist backend\venv (
  echo [1/4] 正在创建 Python 虚拟环境...
  python -m venv backend\venv
)

echo [2/4] 正在安装后端依赖...
call backend\venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend\requirements.txt

if not exist frontend\node_modules (
  echo [3/4] 正在安装前端依赖（仅首次需要）...
  pushd frontend
  call npm install
  popd
)

echo [4/4] 正在构建前端界面...
pushd frontend
call npm run build
popd

echo.
echo 启动成功后请用浏览器打开： http://127.0.0.1:8787
echo 按 Ctrl+C 可停止服务。
echo.

set FRONTEND_DIST=%~dp0frontend\webroot
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787
