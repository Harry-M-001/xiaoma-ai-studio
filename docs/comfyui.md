# ComfyUI 工作流接入细节

> 调研日期：2026-09-15
> 证据等级说明：**[源码]** = 本机安装的 ComfyUI 服务端源码（`server.py` / `execution.py` / `comfy_api/latest/_ui.py` / `comfy_extras/nodes_video.py`，逐行读过）；**[官方文档]** = docs.comfy.org；**[社区]** = 第三方博客 / 项目源码。
> 结论按「源码 > 官方文档 > 社区」的优先级采信；冲突处以源码为准并标注。

---

## 1. 结论摘要

ComfyUI 的接入面比想象中窄：**一个 POST 提交 + 一个 GET 查历史 + 一个 GET 下载文件**就能跑通全流程，参数注入本质上只是「改 JSON 字段」。真正的复杂度集中在三处，也是踩坑最密集的地方：

1. **两种 JSON 格式必须分清**。只有 `workflow_api.json`（「导出（API）」）能提交，`workflow.json`（Ctrl+S 保存）提交必失败。
2. **产物类型不能靠 ui 键名判断**。核心 ComfyUI 的 `SaveImage`、`SaveVideo`、`SaveWEBM`、`SaveAnimatedWEBP` 全部把文件放在 `outputs` 的 **`images`** 键里，只看键名会把 mp4 当图片归档，必须按**文件扩展名**判定。
3. **错误分两层两套结构**。`/prompt` 只做校验（400 + `node_errors` 按节点报错），真正的执行异常记在 `/history` 的 `status.messages` 里（`execution_error` 事件）。只检查 HTTP 状态码会把「跑完了但失败了」当成成功。[源码]

此外有三条容易造成「接口看着正常但请求被拒」的环境约束：CSRF 的 Origin 校验中间件、默认 100 MB 的上传上限、以及 `/prompt` 里 `client_id` 与 `/ws` 的 `clientId` 必须完全一致才能收到进度事件。[源码]

---

## 2. 工作流文件：两种格式

ComfyUI 会导出两种 JSON，用途完全不同。[官方文档]

| 维度 | `workflow.json`（界面格式） | `workflow_api.json`（API 格式） |
| --- | --- | --- |
| 导出方式 | 菜单「文件 → 保存」/ Ctrl+S | 菜单「工作流 → 导出（API）」 |
| 顶层结构 | `{nodes: [...], links: [...], groups: [...], version}` | `{"<节点id>": {"class_type": str, "inputs": {...}}}` |
| 键 | 节点标题/标签 | 数字节点 ID（JSON 里是**字符串** key，如 `"3"`） |
| 布局信息 | 含 x/y/width、颜色、分组 | 不含 |
| 能否提交给 `/prompt` | **否** | **是** |
| 能否加载回界面 | 是 | 可以，但没有布局 |

**节点间引用的语义**：`"positive": ["6", 0]` 表示「取 id 为 `6` 的节点的**第 0 号输出**」。输出索引由节点的 `RETURN_TYPES` 顺序决定，例如 `CheckpointLoaderSimple` 是 0=`MODEL`、1=`CLIP`、2=`VAE`，所以 `"clip": ["4", 1]`、`"vae": ["4", 2]`。DAG 拓扑隐含在这些引用里，服务端自行推导执行顺序，接入方不需要重算拓扑。[官方文档]

需要强调的一点：**API 格式的键是字符串，"数字节点 ID"是文档的通俗说法**。若用 Python 迭代 `graph.keys()` 拿到的是 `"3"` 而不是 `3`。[社区]

---

## 3. 服务端接口清单

本机 `server.py` 中实际注册的路由（`@routes.get/post`）与接入相关的部分：[源码]

