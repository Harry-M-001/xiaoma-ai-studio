"""Render five paired motion-video briefs with direct specs and the configured Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_comparison import matrix, seed_text_provider


CASES = (
    ("01-opener", "片头", "横屏 4 秒。两幕各 2 秒，依次展示「夜航笔记」「城市在入夜后醒来」。用淡入上移。",
     "16:9", [
         {"title": "夜航笔记", "seconds": 2, "motion": "rise"},
         {"title": "城市在入夜后醒来", "seconds": 2, "motion": "rise", "background": "#301B46"},
     ]),
    ("02-data", "数据可视化", "横屏 5 秒，同一幕显示「季度增长」。用三根按比例增长的横条呈现 Q1=24、Q2=42、Q3=60，保留原始数值。",
     "16:9", [
         {"title": "季度增长", "seconds": 5, "layout": "bars",
          "bars": [{"label": "Q1", "value": 24}, {"label": "Q2", "value": 42}, {"label": "Q3", "value": 60}]},
     ]),
    ("03-tutorial", "步骤教程", "方形 6 秒，三幕各 2 秒，做一个咖啡教程。每幕须有独立步骤数字，步骤依次是「称豆 18g」「研磨」「注水 270g」。",
     "1:1", [
         {"title": title, "seconds": 2, "layout": "step", "step": index}
         for index, title in enumerate(("称豆 18g", "研磨", "注水 270g"), 1)
     ]),
    ("04-promo", "竖屏促销", "竖屏 5 秒，同一幕突出显示「周末咖啡」「¥19.90」和行动文案「立即领取」。价格要大于标题，行动文案单独放在按钮形色块上。",
     "9:16", [
         {"title": "周末咖啡", "seconds": 5, "layout": "offer", "price": "¥19.90", "cta": "立即领取",
          "background": "#303019", "accent": "#F4DE72"},
     ]),
    ("05-countdown", "倒计时", "横屏 5 秒。保留标题「直播即将开始」，用同一幕的真实数字倒计时，每秒从 5 到 1，数字居中。无需结束卡。",
     "16:9", [
         {"title": "直播即将开始", "seconds": 5, "layout": "countdown", "accent": "#F4A1E3"},
     ]),
)
SHOWCASE_CASES = (
    ("01-water-park", "水上乐园宣传", "水上乐园宣传。横屏 10 秒，展示游客沿滑道入水和水花，结尾邀请出发。带轻快配乐与水花声。"),
    ("02-gel-pen", "中性笔产品宣传", "中性笔产品宣传。横屏 10 秒，展示笔身特写和笔尖跟随墨迹书写。带轻音乐与书写声。不要虚构商标和产品规格。"),
    ("03-rural-town", "乡村小镇渲染", "乡村小镇渲染。横屏 10 秒，表现晨光、移动薄雾、分层山脊、村舍和溪流。带鸟鸣、水声和舒缓配乐。"),
)


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_agent(case: tuple, output: Path) -> dict:
    slug, label, brief, *_ = case
    session = matrix.http("POST", "/api/agent/sessions", {
        "model_key": matrix.TEXT_MODEL, "thinking": True, "auto_approve": False,
    })["session"]["id"]
    response = matrix.http("POST", f"/api/agent/sessions/{session}/messages", {
        "content": f"用工坊的本地程序化渲染做一条可播放的 MP4，禁止调用生成图或视频模型。{brief}"
                   "如果当前能力只能实现部分要求，请说明差异。渲染后交付最终视频。"
    }, timeout=240)
    tools = [
        {"name": step["toolName"], "status": step["status"], "args": step["toolArgs"],
         "data": step["toolResult"].get("data") if step["toolName"] != "load_skill" else None,
         "error": step["toolResult"].get("error")}
        for step in response["steps"] if step["role"] == "tool"
    ]
    asset = next((t["data"] for t in reversed(tools)
                  if t["name"] in ("render_motion_video", "render_showcase_video") and t["status"] == "ok"), None)
    if asset and asset["url"].startswith("/media/"):
        with urllib.request.urlopen(matrix.APP + asset["url"], timeout=30) as source:
            (output / f"{slug}.mp4").write_bytes(source.read())
    result = {"case": label, "brief": brief, "run": response["run"], "tools": tools}
    save_json(output / f"{slug}.json", result)
    print(f"Agent {label}: {response['run'].get('toolCalls')} 次工具，成片={bool(asset)}", flush=True)
    return result


async def run_direct(output: Path, cases: tuple) -> None:
    from app.services.motion_video import MotionVideo, render

    for slug, label, brief, ratio, scenes in cases:
        spec = MotionVideo.model_validate({"name": label, "ratio": ratio, "scenes": scenes})
        path, metadata = await render(spec)
        shutil.copyfile(path, output / f"{slug}.mp4")
        save_json(output / f"{slug}.json", {
            "case": label, "brief": brief, "spec": spec.model_dump(), "metadata": metadata
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("agent", "direct"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--case", choices=[case[0] for case in (*CASES, *SHOWCASE_CASES)])
    parser.add_argument("--showcase", action="store_true", help="测试三条十秒有声场景")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    matrix.VENV_PY = Path(sys.executable)
    if args.showcase and args.mode != "agent":
        parser.error("有声场景的直接版本已在 dist/complex-motion，使用 agent 模式复测")
    cases = tuple(case for case in (SHOWCASE_CASES if args.showcase else CASES) if args.case in (None, case[0]))
    if not cases:
        parser.error("--case 不属于所选测试组")
    with tempfile.TemporaryDirectory(prefix="xiaoma-motion-comparison-") as work:
        tmp = Path(work)
        if args.mode == "direct":
            os.environ["DATA_DIR"] = str(tmp / "data")
            sys.path.insert(0, str(matrix.BACKEND))
            asyncio.run(run_direct(args.output, cases))
            return
        seed_text_provider(tmp / "data", "deepseek-flash")
        mock, app = matrix.start_processes(tmp)
        try:
            # The fresh database contains only the selected text model.
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(run_agent, case, args.output) for case in cases]
                results = [future.result() for future in futures]
                if not args.case:
                    save_json(args.output / "summary.json", results)
        finally:
            for process in (app, mock):
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
