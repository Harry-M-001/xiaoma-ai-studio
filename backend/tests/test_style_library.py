"""C 期：导演风格库 + 分镜图（风格片段拼装 / 分镜表解析 / 合规红线）。

运行：venv/Scripts/python tests/test_style_library.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.registry.canvas_nodes import (
    NODE_SCHEMAS,
    STORYBOARD_IMAGE_KINDS,
    is_batch_image,
    is_storyboard_image,
)
from app.registry.schema_registry import SCHEMA_REGISTRY
from app.services import storyboard_sheet, style_service
from app.services.style_service import StyleCard

# 8 位导演 + 常见英文/拼音写法：这些绝不能出现在给生图/生视频模型的片段里
_DIRECTOR_NAMES = ["斯皮尔伯格", "卡梅隆", "诺兰", "王家卫", "宫崎骏", "昆汀", "维伦纽瓦"]
_DIRECTOR_LATIN = ["spielberg", "cameron", "nolan", "wong", "anderson", "miyazaki", "tarantino", "villeneuve"]

STORYBOARD = """## 镜头清单
镜头1 | 远景 | 固定 | 4s | 交代环境
镜头2 | 特写 | 缓慢推近 | 3s | 角色情绪

### 镜头1 | 远景 | 固定 | 4s
- 画面：暮光闪闪站在永恒花园门口，衣角被风吹动
- 台词：（无）
- 情绪外化：手指无意识绞紧衣角
- 首帧提示词：Twilight Sparkle standing at a crystal garden gate, wind moving her cloak, wide shot
- 约束：无字幕、无 Logo

