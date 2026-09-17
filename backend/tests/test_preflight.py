"""生成前软校验（直接 python 运行）。

运行：venv/Scripts/python tests/test_preflight.py

这个功能的全部风险在**分寸**上，所以钉的不是「检查能不能算对」，而是四条边界：

1. **永不阻断**：预检服务里一行 `raise` 都不许有，输入模型也不设硬校验，
   返回里 `blocking` 恒为 False。用户点一次生成，最坏的结果只能是「看到一条提示」。
2. **不重复硬校验**：档位合不合法、参考图在不在、模型可不可用，那些是生成接口的活。
   预检里再写一遍就等于多一份会漂移的副本——这里只查**组合**问题。
3. **说清为什么 + 怎么改**：每条告警都必须带 suggestion，否则用户只能猜。
4. **读的是实时状态**：队列上限改了、首帧换了、模型刚连续失败过，告警要跟着变。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Task  # noqa: E402
from app.schemas import (  # noqa: E402
    ImageBatchPreflightIn,
    ImagePreflightIn,
    PreflightOut,
    VideoPreflightIn,
)
from app.services import config_center_service as cc  # noqa: E402
from app.services import preflight  # noqa: E402


# ============================================================
# 脚手架
# ============================================================


def _run(fn):
    async def main():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class _limits:
    """临时改运行期配置（`runtime_value` 读的就是这个 cache）。用完还原。

    这样测的是「读配置」这条真实路径，而不是把 `_queue_limit()` 打桩掉。
    """

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.saved = {k: cc._CACHE.get(k, "__absent__") for k in self.kw}
        cc._CACHE.update(self.kw)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v == "__absent__":
                cc._CACHE.pop(k, None)
            else:
                cc._CACHE[k] = v
        return False


async def _add_image(db, width: int, height: int, name: str = "首帧") -> int:
    a = Asset(kind="image", filename=f"2026-09/{name}-{width}x{height}.png",
              original_name=name, content_type="image/png", source="generated",
              width=width, height=height)
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a.id


async def _add_failures(db, model: str, kind: str, count: int = 3, error: str = "上游 500") -> None:
    for _ in range(count):
        db.add(Task(kind=kind, status="failed", model=model, prompt="x", error=error))
    await db.commit()


def _codes(report) -> set[str]:
    return {w.code for w in report.warnings}


def _by_code(report, code: str):
    return next((w for w in report.warnings if w.code == code), None)


# ============================================================
# 一、永不阻断
# ============================================================


def test_preflight_service_never_raises():
    """最硬的一条：预检源码里不许出现 `raise`。

    一旦这里能抛异常，前端就得给预检写 try/catch 兜底，否则「生成」会被一个
    辅助功能挡掉——那就彻底违背了「告警但不阻断」。

    用语法树判断而不是搜字符串：注释和文档字符串里提到 `raise` 不算数。
    """
    tree = ast.parse((BACKEND / "app" / "services" / "preflight.py").read_text(encoding="utf-8"))
    offenders = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert not offenders, f"预检服务里出现了 raise（第 {offenders} 行），它会变成阻断"


def test_preflight_inputs_accept_anything():
    """输入模型不许设硬校验，否则用户先收到的是 422 而不是一句提示。"""

    async def case(db):
        # 全空也能过校验层
        ImagePreflightIn()
        ImageBatchPreflightIn()
        VideoPreflightIn()
        # 空提示词 / 0 张 / 负数张数 也不报错（预检照实算，拦人不是它的活）
        ImagePreflightIn(prompt="", n=0)
        ImagePreflightIn(prompt="", n=-5)
        VideoPreflightIn(prompt="", ratio="", resolution="", first_frame_asset_id=None)
        return True

    assert _run(case) is True


def test_response_is_marked_non_blocking():
    assert PreflightOut().blocking is False
    assert PreflightOut().warnings == []


def test_router_exposes_preflight_for_all_three_entry_points():
    """三个入口都要有预检，少一个就会出现「图片页有提示、视频页没有」的不一致。"""
    src = (BACKEND / "app" / "routers" / "generation.py").read_text(encoding="utf-8")
    for path in ('"/images/preflight"', '"/images/batch/preflight"', '"/videos/preflight"'):
        assert f"@router.post({path}" in src, f"缺少 {path} 预检端点"


def test_preflight_does_not_redo_hard_validation():
    """预检服务不许去碰档位校验 / 模型解析——那些是生成接口的活。

    按语法树看**实际用到的标识符**，而不是搜字符串：文档字符串里解释
    「这里不重复 validate_option」是正当的，不能被自己的注释误伤。
    """
    tree = ast.parse((BACKEND / "app" / "services" / "preflight.py").read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.name for a in node.names)
    banned = {"resolve_model", "validate_option", "validate_int_option", "HTTPException"}
    hit = banned & used
    assert not hit, f"预检里用到了硬校验的东西 {hit}，那是生成接口该做的事"


# ============================================================
# 二、提示词太薄
# ============================================================


def test_thin_prompt_warns():
    async def case(db):
        for thin in ("", "  ", "猫", "猫 猫"):
            r = await preflight.check_image(db, prompt=thin, model_key="m")
            assert "prompt.thin" in _codes(r), f"「{thin}」应当触发提示词过薄的告警"
        return True

    assert _run(case) is True


def test_normal_prompt_is_quiet():
    async def case(db):
        r = await preflight.check_image(db, prompt="少女站在窗边，逆光，中景，胶片质感", model_key="m")
        assert "prompt.thin" not in _codes(r)
        return True

    assert _run(case) is True


def test_batch_warns_once_for_a_few_thin_lines():
    """批量里只有几行太薄时，不该按行刷屏，只汇总一句。"""

    async def case(db):
        r = await preflight.check_image_batch(
            db, prompts=["少女站在窗边，逆光，中景", "猫", "海"], model_keys=["m"]
        )
        hits = [w for w in r.warnings if w.code == "prompt.thin"]
        assert len(hits) == 1, f"应当只汇总一条，实际 {len(hits)} 条"
        assert "2 行" in hits[0].message
        return True

    assert _run(case) is True


# ============================================================
# 三、首帧与画幅 / 清晰度对不上
# ============================================================


def test_first_frame_ratio_mismatch_warns():
    async def case(db):
        fid = await _add_image(db, 1280, 720)  # 16:9 的首帧
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=fid, ratio="9:16", resolution="720p",
        )
        w = _by_code(r, "video.first_frame_ratio_mismatch")
        assert w is not None, f"16:9 首帧配 9:16 画幅应当告警，实际 {_codes(r)}"
        assert "1280×720" in w.message and "16:9" in w.message and "9:16" in w.message
        assert w.suggestion, "告警必须给出怎么改"
        return True

    assert _run(case) is True


def test_matching_ratio_is_quiet():
    async def case(db):
        fid = await _add_image(db, 1280, 720)
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=fid, ratio="16:9", resolution="720p",
        )
        assert "video.first_frame_ratio_mismatch" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_same_ratio_different_pixels_is_quiet():
    """864×1152 与 3:4 是同一画幅（都是 0.75），不该因为像素不同就告警。"""

    async def case(db):
        fid = await _add_image(db, 864, 1152)
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=fid, ratio="3:4", resolution="720p",
        )
        assert "video.first_frame_ratio_mismatch" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_first_frame_upscaled_warns():
    async def case(db):
        fid = await _add_image(db, 1280, 720)  # 短边 720
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=fid, ratio="16:9", resolution="1080p",
        )
        w = _by_code(r, "video.first_frame_upscaled")
        assert w is not None, f"720p 首帧配 1080p 应当告警，实际 {_codes(r)}"
        assert "720" in w.message and "1080p" in w.message
        return True

    assert _run(case) is True


def test_enough_pixels_is_quiet():
    async def case(db):
        fid = await _add_image(db, 1920, 1080)
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=fid, ratio="16:9", resolution="1080p",
        )
        assert "video.first_frame_upscaled" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_text_to_video_has_no_first_frame_warning():
    """纯文生视频没有首帧，不该冒出任何首帧告警。"""

    async def case(db):
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=None, ratio="9:16", resolution="1080p",
        )
        assert not any(c.startswith("video.first_frame") for c in _codes(r)), _codes(r)
        return True

    assert _run(case) is True


def test_asset_without_pixels_is_skipped():
    """连文件都读不出宽高时，跳过而不是报一个瞎猜的告警。"""

    async def case(db):
        a = Asset(kind="image", filename="2026-09/definitely-missing.png", original_name="old",
                  content_type="image/png", source="uploaded")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        r = await preflight.check_video(
            db, prompt="镜头缓缓推近", model_key="m",
            first_frame_asset_id=a.id, ratio="9:16", resolution="1080p",
        )
        assert not any(c.startswith("video.first_frame") for c in _codes(r)), _codes(r)
        return True

    assert _run(case) is True


def test_old_asset_without_stored_pixels_is_measured_from_the_file():
    """库里没记宽高时（历史数据就是这样）要现读文件，不能让告警静默失效。

    这条守的是一个很隐蔽的失效：字段空着 → 检查直接 return → 用户什么都没看到，
    既不报错也不提示，看起来就像「这个功能对老素材不生效」。
    """
    from app.config import settings

    async def case(db):
        # 手工拼一个 1280×720 的 PNG 头（只读头就够，不需要真的像素数据）
        png = (
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big")
        )
        rel = "2026-09/_preflight_probe.png"
        path = settings.storage_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        try:
            a = Asset(kind="image", filename=rel, original_name="probe",
                      content_type="image/png", source="uploaded")
            db.add(a)
            await db.commit()
            await db.refresh(a)
            assert a.width is None, "这个用例的前提是库里没有宽高"

            r = await preflight.check_video(
                db, prompt="镜头缓缓推近", model_key="m",
                first_frame_asset_id=a.id, ratio="9:16", resolution="1080p",
            )
            w = _by_code(r, "video.first_frame_ratio_mismatch")
            assert w is not None, f"现读文件后应当能对帐，实际 {_codes(r)}"
            assert "1280×720" in w.message
        finally:
            path.unlink(missing_ok=True)
        return True

    assert _run(case) is True


# ============================================================
# 四、队列接不下这次批量
# ============================================================


def test_queue_overflow_is_predicted():
    """队列接不下时先说清「会排多少 / 还能接多少」，而不是等任务被标失败。"""

    async def case(db):
        with _limits(**{"limits.queue_max_pending": 3, "limits.task_concurrency": 1}):
            r = await preflight.check_image_batch(
                db, prompts=[f"第 {i} 个画面，逆光，中景" for i in range(5)], model_keys=["m"]
            )
            w = _by_code(r, "queue.will_reject")
            assert w is not None, f"5 个任务 > 上限 3，应当告警，实际 {_codes(r)}"
            assert "5 个任务" in w.message and "3 个名额" in w.message
            assert "排队上限" in w.suggestion
        return True

    assert _run(case) is True


def test_queue_warning_follows_live_config():
    """同一个批量，把上限调大之后就不该再告警——证明读的是实时配置而不是写死的数。"""

    async def case(db):
        prompts = [f"第 {i} 个画面，逆光，中景" for i in range(5)]
        with _limits(**{"limits.queue_max_pending": 3, "limits.task_concurrency": 1}):
            assert "queue.will_reject" in _codes(
                await preflight.check_image_batch(db, prompts=prompts, model_keys=["m"])
            )
        with _limits(**{"limits.queue_max_pending": 50, "limits.task_concurrency": 1}):
            assert "queue.will_reject" not in _codes(
                await preflight.check_image_batch(db, prompts=prompts, model_keys=["m"])
            )
        return True

    assert _run(case) is True


def test_queue_limit_never_drops_below_concurrency():
    """上限配得比并发还小是配置错误，不该让预检算出「一个都接不下」这种结论。"""

    async def case(db):
        with _limits(**{"limits.queue_max_pending": 1, "limits.task_concurrency": 8}):
            r = await preflight.check_image(db, prompt="少女站在窗边，逆光，中景", n=1)
            assert "queue.will_reject" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_single_task_never_triggers_queue_warning():
    """只生成一个任务时，队列再挤也不该跳出来说「你排太多了」。"""

    async def case(db):
        with _limits(**{"limits.queue_max_pending": 0, "limits.task_concurrency": 1}):
            r = await preflight.check_image(db, prompt="少女站在窗边，逆光，中景", n=1)
            assert "queue.will_reject" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_batch_scale_is_informational_not_alarming():
    """批量规模提醒是 info 级：它说「会跑几轮」，不是「你错了」。"""

    async def case(db):
        r = await preflight.check_image_batch(
            db, prompts=[f"第 {i} 个画面，逆光，中景" for i in range(15)], model_keys=["m"]
        )
        w = _by_code(r, "image.batch_scale")
        assert w is not None and w.level == "info", _codes(r)
        assert "15 个任务" in w.message
        return True

    assert _run(case) is True


# ============================================================
# 五、模型最近一直失败
# ============================================================


def test_repeated_recent_failures_warn():
    async def case(db):
        await _add_failures(db, "bad-model", "video", count=3, error="上游返回 503")
        r = await preflight.check_video(db, prompt="镜头缓缓推近", model_key="bad-model")
        w = _by_code(r, "model.recent_failures")
        assert w is not None, f"最近 3 次全失败应当告警，实际 {_codes(r)}"
        assert "上游返回 503" in w.message, "要把最近一次的真实错误带出来"
        return True

    assert _run(case) is True


def test_a_success_in_between_clears_the_warning():
    """中间成功过一次就不算「一直失败」——不能拿旧账吓人。"""

    async def case(db):
        await _add_failures(db, "ok-model", "video", count=2)
        db.add(Task(kind="video", status="succeeded", model="ok-model", prompt="x"))
        await db.commit()
        r = await preflight.check_video(db, prompt="镜头缓缓推近", model_key="ok-model")
        assert "model.recent_failures" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_failures_of_another_kind_do_not_count():
    """出图一直失败，不代表出视频也会失败——类型要分开算。"""

    async def case(db):
        await _add_failures(db, "m", "image", count=3)
        r = await preflight.check_video(db, prompt="镜头缓缓推近", model_key="m")
        assert "model.recent_failures" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_old_failures_do_not_warn():
    """上周的旧账不算数：窗口外的失败必须靠时间过滤掉。

    构造旧时间要用 `utcnow`（库里就是 UTC），用本地 `datetime.now()` 造出来的
    「一小时前」在 UTC 口径下其实是**八小时后**，这个用例就废了。
    """

    async def case(db):
        from datetime import timedelta

        from app.clock import utcnow

        old = utcnow() - timedelta(minutes=preflight.RECENT_FAILURE_WINDOW_MIN + 30)
        for _ in range(3):
            db.add(Task(kind="video", status="failed", model="stale", prompt="x",
                        error="旧错误", created_at=old))
        await db.commit()
        r = await preflight.check_video(db, prompt="镜头缓缓推近", model_key="stale")
        assert "model.recent_failures" not in _codes(r), _codes(r)
        return True

    assert _run(case) is True


def test_batch_reports_each_failing_model():
    """批量里哪个模型有问题就说哪个，不要含糊成「有模型有问题」。"""

    async def case(db):
        await _add_failures(db, "bad", "image", count=3)
        r = await preflight.check_image_batch(
            db, prompts=["少女站在窗边，逆光，中景"], model_keys=["bad", "good"]
        )
        hits = [w for w in r.warnings if w.code == "model.recent_failures"]
        assert len(hits) == 1 and "「bad」" in hits[0].message, [w.message for w in hits]
        return True

    assert _run(case) is True


# ============================================================
# 六、每条告警都要能落地
# ============================================================


def test_every_warning_carries_a_suggestion():
    """全量扫一遍：任何一条告警都必须告诉用户「怎么改」。

    只丢一句「参数可能有问题」等于没说——用户只能猜，那还不如不提示。
    """

    async def case(db):
        fid = await _add_image(db, 1280, 720)
        await _add_failures(db, "bad", "video", count=3)
        reports = [
            await preflight.check_image(db, prompt="猫", model_key="m", n=8,
                                        ref_asset_ids=[fid, fid, fid, fid]),
            await preflight.check_image_batch(db, prompts=["猫"] * 15, model_keys=["m"]),
            await preflight.check_video(db, prompt="猫", model_key="bad",
                                        first_frame_asset_id=fid, ratio="9:16",
                                        resolution="1080p"),
        ]
        with _limits(**{"limits.queue_max_pending": 2, "limits.task_concurrency": 1}):
            reports.append(
                await preflight.check_image_batch(db, prompts=["猫"] * 5, model_keys=["m"])
            )
        seen = set()
        for r in reports:
            assert r.warnings, "这组输入应当至少产出一条告警，否则用例本身失效了"
            for w in r.warnings:
                seen.add(w.code)
                assert w.message.strip(), f"{w.code} 没有说明为什么"
                assert w.suggestion.strip(), f"{w.code} 没有说怎么改"
                assert w.level in ("warn", "info"), f"{w.code} 的 level 不合法：{w.level}"
        assert len(seen) >= 5, f"应当覆盖到多条规则，实际只命中 {seen}"
        return True

    assert _run(case) is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001  一个用例炸了别把剩下的都带下去
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
