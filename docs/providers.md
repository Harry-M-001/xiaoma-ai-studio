# 模型服务接入指南

在「模型服务 → 添加服务」中配置。服务类型按用途选（OpenAI 兼容 / 火山方舟 Ark / 阿里百炼
DashScope / KIE / ComfyUI / 本机配音）：

- **OpenAI 兼容接口**：绝大多数国内外模型和中转网关都走这一协议。
- **火山方舟 Ark**：豆包系列模型（Seed 文本、Seedream 图片、Seedance 视频）使用方舟的签名与任务接口，需单独选择。
- **DashScope（百炼）**：阿里云百炼的万相 / 可灵图片、视频异步生成接口。
- **ComfyUI**：本机自建的 ComfyUI 服务，通过工作流节点调用（不需要 API Key）。

界面内置了下表服务商的预设，点击即可自动填充 Base URL 与常用模型；具体模型名/模型 ID 请以你在服务商控制台看到的为准（新版本发布后可能变化）。

## OpenAI 兼容类

| 服务商 | Base URL | 模型名示例 | 能力 |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` / `gpt-4o-mini` / `gpt-image-1` | 文本、图片 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | 文本、工具调用、思考模式、原生多模态 |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` / `32k` / `128k` | 文本 |
| 通义千问（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` / `qwen-turbo` / `qwen-max` | 文本 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` / `glm-4-flash` / `cogview-3-plus` | 文本、图片 |
| OpenRouter | `https://openrouter.ai/api/v1` | 控制台中的模型 ID，如 `anthropic/claude-3.5-sonnet` | 文本、图片 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `llama3.1` / `qwen2.5` 等已拉取的模型 | 文本 |

其他任何声明「兼容 OpenAI 协议」的网关 / 中转站：选择 OpenAI 兼容类型，填入对方提供的 Base URL（通常以 `/v1` 结尾）和 Key 即可。

> DeepSeek 当前只有 **V4.1 Flash**（2026-09-10 发布，2026-10-17 升级到 1017），
> API 名是 **`deepseek-flash`**：1M 上下文、原生多模态、支持工具调用与思考模式。
> 旧名 `deepseek-v4-flash` / `deepseek-v4-pro` / `deepseek-chat` / `deepseek-reasoner`
> 都只是兼容别名（V4 Pro 官方已计划下线），新配置请直接填 `deepseek-flash`。

## KIE（图片 / 视频 · 异步）

- **图片**：Seedream 5.x / 4.5 用 `aspect_ratio` + 档位，Nano Banana 2 用 `image_input`，GPT Image 2 用 `input_urls`；老的四代模型仍是 `image_size` + `max_images`。
- **视频**：Seedance 2.0 / 2.5 与 Wan 3.0 支持三种互斥模式——纯文生、首帧（或首尾帧）、多模态参考（参考图 / 参考视频 / 参考音频）；Kling 4.0 关键帧支持首帧与尾帧。**首尾帧不能和参考素材同时出现**，尾帧必须跟首帧一起给；参考素材上限见各模型文档（我们会在提交前拦住）。
- 参考素材先换成公网 URL：图片/音频走 base64 上传，参考视频走流式上传。

- Base URL：`https://api.kie.ai`，类型选「KIE 异步任务（图片/视频）」。
- 图片模型（`createTask` 统一异步接口）：
  - `seedream/5-flash-text-to-image` / `seedream/5-flash-image-to-image`（便宜、快）
  - `seedream/5-pro-text-to-image` / `seedream/5-pro-image-to-image`（质量最高）
  - `seedream/5-lite-text-to-image`、`seedream/4.5-text-to-image`
  - `nano-banana-2`（文生/图生同一个模型）
  - `gpt-image-2-text-to-image` / `gpt-image-2-image-to-image`
- 视频模型：
  - `bytedance/seedance-2-5`：文生 / 首帧，4–30 秒，480p/720p/1080p
  - `bytedance/seedance-2`：同上，价格更低
  - `kling-4.0/keyframe-to-video`：**必须给首帧**，没有纯文生
  - `wan/3-0-video`：文生 / 首帧，分辨率用大写档位（480P/720P/1080P）
- 两代入参形状不同：Seedream 4.0 / Kling 2.5 用 `image_size` / `image_url`；
  新一代用 `aspect_ratio` / `first_frame_url`，字段名不能互相套用。
- 参考图 / 首帧会先上传到 KIE 的临时空间换成 URL（24 小时后删除）。

## 火山方舟 Ark（豆包文本 / 图片 / 视频）