### 镜头2 | 特写 | 缓慢推近 | 3s
- 画面：云宝低头看着手里的星光罗盘
- 台词：我们走吧。
- 情绪外化：呼吸变浅
- 首帧提示词：Rainbow Dash looking down at a glowing compass, close-up
"""


def _cards() -> list[dict]:
    return list(SCHEMA_REGISTRY["director_styles"].seed)


def _as_card(row: dict) -> StyleCard:
    return StyleCard(
        key=row["key"],
        name=row["name"],
        agent_prompt=row["agent_prompt"],
        image_prompt=row["image_prompt"],
        video_prompt=row["video_prompt"],
        negative_prompt=row["negative_prompt"],
    )


# ---------- 风格库种子 ----------


def test_seed_styles_are_complete():
    rows = _cards()
    assert len(rows) == 12, len(rows)
    keys = [r["key"] for r in rows]
    assert len(set(keys)) == 12, keys
    assert keys[:8] == [
        "spielberg_face", "cameron_spectacle", "nolan_realism", "wong_kar_wai",
        "wes_anderson", "miyazaki_nature", "tarantino_tension", "villeneuve_silence",
    ], keys[:8]
    for row in rows:
        for field in ("name", "agent_prompt", "image_prompt", "video_prompt", "negative_prompt"):
            assert row[field].strip(), f"{row['key']} 缺 {field}"


def test_style_variants_present():
    """导演风格 + 通用风格都要有，风格之间必须可区分。"""
    names = {r["name"] for r in _cards()}
    assert {"国漫", "赛博朋克", "水墨", "纪实手持"} <= names, names
    prompts = [r["image_prompt"] for r in _cards()]
    assert len(set(prompts)) == len(prompts), "生图风格词有重复"


def test_agent_prompt_is_chinese_and_structured():
    """给 LLM 的那份要写清镜头语言/光影/节奏/技法，模型才有得遵循。"""
    for row in _cards():
        text = row["agent_prompt"]
        for section in ("镜头语言", "光影色调"):
            assert section in text, f"{row['key']} 缺「{section}」小节"
        assert "代表技法" in text, f"{row['key']} 缺「代表技法」小节"


def test_model_prompts_never_contain_director_names():
    """合规红线：导演名只准出现在给 LLM 的 agent_prompt 里。"""
    for row in _cards():
        for field in ("image_prompt", "video_prompt", "negative_prompt"):
            text = row[field]
            for name in _DIRECTOR_NAMES:
                assert name not in text, f"{row['key']}.{field} 出现了导演名「{name}」"
            low = text.lower()
            for latin in _DIRECTOR_LATIN:
                assert latin not in low, f"{row['key']}.{field} 出现了导演名「{latin}」"


def test_model_prompts_are_english_only():
    """给模型的是英文片段（中文风格名会污染英文提示词）。"""
    for row in _cards():
        for field in ("image_prompt", "video_prompt"):
            assert not any("\u4e00" <= ch <= "\u9fff" for ch in row[field]), (
                f"{row['key']}.{field} 含中文"
            )


# ---------- 风格片段拼装 ----------


def test_agent_block_carries_name_and_hard_rule():
    card = _as_card(_cards()[0])
    block = style_service.agent_block(card)
    assert "【本片风格：斯皮尔伯格·面孔】" in block
    assert card.agent_prompt in block
    assert "硬性约束" in block


def test_agent_block_is_empty_without_card():
    assert style_service.agent_block(None) == ""
    empty = StyleCard(key="x", name="空", agent_prompt="", image_prompt="a", video_prompt="b", negative_prompt="")
    assert style_service.agent_block(empty) == ""


def test_image_and_video_suffix_include_negative():
    card = _as_card(_cards()[3])  # 王家卫
    img = style_service.image_suffix(card)
    assert card.image_prompt in img
    assert card.negative_prompt in img
    assert ", " in img
    vid = style_service.video_suffix(card)
    assert card.video_prompt in vid and card.negative_prompt in vid
    # 两份片段不能串（生图不该拿到运镜词，反之亦然）
    assert card.video_prompt not in img
    assert card.image_prompt not in vid


def test_suffix_without_negative_still_works():
    card = StyleCard(key="x", name="X", agent_prompt="", image_prompt="warm light", video_prompt="slow pan", negative_prompt="")
    assert style_service.image_suffix(card) == "warm light"
    assert style_service.video_suffix(card) == "slow pan"


def test_append_style_merges_and_dedups():
    card = _as_card(_cards()[9])  # 赛博朋克
    suffix = style_service.image_suffix(card)
    merged = style_service.append_style("a neon street at night", suffix)
    assert merged.startswith("a neon street at night, ")
    assert suffix in merged
    # 重复追加不叠加
    assert style_service.append_style(merged, suffix) == merged
    # 空值处理
    assert style_service.append_style("", suffix) == suffix
    assert style_service.append_style("only base", "") == "only base"
    assert style_service.append_style("", "") == ""
    # 尾逗号不留脏
    assert style_service.append_style("base,", "extra") == "base, extra"


def test_suffix_none_is_noop():
    assert style_service.image_suffix(None) == ""
    assert style_service.video_suffix(None) == ""


# ---------- 分镜表解析 ----------


def test_parse_storyboard_basic():
    shots = storyboard_sheet.parse_storyboard(STORYBOARD)
    assert [s.no for s in shots] == ["1", "2"], shots
    assert shots[0].size == "远景" and shots[0].move == "固定" and shots[0].duration == "4s"
    assert "暮光闪闪" in shots[0].scene
    assert shots[0].first_frame.startswith("Twilight Sparkle")
    assert shots[1].dialogue == "我们走吧。"


def test_parse_skips_shot_list_heading():
    """plan 里的「## 镜头清单」不能被当成一个镜头。"""
    shots = storyboard_sheet.parse_storyboard(STORYBOARD)
    assert all("清单" not in s.heading for s in shots)
    assert len(shots) == 2


def test_parse_falls_back_to_scene_when_no_first_frame():
    text = "### 镜头7 | 中景 | 横移 | 3s\n- 画面：一只白猫走过屋顶\n"
    shots = storyboard_sheet.parse_storyboard(text)
    assert len(shots) == 1
    assert shots[0].image_prompt == "一只白猫走过屋顶"


def test_parse_tolerates_bold_headings_and_field_styles():
    text = "\n".join(
        [
            "**镜头1A | 特写 | 推 | 2s**",
            "* **画面**：云宝皱眉",
            "- **首帧提示词**：Rainbow Dash frowning, close-up",
            "**镜头2 | 全景 | 拉 | 5s**",
            "画面: 空荡的街道",
        ]
    )
    shots = storyboard_sheet.parse_storyboard(text)
    assert [s.no for s in shots] == ["1A", "2"], shots
    assert shots[0].first_frame.startswith("Rainbow Dash")
    assert shots[1].scene == "空荡的街道"
    assert shots[1].image_prompt == "空荡的街道"


