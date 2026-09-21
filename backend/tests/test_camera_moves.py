# -*- coding: utf-8 -*-
"""运镜词表（#37）：一份表，四处用。

这一项的存在理由是**同一个概念以前散在四处各写一份词**：

| 位置 | 以前 | 后果 |
| --- | --- | --- |
| 分镜提示词 | 只说「禁止写组合」，**从没给过词表** | 模型自造「跟拍/缓推/推轨」 |
| `animatic` | 自己的 `_MOVE_RULES` | 认不出 → 静默套推近，样片动的不是分镜写的意思 |
| `storyboard_lint` | 自己的 `_MOVE_HINTS` + 手写建议文案 | 建议里推的词表外的词，用户照着改还是错 |
| 体检建议文案 | 手抄一份中文名 | 与表脱节 |

所以这个文件测的重点不是「函数算得对不对」，而是**四处的口径再也不会分叉**。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import animatic  # noqa: E402
from app.services import camera_moves as cm  # noqa: E402
from app.services import storyboard_lint as lint  # noqa: E402
from app.services.storyboard_sheet import Shot  # noqa: E402


def S(no: str, size: str = "", move: str = "", duration: str = "",
      scene: str = "", first_frame: str = "") -> Shot:
    return Shot(no=no, heading=f"镜头{no}", size=size, move=move, duration=duration,
                scene=scene, first_frame=first_frame, emotion="", dialogue="",
                scene_title="")


def test_table_passes_its_own_self_check():
    """表本身要自洽：key 不重、别名不冲突、分组都存在、样片映射合法。

    别名冲突是最难查的一类「今天对明天错」——两条运镜共用一个词时，
    谁赢取决于遍历顺序。`assert_consistent` 就是为了把这种错误在导入期拦下来。
    """
    cm.assert_consistent()


def test_table_has_not_shrunk_by_accident():
    """表被误删/误改会静默降低识别率，所以把规模钉住。

    数量本身不是目的，但**无故变小一定是错的**——每一条运镜都是照着
    「用户真的会这么写」收进来的。
    """
    moves = cm.CAMERA_MOVES
    assert len(moves) >= 30, f"运镜只剩 {len(moves)} 条"
    groups = {str(m["group"]) for m in moves}
    assert groups == {g for g, _ in cm.GROUPS}, f"有分组空着或用了表外的分组：{groups}"
    aliases = sum(len(m["aliases"]) for m in moves)  # type: ignore[arg-type]
    assert aliases >= 150, f"别名只剩 {aliases} 个"
    for m in moves:
        assert m["label"] and m["en"] and m["hint"], f"{m['key']} 缺中文名/英文名/说明"


def test_bare_lock_still_counts_as_static():
    """「lock」要认成固定机位（移植词表时弄丢过，这条是回归闸）。

    `animatic` 旧的 `STATIC_WORDS` 里有单独的 `lock`，新表一开始只留了
    `lock off` / `locked`，于是「lock」掉进「不认识」——而认不出的后果是
    静默套一个推近，**样片会动而分镜写的是不动**。旧口径里认识的字，
    新表必须继续认识，这条就是防这一类无声收窄。
    """
    for raw in ("lock", "LOCK", "Lock", "lock off", "locked", "static"):
        assert cm.is_static(raw), f"「{raw}」没当成固定"
    for raw in ("固定", "固定机位", "静止", "静止镜头", "不动", "机位不变", "定格"):
        assert cm.is_static(raw), f"「{raw}」没当成固定"
    for raw in ("缓慢推近", "横移", "跟拍", "环绕", "", "   ", "随便写"):
        assert not cm.is_static(raw), f"「{raw}」误判成固定"


def test_a_longer_alias_always_beats_a_single_char_one():
    """单字别名只准兜底，不许跟多字别名抢——这条是从一次真判错里来的。

    「飘移推近」曾被认成**右移**：单字别名「移」在第 1 个字上命中，而真正表示
    运镜的「推近」在第 3 个字上，位置优先反而判错。分两层（先多字、没有再单字）
    之后才对。这里用「批量前缀」的方式把整张表都过一遍，而不是只测那一个例子——
    例子只能证明那一个字，规则要能覆盖 183 个别名。
    """
    for m in cm.CAMERA_MOVES:
        for alias in m["aliases"]:  # type: ignore[arg-type]
            word = str(alias)
            if len(word) < 2:
                continue  # 单字别名本来就是兜底用的，前置它没有意义
            for prefix in ("移", "推", "摇"):
                got = cm.move_key(prefix + word)
                assert got == m["key"], (
                    f"「{prefix}{word}」认成了 {got or '（认不出）'}，"
                    f"应该是 {m['key']}——单字别名抢在了多字别名前面"
                )


def test_at_the_same_position_the_longer_alias_wins():
    """同一位置命中多个别名时取更长的那个（「变焦推近」是推近，不是变焦推）。"""
    assert cm.move_key("变焦推近") == "zoom_in"
    assert cm.move_key("变焦推") == "dolly_zoom"
    assert cm.move_key("向左移") == "truck_left"
    assert cm.move_key("向左") == "pan_left"


def test_drifting_words_land_on_the_intended_move():
    """模型自造过的那些说法，现在要落到对的意思上（落到哪个是次要的，认得出才要紧）。"""
    for raw, want in {
        "飘移推近": "push",
        "缓推": "push",
        "推近": "push",
        "跟拍": "track_follow",
        "推轨": "dolly",
        "左移一点点": "truck_left",
        "缓缓拉远": "pull",
        "镜头快速甩过": "whip_pan",
    }.items():
        assert cm.move_key(raw) == want, f"「{raw}」认成了 {cm.move_key(raw) or '（认不出）'}"


def test_a_camera_position_is_not_a_movement():
    """「主观镜头」这类是机位/镜头类型，要单独指出来——用户要改的是**哪一栏**。

    如果只说「不在词表里」，用户会去词表里挑一个最像的，比如把「主观镜头」改成
    「跟拍」——问题其实在这句话该待在画面描述里，运镜栏本来该另写一个运动方式。
    """
    for raw in ("主观镜头", "过肩镜头", "建立镜头", "空镜", "正反打", "POV"):
        assert cm.looks_like_not_a_move(raw), f"「{raw}」没被认成机位/镜头类型"
        assert cm.match(raw) is None, f"「{raw}」不该被当成运镜"
    for raw in ("缓慢推近", "固定", "跟拍", ""):
        assert not cm.looks_like_not_a_move(raw), f"「{raw}」被误判成机位"


def test_closest_only_ever_suggests_words_from_the_table():
    """建议里出现的词必须在表内。

    体检的建议是给用户**照着改**的，推一个表外的词等于把人又推回坑里。
    """
    for raw in ("镜头缓缓飘过", "随便写点什么", "推近拉远", "移焦跟拍", "zoom around"):
        got = cm.closest(raw)
        assert got == "" or got in cm.labels(), f"「{raw}」建议了表外的「{got}」"


def test_prompt_vocabulary_covers_every_move_and_stays_compact():
    """词表要进提示词，所以：全覆盖，且别把提示词撑爆。"""
    vocab = cm.prompt_vocabulary()
    for label in cm.labels():
        assert vocab.count(label) >= 1, f"提示词词表里没有「{label}」"
    for _, glabel in cm.GROUPS:
        assert f"- {glabel}：" in vocab, f"词表缺分组「{glabel}」"
    assert len(vocab) < 1500, f"词表太长了（{len(vocab)} 字），提示词会被挤掉"
    assert vocab.count("\n") + 1 <= 12, "词表行数太多"
    # 不加英文那份也要能用（英文名只是给模型对齐概念用的）
    assert "static shot" in vocab and "static shot" not in cm.prompt_vocabulary(with_en=False)


def test_the_storyboard_prompt_gets_the_table_from_code_not_the_seed():
    """词表**由代码附加**在系统提示词末尾，不能只写在提示词种子数据里。

    为什么必须这样（这一节最容易做错的地方）：`config_center.ensure_seed` **从不覆盖
    已存在的行**，所以任何只写在种子里的规则，对所有**升级上来的**用户都不存在——
    而他们正是多数。实测过：升级后的库里那份分镜提示词仍是旧文本，没有词表。
    词表又是与解析/体检共用的契约，模型看不到它就会自造说法，而样片认不出时会静默按
    默认动效走：分镜写的和看到的对不上，界面上还看不出来。
    """
    import dataclasses

    from app.services import doc_service

    block = doc_service.hard_rules_for("storyboard")
    for label in cm.labels():
        assert label in block, f"附加的硬规则里没有「{label}」"
    assert "禁止自造说法" in block
    assert doc_service.hard_rules_for("novel") == "", "别的岗位不该拿到运镜词表"

    spec = doc_service.AgentSpec(
        key="storyboard", label="分镜", system_prompt="原有正文（用户改过的）",
        user_template="{content}", plan_prompt="", chunk_prompt="", chunk_param="",
        model_key="", temperature=0.8, max_tokens=2000,
    )
    merged = doc_service.apply_hard_rules(spec)
    assert merged.system_prompt.startswith("原有正文（用户改过的）"), "不该覆盖用户改过的正文"
    assert "【运镜词表】" in merged.system_prompt
    # 排在最后 = 优先于前面用户改过的正文（越靠后的指令越被遵守）
    assert merged.system_prompt.rstrip().endswith(cm.prompt_vocabulary())
    other = dataclasses.replace(spec, key="novel")
    assert doc_service.apply_hard_rules(other) is other, "别的岗位该原样返回"

    # 种子数据里不许再抄一份：抄了就是两份，而库里那份永远不会更新
    from app.registry import schema_registry

    board = next(
        s for s in schema_registry.get("agent_prompts").seed if s.get("key") == "storyboard"
    )
    seed_prompt = str(board.get("system_prompt") or "")
    assert "慢推" not in seed_prompt, "词表又被抄进种子数据了——升级上来的用户看不到它"
    assert "末尾那份【运镜词表】" in seed_prompt, "提示词里没说清词表从哪来"
    # 逐镜提示词的输出格式也要跟着改，不然模型会照抄旧的占位符
    assert "从词表里选 1 个" in str(board.get("chunk_prompt") or "")


def test_animatic_and_lint_never_disagree_about_static():
    """「是不是固定机位」只有一个答案。

    两边各写一份词表迟早漂移，然后出现「体检说这不是固定镜头、样片却一动不动」——
    同一份分镜表给出两个说法，用户只会谁都不信。这里把表里所有写法都过一遍。
    """
    words: list[str] = []
    for m in cm.CAMERA_MOVES:
        words.append(str(m["label"]))
        words.extend(str(a) for a in m["aliases"])  # type: ignore[arg-type]
    words += ["", "   ", "随便写点什么", "主观镜头", "镜头缓缓飘过"]
    for w in words:
        a = animatic.is_static_move(w)
        b = lint._is_static_move(w)
        assert a is b, f"「{w}」样片说 {a}、体检说 {b}"


def test_every_move_maps_to_a_legal_animatic_kind():
    """每条运镜都要能在静图样片上表现（映射不合法表自检会拦，这里测「认得出来」）。"""
    legal = {"push", "pull", "pan_left", "pan_right", "tilt_up", "tilt_down", "orbit", "static"}
    for m in cm.CAMERA_MOVES:
        for raw in (str(m["label"]), str(m["en"])):
            got = cm.animatic_kind(raw)
            assert got == m["animatic"], f"「{raw}」映射成 {got}，表里写的是 {m['animatic']}"
            assert got in legal
    # 认不出来时回调用方给的默认值，而不是自己猜一个
    assert cm.animatic_kind("镜头缓缓飘过") == "push"
    assert cm.animatic_kind("镜头缓缓飘过", default="static") == "static"


def test_animatic_notes_say_what_the_sample_cannot_show():
    """样片做不出效果的运镜要有一句说明，界面才能讲清楚「看到的不是最终效果」。

    没有这句，用户会以为「移焦」在样片里没做出来是坏了。
    """
    for m in cm.CAMERA_MOVES:
        note = cm.animatic_note(str(m["label"]))
        assert note == str(m.get("note") or ""), f"「{m['label']}」的说明没传出来"
    assert cm.animatic_note("移焦"), "「移焦」样片做不出来，必须有一句说明"
    assert cm.animatic_note("慢推") == ""
    assert cm.animatic_note("认不出的写法") == ""


def test_list_for_the_frontend_hides_aliases():
    """给界面的列表不带 aliases：那是解析用的，摆出去只会让前端跟着词表一起漂。"""
    items = cm.as_dicts()
    assert len(items) == len(cm.CAMERA_MOVES)
    for it in items:
        assert "aliases" not in it
        assert it["groupLabel"] == cm.group_label(str(it["group"]))
        assert set(it) >= {"key", "label", "en", "group", "groupLabel", "hint", "animatic"}


def test_lint_suggestions_only_offer_words_from_the_table():
    """体检给出的运镜建议里，引号里的词都得是表内的中文名。

    这是「四处一份词」里最容易悄悄脱节的那一处：建议文案以前是手抄的。
    """
    cases = [
        [S("1", "中景", "主观镜头", "3s", "画面1")],          # 写成机位
        [S("1", "中景", "镜头缓缓飘过", "3s", "画面1")],      # 自造说法
        [S("1", "中景", "", "3s", "画面1")],                  # 没写运镜
    ]
    seen_codes = set()
    for shots in cases:
        for f in lint.lint_shots(shots):
            if not f.code.startswith("move") and f.code != "missing_move":
                continue
            seen_codes.add(f.code)
            # 「在这一栏补上运镜，可选：…」——这份候选必须整个来自词表
            if "可选：" in f.suggestion:
                body = f.suggestion.split("可选：", 1)[1].split("。", 1)[0]
                for w in (x for x in body.split("、") if x):
                    assert w in cm.labels(), f"{f.code} 的候选里混进表外的「{w}」"
            # 引号里的词要么是表内中文名，要么是被点名的机位词（那是在引用户自己写的原文）
            for quoted in re.findall(r"「([^」]+)」", f.suggestion):
                assert quoted in cm.labels() or quoted in cm.NOT_MOVES, (
                    f"{f.code} 的建议推了表外的「{quoted}」——用户照改还是错的"
                )
    assert {"move_not_a_move", "move_unknown", "missing_move"} <= seen_codes


def test_the_runner_actually_applies_the_hard_rules():
    """机制写得再对，没接在生成路上也一样白做。

    这是这类「加一层」的功能最容易漏的一步：辅助函数写完、单测全绿，但调用点没接上，
    用户那边毫无变化——而且不会有任何报错。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "doc_service.apply_hard_rules(spec)" in src, "runner 没把硬规则接到生成路上"
    at = src.index("doc_service.apply_hard_rules(spec)")
    # 必须在 spec 定下来之后、真正拼消息之前；三种拼法（大纲/逐块/单次）都用同一个 spec
    for later in (
        "doc_service.plan_messages(spec",
        "doc_service.chunk_messages(",
        "doc_service.single_messages(spec",
    ):
        assert src.index(later) > at, f"{later} 排在附加硬规则之前——那一跑拿不到词表"


