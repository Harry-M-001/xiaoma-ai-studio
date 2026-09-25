# 内置 Agent · 开源项目核对与规划依据（2026-09-24）

用户 2026-09-24 提了「给工坊内置一个完整的 Agent，能调工坊里的工具，授权之后还能用电脑里的工具」，并给了一份开源项目清单（含一张截图）。
本文是**事实核对记录**：每个项目都去看了仓库/模型页/官方文档，标清许可证与运行前置条件，并给出**对本项目（本机 Windows、BYOK、SQLite、便携包、无 Docker）**的可用性判断。

结论先写在前面，详见第五节。

---

## 一、调研口径

三个筛子，任何一项过不去就不引入：

1. **许可证**：本项目是 **PolyForm Noncommercial 1.0.0**（非商用）。所以 **GPL / AGPL / 非标准商业限制协议**的代码**不能进仓库**——这类项目只能「给下载地址、不随包分发」，或干脆不碰。
2. **运行前置**：必须能**本机跑、Windows 能装、不依赖 Docker 与外部数据库**。要 GPU 的要能按硬件分档并说清门槛。
3. **形态匹配**：我们是「视频创作工作台内的协作体」，不是「编码 Agent」。给编码 Agent 做的东西（并行 worktree、仓库索引、代码评审）价值要单独判断。

---

## 二、Agent 框架 / 基础设施类

| 项目 | 地址 | 定位 | 许可证 | 前置条件 | 可用性 |
|---|---|---|---|---|---|
| awesome-llm-apps | https://github.com/Shubhamsaboo/awesome-llm-apps | **示例/模板合集，不是框架**（100+ Agent / RAG 模板） | Apache-2.0 | 各模板依赖不一 | **部分可用**：只有目录组织方式可借鉴；**不含可复用的 Agent 循环** |
| ZCode（智谱 Z.ai） | https://zcode.z.ai/cn/docs/agents | 自研默认智能体，面向 GLM 系列，侧重长任务规划与命令调用 | harness/app 侧专有（权重另议） | 需接 GLM 模型 | **不适用**：不是开源 Agent 框架，拆不出可复用的本机循环 |
| anything2explainer | https://github.com/Vincentwei1021/anything2explainer | Claude Code / Codex skill：主题 → 剧本 → 分镜 → **Remotion 代码** → 渲染 + TTS | **GPL-3.0-or-later** | Remotion + React + TS + TTS | **不引入代码**：许可证与 PolyForm 不兼容。它验证的那条链我们画布上已有 |
| Better Harness（阿里云 Qoder） | 发布说明：https://finance.sina.com.cn/tech/digi/2026-07-29/doc-inikmumr5852225.shtml | 面向 Coding Agent 的**分析与持续改进**工具，适配 Claude Code / Codex / Qoder / Cursor | 开源（详见仓库） | 需接上述编码 Agent | **借口径不借代码**：核心主张「**不把配置存在当能力生效**、每条结论附可追溯证据」——与我们已定的第三条口径同源 |
| agents-hive（阿里云） | https://developer.aliyun.com/article/1734246 | 生产级 Harness Agent 工程：全链路执行回放、质量闭环、多入口统一运行时、内建安全约束 | 开源（详见仓库） | 服务端形态 | **借口径**：执行回放与安全约束的设计值得看 |
| TencentDB-Agent-Memory | https://github.com/Tencent/TencentDB-Agent-Memory | Agent 分层记忆引擎（长/短期记忆、可接向量化与 LLM） | MIT | 偏腾讯 DB 生态 | **借分层不借本体**：我们用 SQLite 自己做分层 |
| Tencent/WeKnora | https://github.com/Tencent/WeKnora | 文档理解 + 语义检索 + ReAct Agent + 自维护 Wiki 的知识平台 | MIT | **Docker + Docker Compose，Go + Vue3 + Python docreader + ParadeDB(postgres)，建议 4 核 8G 起** | **借流程不借部署**：与我们「本机 SQLite + 便携包」正面冲突。文档：https://weknora.weixin.qq.com/docs/01-getting-started/02-installation |
| Tencent/BrowserSkill | https://github.com/Tencent/BrowserSkill | 让 Agent 用你**已登录的真实浏览器**（Rust CLI/daemon + MV3 扩展） | 见仓库 | 需装扩展；会用你的登录态 | **可选外接**：会打破「本机可离线」，默认关、需显式授权 |
| addyosmani/agent-skills | https://github.com/addyosmani/agent-skills | 给编码 Agent 的生产级技能集 | MIT | skill 运行环境 | **借约定**：`skills/<name>/SKILL.md` + `references/` + `scripts/` 这套描述规范 |
| affaan-m/ECC | https://github.com/affaan-m/ECC | Agent Harness 性能优化配置集（技能 / 记忆 / 安全 / hooks） | MIT | 面向多种编码 Agent | **借思路**：记忆与安全约束的组织方式 |
| Panniantong/Agent-Reach | https://github.com/Panniantong/agent-reach | 给 Agent 装眼睛：读 Twitter / Reddit / YouTube / B站 / 小红书 | MIT | Python 3.10+，cookie 认证，**要联网** | **可选外接**：违反离线约束，但「素材采集」是真需求 → 默认关、逐项授权 |
| stablyai/orca | https://github.com/stablyai/orca | 并行编码 Agent 的桌面环境（隔离 git worktree） | MIT | 桌面应用 | **不适用**：解决的是并行编码，不是创作工作台内的协作 |
| JustVugg/colibri | https://github.com/JustVugg/colibri | 纯 C 零依赖推理引擎，跑 MoE 大模型，带 OpenAI 兼容 API | Apache-2.0 | 纯 C 编译 | **只作参考**：我们是**接**本机服务（Ollama / ComfyUI / sherpa-onnx），不是**造**推理引擎 |