def test_parse_without_shot_keyword_falls_back_to_any_heading():
    """模型换了写法（不写「镜头」）时，退一步按标题分块。"""
    text = "### 开场\n- 画面：雨夜街头\n### 转折\n- 画面：主角回头\n"
    shots = storyboard_sheet.parse_storyboard(text)
    assert len(shots) == 2
    assert [s.no for s in shots] == ["1", "2"]


def test_parse_returns_empty_on_plain_prose():
    assert storyboard_sheet.parse_storyboard("这是一段普通文字，没有任何镜头结构。") == []
    assert storyboard_sheet.parse_storyboard("") == []


def test_parse_caps_shot_count():
    lines = []
    for i in range(storyboard_sheet.MAX_SHOTS + 8):
        lines.append(f"### 镜头{i + 1} | 中景 | 固定 | 3s")
        lines.append(f"- 画面：第{i + 1}个画面")
    shots = storyboard_sheet.parse_storyboard("\n".join(lines))
    assert len(shots) == storyboard_sheet.MAX_SHOTS


def test_limit_shots():
    shots = storyboard_sheet.parse_storyboard(STORYBOARD)
    assert len(storyboard_sheet.limit_shots(shots, 1)) == 1
    assert len(storyboard_sheet.limit_shots(shots, 0)) == 2
    assert len(storyboard_sheet.limit_shots(shots, -3)) == 2
    assert len(storyboard_sheet.limit_shots(shots, 99)) == 2


def test_shot_label_and_scan_text():
    shot = storyboard_sheet.parse_storyboard(STORYBOARD)[0]
    assert shot.label.startswith("镜头1（远景")
    # 提及注入扫的是整块镜头内容（中文角色名通常在「画面」里）
    assert "暮光闪闪" in shot.scan_text


def test_parser_matches_storyboard_agent_seed():
    """解析器认的格式，必须和分镜 Agent 种子里写的格式一致（防止两边漂移）。"""
    agents = {row["key"]: row for row in SCHEMA_REGISTRY["agent_prompts"].seed}
    sb = agents["storyboard"]
    template = sb["chunk_prompt"]
    assert "### 镜头" in template, template[:200]
    for field in ("画面", "首帧提示词", "情绪外化"):
        assert field in template, f"分镜模板缺字段「{field}」"
    # 用模板里给出的字段名，解析器必须都认得
    sample = "\n".join(
        [
            "### 镜头1 | 中景 | 缓慢推近 | 4s",
            "- 画面：暮光闪闪抬头",
            "- 台词：（无）",
            "- 情绪外化：手指绞紧衣角",
            "- 首帧提示词：Twilight Sparkle looking up, medium shot",
            "- 约束：无字幕",
        ]
    )
    shots = storyboard_sheet.parse_storyboard(sample)
    assert len(shots) == 1
    assert shots[0].emotion == "手指绞紧衣角"
    assert shots[0].constraints == "无字幕"


# ---------- 节点契约 ----------


def test_storyboard_image_node_contract():
    assert is_storyboard_image("storyboardImage") is True
    assert is_batch_image("storyboardImage") is True
    assert is_batch_image("assetImage") is True
    assert is_batch_image("image") is False
    assert "storyboardImage" in STORYBOARD_IMAGE_KINDS
    schema = NODE_SCHEMAS["storyboardImage"]
    assert schema["category"] == "image"
    for feature in ("styleSelect", "shotLimit", "modelSelect", "imageSize", "sampleCount"):
        assert feature in schema["features"], feature
    assert schema["handles"]["targets"][0]["type"] == "any"
    assert schema["handles"]["sources"][0]["type"] == "image"


def test_style_select_only_on_expected_nodes():
    """风格只挂在会产出内容/画面的节点上（文本素材节点与工作流不需要）。"""
    with_style = {k for k, v in NODE_SCHEMAS.items() if "styleSelect" in v["features"]}
    assert with_style == {"novel", "script", "storyboard", "storyboardImage", "video"}, with_style


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
