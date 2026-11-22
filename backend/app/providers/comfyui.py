"""ComfyUI 本地服务适配器（无鉴权，base_url 填 http://127.0.0.1:8188）。

- 工作流执行：POST /prompt（graph API 格式）→ 轮询 GET /history/{id} → GET /view 下载产物
- 参考图上传：POST /upload/image → LoadImage 节点引用文件名
- 参数发现：GET /object_info（模型列表 / 采样器枚举）
- 文本/图片/视频能力不固定 —— 由工作流本身决定，这里实现 test_connection 与工作流执行原语

接入细节与踩坑记录见 `docs/comfyui.md`（对照官方 server.py / execution.py 核实过）。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx

from app.providers.base import (
    AdapterError,
    AudioRef,
    BaseAdapter,
    SpeechResult,
    VideoStatus,
    network_error_detail,
    unsupported,
    url_host,
)
from app.services.comfy_workflow_service import apply_params, classify_artifact


def _describe_failure(status_info: dict[str, Any]) -> str:
    """从 history 的 status.messages 里挖出可读的失败原因。

    `/prompt` 只负责校验，真正的执行异常记在 history 的 messages 里，
    形如 ["execution_error", {node_id, node_type, exception_message, ...}]。
    """
    msgs = status_info.get("messages") or []
    for m in msgs:
        if isinstance(m, list) and m and m[0] == "execution_error":
            detail = m[1] if len(m) > 1 and isinstance(m[1], dict) else {}
            node = detail.get("node_id")
            node_type = detail.get("node_type") or ""
            reason = (
                detail.get("exception_message")
                or detail.get("exception_type")
                or "未知错误"
            )
            where = f"节点 {node}" + (f"（{node_type}）" if node_type else "")
            return f"{where} 执行失败：{reason}"
    return "工作流执行失败（ComfyUI 未返回具体原因，请查看 ComfyUI 控制台日志）"


@dataclass
class ComfyOutputFile:
    """一次工作流执行产物中的一个文件。"""

    filename: str
    subfolder: str = ""
    folder_type: str = "output"
    kind: str = "image"  # image | video


@dataclass
class ComfyRunStatus:
    status: str  # processing | succeeded | failed
    files: list[ComfyOutputFile] = field(default_factory=list)
    progress: int = 0
    error: str | None = None
    # 拿到的产物无法归档的文件名（如音频），用于给出可读提示而不是静默失败
    unsupported: list[str] = field(default_factory=list)


def _body_bytes(resp: httpx.Response) -> int:
    """响应体字节数。测试用的假响应可能没有 content，所以取不到就算 0。"""
    return len(getattr(resp, "content", b"") or b"")


def _describe_submit_error(resp: httpx.Response) -> tuple[str, str]:
    """把 /prompt 的 400 结构化报错翻译成人能读的话。

    官方返回形如：
    ``{"error": {"type", "message", "details", "extra_info"},
       "node_errors": {"5": {"errors": [{"type","message","details"}],
                             "dependent_outputs": [...], "class_type": "KSampler"}}}``
    直接把原始 JSON 抛给用户等于没说，这里按节点拆出来。

    返回 (给用户看的话, 只用于日志的安全摘要)。日志摘要里**不带节点错误的具体文案**：
    node_errors 里可能带着节点输入，而 CLIPTextEncode 的输入就是用户的提示词。
    """
    try:
        data = resp.json() or {}
    except ValueError:
        return (
            f"提交工作流失败：HTTP {resp.status_code} {resp.text[:200]}",
            f"submit HTTP {resp.status_code} body={_body_bytes(resp)}B",
        )

    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    node_errors = data.get("node_errors") if isinstance(data.get("node_errors"), dict) else {}

    parts: list[str] = []
    message = str(err.get("message") or "").strip()
    if message:
        parts.append(message)
    kinds: set[str] = set()
    for node_id, info in list(node_errors.items())[:3]:
        if not isinstance(info, dict):
            continue
        class_type = str(info.get("class_type") or "").strip()
        for item in (info.get("errors") or [])[:2]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip()
            if kind:
                kinds.add(kind[:48])
            detail = str(item.get("details") or item.get("message") or "").strip()
            if not detail:
                continue
            where = f"节点 {node_id}" + (f"（{class_type}）" if class_type else "")
            parts.append(f"{where}：{detail}")
    if not parts:
        parts.append(str(err.get("type") or f"HTTP {resp.status_code}"))

    log_detail = (
        f"submit HTTP {resp.status_code} nodes={len(node_errors)} "
        f"types={'|'.join(sorted(kinds)) or (str(err.get('type') or '-'))[:48]}"
    )
    return (
        "工作流校验未通过：" + "；".join(parts) + "（在 ComfyUI 里可正常运行的图，导出 API 格式后通常即可提交）",
        log_detail,
    )


class ComfyUIAdapter(BaseAdapter):
    kind = "comfyui"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        *,
        workflow: dict[str, Any] | None = None,
        params: list[dict[str, Any]] | None = None,
    ) -> None:
        """`workflow` / `params` 只在使用「工作流即模型」时传入。

        普通连接测试、画布工作流节点仍然是 stateless 的：它们自己带着图调用
        `submit_workflow`。绑定工作流时，适配器才具备 generate_image / submit_video
        这两条通用能力。
        """
        super().__init__(base_url, api_key)
        self.workflow = workflow
        self.params = params or []

    def headers(self) -> dict[str, str]:
        return {}  # 本地服务无鉴权

    def _bound_workflow(self) -> dict[str, Any]:
        if not isinstance(self.workflow, dict) or not self.workflow:
            raise unsupported("直接生成（这个 ComfyUI 服务没有绑定工作流）")
        return self.workflow

    def _params_for(self, field: str) -> list[dict[str, Any]]:
        """同一字段可能出现在多个节点上（图片 latent + 视频 latent 都有 width）。"""
        return [p for p in self.params if str(p.get("field") or "") == field]

    def _values_for(self, size: str, n: int) -> dict[str, Any]:
        """只填工作流里真实存在的参数：宽高 / 张数。

        注意 key 是 `节点id_字段`（如 `5_width`），不是裸字段名——以前按裸字段
        判断，结果宽高从来没传进去过（工作流一直用图里写死的尺寸）。
        """
        values: dict[str, Any] = {}
        try:
            w, h = (int(x) for x in str(size).lower().split("x", 1))
        except (TypeError, ValueError):
            w = h = 0
        if w:
            for p in self._params_for("width"):
                values[str(p["key"])] = w
        if h:
            for p in self._params_for("height"):
                values[str(p["key"])] = h
        if n > 1:
            for p in self._params_for("batch_size"):
                values[str(p["key"])] = int(n)
        return values

    @staticmethod
    def _video_size(resolution: str, ratio: str) -> tuple[int, int]:
        """分辨率档位 + 画幅 → 宽高（按 16 对齐）。认不出返回 (0, 0)。"""
        heights = {"480p": 480, "720p": 720, "1080p": 1080, "2160p": 2160}
        height = heights.get(str(resolution or "").lower(), 0)
        if not height:
            return 0, 0
        try:
            rw, rh = (int(x) for x in str(ratio or "").split(":", 1))
        except (TypeError, ValueError):
            rw, rh = 16, 9
        if rw <= 0 or rh <= 0:
            rw, rh = 16, 9
        width = round(height * rw / rh)
        return max(16, width // 16 * 16), max(16, height // 16 * 16)

    def _video_values(self, duration: int, ratio: str, resolution: str) -> dict[str, Any]:
        """时长 / 画幅 → 工作流里的 width/height/length。

        采样器（步数/CFG/采样器）**不动**：那是工作流作者调好的，交给工作流原值。
        只把用户真正会改的两件事映射出去：出多长、出多大。
        """
        values: dict[str, Any] = {}

        width, height = self._video_size(resolution, ratio)
        if width and height:
            for p in self._params_for("width"):
                values[str(p["key"])] = width
            for p in self._params_for("height"):
                values[str(p["key"])] = height

        fps = 16.0
        for name in ("fps", "frame_rate"):
            found = self._params_for(name)
            if found:
                try:
                    fps = float(found[0].get("default") or fps)
                except (TypeError, ValueError):
                    fps = 16.0
                break
        frames = max(1, round(float(duration or 5) * fps))
        for name in ("length", "num_frames", "frames"):
            for p in self._params_for(name):
                # Wan 一类的 length 要求 4n+1（1、5、9…）：向上取到最近的合法值，
                # 宁可多一帧也不要把用户要的 8 秒缩成 7.8 秒
                values[str(p["key"])] = ((frames + 2) // 4 * 4 + 1) if name == "length" else frames
        return values

    async def _upload_refs(self, blobs: list[bytes]) -> dict[str, str]:
        """按参数表里 image 槽位的顺序，把参考图/首帧上传成 ComfyUI 文件名。"""
        if not blobs:
            return {}
        slots = [p for p in self.params if p.get("type") == "image"]
        if not slots:
            raise AdapterError(
                "这个工作流没有参考图槽位（LoadImage），不能带图运行。",
                log_detail="comfy_no_image_slot",
            )
        files: dict[str, str] = {}
        for i, (param, blob) in enumerate(zip(slots, blobs)):
            files[str(param.get("key"))] = await self.upload_image(
                blob, f"xiaoma-ref-{i}.png"
            )
        return files

    async def _wait_workflow(self, prompt_id: str, timeout: float = 300.0) -> ComfyRunStatus:
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(2.0)
            waited += 2.0
            status = await self.poll_workflow(prompt_id)
            if status.status != "processing":
                return status
        raise AdapterError(
            f"ComfyUI 工作流 {prompt_id} 超过 {int(timeout)} 秒仍未完成",
            log_detail="comfy_timeout",
        )

    def _view_url(self, file: ComfyOutputFile) -> str:
        query = urlencode(
            {"filename": file.filename, "subfolder": file.subfolder, "type": file.folder_type}
        )
        return f"{self.base_url}/view?{query}"

    async def test_connection(self, model: str | None = None) -> None:
        if not self.base_url:
            raise AdapterError("Base URL 为空，请填写 ComfyUI 地址（默认 http://127.0.0.1:8188）", log_detail="config_invalid base_url_empty")
        client = await self.client()
        try:
            resp = await client.get(f"{self.base_url}/system_stats", headers=self.headers())
        except httpx.HTTPError as e:
            raise AdapterError(f"无法连接 ComfyUI：{e}（请确认 ComfyUI 已启动且地址正确）", log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            raise AdapterError(f"ComfyUI 响应异常：HTTP {resp.status_code}", log_detail=f"HTTP {resp.status_code} host={url_host(self.base_url)}")

    # ---- 通用能力不适用 ----

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        raise unsupported("文本对话（ComfyUI 只执行工作流）")
        yield ""  # pragma: no cover

    async def generate_image(
        self,
        *,
        model: str = "",
        prompt: str = "",
        n: int = 1,
        size: str = "1024x1024",
        ref_images: list[bytes] | None = None,
    ) -> list[bytes]:
        """把绑定的工作流当成一个「图片模型」跑一次。"""
        graph = apply_params(
            self._bound_workflow(),
            self.params,
            self._values_for(size, max(1, int(n))),
            await self._upload_refs(ref_images or []),
            prompt_text=prompt,
        )
        prompt_id = await self.submit_workflow(graph)
        status = await self._wait_workflow(prompt_id)
        if status.status != "succeeded":
            raise AdapterError(status.error or "ComfyUI 工作流执行失败", log_detail="comfy_workflow_failed")
        images = [f for f in status.files if f.kind == "image"]
        if not images:
            raise AdapterError(
                "工作流执行成功，但没有图片产物。确认图里包含 SaveImage / SaveAnimatedWEBP 这类输出节点。",
                log_detail="comfy_no_image_output",
            )
        return [await self.download_output(f) for f in images]

    async def submit_video(
        self,
        *,
        model: str = "",
        prompt: str = "",
        first_frame: bytes | None = None,
        duration: int = 5,
        ratio: str = "16:9",
        resolution: str = "720p",
        last_frame: bytes | None = None,
        ref_images: list[bytes] | None = None,
        ref_videos: list[bytes] | None = None,
        ref_audio: AudioRef | None = None,
    ) -> str:
        """把绑定的工作流当成「视频模型」提交；返回 ComfyUI prompt_id。"""
        if last_frame or ref_videos or ref_audio:
            raise AdapterError(
                "ComfyUI 工作流这一版只接首帧/参考图；尾帧、视频参考、参考音频还没接。",
                log_detail="comfy_video_ref_unsupported",
            )
        blobs: list[bytes] = []
        if first_frame is not None:
            blobs.append(first_frame)
        if ref_images:
            blobs.extend(ref_images)
        graph = apply_params(
            self._bound_workflow(),
            self.params,
            self._video_values(duration, ratio, resolution),
            await self._upload_refs(blobs),
            prompt_text=prompt,
        )
        return await self.submit_workflow(graph)

    async def poll_video(self, remote_id: str) -> VideoStatus:
        status = await self.poll_workflow(remote_id)
        if status.status == "failed":
            return VideoStatus(status="failed", error=status.error or "ComfyUI 工作流执行失败")
        if status.status != "succeeded":
            return VideoStatus(status="processing", progress=status.progress)
        files = [f for f in status.files if f.kind == "video"] or status.files
        if not files:
            return VideoStatus(
                status="failed",
                error="工作流执行成功，但没有视频产物。确认图里包含 SaveVideo / VHS_VideoCombine 这类输出节点。",
            )
        return VideoStatus(status="succeeded", video_url=self._view_url(files[0]), progress=100)

    async def synthesize_speech(self, **kwargs: Any) -> SpeechResult:
        raise unsupported("语音合成（ComfyUI 走工作流，不在适配器层做）")

    # ---- 工作流原语 ----

    async def get_object_info(self) -> dict[str, Any]:
        """全部节点定义（模型列表 / 采样器枚举等，用于参数表单 options）。"""
        client = await self.client()
        try:
            resp = await client.get(f"{self.base_url}/object_info", headers=self.headers())
        except httpx.HTTPError as e:
            raise AdapterError(f"获取 ComfyUI 节点信息失败：{e}", log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            raise AdapterError(f"获取节点信息失败：HTTP {resp.status_code}", log_detail=f"HTTP {resp.status_code} host={url_host(self.base_url)}")
        return resp.json() or {}

    async def upload_image(self, blob: bytes, filename: str) -> str:
        """上传参考图，返回 ComfyUI 侧文件名（LoadImage 引用它）。"""
        client = await self.client()
        try:
            resp = await client.post(
                f"{self.base_url}/upload/image",
                headers=self.headers(),
                files={"image": (filename, blob, "application/octet-stream")},
                data={"overwrite": "true"},
            )
        except httpx.HTTPError as e:
            raise AdapterError(f"上传参考图到 ComfyUI 失败：{e}", log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            raise AdapterError(f"上传参考图失败：HTTP {resp.status_code} {resp.text[:200]}", log_detail=f"upload_ref HTTP {resp.status_code} body={len(resp.content)}B host={url_host(self.base_url)}")
        data = resp.json() or {}
        return str(data.get("name") or filename)

    async def submit_workflow(self, graph: dict[str, Any]) -> str:
        """提交工作流（API 格式 JSON），返回 prompt_id。"""
        client = await self.client()
        body = {"prompt": graph, "client_id": uuid.uuid4().hex}
        try:
            resp = await client.post(
                f"{self.base_url}/prompt",
                headers=self.headers(),
                json=body,
            )
        except httpx.HTTPError as e:
            raise AdapterError(f"提交工作流失败：{e}", log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            message, log_detail = _describe_submit_error(resp)
            raise AdapterError(message, log_detail=log_detail)
        data = resp.json() or {}
        pid = data.get("prompt_id")
        if not pid:
            raise AdapterError(f"ComfyUI 未返回任务 ID：{str(data)[:200]}", log_detail=f"missing_prompt_id status=200 body={len(str(data))}B")
        return str(pid)

    async def poll_workflow(self, prompt_id: str) -> ComfyRunStatus:
        """查询工作流执行状态。

        三个必须按实际情况处理的行为（均已在官方 server.py 中核实）：
        1. `/history/{id}` 在**查不到该 prompt 时返回 200 + `{}`**，不是 404 → 视为仍在执行；
        2. `status.status_str == "error"` 才是失败，细节在 `status.messages` 的
           `execution_error` 事件里（含 node_id / exception_message）；
        3. `outputs` 里只有**带 UI 输出的节点**才有条目，且产物类型必须按**文件扩展名**判定：
           核心 ComfyUI 的 SaveImage / SaveVideo / SaveWEBM / SaveAnimatedWEBP 全部返回
           `{"images": [...]}` 同一个键，靠键名区分会把 mp4 当图片。
        """
        client = await self.client()
        try:
            resp = await client.get(
                f"{self.base_url}/history/{prompt_id}", headers=self.headers()
            )
        except httpx.HTTPError as e:
            # 网络抖动按处理中处理
            return ComfyRunStatus(status="processing", error=str(e))
        if resp.status_code == 404:
            return ComfyRunStatus(status="processing")
        if resp.status_code != 200:
            return ComfyRunStatus(status="processing", error=f"HTTP {resp.status_code}")
        data = resp.json() or {}
        entry = data.get(prompt_id)
        if not entry:
            return ComfyRunStatus(status="processing")

        status_info = entry.get("status") or {}
        if status_info.get("status_str") == "error":
            return ComfyRunStatus(status="failed", error=_describe_failure(status_info))

        outputs = entry.get("outputs") or {}
        files: list[ComfyOutputFile] = []
        unsupported: list[str] = []
        for _node_id, out in outputs.items():
            if not isinstance(out, dict):
                continue
            # 扫该节点的**所有** ui 键而不是只认 images/gifs/videos：
            # 键名随节点而异（images / gifs / videos / audio / latents…），
            # 少扫一个键就会让任务一直轮询到超时。类型一律按扩展名判定。
            for _slot, items in out.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    fname = str(item.get("filename") or "")
                    if not fname:
                        continue
                    kind = classify_artifact(fname)
                    if kind == "other":
                        unsupported.append(fname)
                        continue
                    files.append(
                        ComfyOutputFile(
                            filename=fname,
                            subfolder=str(item.get("subfolder") or ""),
                            # type 必须原样用回传值：PreviewImage 落在 temp 目录，
                            # 硬编码 output 会 404
                            folder_type=str(item.get("type") or "output"),
                            kind=kind,
                        )
                    )
        if not files:
            if unsupported:
                return ComfyRunStatus(
                    status="failed",
                    error=(
                        "工作流跑完了，但产物类型暂不支持归档："
                        f"{'、'.join(unsupported[:5])}（目前只归档图片与视频）"
                    ),
                    unsupported=unsupported,
                )
            # 已进 history 但还没有产物 → 可能刚入队，继续等
            return ComfyRunStatus(status="processing")
        return ComfyRunStatus(status="succeeded", files=files, progress=100)

    async def download_output(self, file: ComfyOutputFile) -> bytes:
        """下载产物文件内容。"""
        client = await self.client()
        params: dict[str, str] = {
            "filename": file.filename,
            "subfolder": file.subfolder,
            "type": file.folder_type,
        }
        try:
            resp = await client.get(
                f"{self.base_url}/view", headers=self.headers(), params=params
            )
        except httpx.HTTPError as e:
            raise AdapterError(f"下载工作流产物失败：{e}", log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            raise AdapterError(f"下载产物失败：HTTP {resp.status_code} {file.filename}", log_detail=f"download_output HTTP {resp.status_code}")
        return resp.content