---

## 三、知识库解析与检索

| 项目 | 地址 | 定位 | 许可证 | 体积/硬件 | 可用性 |
|---|---|---|---|---|---|
| WeVisDoc | https://github.com/Tencent/WeVisDoc · https://huggingface.co/Tencent/WeVisDoc-4B | 腾讯微信视觉的**文档解析**（页面图 → Markdown，含表格/公式/阅读顺序） | Apache-2.0 | 2B / 4B，**要 GPU** | **只进引擎清单候选**（给地址 + 门槛，不代装）；不适合做本机默认解析路径 |
| turbovec | https://github.com/RyanCodrai/turbovec · https://pypi.org/project/turbovec/ | **不是检索模型**：向量量化压缩库（2–4 bits/dim） | MIT | CPU 可用 | **留作后续存储优化**；与「解析/检索模型」是两件事 |
| sqlite-vec | https://github.com/asg017/sqlite-vec | SQLite 向量检索扩展（纯 C，进程内，**Windows 可跑**） | MIT / Apache-2.0 双许可 | 极小 | **本机知识库的地基** |
| sqlite-vec-client | https://pypi.org/project/sqlite-vec-client/ | 上面的 Python 客户端（文本 + JSON 元数据 + float32 向量 + 相似度检索） | MIT | Python ≥3.9 | 同上 |
| bge-small-zh-v1.5 | https://huggingface.co/BAAI/bge-small-zh-v1.5 | 中文 embedding | MIT | **95.8 MB**，CPU 可跑 | **首选**：体积最小、中文够用 |
| bge-base-zh-v1.5 | https://huggingface.co/BAAI/bge-base-zh-v1.5 | 中文 embedding | MIT | 409 MB | 体积/效果平衡档 |
| bge-m3 | https://huggingface.co/BAAI/bge-m3 | 多语言 embedding | MIT | 约 568 MB | 要跨语言时选它 |
| Qwen3-Embedding-0.6B | https://huggingface.co/Qwen/Qwen3-Embedding-0.6B | 中英 embedding，长上下文 | Apache-2.0 | 约 595 MB | 中文效果好，作为候选 |
| bge-reranker-v2-m3 | https://huggingface.co/BAAI/bge-reranker-v2-m3 | 多语言 rerank | MIT | 2.27 GB | **先不上**：召回不够准再说 |

**最小可行本机知识库**：`SQLite + sqlite-vec + bge-small-zh-v1.5`，全程 CPU、无外部服务、无 Docker。素材入库 → 生成 embedding → 写进 SQLite → 检索时做相似度搜索。非实时，可以批量补算。

---

## 四、剪辑 / 配音 / 写作