| 端点 | 方法 | 用途 | 关键点 |
| --- | --- | --- | --- |
| `/prompt` | POST | 提交工作流 | 200 返回 `{prompt_id, number, node_errors}`；校验失败 400 返回 `{error, node_errors}` |
| `/prompt` | GET | 队列剩余数 | 返回 `{exec_info: {queue_remaining}}` |
| `/history/{prompt_id}` | GET | 查执行结果 | **查不到时返回 200 + `{}`，不是 404** |
| `/history` | GET | 批量历史 | `max_items` / `offset`，`offset<0` 时取尾部 N 条 |
| `/view` | GET | 下载产物 | `filename` 必需；`subfolder`、`type`、`preview`、`channel` 可选 |
| `/upload/image` | POST | 上传参考图 | multipart，字段 `image`、`type`、`subfolder`、`overwrite` |
| `/upload/mask` | POST | 上传遮罩 | 额外需要 `original_ref` |
| `/object_info` | GET | 全部节点定义 | 参数表单 options 的来源 |
| `/object_info/{class}` | GET | 单节点定义 | **未知节点返回 200 + `{}`**，不是 404 |
| `/queue` | GET | 队列快照 | `{queue_running, queue_pending}` |
| `/queue` | POST | 清队列/删待执行 | `{clear: true}` 或 `{delete: [prompt_id]}`；**删不掉正在运行的任务** |
| `/interrupt` | POST | 中断 | `{}` 全局中断；`{prompt_id}` 定向中断（仅当该任务在运行才生效） |
| `/system_stats` | GET | 探活与版本 | 含 `system`（版本/内存/python）与 `devices`（显存），**主设备在 `devices[0]`** |
| `/ws?clientId=` | WS | 实时进度 | 消息信封统一为 `{type, data}`，服务端连接后立即下发 `status` |

**`/api` 前缀双路径**：`add_routes()` 会为每条路由自动再注册一份 `/api` + path 的别名，注释明确写「新旧端点同时支持」。所以 `/prompt` 与 `/api/prompt` 等价。[源码]

**迁移方向**：官方云 API 文档已把 `/history` 系列标记为 Deprecated、建议改用 `/api/jobs`；本地自托管版本 `/history` 仍正常且无弃用警告，但新增的 `/api/jobs`（列表/详情/取消）在开源版本里也已经存在，属未来方向。[源码][官方文档]

### 3.1 `/prompt` 的请求体

```json
{
  "prompt": { "<node_id>": {"class_type": "...", "inputs": {...}} },
  "client_id": "<与 /ws?clientId 完全一致>",
  "prompt_id": "<可选，自定义任务 id>"
}
```

`client_id` 是**纯路由键**：`/prompt` 里的值会写进 `extra_data["client_id"]`，执行期的进度事件按它投递给对应的 WebSocket 连接。**两者不一致就收不到 `executing` / `progress` / `executed` 消息**（非广播事件被静默丢弃），这是官方示例必须传 `client_id` 的原因。[源码]

`prompt_id` 若自定义，必须是**规范小写连字符形式的 UUID**，否则 400 `invalid_prompt_id`；不传或传 `null` 由服务端生成。[源码]

### 3.2 响应与错误

成功（200）：

```json
{"prompt_id": "<uuid>", "number": 42, "node_errors": {}}
```

校验失败（400）：

```json
{
  "error": {"type": "prompt_outputs_failed_validation", "message": "...", "details": "...", "extra_info": {}},
  "node_errors": {
    "5": {"class_type": "KSampler", "errors": [{"type": "...", "message": "...", "details": "..."}],
          "dependent_outputs": ["9"]}
  }
}
```

`error.type` 的已知取值包括 `no_prompt`、`invalid_prompt_id`、`prompt_no_outputs`、`missing_node_type`、`prompt_outputs_failed_validation`、`dependency_cycle`。[源码]

**`node_errors` 是最有价值的字段**：它把「哪个节点、哪个输入、为什么不合法」说清楚了。接入方应当解析它再展示，直接把原始 JSON 抛给用户等于没说。上游节点报错时，下游节点会出现在 `dependent_outputs` 里但自身 `errors` 为空。[源码]

> 注意：`/prompt` 返回 200 只代表**通过校验并进入队列**，不代表执行成功。

---

## 4. 产物定位

这是接入中最容易做错的一环。

### 4.1 `outputs` 的结构

`/history/{prompt_id}` 的条目形如：

```
{
  "<prompt_id>": {
    "prompt": [number, prompt_id, prompt, extra_data, outputs_to_execute],
    "outputs": {"<node_id>": <该节点的 UI 输出字典>},
    "status": {"status_str": "success"|"error", "completed": bool, "messages": [[event, data], ...]}
  }
}
```

