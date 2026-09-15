# 模型服务接入指南

在「模型服务 → 添加服务」中配置。服务类型四选一：

- **OpenAI 兼容接口**：绝大多数国内外模型和中转网关都走这一协议。
- **火山方舟 Ark**：豆包系列模型（Seedream 图片、Seedance 视频）使用方舟的签名与任务接口，需单独选择。
- **DashScope（百炼）**：阿里云百炼的 Kling 图片 / wan2.7 等异步生成接口。
- **ComfyUI**：本机自建的 ComfyUI 服务，通过工作流节点调用（不需要 API Key）。

界面内置了下表服务商的预设，点击即可自动填充 Base URL 与常用模型；具体模型名/模型 ID 请以你在服务商控制台看到的为准（新版本发布后可能变化）。

## OpenAI 兼容类

| 服务商 | Base URL | 模型名示例 | 能力 |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` / `gpt-4o-mini` / `gpt-image-1` | 文本、图片 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` / `deepseek-reasoner` | 文本 |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` / `32k` / `128k` | 文本 |
| 通义千问（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` / `qwen-turbo` / `qwen-max` | 文本 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` / `glm-4-flash` / `cogview-3-plus` | 文本、图片 |
| OpenRouter | `https://openrouter.ai/api/v1` | 控制台中的模型 ID，如 `anthropic/claude-3.5-sonnet` | 文本、图片 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `llama3.1` / `qwen2.5` 等已拉取的模型 | 文本 |

其他任何声明「兼容 OpenAI 协议」的网关 / 中转站：选择 OpenAI 兼容类型，填入对方提供的 Base URL（通常以 `/v1` 结尾）和 Key 即可。

## 火山方舟 Ark（豆包图片 / 视频）

- Base URL：`https://ark.cn-beijing.volces.com/api/v3`
- API Key：在[火山方舟控制台](https://console.volcengine.com/ark)创建的 API Key
- 模型名：填写你开通的**具体模型版本 ID**，例如：
  - 图片：`doubao-seedream-4-0-250828`（能力选「图片」）
  - 视频：`doubao-seedance-1-0-pro-250528`（能力选「视频」）
- 视频支持「首帧图片」（图生视频）：在视频生成页上传或从资产库带入一张图片即可。

> 模型版本会持续更新，若预设中的模型名无法调用，请到方舟控制台复制当前可用的模型版本 ID 手动添加。

## ComfyUI（本机工作流）

- Base URL：`http://127.0.0.1:8188`（ComfyUI 默认端口；改了 `--port` 就填对应值）
- API Key：留空（本机服务无鉴权）
- 模型：不需要在服务里登记。工作流里用什么模型由 `workflow_api.json` 决定。
- 用法：画布 → 添加「ComfyUI 工作流」节点 → 浮框里上传 `workflow_api.json`（ComfyUI 的「工作流 → 导出（API）」菜单产出）→ 后端自动列出可调参数 → 运行。
- 排查：
  - 「无法连接 ComfyUI」→ 确认 ComfyUI 已启动、地址与端口正确。
  - 提示缺少模型 → 在 ComfyUI 里把模型装好（工作流引用的 checkpoint/LoRA 必须存在）。
  - 采样器 / 模型下拉是空的 → 点浮框里的刷新按钮重新解析（需要 ComfyUI 在线）。

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