| 项目 | 地址 | 定位 | 许可证 | 前置条件 | 可用性 |
|---|---|---|---|---|---|
| auto-editor | https://github.com/WyattBlue/auto-editor | 命令行**静音/死空自动剪辑**（silencedetect / loudness / motion） | **Unlicense（公有领域）** | Python ≥3.9，跨平台 | **最值得抄的一个**：许可证最干净，正是「一键剪口播」的参考实现 |
| jianying-headless | https://github.com/mcncarl/jianying-headless | 剪映专业版无头草稿生成/导出 | **Personal Learning and Non-Commercial Use（非标准）** | **仅 macOS** | **不适用**：平台与许可证双重不合适 |
| capcut-cli | https://github.com/renezander030/capcut-cli | CapCut / 剪映本地草稿 CLI | MIT | Node，依赖本地草稿目录 | **只作参考**：生成草稿仍需客户端打开 |
| Video2X | https://github.com/k4yt3x/video2x | ML 视频超分 + 补帧框架 | **AGPL-3.0** | Windows/Linux，Vulkan + ncnn + VS2022 | **不引入**：许可证与依赖都不合适，且内部算法我们已自建三条路线 |
| LosslessCut | https://github.com/mifi/lossless-cut | FFmpeg GUI：无损切割 / 重排 / 合并 | **GPL-2.0** | Electron 桌面 | **不引入**：只有 GUI，无 CLI/库 |
| chinese-novelist-skill | https://github.com/PenglongHuang/chinese-novelist-skill | 结构化中文小说写作流程（分步 + 偏好记忆 + 中断续写 + 自动校验） | MIT | skill 运行环境 | **借方法论**：写作分步与校验思路可用于小说/剧本节点的提示词 |
| Qwen3-TTS | https://qwen.ai/blog?id=qwen3tts-0115 | 开源 TTS，**3 秒声音克隆**，10 语种 | **Apache-2.0** | `0.6B-Base` **约 3GB 显存** / `1.7B` 约 6GB | **新增一档本机配音候选**：门槛比 VoxCPM（≥6GB）低一半 |

> 用户原话提到的「Qwen-Audio-3.1」**未找到该名称**，可能是 Qwen3-TTS 或 Qwen3-Omni 的记混；上表按核实到的 Qwen3-TTS 记录。

---

## 五、结论

### 可借鉴的三件事（借约定，不借框架）

1. **工具怎么描述**：`SKILL.md` 那套（名称 / 用途 / 何时用 / 参数 / 验证条件 / 执行路径）——比裸 JSON Schema 多出「何时该用」与「怎么算成功」两栏，正是限制工具选错的关键。
2. **能力怎么验证**：Better Harness 的「不把配置存在当能力生效、每条结论附可追溯证据」——落到我们这里：**Agent 说做完了不算数，去查库看是不是真多了一个资产或任务**。
3. **记忆怎么分层**：TencentDB-Agent-Memory 的分层思路，用我们已有的 SQLite 实现（短期 = 本次会话步骤；长期 = 调用记录 + 用户偏好）。

### 捡到的三个具体能力（与 Agent 无关也该做）

1. **一键剪口播**（auto-editor 思路，ffmpeg `silencedetect` 自己实现）——与数字人搭配补上「后期」这一环。
2. **本机知识库**（sqlite-vec + bge-small-zh）——把资产库并进可检索的知识库。
3. **Qwen3-TTS 配音档**——填上 v1.1.30 留下的「本机配音不支持音色克隆」那个洞。

### 不借鉴（含理由）

见 `路线图.md` 批次 10 的「明确不借鉴」表，共 9 条，每条都写了为什么（许可证不兼容 / 要 Docker / 平台不对 / 问题不匹配）。

---

## 六、知识库：大厂怎么做（2026-09-24 第二轮）

用户要求「查查一般大厂怎么处理」，含 WorkBuddy。

### 6.1 WorkBuddy 的知识库

