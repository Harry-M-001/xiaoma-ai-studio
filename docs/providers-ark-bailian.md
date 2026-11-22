# 火山方舟 Ark 与阿里百炼 DashScope 适配说明

本文说明这一版对两家供应商做了哪些适配、各模型的输入输出能力，以及接入后怎么用。

> 依据：2026-11-20 / 2026-11-21 的官方文档（火山方舟模型列表与图片、视频生成 API；阿里云百炼
> 万相 2.7 / 3.0、可灵 3.0 / 4.0 API 参考）。模型版本号会继续更新，预设里的 ID 过期后到控制台
> 复制当前版本即可。**这次没有真实 Key，未做真机出图/出片**；接口形状按官方文档实现，
> 并由单元测试覆盖（见 `backend/tests/test_ark_bailian.py`）。

## 一句话结论：接入后能直接用吗？

能。在「模型服务 → 添加服务」里选预设、填 Key、点「测试连接」，勾选要用的模型后，
图片页 / 视频页 / 文本对话里就能直接选到它们，不需要写代码。

前提只有两条：

1. 该模型已经在供应商控制台**开通**（方舟按模型开通，百炼的可灵还要单独开通「可灵AI」）；
2. 账号有**余额 / 额度**（Seedance 2.5 与 2.0 系列要求余额 > 200 元或购买资源包）。

失败时我们会在本地先拦掉能判的错（尺寸、时长、素材数量、参考音频格式等），
剩下的上游错误原样翻译成中文提示，不会静默降级。

## 一、火山方舟 Ark

- 服务类型：**火山方舟 Ark**（协议名 `ark`）
- Base URL：`https://ark.cn-beijing.volces.com/api/v3`
- API Key：`ark-<uuid>-<尾码>`（老账号的裸 UUID 也认）
- 模型名：必须填**具体版本 ID**，不是「豆包视频」这种别名

### 已适配模型

| 模型 ID | 能力 | 我们做了什么 | 限制 / 说明 |
| --- | --- | --- | --- |
| `doubao-seed-2-1-pro-260915` | 文本、多模态理解、深度思考 | 走 OpenAI 兼容 `/chat/completions`，支持流式 | 256K 上下文 |
| `doubao-seed-2-1-lite-260915` | 文本、多模态理解 | 同上 | 256K 上下文，更便宜 |
| `doubao-seedream-5-5-261028` | 文生图、图生图、图层拆分、RGBA | 走 `/images/generations`；`size` 支持 `1K/1.5K/2K` 或 `宽x高`；参考图 1–10 张（base64） | 一次请求只出 1 张图，界面选 N 张会发 N 次请求 |
| `doubao-seedream-5-0-pro-260628` | 文生图、图生图、精准编辑 | 同上 | 单图；参考图最多 10 张 |
| `doubao-seedream-5-0-lite-260128` | 文生图、图生图、组图 | 同上 | 支持 2K/3K/4K 档位 |
| `doubao-seedance-2-5-260628` | 文生、首帧、首尾帧、全模态参考、编辑、延长 | 走 `/contents/generations/tasks`；参考图 0–30、参考视频 0–10、参考音频 0–10；任务轮询 | 4–30 秒；480p/720p/1080p/4k；参考音频可单独输入 |
| `doubao-seedance-2-5-flash-261115` | 同上（更快、更便宜） | 同上 | 480p/720p；参考上限 10 图 + 5 视频 + 5 音频 |
| `doubao-seedance-2-0-260128` | 文生、首帧、首尾帧、全模态参考 | 同上 | 4–15 秒左右；参考图 0–9、视频 0–3、音频 0–3 |
| `doubao-seedance-1-0-pro-250528` | 文生、首帧、首尾帧 | 保留兼容 | 不支持参考音频，旧项目继续可用 |

### 视频请求的素材角色（适配器已按官方字段拼好）

- 首帧：`content[].type=image_url`，`role=first_frame`
- 尾帧：`role=last_frame`（必须和首帧一起传）
- 参考图：`role=reference_image`
- 参考视频：`type=video_url`，`role=reference_video`
- 参考音频：`type=audio_url`，`role=reference_audio`
- 顶层参数：`duration`（秒）、`ratio`、`resolution`

本地能判的先判：参考音频只收 wav/mp3、单个不超过 15 MB；带参考音频却被 400 拒绝时，
提示会直接告诉你是哪几档模型认音频（Seedance 1.5 pro / 2.0 / 2.5）。

图片尺寸也会先对齐模型下限：Seedream 5.5 / 5.0 pro 最低 1280×720（约 92 万像素），
5.0 Lite / 4.5 最低 2K（约 369 万像素）。界面给的小尺寸会**按原比例**抬到下限，
不会因为一句「参数不合法」让用户猜。

## 二、阿里百炼 DashScope

