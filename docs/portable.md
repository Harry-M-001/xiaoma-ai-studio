# Windows 便携包（解压即用）

给「不想装 Python、不想联网装依赖、只想双击打开」的用户用的分发包。

## 产物

| 路径 | 说明 |
| --- | --- |
| `dist/portable/` | 解包目录，可直接双击里面的 `start.bat` 试跑 |
| `dist/xiaoma-ai-studio-v<版本>-win64-portable.zip` | 最终分发包（zip 内有一个同名顶层目录，解压出来就是一个文件夹） |

两者都在 `.gitignore` 里（`dist/`），不会污染仓库。

## 打包

```bat
REM 用装了后端依赖的 3.11 解释器跑（构建侧解释器必须与 embed 包同为 3.11 win_amd64）
backend\venv\Scripts\python.exe backend\tools\make_portable.py
```

参数：

| 参数 | 作用 |
| --- | --- |
| `--out <dir>` | 解包目录，默认 `dist/portable`（相对路径按当前工作目录解析） |
| `--skip-deps` | 复用已装好的 `runtime/`，跳过下载/解压/装依赖，二次打包从 3 分钟降到几秒 |
| `--python-url <url>` | 嵌入式 Python 的 zip 地址；也可指向本地文件路径（内网/离线用） |
| `--keep-zip` | 目标 zip 已存在时保留它、跳过压缩（只想刷新解包目录时用） |
| `--cache-dir <dir>` | 下载缓存目录；默认取环境变量 `XIAOMA_PORTABLE_CACHE`，否则用系统临时目录 |

缓存里存的是官方 embed 包（`python-3.11.9-embed-amd64.zip`，约 10.7 MB）。命中缓存后**完全不联网**，
所以同一台机器上二次构建是离线可用的（首次构建的依赖仍需网络，或用 `--skip-deps` 复用）。

## 包里有什么

```
xiaoma-ai-studio-v<版本>-win64-portable/
├─ start.bat                    启动器（纯 ASCII，见下）
├─ README-PORTABLE.txt          中文说明（面向最终用户，UTF-8）
├─ runtime/                     官方 Windows 嵌入式 Python 3.11.9（含 Lib/site-packages）
├─ backend/                     app / alembic / tools / alembic.ini / requirements.txt
│  └─ data/                     空目录，用户数据落这里（数据库、storage、logs）
├─ frontend/webroot/            前端构建产物（后端直接托管）
└─ README.md / LICENSE / CHANGELOG.md
```

不含：pip、venv、node_modules、前端源码、`.env`、任何密钥。

## 打包脚本做了什么

1. 校验源文件：版本号从 `backend/app/__init__.py` 用正则读；`frontend/webroot/index.html` 不存在
   就直接报错退出（**不跑 npm、不构建前端**）。
2. 下载 → 解压官方 embed 包到 `<out>/runtime/`（已缓存就不联网）。
3. **改写 `runtime/python311._pth`**：保留原内容，末尾追加 `Lib\site-packages` 与 `import site`。
   嵌入式 Python 只要存在 `._pth` 就进入隔离模式（等价 `-I`），`sys.path` 完全由该文件决定，
   不追加这一行，`import fastapi` 必然 `ModuleNotFoundError`。
4. 用**构建侧解释器**的 `pip --target runtime/Lib/site-packages -r backend/requirements.txt`
   （`--prefer-binary --upgrade --no-warn-script-location`）灌依赖。
5. **用运行时解释器真起一次进程**去 `import` fastapi / uvicorn / sqlalchemy / alembic / httpx /
   pydantic / cryptography / aiosqlite（外加 httptools、websockets、watchfiles、yaml、dotenv
   这几个 `uvicorn[standard]` 附带的），缺任何一个必需项就报错终止、不出包。
6. 拷贝应用 + 前端产物（排除 `__pycache__` / `*.pyc`），生成 `start.bat` 与 `README-PORTABLE.txt`，
   压缩成 zip，最后打印目录体积、zip 体积与耗时。

## 如何验证（不动 8787 上的开发服务）

```bat
REM 换到 8788，前端产物、迁移、健康检查都走一遍
dist\portable\runtime\python.exe -m uvicorn app.main:app --app-dir dist\portable\backend --host 127.0.0.1 --port 8788
curl http://127.0.0.1:8788/api/health      REM {"status":"ok"}
curl http://127.0.0.1:8788/api/projects    REM []
```

## 踩过的坑（改这个脚本前先看）

- **`start.bat` 必须纯 ASCII。** cmd.exe 按字节偏移解析批处理；文件里出现多字节字符（中文）
  叠加 `chcp 65001` 会让偏移错位，真实症状是 `'cho.' is not recognized` —— 失败提示本身先崩，
  用户什么都看不到。所以包里的 `start.bat` 只写英文，中文说明全部放 `README-PORTABLE.txt`；
  脚本写盘后会**断言字节全 < 128**，非 ASCII 直接报错终止（`backend/tests/test_env_report.py`
  对仓库根的那份 `start.bat` 有同样的把关）。
- **`._pth` 的两个细节**：必须在末尾补 `Lib\site-packages`（隔离模式不会自动加），并且
  必须以无 BOM 的 ASCII 写回（解释器按字节读这个文件）；`import site` 是给 site.py 一个
  执行机会，处理 site-packages 里的 `.pth`。
- **隔离模式的副作用**：`._pth` 存在时 `PYTHONPATH` / `PYTHONUTF8` / `PYTHONIOENCODING`
  这类环境变量会被忽略（等价 `-I`），所以别指望在 `start.bat` 里用环境变量调编码，
  要调就用 Python 侧 `sys.stdout.reconfigure()`。
- **`pip --target` 生成的入口脚本目录要删掉。** pip 会在 `<target>/bin`（Windows 上实测是 `bin`）
  放 `uvicorn.exe` 之类的启动器，里面嵌的是**构建机解释器**的绝对路径，到用户机器上跑不起来，
  还会把构建机路径泄露进包里。打包脚本会删掉它，只用 `-m uvicorn`。
- **依赖要与 embed 包同版本。** 构建侧解释器是 3.11 时拿到的都是 `cp311-win_amd64` 轮子；
  版本不一致（比如用 3.13 打包）pip 会去取源码包，在没编译器的机器上必然失败。脚本会对非 3.11
  的构建解释器给出警告。
- **中文路径实测可用**：把 zip 解到含中文的目录、并用含中文的路径启动，迁移与服务都正常，
  `sys.path` 里的路径也正确（`runtime/python311.zip`、`runtime`、`runtime\Lib\site-packages`）。

## 面向最终用户的说明

`README-PORTABLE.txt` 随包发出，内容与本文件的后半部分类似：怎么启动、数据在 `backend\data`、
端口 8787 被占用怎么改 `start.bat` 里的 `set "PORT=8787"`、日志在 `backend\data\logs\app.log`、
以及「这是便携包不是安装包，删目录即卸载」。