def test_lint_summary_groups_moves_by_the_vocabulary_name():
    """概览里的运镜按词表归类：「缓推」与「推近」是同一种，不能算成两种。

    概览是用户看体检报告时第一眼扫的那一行。按原文分组会把同一种运镜拆成好几格，
    于是「怎么一直在推」在那一行里反而看不出来——而它正是最该一眼看见的事。
    认不出来的写法要**原样留着**：下面那条提醒点名的就是它，两处得对得上。
    """
    summary = lint.summarize([
        S("1", "中景", "缓推", "3s", "画面1"),
        S("2", "近景", "推近", "3s", "画面2"),
        S("3", "全景", "推近", "3s", "画面3"),
        S("4", "中景", "镜头缓缓飘过", "3s", "画面4"),
    ])
    assert summary["moves"] == {"慢推": 3, "镜头缓缓飘过": 1}, summary["moves"]


def test_the_endpoint_serves_the_table_to_the_frontend():
    """接口下发的就是这一张表。

    体检报「不在运镜词表里」时，界面要靠它把可选词摆出来——前端不抄一份词表，
    所以「接口通不通」是这条链上不能断的一环。
    """
    import asyncio

    import httpx

    async def case():
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/meta/camera-moves")
            assert r.status_code == 200, r.text[:200]
            return r.json()

    body = asyncio.run(case())
    assert [g["key"] for g in body["groups"]] == [k for k, _ in cm.GROUPS]
    assert len(body["moves"]) == len(cm.CAMERA_MOVES)
    assert {m["label"] for m in body["moves"]} == set(cm.labels())
    assert body["notMoves"] == list(cm.NOT_MOVES)
    for m in body["moves"]:
        assert "aliases" not in m, "别名是解析用的，不该下发到前端"
        assert m["groupLabel"] and m["hint"], f"{m['key']} 少了分组名或用途说明"
    # 界面上要按分组渲染，所以每条 move 的 group 都得能在 groups 里找到
    keys = {g["key"] for g in body["groups"]}
    assert {m["group"] for m in body["moves"]} <= keys


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