- 服务类型：**阿里百炼（图片/视频）**（协议名 `dashscope`）
- Base URL：`https://dashscope.aliyuncs.com/api/v1`（现有域名仍可正常使用）
  - 新业务空间域名：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1`
  - 地域必须一致：模型、地址、Key 三者同地域，否则鉴权失败
- API Key：百炼控制台的 `sk-…`
- 文本模型不在这条服务里：另建一条「通义千问」OpenAI 兼容服务

### 已适配模型

| 模型 ID | 能力 | 我们做了什么 | 限制 / 说明 |
| --- | --- | --- | --- |
| `wan2.7-image-pro` | 文生图、参考图编辑、组图 | 异步 `/services/aigc/image-generation/generation` → `/tasks/{id}` 轮询；`size` 传 `宽*高` 或档位；`n` 控制张数 | 文生图最高 4K；编辑/组图最高 2K |
| `wan2.7-image` | 同上（更快） | 同上 | 参考图 0–9 张，支持 base64 |
| `wan3.0-video` | 文生、首帧/首尾帧、全能参考、有声 | 异步 `/services/aigc/video-generation/video-synthesis`；`media` 支持 `first_frame` / `last_frame` / `reference_image`；时长夹到 2–30 秒 | 480P/720P/1080P；ratio 支持 `adaptive`；参考视频/音频只收公网 URL |
| `wan3.0-video-prime` | 同上（资源紧张时可能按标准速率完成） | 同上 | 同上 |
| `wan2.7-t2v` | 文生视频 | 同上 | 2–15 秒；720P/1080P |
| `wan2.7-i2v` | 首帧 / 首尾帧 / 续写 / 音频驱动 | 同上 | 2–15 秒 |
| `wan2.7-r2v-2026-06-12` | 参考图生视频（多角色） | 同上 | 2–10 秒 |
| `kling/kling-v4-video-generation` | 文生、关键帧（首/中/尾任意组合） | 首帧 / 尾帧 / 参考图走 `media`；时长 3–30 秒；`mode` 仅 std/pro | 固定有声；不支持 4k；帧图合计 ≤ 10 张 |
| `kling/kling-v4-omni-reference-video-generation` | 关键帧 + 参考图/视频 + 主体 + 音色 | 同上 | 图片 ≤ 10、视频 ≤ 5、主体 ≤ 7；固定有声 |
| `kling/kling-v3-video-generation` | 文生、首帧、首尾帧 | 同上 | 3–15 秒；std(720P)/pro(1080P)/4k；画幅只有 16:9/9:16/1:1 |
| `kling/kling-v3-omni-video-generation` | 首尾帧、参考生视频、视频编辑 | 同上 | 3–15 秒 |
| `kling/kling-v3-turbo-video-generation` | 文生、首帧 | 同上；尾帧会在本地拦下并提示换模型 | 不支持尾帧 |

### 百炼的三条硬边界（已写进适配器）

1. **参考视频 / 参考音频只收公网 URL 或 OSS 临时地址**，不收 base64。
   本机工作台的素材在本地磁盘上，所以这两类参考会在提交前直接报错并给出替代方案，
   不会静默丢掉素材。
2. **参考图收 base64**（`data:image/png;base64,...`），本地图片可以直接用。
3. **可灵只在华北2（北京）提供**，且需要先在控制台开通「可灵AI」；跨地域调用会失败。

> 可灵的官方文档只写了图片 URL、没写 base64。适配器仍按 base64 data URI 发送
> （这是本地文件唯一能走的形式）；如果上游明确拒绝，请改用 `wan3.0-video`——
> 它的文档明确支持 base64 图片。

## 三、怎么接入（两条服务）

1. 打开「模型服务 → 添加服务」。
2. 火山方舟：点「火山方舟 Ark」预设 → 填 API Key → 测试连接 → 勾选文本 / 图片 / 视频模型。
3. 阿里百炼：
   - 图片 / 视频：点「阿里百炼（图片/视频）」预设 → 填 `sk-…` → 测试连接 → 勾选模型；
   - 文本：再点「通义千问」预设（`https://dashscope.aliyuncs.com/compatible-mode/v1`），
     勾选 `qwen4-max` / `qwen3.8-max` / `qwen3.8-flash`。
4. 回到「图片生成 / 视频生成」页，模型下拉里就能直接选到刚勾选的模型。

## 四、怎么验证（不用花钱也能先验一半）

1. **测试连接**：只校验 Base URL 与 Key，不产生生成费用。
2. **文本**：发一句「你好」，确认能流式返回。
3. **图片**：先出 1 张 1024×1024，确认返回的是图不是错误 JSON。
4. **视频**：先用最短时长（如 5 秒）+ 720p 跑一条，确认任务能提交并轮询到成片。
5. 失败时看「任务详情」里的上游原文：鉴权、未开通、余额不足、参数不合法都会原样保留。

## 五、官方文档

- 方舟模型列表：<https://www.volcengine.com/docs/82379/1330310>
- 方舟图片生成 API：<https://ark.volcengine.com/region:cn-beijing/docs/image-generation-api>
- 方舟视频生成 API：<https://docs.volcengine.com/docs/82379/1520757>
- 百炼模型列表（视频生成与编辑）：<https://help.aliyun.com/zh/model-studio/video-generate-edit-model/>
- 百炼万相 3.0 视频 API：<https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference>
- 百炼可灵视频 API：<https://help.aliyun.com/zh/model-studio/kling-video-generation-api-reference/>
- 百炼万相 2.7 图像 API：<https://help.aliyun.com/zh/model-studio/wan-image-generation-and-editing-api-reference>