- **只有带 UI 输出的节点**才会出现在 `outputs` 里。`KSampler`、`VAEDecode` 这类中间节点不会出现——它们是 `result` 而非 `ui`。[源码]
- `status.messages` 是执行期 WebSocket 事件的累积，每条带毫秒时间戳。[源码]

### 4.2 ui 键的真实分布

本机源码逐处核对的结果：[源码]

| 节点 | ui 键 | 说明 |
| --- | --- | --- |
| `SaveImage` / `PreviewImage` | `images` | `PreviewImage` 落在 **temp** 目录 |
| `SaveVideo` | `images` | `PreviewVideo.as_dict()` 返回 `{"images": [...], "animated": (True,)}` |
| `SaveWEBM` | `images` | 同上 |
| `SaveAnimatedWEBP` / `SaveAnimatedPNG` | `images` | `SavedImages.as_dict()`，animated 时带 `animated` 标志 |
| `SaveLatent` | `latents` | |
| `SaveAudio*` | `audio` | `SavedAudios.as_dict()` |
| `VHS_VideoCombine`（第三方 VideoHelperSuite） | `gifs` | mp4/webm 也放在这个槽位 |

**结论：除音频与 latent 外，几乎所有媒体产物都走 `images` 这一个键。** 因此**不能靠键名判断产物类型**，必须按文件扩展名判定。这是本条调研中最具实操价值的一条。

每一项的结构一致：

```json
{"filename": "ComfyUI_00001_.png", "subfolder": "video", "type": "output"}
```

- `type` ∈ `output` / `temp` / `input`，**必须原样回传**给 `/view`。硬编码 `type=output` 会让 `PreviewImage` 的产物 404。
- `subfolder` 是相对根目录的路径，可能为空串。

### 4.3 下载

```
GET /view?filename=<filename>&subfolder=<subfolder>&type=<type>
```

`/view` 的安全细节：`filename` 以 `/` 开头或含 `..` → 400；`subfolder` 越出根目录 → 403；`type` 非法 → 400；带危险 MIME（HTML/JS/SVG 等）会被强制 `application/octet-stream` + `attachment` 并加 `nosniff`。[源码]

---

## 5. 参数注入的两条路线

ComfyUI 没有「参数映射」这一官方抽象，只有「改 JSON 字段」。官方示例是最直接的做法：`workflow[node_id]["inputs"][input_name] = value`。[官方文档]

| 路线 | 做法 | 优点 | 风险 |
| --- | --- | --- | --- |
| 按 `node_id` 直改 | 上传时扫描图、生成 `{参数 → node_id+field}` 映射表 | 简单、显式、可展示成表单 | **节点 id 会在重排/增删/重新导出后变化**，硬编码 id 会静默改错对象或 KeyError |
| 按 `_meta.title` 反查 | 用节点标题定位 id 再改 | 对重导出更稳 | 需处理「同名多节点」；标题被用户改动即失效 |

社区共识是：**不要硬编码 id**，若用标题反查则必须处理同名冲突。[社区]

本项目的取舍：**在「上传时解析一次」的快照上固化 id**，把解析结果存库；同时提供「重新解析」按钮，用户重新导出工作流后可以刷新映射表。这样既保留了参数表单的易用性，又把 id 漂移的风险交给显式操作，而不是藏在运行期。

另一条需要知道的边界：**前端的「启用/禁用/bypass」不会体现在 API 格式里**，因此 API 提交时的「剪枝」需要脚本自己删节点。[社区]

### 5.1 常用节点的 inputs 字段

