"""Compare a direct canvas run and DeepSeek Agent on the same synthetic brief."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import closing
from pathlib import Path

# The Windows portable interpreter uses an isolated ._pth.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_matrix as matrix


def seed_text_provider(data: Path, model: str) -> None:
    """Copy only the selected text provider, with its credential still encrypted."""
    with closing(sqlite3.connect(f"{matrix.MAIN_DB.as_uri()}?mode=ro", uri=True)) as source:
        rows = source.execute(
            "SELECT id,name,kind,base_url,api_key_enc,models_json FROM provider_services "
            "WHERE enabled=1 ORDER BY sort_order,id"
        ).fetchall()
    for provider_id, name, kind, base_url, key, catalog in rows:
        matches = [m for m in json.loads(catalog) if m.get("name") == model and m.get("modality") == "text"]
        if matches and key:
            break
    else:
        raise SystemExit(f"找不到已启用且配置凭据的文本模型 {model}")
    data.mkdir()
    shutil.copyfile(matrix.SECRET, data / "secret.key")
    env = {**os.environ, "DATA_DIR": str(data), "PYTHONIOENCODING": "utf-8"}
    subprocess.run(
        [sys.executable, "-c", "import asyncio, sys; sys.path.insert(0, '.'); "
         "from app.database import init_db; asyncio.run(init_db())"],
        cwd=matrix.BACKEND, env=env, check=True, capture_output=True,
    )
    with closing(sqlite3.connect(data / "xiaoma.db")) as target, target:
        target.execute(
            "INSERT INTO provider_services (id,name,kind,base_url,api_key_enc,enabled,sort_order,models_json)"
            " VALUES (?,?,?,?,?,1,0,?)",
            (provider_id, name, kind, base_url, key, json.dumps(matches)),
        )
    matrix.TEXT_MODEL = f"{provider_id}:{model}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent-only", action="store_true")
    parser.add_argument("--render", action="store_true", help="另外测试 Agent 的本地合成视频技能")
    parser.add_argument("--video-output", type=Path, help="保留本地合成的测试 MP4")
    args = parser.parse_args()
    matrix.VENV_PY = Path(sys.executable)
    result: dict = {"model": args.model}
    with tempfile.TemporaryDirectory(prefix="xiaoma-agent-comparison-") as work:
        tmp = Path(work)
        seed_text_provider(tmp / "data", args.model)
        mock, app = matrix.start_processes(tmp)
        try:
            keys = matrix.ensure_providers()
            img, vid, brief = matrix.IMAGE_MODELS[0], matrix.VIDEO_MODELS[0], matrix.TASKS[0]
            if not args.agent_only:
                result["direct"] = matrix.run_canvas_case(1, brief, img, vid, keys, "对照")
                print("直接画布完成", flush=True)
            project = matrix.http("POST", "/api/agent/projects", {"name": "合成任务对照"})
            result["agent"] = matrix.run_agent_case(
                1, brief, img, vid, keys, project["id"], "对照", thinking=True,
            )
            print("DeepSeek 画布测试已返回", flush=True)
            if args.render:
                session = matrix.http("POST", "/api/agent/sessions", {
                    "model_key": matrix.TEXT_MODEL, "thinking": True,
                })["session"]["id"]
                out = matrix.http("POST", f"/api/agent/sessions/{session}/messages", {
                    "content": "请用本地程序化渲染实际生成一条横屏 MP4。两幕，每幕 2 秒。"
                               "标题依次为「雨夜便利店」「橘猫的夜班」。"
                               "不要调用图片或视频模型。完成后给出产物。"
                }, timeout=300)
                result["render"] = {
                    "run": out.get("run"),
                    "tools": [{"name": s["toolName"], "status": s["status"], "result": s["toolResult"]}
                              for s in out["steps"] if s["role"] == "tool"],
                }
                rendered = next((s["result"]["data"] for s in result["render"]["tools"]
                                 if s["name"] == "render_motion_video" and s["status"] == "ok"), None)
                if args.video_output and rendered and rendered["url"].startswith("/media/"):
                    with urllib.request.urlopen(matrix.APP + rendered["url"], timeout=30) as source:
                        args.video_output.write_bytes(source.read())
                print("DeepSeek 本地渲染测试已返回", flush=True)
            result["mock"] = [
                json.loads(line) for line in (tmp / "mock_requests.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            for process in (app, mock):
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
