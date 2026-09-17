"""后台任务执行器：进程内 asyncio 任务 + SQLite 持久化。

- 图片：同步接口，调用完成即落盘；
- 视频：提交异步任务后周期轮询，成功下载视频到本地；
- 工作流：提交 ComfyUI /prompt 后轮询 /history，产物逐个下载落库；
- 文本（自动链文档）：调 LLM 生成 Markdown，超长内容按块续写后合并落盘；
- 服务重启时，有上游任务 ID 的视频/工作流任务自动重新挂接轮询，其余标记失败。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.clock import utcnow
from app.database import SessionLocal
from app.models import AgentPrompt, Asset, ComfyWorkflow, ProviderService, Task
from app.providers.base import AdapterError
from app.providers.comfyui import ComfyUIAdapter, ComfyOutputFile
from app.services import doc_service, log_service, provider_store, storage, style_service
from app.services.comfy_workflow_service import apply_params

logger = logging.getLogger("xiaoma.runner")


def _config_int(key: str, default: int) -> int:
    from app.services.config_center_service import runtime_value

    try:
        return int(runtime_value(key, default) or default)
    except (TypeError, ValueError):
        return default


def _video_poll_interval() -> int:
    return max(1, _config_int("limits.video_poll_interval", 5))


def _video_max_wait() -> int:
    """单个视频任务最多等待的秒数。"""
    return max(60, _config_int("limits.video_max_wait_minutes", 30) * 60)


def _concurrency_limit() -> int:
    return max(1, _config_int("limits.task_concurrency", 4))


def _queue_limit() -> int:
    """在册任务的**总数**上限（正在跑的 + 还在排队的）。

    到顶就直接拒绝新任务并给出可照做的提示，**不做无限排队**：
    用户一口气点下几百个任务时，他真正需要知道的是「已经排了这么多、大概要等很久」，
    而不是把任务默默收下、让他对着一个不动的进度条猜是不是坏了。
    上限至少不低于并发数，否则配错成 1 会让整个队列立刻瘫痪。
    """
    return max(_concurrency_limit(), _config_int("limits.queue_max_pending", 200))


def _queue_full_message() -> str:
    limit = _queue_limit()
    return (
        f"排队中的任务太多了（上限 {limit} 个），这次没有接单。"
        "请等前面几批跑完再发起；确实要一次跑很多，可以到「系统设置 → 系统配置」"
        "把「任务并发数」调大（更贵的做法：加大上游配额）或把「排队上限」调高。"
    )


class _ConcurrencyGate:
    """按配置动态限流：每次进入时读取最新配置，设置里改完立即生效。"""

    def __init__(self) -> None:
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def running(self) -> int:
        return self._active

    async def __aenter__(self) -> "_ConcurrencyGate":
        async with self._cond:
            while self._active >= _concurrency_limit():
                await self._cond.wait()
            self._active += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        async with self._cond:
            self._active = max(0, self._active - 1)
            # 只唤醒一个：notify_all 会让所有等待者同时醒来抢同一把锁（惊群），
            # 而这里每次释放只腾出一个名额
            self._cond.notify(1)
        return False


def _asset_url(rel: str) -> str:
    return f"/media/{rel}"


class TaskRunner:
    def __init__(self) -> None:
        self._jobs: dict[int, asyncio.Task] = {}
        self._gate = _ConcurrencyGate()

    def shutdown(self) -> None:
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()

    # ---------- 对外入口 ----------

    def start_image(self, task_id: int) -> bool:
        return self._spawn(task_id, lambda: self._run_image(task_id))

    def start_video(self, task_id: int) -> bool:
        return self._spawn(task_id, lambda: self._run_video(task_id))

    def start_comfy(self, task_id: int) -> bool:
        return self._spawn(task_id, lambda: self._run_comfy(task_id))

    def start_text(self, task_id: int) -> bool:
        return self._spawn(task_id, lambda: self._run_text(task_id))

    def start(self, kind: str, task_id: int) -> bool:
        """按任务类型投递。两个 dispatch 助手共用这一份分支，免得两处各写一遍再慢慢漂移。"""
        if kind == "video":
            return self.start_video(task_id)
        if kind == "workflow":
            return self.start_comfy(task_id)
        if kind == "text":
            return self.start_text(task_id)
        return self.start_image(task_id)

    async def start_or_fail(self, kind: str, task_id: int) -> bool:
        """投递；队列满时把这条任务标成失败并写明原因。

        为什么由 runner 统一做：所有投递口（单个生成 / 批量 / 画布 / 重跑）都走这里，
        提示口径就只有一份；否则「队列满」会表现成任务永远停在排队中，或者每个入口
        各写一句不一样的话。
        """
        if self.start(kind, task_id):
            return True
        await self._mark_failed(task_id, AdapterError(_queue_full_message()))
        return False

    def reattach(self, task_id: int) -> bool:
        # 恢复轮询不占生成名额：它只是定时问一下上游，不吃我们的算力
        return self._spawn(task_id, lambda: self._poll_video(task_id))

    def reattach_comfy(self, task_id: int) -> bool:
        return self._spawn(task_id, lambda: self._poll_comfy(task_id))

    def queue_stats(self) -> dict[str, int]:
        """给界面/健康检查看的队列概况。"""
        live = len(self._live_jobs())
        return {
            "running": self._gate.running,
            "live": live,
            "maxLive": _queue_limit(),
            "concurrency": _concurrency_limit(),
        }

    def _live_jobs(self) -> list[asyncio.Task]:
        return [j for j in self._jobs.values() if not j.done()]

    def _forget(self, task_id: int, task: asyncio.Task) -> None:
        """任务结束后把它从在册表里摘掉。

        早先这张表只增不减：一个跑了几千个任务的实例里，每个任务都会留一个
        asyncio.Task 对象在那，属于慢性泄漏，也会让「队列里有多少」这个判断失真。
        """
        if self._jobs.get(task_id) is task:
            self._jobs.pop(task_id, None)

    def _spawn(self, task_id: int, factory: Any) -> bool:
        """投递一个任务。返回 False 表示队列已满、没有收下。

        传的是 `factory`（一个不带参数的函数）而不是现成的协程对象：
        拒绝时不能留下一个「创建了却没人 await」的协程，那既会打印 RuntimeWarning，
        也容易被误读成协程泄漏。
        """
        old = self._jobs.get(task_id)
        if old and not old.done():
            old.cancel()
        # 先看再建：投递速度再快也不会超卖名额
        if len(self._live_jobs()) >= _queue_limit():
            logger.warning(
                "队列已满（%s 个上限），拒绝任务 %s（当前在跑 %s 个）",
                _queue_limit(),
                task_id,
                self._gate.running,
            )
            return False
        task = asyncio.create_task(self._with_task_logs(task_id, factory()))
        self._jobs[task_id] = task
        task.add_done_callback(lambda t, tid=task_id: self._forget(tid, t))
        return True

    async def _with_task_logs(self, task_id: int, coro: Any) -> None:
        """任务执行期间的日志额外留一份在内存，供任务中心展示「这个任务发生了什么」。

        挂在 `_spawn` 这一个出口上，所有任务（图片/视频/文本/ComfyUI 以及重启后的
        重新接管）都自动覆盖，不必在每个 `_run_*` 里各写一遍。
        """
        async with log_service.task_log_scope(task_id):
            await coro

    def is_running(self, task_id: int) -> bool:
        job = self._jobs.get(task_id)
        return bool(job and not job.done())

    # ---------- 图片 ----------

    async def _run_image(self, task_id: int) -> None:
        async with self._gate:
            try:
                async with SessionLocal() as db:
                    task = await db.get(Task, task_id)
                    if task is None or task.status in ("cancelled", "failed"):
                        return
                    resolved = await provider_store.resolve_model(db, task.model, "image")
                    params = json.loads(task.params_json or "{}")
                    ref_bytes: list[bytes] = []
                    for aid in params.get("ref_asset_ids", []):
                        asset = await db.get(Asset, int(aid))
                        if asset:
                            ref_bytes.append(storage.abs_path(asset.filename).read_bytes())
                    task.status = "processing"
                    task.progress = 20
                    await db.commit()
                    model_name = resolved.model_name
                    adapter = resolved.adapter
                    # 资产链：资产名/类型随任务带下来，落库后下游才能按名自动引用
                    asset_name = str(params.get("asset_name") or "")[:80]
                    asset_category = str(params.get("asset_category") or "")[:20]

                images = await adapter.generate_image(
                    model=model_name,
                    prompt=task.prompt,
                    n=int(params.get("n", 1) or 1),
                    size=str(params.get("size") or "1024x1024"),
                    ref_images=ref_bytes or None,
                )
                await adapter.close()

                async with SessionLocal() as db:
                    task = await db.get(Task, task_id)
                    if task is None:
                        return
                    for blob in images:
                        rel = storage.save_bytes(blob, "image/png", preferred_ext="png")
                        db.add(
                            Asset(
                                kind="image",
                                filename=rel,
                                original_name=rel.split("/")[-1],
                                content_type="image/png",
                                size=len(blob),
                                source="generated",
                                task_id=task_id,
                                name=asset_name,
                                category=asset_category,
                                prompt=task.prompt,
                                # 出图尺寸是下游图生视频要对帐的依据，必须落库
                                **storage.image_size_kwargs(blob, "image/png"),
                            )
                        )
                    task.status = "completed"
                    task.progress = 100
                    task.completed_at = utcnow()
                    task.error = None
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("image task %s failed", task_id)
                await self._mark_failed(task_id, e)

    # ---------- 视频 ----------

    async def _run_video(self, task_id: int) -> None:
        # 闸机只圈住「提交」这一段。
        #
        # 原来 `_poll_video` 是在闸机里调用的，于是一个视频任务会**攥着并发名额等上游出片**——
        # 可能几十分钟。并发数默认 4，四个视频任务就能把整条队列堵死，
        # 后面的图片/文本任务全在干等，用户看到的现象是「点了没反应」。
        # 而等待上游根本不占我们任何资源，不该算进并发成本。
        try:
            async with self._gate:
                async with SessionLocal() as db:
                    task = await db.get(Task, task_id)
                    if task is None or task.status in ("cancelled", "failed"):
                        return
                    resolved = await provider_store.resolve_model(db, task.model, "video")
                    params = json.loads(task.params_json or "{}")

                    async def _asset_bytes(aid: Any) -> bytes | None:
                        if not aid:
                            return None
                        asset = await db.get(Asset, int(aid))
                        if asset:
                            return storage.abs_path(asset.filename).read_bytes()
                        return None

                    first_frame = await _asset_bytes(params.get("first_frame_asset_id"))
                    last_frame = await _asset_bytes(params.get("last_frame_asset_id"))
                    ref_images = [
                        b for b in (await asyncio.gather(*[_asset_bytes(i) for i in params.get("ref_asset_ids", [])])) if b
                    ] or None
                    ref_videos = [
                        b for b in (await asyncio.gather(*[_asset_bytes(i) for i in params.get("video_ref_asset_ids", [])])) if b
                    ] or None

                    task.status = "processing"
                    task.progress = 10
                    await db.commit()
                    adapter = resolved.adapter
                    remote_id = await adapter.submit_video(
                        model=resolved.model_name,
                        prompt=task.prompt,
                        first_frame=first_frame,
                        last_frame=last_frame,
                        ref_images=ref_images,
                        ref_videos=ref_videos,
                        duration=int(params.get("duration", 5)),
                        ratio=str(params.get("ratio", "16:9")),
                        resolution=str(params.get("resolution", "720p")),
                    )
                    task.remote_job_id = remote_id
                    await db.commit()
                    await adapter.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("video task %s submit failed", task_id)
            await self._mark_failed(task_id, e)
            return

        # 出了闸机再轮询；轮询自己要能兜住异常，否则协程里未捕获的异常会让任务
        # 永远停在 processing（asyncio 只会打印一句 "Task exception was never retrieved"）
        try:
            await self._poll_video(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("video task %s polling failed", task_id)
            await self._mark_failed(task_id, e)

    async def _poll_video(self, task_id: int) -> None:
        waited = 0
        interval = _video_poll_interval()
        max_wait = _video_max_wait()
        while waited < max_wait:
            async with SessionLocal() as db:
                task = await db.get(Task, task_id)
                if task is None:
                    return
                if task.status == "cancelled":
                    return
                if task.status == "completed":
                    return
                if not task.remote_job_id:
                    task.status = "failed"
                    task.error = "视频任务未能提交，请重试"
                    task.completed_at = utcnow()
                    await db.commit()
                    return
                resolved = await provider_store.resolve_model(db, task.model, "video")
                status = await resolved.adapter.poll_video(task.remote_job_id)
                await resolved.adapter.close()

                if status.status == "succeeded" and status.video_url:
                    import httpx

                    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as dl:
                        resp = await dl.get(status.video_url)
                        if resp.status_code != 200:
                            task.error = f"下载视频失败：HTTP {resp.status_code}"
                            await db.commit()
                            await asyncio.sleep(interval)
                            waited += interval
                            continue
                        blob = resp.content
                    rel = storage.save_bytes(blob, "video/mp4", preferred_ext="mp4")
                    db.add(
                        Asset(
                            kind="video",
                            filename=rel,
                            original_name=rel.split("/")[-1],
                            content_type="video/mp4",
                            size=len(blob),
                            source="generated",
                            task_id=task_id,
                            prompt=task.prompt,
                            duration=json.loads(task.params_json or "{}").get("duration"),
                        )
                    )
                    task.status = "completed"
                    task.progress = 100
                    task.completed_at = utcnow()
                    task.error = None
                    await db.commit()
                    return

                if status.status == "failed":
                    task.status = "failed"
                    task.error = status.error or "视频生成失败"
                    task.completed_at = utcnow()
                    await db.commit()
                    return

                if status.progress and status.progress > task.progress:
                    task.progress = status.progress
                await db.commit()

            await asyncio.sleep(interval)
            waited += interval

        await self._mark_failed(
            task_id, AdapterError(f"视频生成超时（超过 {max_wait // 60} 分钟）")
        )

    # ---------- ComfyUI 工作流 ----------

    _COMFY_CONTENT_TYPES = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".gif": "image/gif",
    }

    def _comfy_content_type(self, file: ComfyOutputFile) -> tuple[str, str]:
        ext = Path(file.filename).suffix.lower() or (".mp4" if file.kind == "video" else ".png")
        ct = self._COMFY_CONTENT_TYPES.get(ext) or ("video/mp4" if file.kind == "video" else "image/png")
        return ct, ext.lstrip(".")

    async def _run_comfy(self, task_id: int) -> None:
        # 与视频同理：闸机只圈「提交工作流」这一段，轮询（ComfyUI 出一段视频可能十几分钟）
        # 不占并发名额，也不该在整个轮询期间攥着数据库会话。
        needs_poll = False
        try:
            async with self._gate:
                async with SessionLocal() as db:
                    task = await db.get(Task, task_id)
                    if task is None or task.status in ("cancelled", "failed"):
                        return
                    params = json.loads(task.params_json or "{}")
                    wf = await db.get(ComfyWorkflow, int(params.get("workflow_id") or 0))
                    if wf is None:
                        raise AdapterError("工作流已被删除，请重新选择")
                    provider = await db.get(ProviderService, wf.provider_id)
                    if provider is None or not provider.enabled:
                        raise AdapterError("ComfyUI 服务不可用，请在「模型服务」中检查配置")

                    task.status = "processing"
                    task.progress = 10
                    await db.commit()

                    if task.remote_job_id:
                        # 服务重启后的恢复场景：已提交过，直接进入轮询
                        needs_poll = True
                    else:
                        # 参考图（上游/手动，按 refImages 顺序）：上传到 ComfyUI 供 LoadImage 引用
                        ref_blobs: list[bytes] = []
                        for aid in params.get("ref_asset_ids", []):
                            asset = await db.get(Asset, int(aid))
                            if asset:
                                ref_blobs.append(storage.abs_path(asset.filename).read_bytes())

                        adapter = ComfyUIAdapter(provider.base_url, "")
                        param_map = json.loads(wf.param_map_json or "[]")
                        image_slots = [p for p in param_map if p.get("type") == "image"]
                        image_files: dict[str, str] = {}
                        for i, blob in enumerate(ref_blobs[: len(image_slots)]):
                            ext = "png"
                            fname = f"canvas_ref_{task_id}_{i}.{ext}"
                            image_files[image_slots[i]["key"]] = await adapter.upload_image(blob, fname)

                        graph = apply_params(
                            json.loads(wf.graph_json),
                            param_map,
                            params.get("param_values") or {},
                            image_files,
                            task.prompt,
                        )
                        prompt_id = await adapter.submit_workflow(graph)
                        await adapter.close()
                        task.remote_job_id = prompt_id
                        await db.commit()
                        needs_poll = True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("comfy task %s submit failed", task_id)
            await self._mark_failed(task_id, e)
            return

        if not needs_poll:
            return
        try:
            await self._poll_comfy(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("comfy task %s polling failed", task_id)
            await self._mark_failed(task_id, e)

    async def _poll_comfy(self, task_id: int) -> None:
        waited = 0
        interval = _video_poll_interval()
        max_wait = _video_max_wait()
        while waited < max_wait:
            async with SessionLocal() as db:
                task = await db.get(Task, task_id)
                if task is None or task.status in ("cancelled", "completed"):
                    return
                params = json.loads(task.params_json or "{}")
                wf = await db.get(ComfyWorkflow, int(params.get("workflow_id") or 0))
                if wf is None:
                    task.status = "failed"
                    task.error = "工作流已被删除"
                    task.completed_at = utcnow()
                    await db.commit()
                    return
                provider = await db.get(ProviderService, wf.provider_id)
                if provider is None or not provider.enabled:
                    task.status = "failed"
                    task.error = "ComfyUI 服务不可用"
                    task.completed_at = utcnow()
                    await db.commit()
                    return
                if not task.remote_job_id:
                    task.status = "failed"
                    task.error = "工作流未能提交，请重试"
                    task.completed_at = utcnow()
                    await db.commit()
                    return

                adapter = ComfyUIAdapter(provider.base_url, "")
                status = await adapter.poll_workflow(task.remote_job_id)
                if status.status == "succeeded":
                    try:
                        for f in status.files:
                            blob = await adapter.download_output(f)
                            ct, ext = self._comfy_content_type(f)
                            rel = storage.save_bytes(blob, ct, preferred_ext=ext)
                            db.add(
                                Asset(
                                    kind=f.kind,
                                    filename=rel,
                                    original_name=f.filename,
                                    content_type=ct,
                                    size=len(blob),
                                    source="generated",
                                    task_id=task_id,
                                    prompt=task.prompt,
                                    # 图片能读出宽高就记下来；视频这里读不出（归 ffprobe 管）
                                    **storage.image_size_kwargs(blob, ct),
                                )
                            )
                        task.status = "completed"
                        task.progress = 100
                        task.completed_at = utcnow()
                        task.error = None
                        await db.commit()
                    finally:
                        await adapter.close()
                    return
                await adapter.close()
                if status.status == "failed":
                    task.status = "failed"
                    task.error = status.error or "工作流执行失败"
                    task.completed_at = utcnow()
                    await db.commit()
                    return
                if status.progress and status.progress > task.progress:
                    task.progress = status.progress
                await db.commit()

            await asyncio.sleep(interval)
            waited += interval

        await self._mark_failed(
            task_id, AdapterError(f"工作流执行超时（超过 {max_wait // 60} 分钟）")
        )

    # ---------- 文本（自动链文档） ----------

    async def _collect(
        self,
        adapter: Any,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        """把流式增量收集成完整文本。"""
        parts: list[str] = []
        async for piece in adapter.chat_stream(
            model, messages, temperature=temperature, max_tokens=max_tokens
        ):
            parts.append(piece)
        text = "".join(parts).strip()
        if not text:
            raise AdapterError("模型返回了空内容，请重试或更换模型")
        return text

    async def _set_progress(self, task_id: int, value: int) -> None:
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task and task.status == "processing":
                task.progress = max(task.progress, min(99, value))
                await db.commit()

    async def _run_text(self, task_id: int) -> None:
        async with self._gate:
            try:
                async with SessionLocal() as db:
                    task = await db.get(Task, task_id)
                    if task is None or task.status in ("cancelled", "failed"):
                        return
                    params = json.loads(task.params_json or "{}")
                    agent_key = str(params.get("agent_key") or "")
                    rows = await db.execute(
                        select(AgentPrompt).where(AgentPrompt.key == agent_key)
                    )
                    row = rows.scalars().first()
                    if row is None or not row.enabled:
                        raise AdapterError(
                            f"创作 Agent「{agent_key or '未指定'}」不存在或已停用"
                            "（系统设置 → 创作 Agent）"
                        )
                    spec = doc_service.to_spec(row)
                    if not spec.user_template.strip():
                        raise AdapterError(
                            f"创作 Agent「{spec.label}」的用户消息模板为空，请先补全"
                        )
                    # 风格注入：风格卡的「给 Agent 的风格要求」追加到系统提示词末尾
                    # （这里可以出现导演名，它是给 LLM 理解技法用的；给生图模型的那份在别处）
                    card = await style_service.load_card(db, str(params.get("style_key") or ""))
                    block = style_service.agent_block(card)
                    if block:
                        spec = replace(spec, system_prompt=f"{spec.system_prompt.rstrip()}\n\n{block}")
                        logger.info("文本任务 %s 注入风格：%s", task_id, card.name if card else "")
                    # Agent 上指定了模型就用它，否则用节点上选的
                    resolved = await provider_store.resolve_model(
                        db, spec.model_key or task.model, "text"
                    )
                    model_name = resolved.model_name
                    adapter = resolved.adapter

                    content = task.prompt or ""
                    extra = str(params.get("extra") or "")
                    if not content.strip() and not extra.strip():
                        raise AdapterError(
                            "节点没有可用的输入内容：请填写想法/补充要求，或先运行上游节点"
                        )
                    total = doc_service.resolve_total(spec, params.get("node_params") or {})

                    task.status = "processing"
                    task.progress = 10
                    await db.commit()

                # 先判断要不要分块：单次调用输出有上限，超长内容只能逐块续写
                chunked = doc_service.needs_chunking(spec, total)
                plan = ""
                if chunked:
                    plan = await self._collect(
                        adapter,
                        model_name,
                        doc_service.plan_messages(spec, content, extra, total),
                        spec.temperature,
                        spec.max_tokens,
                    )
                    await self._set_progress(task_id, doc_service.progress_for(1, total + 1, 10, 85))

                chunks: list[str] = []
                if chunked:
                    for i in range(1, total + 1):
                        chunks.append(
                            await self._collect(
                                adapter,
                                model_name,
                                doc_service.chunk_messages(
                                    spec, content, extra, total, i, plan
                                ),
                                spec.temperature,
                                spec.max_tokens,
                            )
                        )
                        await self._set_progress(
                            task_id, doc_service.progress_for(i + 1, total + 1, 10, 85)
                        )
                else:
                    chunks.append(
                        await self._collect(
                            adapter,
                            model_name,
                            doc_service.single_messages(spec, content, extra, total),
                            spec.temperature,
                            spec.max_tokens,
                        )
                    )
                await adapter.close()

                text = doc_service.compose_document(plan, chunks, with_plan=chunked)
                blob = text.encode("utf-8")
                rel = storage.save_bytes(blob, "text/markdown", preferred_ext="md")
                async with SessionLocal() as db:
                    task = await db.get(Task, task_id)
                    if task is None:
                        return
                    db.add(
                        Asset(
                            kind="document",
                            filename=rel,
                            original_name=f"{spec.key}.md",
                            content_type="text/markdown",
                            size=len(blob),
                            source="generated",
                            task_id=task_id,
                            prompt=(extra or content)[:500],
                        )
                    )
                    task.status = "completed"
                    task.progress = 100
                    task.completed_at = utcnow()
                    task.error = None
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("text task %s failed", task_id)
                await self._mark_failed(task_id, e)

    # ---------- 恢复与兜底 ----------

    async def recover(self) -> None:
        """启动时恢复未完成任务。"""
        async with SessionLocal() as db:
            rows = await db.execute(
                select(Task).where(Task.status.in_(["pending", "processing"]))
            )
            unfinished = list(rows.scalars().all())
            for task in unfinished:
                if task.kind == "video" and task.remote_job_id:
                    logger.info("reattach video task %s -> %s", task.id, task.remote_job_id)
                    self.reattach(task.id)
                elif task.kind == "workflow" and task.remote_job_id:
                    logger.info("reattach comfy task %s -> %s", task.id, task.remote_job_id)
                    self.reattach_comfy(task.id)
                else:
                    task.status = "failed"
                    task.error = "服务已重启，该任务未完成，请重新发起"
                    task.completed_at = utcnow()
            await db.commit()

    async def _mark_failed(self, task_id: int, exc: Exception) -> None:
        try:
            async with SessionLocal() as db:
                task = await db.get(Task, task_id)
                if task and task.status not in ("completed", "cancelled"):
                    task.status = "failed"
                    task.error = str(exc) or exc.__class__.__name__
                    task.completed_at = utcnow()
                    await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to mark task %s", task_id)


runner = TaskRunner()