| class_type | inputs |
| --- | --- |
| `KSampler` | `seed, steps, cfg, sampler_name, scheduler, denoise, model, positive, negative, latent_image` |
| `KSamplerAdvanced` | 上述基础上换成 `noise_seed / add_noise / start_at_step / end_at_step / return_with_leftover_noise` |
| `CLIPTextEncode` | `text, clip` |
| `CheckpointLoaderSimple` | `ckpt_name` |
| `LoraLoader` | `model, clip, lora_name, strength_model, strength_clip` |
| `VAELoader` | `vae_name` |
| `EmptyLatentImage` | `width, height, batch_size` |
| `LoadImage` | `image`（input 目录下的文件名） |
| `SaveImage` | `images, filename_prefix` |
| `CreateVideo` | `images, fps, audio(选填), bit_depth` |
| `SaveVideo` | `video, filename_prefix, format, codec` |
| `SaveWEBM` | `images, filename_prefix, codec, fps, crf` |
| `SaveAnimatedWEBP` | `images, filename_prefix, fps, lossless, quality, method` |
| `VHS_VideoCombine` | `images, frame_rate, loop_count, filename_prefix, format, pingpong, save_output` |

一个易混点：**核心节点用 `fps`，第三方 VideoHelperSuite 用 `frame_rate`**。只认其中一个就会漏掉另一类工作流的帧率参数。[源码][社区]

权威做法是用 `GET /object_info/{class}` 取节点的 required/optional 定义，而不是照文档抄字段名。[官方文档]

### 5.2 参考图上传

两段式：[官方文档]

1. `POST /upload/image`（multipart：`image` 文件、`type=input`、`overwrite=true`）→ 返回 `{name, subfolder, type}`
2. 把返回的 `name` 写回 `LoadImage.inputs.image`

细节：同名时会比对**文件内容哈希**，完全相同则复用原文件名并标记 `image_is_duplicate`；`overwrite` 只在传 `"true"`/`"1"` 时生效，否则自动改名 `name (1).ext`。[源码]

---

## 6. 错误处理：两层校验

| 层 | 位置 | 结构 | 接入方要做的 |
| --- | --- | --- | --- |
| 提交校验 | `/prompt` 400 | `{error, node_errors}` | 解析 `node_errors`，按「节点 + 输入 + 原因」展示 |
| 执行异常 | `/history` 的 `status.status_str == "error"` | `status.messages` 里的 `execution_error` | 取 `node_id / node_type / exception_message` 展示 |

常见失败模式：

- **`Prompt has no outputs`**：图里没有输出节点，或输出节点的输入断线。绝大多数是忘了放/忘了连保存节点。[社区]
- **`SaveImage` 的 `images` 未连线**：节点会**静默执行、不报错、不写盘**，`outputs` 自然为空。属于最难查的失败模式，建议接入方额外做「输出目录是否出现新文件」的兜底校验。[社区]
- **显存不足**：`torch.cuda.OutOfMemoryError`，客户端侧表现为任务长时间不完成或 `execution_error`。降级顺序：先降分辨率/批量/帧数 → `--reserve-vram` / `--lowvram` / `--cpu` → 换量化版（GGUF / fp8）。[社区]

---

## 7. 实时进度：WebSocket

连接 `ws://host:8188/ws?clientId=<id>`，服务端连接后**立即下发** `status`。消息信封统一为 `{"type": <event>, "data": {...}}`；预览图是**二进制帧**，与文本事件混在同一连接上。[源码]

关键事件：[官方文档][源码]

| type | data 关键字段 |
| --- | --- |
| `status` | `status.exec_info.queue_remaining` |
| `execution_start` | `prompt_id` |
| `execution_cached` | `prompt_id`, `nodes` |
| `executing` | `node`, `display_node`, `prompt_id`（**`node == null` 表示整个 prompt 结束**） |
| `progress` | `node`, `value`, `max` |
| `executed` | `node`, `output`（与 `outputs` 同源） |
| `execution_error` | `node_id`, `node_type`, `exception_message`, `traceback` |
| `execution_interrupted` | `broadcast=true`，无 client_id 也能收到 |

**WebSocket 与轮询的取舍**：WebSocket 能拿到细粒度进度（`progress` 的 value/max）且不必轮询；但需要维护长连接与断线重连，且 `client_id` 必须与提交时一致。轮询 `/history` 实现简单、天然幂等、对断线免疫，代价是进度粒度粗（只能靠「产物是否出现」判断完成）。本项目选择轮询，因为它是单机低频场景，稳定性优先于进度平滑度。

---

## 8. 环境与安全约束

三条容易造成「接口正常但请求被拒」的行为，均在 `server.py` 中核实：[源码]

