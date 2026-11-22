"""9 组模型组合 × 9 个任务：走本地模拟服务把画布链路真跑一遍。

用法（在仓库根执行）：
    backend\\venv\\Scripts\\python.exe backend\\tools\\mock_matrix.py --mode canvas
    backend\\venv\\Scripts\\python.exe backend\\tools\\mock_matrix.py --mode both

它做四件事：
1. 把开发库复制到临时目录（含 secret.key），另起一个 8791 端口实例，**不碰你的 8787**；
2. 起本地模拟模型服务（8790）；
3. 对 3 个图片模型 × 3 个视频模型的 9 组组合，各建一张「图片 → 视频（首尾帧）」画布并运行整图；
4. 可选：把同样的 9 句需求交给工坊里的 Agent，记录它的步骤数与工具调用。

全程只打本地模拟服务，不产生任何真实模型费用（Agent 的文本模型除外）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
TOOLS = BACKEND / "tools"
VENV_PY = BACKEND / "venv" / "Scripts" / "python.exe"
MAIN_DB = BACKEND / "data" / "xiaoma.db"
SECRET = BACKEND / "data" / "secret.key"

MOCK_PORT = 8790
APP_PORT = 8791
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"

IMAGE_MODELS = [
    ("doubao-seedream-5-5-261028", "Seedream 5.5"),
    ("doubao-seedream-5-0-pro-260628", "Seedream 5.0 Pro"),
    ("qwen-image-2.1", "Qwen-Image 2.1"),
]
VIDEO_MODELS = [
    ("doubao-seedance-2-0-260128", "Seedance 2.0"),
    ("doubao-seedance-2-5-260628", "Seedance 2.5"),
    ("wan3.0-video", "Wan 3.0"),
]

TASKS = [
    {
        "name": "雨夜便利店",
        "image": "雨夜的街角便利店，玻璃门内暖黄灯光，门口蹲着一只湿漉漉的橘猫，水洼里映出红蓝霓虹",
        "video": "镜头从水洼倒影缓慢抬起，推向橘猫，雨丝斜落，霓虹在水面轻轻晃动",
    },
    {
        "name": "清晨面馆",
        "image": "清晨的老面馆，木桌上摆着一碗手擀面，热气升起，窗外天刚亮，厨房灯还亮着",
        "video": "镜头沿桌面缓慢前推，热气在光里散开，窗外光线逐渐变亮",
    },
    {
        "name": "沙漠公路",
        "image": "正午的沙漠公路，一辆旧皮卡停在路边，远处热浪扭曲，天空没有云",
        "video": "镜头低位向前滑行，热浪在车身周围抖动，皮卡逐渐占满画面",
    },
    {
        "name": "老式理发店",
        "image": "老式理发店的旋转灯柱特写，红白蓝条纹，玻璃里有细小气泡，背景是虚化的街景",
        "video": "镜头绕着灯柱缓慢旋转，条纹持续转动，背景车灯拉成光带",
    },
    {
        "name": "山间木屋",
        "image": "山间木屋的窗边，一盏旧台灯亮着，窗外是深蓝色暮色与松林轮廓，玻璃上有雾气",
        "video": "镜头从窗外缓慢推近窗内台灯，雾气被灯光照亮，松林在风中轻轻摆动",
    },
    {
        "name": "末班地铁",
        "image": "深夜地铁末班车的空车厢，冷白灯管，橙色座椅，车窗外是模糊的隧道灯",
        "video": "镜头沿车厢过道缓慢前进，灯管轻微闪烁，窗外灯光连续掠过",
    },
    {
        "name": "夏夜天台",
        "image": "夏夜的天台，一只旧风筝挂在栏杆上，远处城市灯火，天空是深紫到墨蓝的渐变",
        "video": "镜头从风筝缓慢拉远，城市灯火逐渐铺开，风吹动风筝尾带",
    },
    {
        "name": "旧书摊相册",
        "image": "旧书摊上摊开的一本相册，泛黄照片，旁边压着一副老花镜，午后阳光斜照",
        "video": "镜头俯拍缓慢下移，阳光在相册页面上移动，灰尘在光柱里漂浮",
    },
    {
        "name": "雪后寺庙",
        "image": "雪后的寺庙屋檐，一排冰凌垂下，红墙与白雪对比，天空是淡淡的青色",
        "video": "镜头沿着屋檐缓慢横移，冰凌尖端滴水，雪粒被风轻轻吹落",
    },
]

TEXT_MODEL = "2:deepseek-flash"


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------


def http(method: str, path: str, body: Any = None, *, timeout: int = 600) -> Any:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{APP}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail[:500]}") from e


def wait_http(url: str, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise RuntimeError(f"等待服务超时：{url}")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


# --------------------------------------------------------------------------
# 环境
# --------------------------------------------------------------------------


def prepare_data_dir() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="xiaoma-mock-matrix-"))
    dst = tmp / "data"
    dst.mkdir()
    src = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    out = sqlite3.connect(dst / "xiaoma.db")
    with out:
        src.backup(out)
    src.close()
    out.close()
    if SECRET.is_file():
        shutil.copy2(SECRET, dst / "secret.key")
    return tmp


def start_processes(tmp: Path) -> tuple[subprocess.Popen, subprocess.Popen]:
    if not port_free(MOCK_PORT):
        raise SystemExit(f"端口 {MOCK_PORT} 已被占用，先停掉旧的模拟服务")
    if not port_free(APP_PORT):
        raise SystemExit(f"端口 {APP_PORT} 已被占用，先停掉旧的测试实例")

    mock_log = tmp / "mock_requests.jsonl"
    mock_out = open(tmp / "mock-server.log", "w", encoding="utf-8")
    mock = subprocess.Popen(
        [str(VENV_PY), str(TOOLS / "mock_model_server.py"),
         "--port", str(MOCK_PORT), "--log", str(mock_log)],
        cwd=str(BACKEND),
        stdout=mock_out,
        stderr=subprocess.STDOUT,
    )
    env = {
        **os.environ,
        "DATA_DIR": str(tmp / "data"),
        "FRONTEND_DIST": str(ROOT / "frontend" / "webroot"),
        "PYTHONIOENCODING": "utf-8",
    }
    app_out = open(tmp / "app.log", "w", encoding="utf-8")
    app = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(APP_PORT)],
        cwd=str(BACKEND),
        env=env,
        stdout=app_out,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_http(f"{MOCK_BASE}/healthz")
        wait_http(f"{APP}/api/health")
    except Exception:
        mock.terminate()
        app.terminate()
        raise
    return mock, app


def ensure_providers() -> tuple[dict[str, str], dict[str, str]]:
    """把两个模拟服务接进测试实例，返回 {模型名: model_key}。"""
    existing = http("GET", "/api/providers")
    by_base = {p["base_url"].rstrip("/"): p for p in existing}

    specs = [
        {
            "name": "Mock Ark（本地模拟）",
            "kind": "ark",
            "base_url": f"{MOCK_BASE}/ark/api/v3",
            "api_key": "mock-ark-key",
            "sort_order": 90,
            "models": [
                {"name": "doubao-seedream-5-5-261028", "modality": "image", "label": "Seedream 5.5（模拟）"},
                {"name": "doubao-seedream-5-0-pro-260628", "modality": "image", "label": "Seedream 5.0 Pro（模拟）"},
                {"name": "doubao-seedance-2-0-260128", "modality": "video", "label": "Seedance 2.0（模拟）"},
                {"name": "doubao-seedance-2-5-260628", "modality": "video", "label": "Seedance 2.5（模拟）"},
            ],
        },
        {
            "name": "Mock 百炼（本地模拟）",
            "kind": "dashscope",
            "base_url": f"{MOCK_BASE}/bailian/api/v1",
            "api_key": "mock-bailian-key",
            "sort_order": 91,
            "models": [
                {"name": "qwen-image-2.1", "modality": "image", "label": "Qwen-Image 2.1（模拟）"},
                {"name": "wan3.0-video", "modality": "video", "label": "Wan 3.0（模拟）"},
            ],
        },
    ]
    for spec in specs:
        if spec["base_url"].rstrip("/") not in by_base:
            http("POST", "/api/providers", spec)

    # 关掉真实图片/视频服务：同名模型挂在两个服务上时，Agent 可能「贴心地」
    # 把 9:xxx 换成 1:xxx，于是跑到真模型上去（测试里直接 404，线上就是真花钱）。
    # 矩阵只该用模拟服务，所以这里把带 image/video 的非 Mock 服务停用。
    for p in http("GET", "/api/providers"):
        if p["name"].startswith("Mock ") or not p["enabled"]:
            continue
        if any(m["modality"] in ("image", "video") for m in p.get("models") or []):
            http("PUT", f"/api/providers/{p['id']}", {
                "name": p["name"],
                "kind": p["kind"],
                "base_url": p["base_url"],
                "enabled": False,
                "sort_order": p.get("sort_order") or 0,
                "models": p.get("models") or [],
            })

    options = http("GET", "/api/providers/models")
    keys: dict[str, str] = {}
    for o in options:
        if o["service_name"].startswith("Mock "):
            keys[o["name"]] = o["key"]
    missing = [n for n, _ in IMAGE_MODELS + VIDEO_MODELS if n not in keys]
    if missing:
        raise SystemExit(f"模拟服务里缺少模型：{missing}")
    return keys


# --------------------------------------------------------------------------
# 画布
# --------------------------------------------------------------------------


def canvas_doc(img_key: str, vid_key: str, task: dict) -> dict:
    return {
        "schemaVersion": 1,
        "nodes": [
            {
                "id": "img",
                "type": "image",
                "position": {"x": 80, "y": 140},
                "data": {
                    "prompt": task["image"],
                    "size": "1280x720",
                    "n": 1,
                    "model_key": img_key,
                    "nodeType": "image",
                },
            },
            {
                "id": "vid",
                "type": "video",
                "position": {"x": 480, "y": 140},
                "data": {
                    "prompt": task["video"],
                    "nodeType": "video",
                    "mode": "first_last",
                    "model_key": vid_key,
                    "duration": 4,
                    "ratio": "16:9",
                    "resolution": "720p",
                },
            },
        ],
        "edges": [
            {"id": "e1", "source": "img", "sourceHandle": "out-image",
             "target": "vid", "targetHandle": "in-any"}
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def run_canvas_case(idx: int, task: dict, img: tuple[str, str], vid: tuple[str, str],
                    keys: dict[str, str], run_tag: str) -> dict:
    img_key, vid_key = keys[img[0]], keys[vid[0]]
    project = http("POST", "/api/projects", {
        "name": f"矩阵 {run_tag} #{idx:02d} {task['name']}",
        "description": f"{img[1]} → {vid[1]}（本地模拟）",
    })
    pid = project["id"]
    http("PUT", f"/api/canvas/{pid}", canvas_doc(img_key, vid_key, task))
    started = time.time()
    run = http("POST", f"/api/canvas/{pid}/run", {})
    run_id = run["runId"]

    summary: dict[str, Any] = {}
    deadline = started + 240
    while time.time() < deadline:
        time.sleep(2)
        summary = http("GET", f"/api/canvas/{pid}/run-summary?run_id={run_id}")
        if summary.get("finished"):
            break
    status = http("GET", f"/api/canvas/{pid}/status")
    elapsed = round(time.time() - started, 1)
    return {
        "index": idx,
        "task": task["name"],
        "image_model": img[1],
        "video_model": vid[1],
        "project_id": pid,
        "run_id": run_id,
        "finished": bool(summary.get("finished")),
        "calls": summary.get("calls"),
        "failed": summary.get("failed"),
        "products": summary.get("products"),
        "elapsed": elapsed,
        "nodes": {
            nid: {
                "status": st.get("status"),
                "assets": len(st.get("assets") or []),
                "assetUrl": st.get("assetUrl"),
                "error": st.get("error"),
            }
            for nid, st in (status.get("nodes") or {}).items()
        },
        "raw_status": status,
    }


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


def run_agent_case(idx: int, task: dict, img: tuple[str, str], vid: tuple[str, str],
                   keys: dict[str, str], agent_project_id: int, run_tag: str) -> dict:
    project = http("POST", "/api/projects", {
        "name": f"Agent 矩阵 {run_tag} #{idx:02d} {task['name']}",
        "description": "Agent 对照运行（本地模拟模型）",
    })
    session = http("POST", "/api/agent/sessions", {
        "model_key": TEXT_MODEL,
        "project_id": project["id"],
        "agent_project_id": agent_project_id,
        "auto_approve": True,
        "thinking": False,
    })
    sid = session["session"]["id"]
    instruction = (
        f"在这张画布上完成一个 4 秒的短视频：{task['name']}。\n"
        f"1) 建一个「图片生成」节点，提示词：{task['image']}；图片模型必须用 {keys[img[0]]}（{img[1]}）。\n"
        f"2) 建一个「视频生成」节点，提示词：{task['video']}；模式选首尾帧；视频模型必须用 {keys[vid[0]]}（{vid[1]}），"
        "时长 4 秒、16:9、720p。\n"
        "3) 把图片节点的输出连到视频节点，然后运行整图，直到两段产物都出来。\n"
        "不要使用其它模型，不要改模型。完成后简要说明产物。"
    )
    started = time.time()
    try:
        payload = http("POST", f"/api/agent/sessions/{sid}/messages",
                       {"content": instruction}, timeout=900)
        error = ""
    except Exception as e:  # noqa: BLE001
        payload, error = None, str(e)
    elapsed = round(time.time() - started, 1)
    steps = (payload or {}).get("steps") or []
    tool_steps = [s for s in steps if s.get("role") == "tool"]
    canvas_id = (payload or {}).get("session", {}).get("canvasId")
    try:
        status = http("GET", f"/api/canvas/{canvas_id}/status") if canvas_id else {"nodes": {}}
    except Exception:  # noqa: BLE001
        status = {"nodes": {}}
    return {
        "index": idx,
        "task": task["name"],
        "image_model": img[1],
        "video_model": vid[1],
        "session_id": sid,
        "canvas_id": canvas_id,
        "elapsed": elapsed,
        "error": error,
        "status": (payload or {}).get("run", {}).get("status"),
        "reply": next(
            (s.get("content") for s in reversed(steps)
             if s.get("role") == "assistant" and s.get("content")),
            "",
        ),
        "steps": len(steps),
        "tool_steps": len(tool_steps),
        "tools": [s.get("toolName") for s in tool_steps],
        "failed_tools": [s.get("toolName") for s in tool_steps if s.get("status") == "failed"],
        "details": [
            {
                "seq": s.get("seq"),
                "role": s.get("role"),
                "tool": s.get("toolName"),
                "status": s.get("status"),
                "args": s.get("toolArgs"),
                "result": str(s.get("toolResult") or "")[:400],
                "content": s.get("content"),
            }
            for s in steps
        ],
        "nodes": {
            nid: {"status": st.get("status"), "assets": len(st.get("assets") or [])}
            for nid, st in (status.get("nodes") or {}).items()
        },
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["canvas", "agent", "both"], default="canvas")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录（默认跑完删除）")
    parser.add_argument("--report", default="", help="把结果写到这个 Markdown 路径")
    args = parser.parse_args()

    if not VENV_PY.is_file():
        raise SystemExit(f"找不到虚拟环境解释器：{VENV_PY}")
    if not MAIN_DB.is_file():
        raise SystemExit(f"找不到开发库：{MAIN_DB}")

    run_tag = time.strftime("%m%d-%H%M%S")
    tmp = prepare_data_dir()
    print(f"临时数据目录：{tmp}")
    mock = app = None
    results: dict[str, Any] = {"tag": run_tag, "canvas": [], "agent": [], "mock_log": []}
    try:
        mock, app = start_processes(tmp)
        keys = ensure_providers()
        combos = [(img, vid) for img in IMAGE_MODELS for vid in VIDEO_MODELS]

        agent_project_id = None
        if args.mode in ("agent", "both"):
            ap = http("POST", "/api/agent/projects", {
                "name": f"模型矩阵对照 {run_tag}",
                "description": "9 个任务 × 9 组模型，模拟服务",
            })
            agent_project_id = ap["id"]

        for idx, ((img, vid), task) in enumerate(zip(combos, TASKS), start=1):
            print(f"[{idx}/9] {task['name']} · {img[1]} → {vid[1]}")
            if args.mode in ("canvas", "both"):
                try:
                    row = run_canvas_case(idx, task, img, vid, keys, run_tag)
                except Exception as e:  # noqa: BLE001
                    row = {"index": idx, "task": task["name"], "image_model": img[1],
                           "video_model": vid[1], "error": str(e), "finished": False,
                           "failed": 1, "calls": 0, "products": {}, "elapsed": 0, "nodes": {}}
                results["canvas"].append(row)
                print(f"      画布：finished={row['finished']} failed={row['failed']} "
                      f"products={row['products']} {row['elapsed']}s")
            if args.mode in ("agent", "both"):
                assert agent_project_id is not None
                try:
                    row = run_agent_case(idx, task, img, vid, keys, agent_project_id, run_tag)
                except Exception as e:  # noqa: BLE001
                    row = {"index": idx, "task": task["name"], "image_model": img[1],
                           "video_model": vid[1], "error": str(e), "steps": 0,
                           "tool_steps": 0, "tools": [], "failed_tools": [],
                           "elapsed": 0, "status": "error", "nodes": {}}
                results["agent"].append(row)
                print(f"      Agent：steps={row['steps']} tools={row['tool_steps']} "
                      f"status={row['status']} {row['elapsed']}s {row['error'][:120]}")

        # 模拟服务日志：确认没出现鉴权 / 请求头失败
        try:
            with urllib.request.urlopen(f"{MOCK_BASE}/__requests", timeout=30) as resp:
                results["mock_log"] = json.load(resp).get("items") or []
        except Exception:  # noqa: BLE001
            pass
        bad = [x for x in results["mock_log"] if x.get("event", "").endswith("failed")
               or x.get("event") in ("bad_content_type", "missing_async_header")]
        print(f"模拟服务请求：{len(results['mock_log'])} 条，异常 {len(bad)} 条")

        out = Path(tempfile.gettempdir()) / f"xiaoma-mock-matrix-{run_tag}.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入：{out}")

        if args.report:
            write_report(Path(args.report), results)
            print(f"报告已写入：{args.report}")

        ok = sum(1 for r in results["canvas"] if r["finished"] and not r["failed"])
        if results["canvas"]:
            print(f"画布通过：{ok}/{len(results['canvas'])}")
        if results["agent"]:
            ok = sum(1 for r in results["agent"] if not r["error"] and r["status"] != "error")
            print(f"Agent 通过：{ok}/{len(results['agent'])}")
    finally:
        for proc in (app, mock):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"已保留临时目录：{tmp}")


def write_report(path: Path, results: dict[str, Any]) -> None:
    lines = [
        "# 本地模拟模型矩阵报告",
        "",
        f"- 运行时间：{results['tag']}",
        f"- 模型：Seedream 5.5 / 5.0 Pro · Qwen-Image 2.1 × Seedance 2.0 / 2.5 · Wan 3.0",
        "",
        "## 画布链路（图片 → 首尾帧视频）",
        "",
        "| # | 任务 | 图片模型 | 视频模型 | 调用 | 失败 | 产物 | 耗时 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results["canvas"]:
        p = r.get("products") or {}
        made = f"{p.get('images', 0)} 图 / {p.get('videos', 0)} 视频"
        ok = "通过" if r["finished"] and not r["failed"] else "失败"
        lines.append(
            f"| {r['index']} | {r['task']} | {r['image_model']} | {r['video_model']} | "
            f"{r['calls']} | {r['failed']} | {made} | {r['elapsed']}s | {ok} |"
        )
    if results["agent"]:
        lines += [
            "",
            "## Agent 对照",
            "",
            "| # | 任务 | 步骤 | 工具调用 | 耗时 | 结果 | 失败工具 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in results["agent"]:
            ok = "通过" if not r["error"] and r["status"] != "error" else "失败"
            lines.append(
                f"| {r['index']} | {r['task']} | {r['steps']} | {r['tool_steps']} | "
                f"{r['elapsed']}s | {ok} | {', '.join(r['failed_tools']) or '-'} |"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