**WorkBuddy 是腾讯（CodeBuddy 团队）的「全场景 AI 智能体桌面工作台」**（2026-03-09 上线，桌面端 + IM + 小程序），不是纯知识库产品 [$TRAE_REF](https://workbuddy.tencent.com)。它的知识能力分两层：

- **个人版**：走「资料库」**连接外部知识源**——ima 知识库（扫码授权后可检索个人/共享/订阅库并直接引用）、腾讯文档、腾讯乐享 [$TRAE_REF](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Knowledge-Base/IMA%20Knowledge%20Base/01-Workbuddy-IMA-Basic-Guide)
- **企业版**：「构建企业专属知识库，提供精准问答」，支持本地 Word/Excel/PPT/PDF 勾选加入任务 [$TRAE_REF](https://www.workbuddy.cn/docs/enterprise/Overview)
- 检索是**语义搜索 + 关键词搜索**两条；单次最多 50 个文件、单文件大小不限
- **未公开**：切分参数、chunk/overlap、rerank、引用溯源的块级高亮细节。**不要臆测**

### 6.2 主流产品对比

| 产品 | 切分 | 检索 | 引用溯源 | 解析 |
|---|---|---|---|---|
| Dify | 通用 / **父子**两种模式；父子=子块匹配、父块全文回填 [$TRAE_REF](https://docs.dify.ai/zh-hans/guides/knowledge-base/create-knowledge-and-upload-documents/chunking-and-cleaning-text) | 向量 / 全文 / **混合**（语义与关键词权重可调，或接 Rerank）；TopK 默认 3、Score 阈值 0.5、Rerank 默认关 [$TRAE_REF](https://docs.dify.ai/zh-hans/guides/knowledge-base/create-knowledge-and-upload-documents/setting-indexing-methods) | 分段级 | Q&A 模式、分段摘要增强 |
| RAGFlow | 多 chunk 模板按版式选；**parent_child** 分块 [$TRAE_REF](https://ragflow.io/docs/) | 向量 + 关键词 | **分块可视、可手工加关键词干预** | DeepDoc 默认做 **OCR/版面分析**；0.23.0 起「图像/表格上下文窗口」把邻近文本与视觉元素并入同块 [$TRAE_REF](https://ragflow.com.cn/docs/set_context_window) |
| Coze | 自定义分段标识符 + 字符长度 [$TRAE_REF](https://docs.coze.cn/guides_create_knowledge) | 增强检索 | 命中次数统计 | 分「精准解析」（OCR、提图、提表，PDF 可按页过滤）与「快速解析」；知识库分文本/表格/**照片**三类 [$TRAE_REF](https://docs.coze.cn/guides_knowledge_faq) |
| **WeKnora** | 可配置分块，含父子与自适应 [$TRAE_REF](https://weknora.weixin.qq.com/docs/01-getting-started/01-introduction) | **向量 + BM25 混合 + RRF 融合 + Rerank** + 查询改写 | 回答带来源引用、可打开原文核对 | 版式分析、扫描件 OCR、表格抽取、**图片 VLM 描述**、ASR；chunk 类型含 text/faq/**image**/table/entity |
| FastGPT | — | 混合检索用 **RRF 融合** + rerank 模型重排并按分分数过滤 [$TRAE_REF](https://doc.fastgpt.io/en/docs/introduction/guide/knowledge_base/dataset_engine) | — | — |
| Claude Projects | 由 Citations API 决定：纯文本**按句**切、PDF 每页、自定义内容不切 [$TRAE_REF](https://platform.claude.com/docs/en/build-with-claude/citations) | 文件常驻上下文（**不是向量库**） | 返回字符区间 / 页码 / block 索引 | PDF、DOCX、CSV、XLSX、图片等；单文件 30MB、单次 ≤20 文件 |
| ChatGPT Projects | — | 项目知识自动检索 | 文件级引用 | 单文件 512MB/2M token、单次 ≤10 个；**文本抽取为主、图片丢弃** |

### 6.3 三个直接改变我们方案的结论

**① 素材和文档是「一套索引、两套摄取」，不是两张表。**
主流做法：图片/视频先进「文本化」环节（VLM 描述 / OCR / 标签 / ASR），产出文本进**同一个检索索引**，原文件仍留在资产存储里。WeKnora 的 chunk 类型里就有 image；Dify 用多模态嵌入直接跨模态检索 [$TRAE_REF](https://docs.dify.ai/zh-hans/guides/knowledge-base/create-knowledge-and-upload-documents/setting-indexing-methods)。

**② 但「生产用资产库」和「知识库检索单元」没有一家做成同一张表。**
合成一张表会把大文件、时长、尺寸这些与检索无关的字段混进索引，且一改切分策略就得动资产数据。所以：`assets` 保持不动当**物理层**，新增 `kb_documents` / `kb_chunks` 当**检索层**，`asset_id` 外键关联；前端表现为**一个入口、两个页签**。

**③ 中文的 chunk 与检索有公认数字区间。**

| 场景 | chunk_size | overlap |
|---|---|---|
| 技术文档 | 400–1000 tokens | 80–150 [$TRAE_REF](https://www.cnblogs.com/lqf-dev/articles/19994317) |
| 通用经验式 | 500–1000 | chunk 的 20–30% [$TRAE_REF](https://www.besthub.dev/articles/how-i-doubled-rag-accuracy-with-targeted-optimizations-bddfcd24a71b) |
| 实测最优 | 1024 | 128（命中率@5 = 84%）[$TRAE_REF](https://segmentfault.com/a/1190000048303154) |

两条比数字更重要的：**overlap 比 chunk_size 更值得调**（50→128 的收益比 512→1024 更稳），且**按结构切、不要按字符数切** [$TRAE_REF](https://segmentfault.com/a/1190000048303154)。混合检索的 RRF 融合 `k=60`，cross-encoder 重排 top-20 → top-5 [$TRAE_REF](https://lobehub.com/skills/curiositech-windags-skills-rag-retrieval-pattern-design)；中文 rerank 常用 `bge-reranker-v2-m3`。**Dify 官方没给语义/关键词的推荐权重**，所以这一项我们不自称有最优值，用自有问题集测。

**④ 成熟度阶梯**（我们的路线按这个走）：L0 建库（先确认真有人会问、定 20–50 个真实问题当评测集）→ L1 最小可用（按标题层级切 500–800 字符、overlap 10–15%、单路向量检索、文件级引用）→ L2 提质（BM25+向量混合 + rerank + 图片走 VLM/OCR 文本进同一索引 + **块级高亮**）→ L3 完整（父子分块、查询改写、结构化过滤、FAQ/预建问题、持续评测）[$TRAE_REF](https://www.lyron-ai.com/en/news/ai-knowledge-base-rag-sme/)[$TRAE_REF](https://www.aininza.com/blog/rag-implementation-timeline-2026-30-60-90-day-plan/)

---

## 七、LibTV 新版与 TV director（2026-09-24）

用户给了微信链接，抓取成功 [$TRAE_REF](https://mp.weixin.qq.com/s/IJiZjeoUPn02wFdxlhicmw)。

**LibTV 是什么**：LibLibAI（哩布哩布）的 AI 影视/视频创作平台，网页端**无限画布 + 节点式工作流**，免费体验 + 会员 + 积分制 [$TRAE_REF](https://www.aitop100.cn/tools/liblib-tv)。

**TV director 就是「能操作画布的 Agent」**，三条关键事实：

1. **入口是画布下方的一个「小蓝球」**（旧版在右上角对话框）——即「就地入口 + 常驻存在感」，**不是独立聊天页** [$TRAE_REF](https://mp.weixin.qq.com/s/IJiZjeoUPn02wFdxlhicmw)
2. **能力边界**：① **建节点**（按剧本自动创建视频节点、参考素材与提示词自动填好）；② **改文本**（选中原文给意见，像改作文一样划掉旧版、旁边写新版）；③ **关联修改提示**（大纲改完**自动列出 9 处联动修改点**）；④ **批量**（自动跑人物/画面提示词、批量生成、拼成片）；⑤ **连线**（脚本节点内 @资产卡片自动识别并自动连线）[$TRAE_REF](https://m.thepaper.cn/newsDetail_forward_33604553)
3. **有确认门与授权**：执行前**主动确认并给多个选项**、对无法过审的图自动改提示词重生成；权限需先授权「自动生成」与「通知」两项。**「暂停 / 撤销」未查到明确说明** [$TRAE_REF](https://mp.weixin.qq.com/s/IJiZjeoUPn02wFdxlhicmw)

**角色造型室（用户说的「捏脸」）——用户 2026-09-24 提供了 10 张截图，逐张读完，形状已完整摸清：**

**入口**：**画布下方**一排按钮里的「角色造型室」（与 TV director 那个小蓝球**同一排**）——再次印证「就地入口」，不是独立页面。

**它是一条五步流程**（截图上的分步提示文字）：① 进「角色造型室」→ ② 生成角色（用本剧已有角色设定 / 直接描述想要的人物形象 / 上传参考图）→ ③ 编辑（角色面部骨骼 / 肤质 / 增添痣·伤疤等特征 / 调节身材比例）→ ④ 编辑造装设计（换装 / 发型漂色 / 上传参考图一键复刻 / 根据场景设定直出服装）→ ⑤ 音色设计。

**左侧五个模块，右栏的实际形状**（这一栏最值得学）：

| 模块 | 右栏字段 | 交互形态 |
|---|---|---|
| 脸部精修（限免） | 质感磨皮、AI 美颜（脸部）、皮肤纹理（脸部）、皮肤透亮、皮肤红润、皮肤美白、祛斑祛痘（开关）；顶部另有一排分页图标 | **滑块 + 开关** |
| 脸部 & 身体特征 | 纹身 & 疤痕：痣、疤痕_深、疤痕_浅、雀斑、全脸雀斑、脸颊雀斑、**自定义** | **预设缩略图点选** |
| 身材塑形（限免） | 丰胸、**AI 增高**、驼背、长腿、直角肩、小头、瘦身（共 7 个滑块） | **滑块** |
| 服装造型 | 分页「造型 / 衣服 / 配饰」+ **AI 生成** 按钮；发型按风格分组（现代 / 古风 / 民国 / 仙侠 / 科幻 / 民族 / 儿童）× 12 款缩略图（长直发、大波浪、波波头、齐肩中短发、高马尾、低马尾、双马尾、丸子头、半扎发…） | **预设缩略图 + AI 生成** |
| 音色音调 | 分页「音色库 / **我的音色** / AI 生成」；音色**带具体名字**——青涩青年音色、精英青年音色、霸道青年音色、青年大学生音色、少女音色、御姐音色、成熟女性音色、甜美女性音色…每个标注「中文（普通话）· 男/女」 | **预设列表 + 试听 + AI 生成** |

**其它结构**：顶部是角色名 + **结构化标签**「♀ · 青年 · 东亚 · 现代」（性别 / 年龄段 / 文化区域 / 时代）；底部「**保存时生成角色资产 · ¤44**」+「保存」+「保存并添加到画布」；左下有三个图标（撤销 / 重做 / 重置）；角色库分「**我的角色库** / 官方角色库」，可按性别·年龄段·文化区域筛选。

**三条结论**：

1. **它是参数化调节，不是「再写一段提示词」**。右栏全是滑块、预设缩略图、开关——这是与纯提示词重生成的根本差别。要学的是这个形态：**先把「角色」拆成可调维度，再谈生成**。
2. **但每个模块都留了「AI 生成」出口**（发型有、音色有）。所以是**「预设/参数为主 + AI 生成为补」**，不是二选一。
3. **「我的音色」页签就是「导入自有音色」**；它的音色名（青涩青年音色…）正是那种「具名」。

> **上轮「变身高没查到」这一条已解决**：它叫「**AI 增高**」，是身材塑形里的独立滑块。**不需要再补截图。**

**其它同方向功能**：**3D-BOX**（一句话搭 3D 场景、排机位、手绘运镜轨迹、导出白模预演进画布）、**创意片头**（自动避开人脸与关键画面、按构图留字位）、**Skill Hub**（约 100 条 Skill，**可把一次对话一键转成 Skill**）、**智能引用**（自动识别画布素材插入提示词正确位置）[$TRAE_REF](https://www.c114.net.cn/ainews/126196.html)。

**技术实现有一处开源**：`libtv-labs/libtv-skills`（**MIT**，遵循 OpenClaw 技能规范），暴露 OpenAPI（`POST /openapi/session` 创建会话并发消息、`GET /openapi/session/:id` 增量拉取）——有意思的是，**外部 Agent 是通过「IM 会话式自然语言消息」驱动 LibTV 的，不是节点图 JSON 协议** [$TRAE_REF](https://github.com/libtv-labs/libtv-skills)。**内部怎么实现画布 Agent（function calling 调节点 vs 生成工作流 JSON）、有无官方技术博客：均未查到。**

**注意**：以上多为媒体实测口径，非官方规格书。

---

## 八、Agent 入口与信息架构（大厂形态）

| 产品 | Agent 放在哪 | 组织单元 |
|---|---|---|
| Claude Code | 终端内嵌，工作目录即上下文 | **会话 = 任务**，可并行多会话 |
| Cursor | IDE 内嵌面板 | 会话 + 编辑器上下文 |
| Coze | 工作空间 → 智能体列表（每个是可配置对象）；新版把云电脑 Agent、本地 Agent 汇聚进同一会话 [$TRAE_REF](https://docs.coze.cn/cozespace_coze_app_faq) | 配置对象 |
| Manus | 云端独立环境，会话=任务；可从任一节点 **Branch** 出并行会话并继承上下文 [$TRAE_REF](https://manus.im/zh-cn/blog/manus-branch) | 会话 + 分叉 |
| Devin | 会话 + `/steps` 列步骤、`/fork`、`/revert`、**有 replay 回放 UI** [$TRAE_REF](https://docs.devin.ai/zh/cli/changelog/stable) | 会话 + 步骤 |
| 即梦 | 左侧「生成 → Agent」模式；**并行保留「无限画布」与「我的作品」** [$TRAE_REF](https://runyoung0613.github.io/jimeng-tutorial/charpter/ch05-Agent.html) | 模式 + 画布 |
| Claude / ChatGPT Projects | Project = 上下文容器（文件 + 自定义指令 + 会话历史） | 项目容器 |

**三个设计问题的结论**：

1. **「家」不是三选一，主流是三层叠用**：**项目（容器）→ 任务列表（视图）→ 会话（执行单元）**。纯「会话」当家缺归属与素材复用，纯「任务列表」当家缺上下文。这**正好对上用户「新开一个项目、项目里有很多任务」的想法**。
2. **展示与介入**：时间线 + 步骤卡片（工具名 / 入参 / 产出缩略图 / 耗时，可展开原始日志）+ **可回放**。介入门**只在危险/不可逆操作**（删改资产、覆盖、付费 API、对外发布），单步确认太累；配检查点做事后撤销。
3. **多任务并行 = 多会话**，用**任务中心**统一展示「排队中 / 运行中 / 待确认 / 已完成」，点开跳会话——这是 Devin / Manus / Coze 的共识形态。**别把多任务塞进一个会话。**

---

## 九、音色的授权（用户问「音色是不是开源的」）

**结论：是开源的，而且可商用。**

| 项 | 事实 | 来源 |
|---|---|---|
| Kokoro 模型权重 | **Apache-2.0**，**可商用、无限制** | [$TRAE_REF](https://huggingface.co/hexgrad/Kokoro-82M) |
| 训练数据来源 | 只用「宽松/无版权音频 + IPA 标签」：公共领域音频、Apache/MIT 音频、大厂闭源 TTS 生成的合成音频（**明确排除开源 TTS 与自定义克隆声**）；含两条 CC BY（Koniwa CC BY 3.0、SIWIS CC BY 4.0） | 同上 |
| 中文说话人 | v1.1-zh 的 **100 个中文说话人由「龙猫数据」免费无偿提供**，模型仍 **Apache-2.0** | [$TRAE_REF](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/blob/main/samples/make_zh.py) |
| 我们用的 sherpa-onnx 包 | 音色即上游 voice 表，随 Apache-2.0，包内附 LICENSE；`v1_0` = 53 说话人、**`v1_1` = 103 说话人（中英）→ 对应界面上的 0–102** | [$TRAE_REF](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html) |
| **音色名** | 上游 `samples/make_zh.py` 里音色 ID 是 **`zf_001` / `zm_010`**（`z`=中文、`f`/`m`=女/男、三位编号）——**命名规律可用**，比「按号选」强得多 | [$TRAE_REF](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/blob/main/samples/make_zh.py) |
| **一个要核的点** | Kokoro 的文本前端依赖 **`espeak-ng`（GPLv3）**。按我们 `#40` 的「引擎包**不随仓库与便携包分发**、用户自己去上游下」这条口径，我们不分发它 → 不构成传染。**落地前把那份 release 的 LICENSE / NOTICE 实际看一遍再确认** | [$TRAE_REF](https://huggingface.co/hexgrad/Kokoro-82M) |

**「用户导入自己的音色」= 走克隆模型。** 许可证干净、本机可跑的：

| 方案 | 许可证 | 显存 | 克隆 | 中文 |
|---|---|---|---|---|
| **Qwen3-TTS-0.6B-Base** | **Apache-2.0** | ~3GB | 3 秒 | 10 语 |
| **GPT-SoVITS** | **MIT** | 低，**可 CPU** | 5 秒零样本 / 1 分钟微调 | 强 |
| IndexTTS / IndexTTS2 | Apache-2.0 | ~8GB | 零样本 | 强 |
| CosyVoice 2/3 | 代码 Apache-2.0，**权重未逐项核实** | ~8GB | ✓ | 强 |
| ~~Fish-Speech~~ | **Research License：非商用，商用需单独授权** | — | ✓ | ✓ |
| ~~XTTS v2~~ | 商用受限 | — | ✓ | — |

**导入自有音色必须加的免责**：用户**勾选确认「本人声音或已获授权」并留痕**；禁止克隆公众人物与第三方；界面写明 AI 合成标识（国内涉深度合成与生成式 AI 管理规定）。**是否有产品做了勾选式免责声明：未查到可靠公开证据**，所以这一条我们按国内监管要求自己定，不照抄。

---

## 十、拍板状态（2026-09-24 二轮更新）

| # | 问题 | 状态 |
|---|---|---|
| 1 | 知识库形态：并进还是挂在旁边 | ✅ **已定**（行业共识）：`assets` 不动当物理层 + 新增 `kb_documents` / `kb_chunks` 当检索层。**动手第一步先补 `assets.project_id`** |
| 2 | Agent 入口 | ✅ **已定**（用户）：要一个导航入口。**并且把 Agent 的定位抬高了**——见下表第 4 条 |
| 3 | 外接工具（联网 / 用已登录浏览器） | 建议**「只做导入、不做抓取」**：读本地素材目录（带路径守卫）能拿到 90% 价值，不碰反爬与登录态。**待用户确认** |
| 4 | Agent 是什么 | ✅ **已定**（用户 2026-09-24）：**通用本机 Agent 工作台**，不只是视频工作流编排器——「让 Agent 在项目里**写代码、做项目、搞设计、调用工具**」，对标 WorkBuddy，可有小创新 |
| 5 | 导航改名 | ✅ **已定**（用户）：现有「项目」改名「**自由画布**」，「项目」这个词让给 Agent。**代码里 `projects` 保持不动**（它现在指画布容器），Agent 侧用 `agent_projects` / `/api/agent/projects` |
| 6 | Agent 项目的落点 | ✅ **已定**（用户）：默认根目录下建文件夹，**默认根目录可在设置里改**（新配置项）；用户也可以指定别的目录 |

**LibTV 截图这一项已闭合**：用户给的 10 张截图已逐张读完，「变身高」确认叫「AI 增高」（身材塑形里的独立滑块），**不需要再补截图**。

**新的开放问题（动手前要定）**：

1. ~~命令执行工具要不要白名单~~ → ✅ **已定**（用户 2026-09-25）：**参照 Trae 的权限模型**，做成三个模式（见 §11）
2. **Agent 产出的东西算不算项目资产**（要不要进资产库、能不能被画布引用）？（`#63`）
3. ~~外接工具~~ → ✅ **已定**（用户 2026-09-25）：**只做导入、不做抓取**——读用户显式添加的本地素材目录 + 保持手工上传；`BrowserSkill` 与 `Agent-Reach` 都不引入
4. ✅ **已定**（用户 2026-09-25）：**资产库与知识库分家**——资产库保持现状（画布 Agent 与导演台产出并入）、知识库独立且**手动创建**、通用 Agent 产出**不自动导入**知识库

---

## 十一、命令执行的权限模型：Trae 怎么做的（2026-09-25）

用户指定「参照 Trae 的 beta 权限设置」。核过官方文档，它实际是**两层**。

### 11.1 第一层：命令执行模式（三选一）

位置：设置 → 对话流 → 自动运行 → 自动运行命令。

| 模式 | 行为 | 官方口径 |
|---|---|---|
| **沙箱运行（支持白名单）** | 白名单内的命令在**沙箱外**自动跑；不在白名单的在**沙箱内**自动跑，失败时问是否要在沙箱外再试 | **默认开启，推荐大多数用户** [$TRAE_REF](https://www.trae.ai/blog/engineering_thought_0108) |
| **手动运行** | 每条命令都要人工确认 | [$TRAE_REF](https://docs.trae.cn/ide_sandbox) |
| **自动运行** | 始终在沙箱外自动执行，**绕过所有安全检查** | 官方明说「**非必要不要开启**」[$TRAE_REF](https://docs.trae.ai/ide/auto-run-and-security) |

### 11.2 第二层：沙箱本身

沙箱为智能体生成的命令提供**受限执行环境**，防止未经授权的文件访问；**按命令是否在允许列表决定在沙箱内还是沙箱外执行**；可用 `sandbox.json` 自定义沙箱配置 [$TRAE_REF](https://docs.trae.cn/ide_sandbox)[$TRAE_REF](https://docs.trae.ai/ide/sandbox)。

### 11.3 另外三条值得抄的细节

1. **删除类操作在文档里是单列的**（"File deletion operations" 有自己的一节），不混在普通命令里 [$TRAE_REF](https://docs.trae.ai/ide/auto-run-and-security)
2. **TRAE CLI 另有一套 `permission_mode`**：`default`（所有**非只读**的工具调用都要在执行前请求授权）/ `plan`（先分析需求生成计划、待用户确认后再执行）/ `bypass…`（跳过）[$TRAE_REF](https://docs.trae.cn/cli/permission-mode)——**`plan` 与我们已定的「先定计划再执行」同源**
3. 官方明确提示**外部提示词注入**风险，建议在可信环境充分评估后再开自动运行 [$TRAE_REF](https://docs.trae.ai/ide/auto-run-and-security)

### 11.4 我们怎么落地（`#62`）

**照它收成三个模式**：**沙箱 + 白名单（默认）** / **手动运行** / **完全访问（要点开一个明说风险的开关）**。

**但必须说明一处关键差别**：Trae 的沙箱跑在 IDE 里（macOS 有系统级沙箱），我们跑在 **Windows 本机、便携包**，**做不到系统级隔离**。所以我们的「沙箱」实际是 **受限的工作目录 + 路径守卫 + 命令白名单**，**这一点必须在界面上如实说**——不能让人以为「沙箱」是绝对安全的。这是与项目一贯口径（「判得出来才给开」「不假装什么机器都能跑」）一致的做法。

---

## 十二、外部知识库为什么必须授权（`#65`，2026-09-25）

用户问「连接外部知识库是不是需要其他应用授权？比如 ima」。**答案是必须，不授权就完全连不上。**

**WorkBuddy 接 ima 的实际流程**：连接器 → ima 知识库 → **授权登录** → **微信扫码登录 ima** → **再次确认授权**，之后才拿到三项权限：查看我的知识库列表 / 查看或搜索知识库资料 / 把文件添加到任务或保存回 ima [$TRAE_REF](https://cloud.tencent.com/document/product/1831/134397)。也就是说它**不是「填个地址就能读」的东西**。

**ima 的官方 OpenAPI**：需在官方页面**申请 `Client ID` 和 `API Key`**，凭证存本机，再走统一的 HTTP POST 调用 [$TRAE_REF](https://blog.csdn.net/weixin_44903776/article/details/159506719)。

**第三方 MCP 的一条路我们不采用**：社区有 ima MCP server 需要在「**浏览器开发者工具里把 token 抓出来**」[$TRAE_REF](https://lobehub.com/mcp/hdsz25-tencent_ima_mcp)——依赖前端私有接口，服务端一改就失效，且绕过了正规授权。**和我们「不引入会烂的依赖」是同一条判据。**

**结论**：外部知识库全是**云端服务**，与「本机可离线」正面冲突 → 做成**可选连接器**（默认关、逐项授权、明说「连上就会联网」、密钥存本机），且**第一版可以不排期**。先把本机知识库做好。