1. **CSRF 的 Origin 校验中间件**：请求头带 `Sec-Fetch-Site: cross-site` 直接 **403**；Host 为 loopback 时，`Origin` 与 `Host` 的主机名不一致也 403。这是为了阻止网页向本机 `127.0.0.1` 发起 POST 排队工作流。服务端到服务端调用（如本项目用 httpx）不受影响，但**浏览器直连会踩到**。
2. **上传上限**：`client_max_size` 由 `--max-upload-size` 控制，默认 **100 MB**，超限被 aiohttp 拒绝。
3. **CORS 默认关闭**：仅在启动加 `--enable-cors-header` 时启用。响应体 gzip 需 `--enable-compress-response-body` 且仅对 JSON/text。

历史容量：`MAXIMUM_HISTORY_SIZE = 10000`，超出丢弃最旧一条。[源码]

---

## 9. 常见坑清单

| # | 坑 | 表现 | 规避 |
| --- | --- | --- | --- |
| 1 | 提交了 `workflow.json`（界面格式） | 400 `missing_node_type` / `prompt_no_outputs` | 只用「导出（API）」的产物；解析前校验 `{节点id: {class_type, inputs}}` |
| 2 | 按 ui 键名判断产物类型 | mp4/webm 被归档成图片，下游视频节点收不到 | **按文件扩展名判定** |
| 3 | 硬编码 `type=output` 下载 | `PreviewImage` 的产物 404 | `type` 原样用 `/history` 回传值 |
| 4 | 只检查 HTTP 200 | 「跑完了但失败」被当成功，产物为空 | 两层都查：`/prompt` 的 `node_errors` + `/history` 的 `status_str` |
| 5 | 把 `/history` 的 404 当「进行中」 | 404 不会出现，逻辑分支为空 | 官方对未知 id 返回 **200 + `{}`** |
| 6 | `/prompt` 与 `/ws` 的 client_id 不一致 | 收不到任何进度事件（静默丢弃） | 两者用同一个值；或干脆走轮询 |
| 7 | 节点 id 在重导出后漂移 | 改错参数或 KeyError | 上传时解析成映射表 + 提供「重新解析」 |
| 8 | 只认 `frame_rate` 或只认 `fps` | 视频工作流的帧率参数不出现 | 两个都认 |
| 9 | 输出节点输入断线 | 静默不写盘，`outputs` 为空 | 提交前用 `/object_info` 校验必填输入 |
| 10 | 忽略 Origin 中间件 | 浏览器侧 403 | 服务端调用不受影响；浏览器直连需注意 |

---

## 10. 本项目实现的对照结论

对照上述结论核查 `backend/app/providers/comfyui.py` 与 `backend/app/services/comfy_workflow_service.py`，发现并修复了 3 个真实缺陷：

| # | 缺陷 | 影响 | 修复 |
| --- | --- | --- | --- |
| 1 | `SaveWEBM` 被列入 `_IMAGE_OUTPUT_CLASSES` | 纯 webm 工作流被预判为图片 | 归入视频输出集合 |
| 2 | 产物类型由 ui 键名推导，而 `SaveVideo` / `SaveWEBM` / `SaveAnimatedWEBP` 都走 `images` 键 | **视频产物被归档为 `kind="image"`**，下游视频节点收不到，前端把视频当图片渲染 | 新增 `classify_artifact()` 按扩展名判定，作为单一事实来源 |
| 3 | 视频参数只认 `frame_rate` | 核心节点（`SaveVideo` / `SaveWEBM` / `CreateVideo`）的 `fps` 参数在浮框里不出现 | `fps` 与 `frame_rate` 同时识别 |

顺带做了两处可读性改进：提交失败时解析 `/prompt` 的 `node_errors` 给出「节点 X（KSampler）：字段 Y 原因」级别的提示；执行失败时从 `status.messages` 提取 `node_id / node_type / exception_message`。此外，产物扫描从「只认 `images`/`gifs`/`videos` 三个键」改为「扫描该节点的全部 ui 键」，避免纯音频等工作流一直轮询到超时。

**已核实正确的实现点**：`/history` 未知 id 返回 200 + `{}` 的容错分支、`type` 原样回传、`subfolder` 透传、seed 空值随机语义、`/system_stats` 探活。