- Base URL：`https://ark.cn-beijing.volces.com/api/v3`
- API Key：在[火山方舟控制台](https://console.volcengine.com/ark)创建的 API Key，形如 `ark-<uuid>-<尾码>`（老账号可能还是裸 UUID，两种都认）
- 模型名：填写你开通的**具体模型版本 ID**（预设里已是当前版本）：
  - 文本：`doubao-seed-2-1-pro-260915` / `doubao-seed-2-1-lite-260915`
  - 图片：`doubao-seedream-5-5-261028`（Seedream 5.5）、`doubao-seedream-5-0-pro-260628`、`doubao-seedream-5-0-lite-260128`
  - 视频：`doubao-seedance-2-5-260628`（Seedance 2.5，4–30 秒）、`doubao-seedance-2-5-flash-261115`、`doubao-seedance-2-0-260128`
- 视频支持首帧 / 首尾帧 / 全模态参考（参考图、参考视频、参考音频）；参考音频要 Seedance 2.0 及以上。
- Seedream 5.x 一次请求出 1 张图：界面上选 N 张会发 N 次请求。

> 模型版本会持续更新，若预设中的模型名无法调用，请到方舟控制台复制当前可用的模型版本 ID 手动添加。

## 阿里百炼 DashScope（图片 / 视频）

- Base URL：`https://dashscope.aliyuncs.com/api/v1`（老域名仍可用）；新业务空间域名形如
  `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1`，两者填一个即可。
- API Key：百炼控制台的 `sk-…` Key；**模型、地址、Key 必须在同一地域**。
- 图片：`wan2.7-image-pro` / `wan2.7-image`（文生图、参考图编辑）。
- 视频：`wan3.0-video`（2–30 秒，首帧 / 首尾帧 / 全能参考）、`wan3.0-video-prime`、
  `wan2.7-t2v` / `wan2.7-i2v` / `wan2.7-r2v-2026-06-12`；可灵需要先在控制台开通「可灵AI」，
  且只在华北2（北京）提供：`kling/kling-v4-video-generation`、
  `kling/kling-v4-omni-reference-video-generation`、`kling/kling-v3-video-generation`、
  `kling/kling-v3-omni-video-generation`。
- **能力边界**：参考图可以走 base64（本地文件直接可用）；参考视频 / 参考音频只收公网 URL 或
  OSS 临时地址，本机文件发不出去，适配器会在提交前明确提示。
- 文本模型（qwen4-max / qwen3.8-max 等）走 OpenAI 兼容模式，请用上表的「通义千问」预设另建一条。

完整的模型清单、参数映射与限制见 [火山方舟与阿里百炼适配说明](./providers-ark-bailian.md)。

## ComfyUI（本机工作流）

- Base URL：`http://127.0.0.1:8188`（ComfyUI 默认端口；改了 `--port` 就填对应值）
- API Key：留空（本机服务无鉴权）
- 模型：不需要手工登记。在服务卡片的「工作流根目录」里填 ComfyUI 的安装目录
  （例如 `D:\ComfyUI`），点「扫描工作流」即可识别 `user/default/workflows` 下保存的图。
- **工作流即模型**：扫描结果里勾选导入后，它会作为该服务下的一个模型出现在
  图片 / 视频页（名称形如 `comfy:12`），直接选它就能跑。
- 格式说明：ComfyUI「保存」出来的是 UI 格式，后端会自动转成 `POST /prompt` 需要的
  API 格式；转换不了的图会明确提示「请在 ComfyUI 里用『导出（API）』另存一份」。
- 排查：
  - 「无法连接 ComfyUI」→ 确认 ComfyUI 已启动、地址与端口正确。
  - 提示缺少模型 → 在 ComfyUI 里把模型装好（工作流引用的 checkpoint/LoRA 必须存在）。
  - 扫描不到 → 确认根目录填的是 ComfyUI 安装目录，且里面有 `user/default/workflows`。

## 图片与视频参数说明

- 图片：比例对应提交给接口的尺寸（如 `1024x1024`、`1280x720`）；数量 1-4 张；参考图用于支持图生图的模型（不支持时会被忽略）。
- 视频：时长 5/10 秒；画幅支持 16:9、9:16、1:1、4:3、3:4、21:9；清晰度 480p/720p/1080p。参数支持以模型为准。

## 排查步骤

1. 点「测试连接」：
   - 报网络错误 → 检查 Base URL、本机网络/代理、该服务商是否可访问。
   - 报 401/403 → Key 错误或被禁用。
   - 报模型不存在 → 模型名/模型 ID 与控制台不一致。
2. 本地服务（如 Ollama）确认其已启动并监听对应端口。
3. 若使用局域网内的网关，确认填写的是可从本机访问的地址。