守这些行为的测试：`backend/tests/test_comfy_adapter.py`（13 项）与 `backend/tests/test_comfy_parse.py`（9 项）。

---

## 11. 未验证与待确认

以下内容本次未能取证或未做实测，属于已知的空白，不应作为实现依据：

- **未做真实出图验证**：本机 ComfyUI 的模型目录为空（`checkpoints` / `diffusion_models` / `unet` 均为 0 个文件），无法跑通一次真实生成，因此「参数注入 → 真实产物」这一段是逻辑验证而非端到端验证。
- **`/prompt` 的具体校验错误码集合未穷尽**：`error.type` 与 `node_errors[].errors[].type` 的枚举值只确认了一部分，未找到官方完整清单。
- **`VHS_VideoCombine` 无官方文档**：其 inputs 与 `gifs` 槽位行为仅有社区文档与源代码佐证。
- **`/api/jobs` 的完整字段未核实**：已知其在开源版本中存在，但本次未逐字段验证。
- **Toonflow 的直连细节**：其公开仓库中的工作流以业务表数据（`flowData`）形式存在，未定位到静态的 ComfyUI workflow JSON 模板；「编排层与 ComfyUI 后端对接」这一环节目前只有社区桥接仓库佐证。

---

## 12. 参考来源

**本机安装副本（一手源码，抓取日 2026-09-15）**

- [1] `server.py` — 路由注册、`/prompt`、`/history`、`/view`、`/upload/image`、`/object_info`、`/queue`、`/interrupt`、`/system_stats`、`/ws`、Origin 中间件
- [2] `execution.py` — `get_history()` 的未知 id 行为、`HistoryEntry` 构造、`MAXIMUM_HISTORY_SIZE`
- [3] `comfy_api/latest/_ui.py` — `PreviewVideo.as_dict()` / `SavedImages.as_dict()` 的 ui 键与 `SavedResult` 结构
- [4] `comfy_extras/nodes_video.py` — `SaveWEBM` / `SaveVideo` / `CreateVideo` 的 inputs 与返回
- [5] `comfy_extras/nodes_images.py` — `SaveAnimatedWEBP` / `SaveAnimatedPNG`
- [6] `nodes.py` — `SaveImage` / `PreviewImage` / `SaveLatent` 的 ui 返回

**官方文档**

- [7] ComfyUI API 格式说明：https://docs.comfy.org/zh/development/api-development/workflow-api-format
- [8] 服务端路由与通信：https://docs.comfy.org/development/comfyui-server/comms_routes
- [9] API 调用示例：https://docs.comfy.org/development/comfyui-server/api-examples
- [10] WebSocket 消息类型：https://docs.comfy.org/zh/development/comfyui-server/comms_messages
- [11] 工作流 JSON 规范：https://docs.comfy.org/specs/workflow_json
- [12] `KSampler` 节点定义：https://docs.comfy.org/built-in-nodes/KSampler.md
- [13] `CreateVideo` 节点定义：https://docs.comfy.org/built-in-nodes/CreateVideo
- [14] 官方仓库：https://github.com/comfyanonymous/ComfyUI

**社区实践**

- [15] ComfyUI API 开发者指南（节点 id 漂移 / 引用语义）：https://www.runflow.io/blog/comfyui-api-developer-guide
- [16] 按 `_meta.title` 反查节点、bypass 不进 API 格式：https://gist.github.com/memoakten/3c51abce7af74ce779026ca48f59b9a1
- [17] 「Prompt has no outputs」成因：https://aibudwp.com/prompt-has-no-outputs-error-in-comfyui-fix-it-fast/
- [18] `SaveImage` 未连线静默失败：https://theneuralbase.com/comfyui/learn/beginner/save-image/
- [19] 显存不足的降级顺序：https://blog.csdn.net/gitblog_00780/article/details/159779018
- [20] `VHS_VideoCombine` 参数：https://www.runcomfy.com/comfyui-nodes/ComfyUI-VideoHelperSuite/VHS_VideoCombine
- [21] Toonflow 参考图与工作流编排（源码 `getImageFlow.ts` / `generateFlowImage.ts`）：https://github.com/HBAI-Ltd/Toonflow-app
