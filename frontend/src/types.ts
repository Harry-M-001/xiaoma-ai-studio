export type Modality = "text" | "image" | "video";
export type ProviderKind = "openai" | "ark" | "dashscope" | "comfyui";

export interface AppInfo {
  name: string;
  version: string;
  auth_required: boolean;
}

export interface ModelSpec {
  name: string;
  modality: Modality;
  label: string;
}

export interface Provider {
  id: number;
  name: string;
  kind: ProviderKind;
  base_url: string;
  enabled: boolean;
  sort_order: number;
  models: ModelSpec[];
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProviderInput {
  name: string;
  kind: ProviderKind;
  base_url: string;
  api_key?: string | null;
  enabled: boolean;
  sort_order?: number;
  models: ModelSpec[];
}

export interface ModelOption {
  key: string;
  service_id: number;
  service_name: string;
  name: string;
  label: string;
  modality: Modality;
}

export interface ChatSession {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id?: number;
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
  created_at?: string;
}

export interface AssetBrief {
  id: number;
  kind: string;
  url: string;
  width?: number | null;
  height?: number | null;
}

/**
 * 任务状态。
 *
 * 后端实际写入的是 `completed`（见 backend 的 runner / models），
 * `succeeded` 只是早期约定的历史别名（provider 层查上游任务时用它），
 * 这里两者都收，展示层统一按「已完成」处理——不然查不到就会回落成「排队中」。
 */
export type TaskStatus =
  | "pending"
  | "processing"
  | "completed"
  | "succeeded"
  | "failed"
  | "cancelled";

/** 重新生成时的参数覆盖；留空即完整沿用原任务的参数快照 */
export interface TaskRetryIn {
  prompt?: string;
  n?: number;
  size?: string;
  duration?: number;
  ratio?: string;
  resolution?: string;
}

export interface Task {
  id: number;
  kind: string;
  status: TaskStatus;
  service_id: number | null;
  model: string;
  prompt: string;
  params: Record<string, unknown>;
  error: string | null;
  progress: number;
  assets: AssetBrief[];
  created_at: string;
  completed_at: string | null;
  /** 若是「重新生成」出来的，这里是原任务 id */
  retry_of_task_id?: number | null;
}

/* ---------------- 生成前软校验 ---------------- */

/** 一条软告警。`code` 是稳定标识，文案可以改而前端不受影响。 */
export interface PreflightWarning {
  code: string;
  /** warn = 大概率不是你想要的；info = 只是提醒一下 */
  level: "warn" | "info";
  message: string;
  suggestion: string;
}

export interface PreflightResult {
  warnings: PreflightWarning[];
  /** 恒为 false：预检只告警不阻断。留着是为了前端不必去猜这类接口会不会挡人 */
  blocking: boolean;
}

export interface Asset {
  id: number;
  kind: string;
  url: string;
  original_name: string;
  content_type: string;
  size: number;
  source: string;
  /** 资产链身份：资产名（角色/场景/道具名）与类型；非资产链产物为空串 */
  name?: string;
  category?: string;
  prompt: string | null;
  width: number | null;
  height: number | null;
  duration: number | null;
  task_id: number | null;
  created_at: string;
}

// ---------- 导演台：转场与转场音效 ----------

/**
 * 转场预设。`xfade` 是发给 ffmpeg 的滤镜名，与 `key` **不一定同名**
 * （「过黑」的 key 是 `fade`、滤镜名是 `fadeblack`），所以两个都留着。
 */
export interface TransitionPreset {
  key: string;
  label: string;
  xfade: string;
  hint: string;
}

/** 转场音效预设。key 为空表示不加音效（`hint` 里说明） */
export interface TransitionSfxPreset {
  key: string;
  label: string;
  hint: string;
}

export interface TransitionOptions {
  presets: TransitionPreset[];
  sfx: TransitionSfxPreset[];
  seconds: { min: number; max: number; default: number };
}

/**
 * 合并前的算账结果。**成片会比硬切短**（转场是把相邻两段交叠，不是插一段新的），
 * 所以这两个数必须摆出来，而不是等用户拿到片子发现短了再去猜。
 */
export interface MergePreview {
  transition: string;
  sfx: string;
  transitionSeconds: number;
  /** 逐段时长；读不出来的那一段是 null */
  clipSeconds: (number | null)[];
  hardCutSeconds: number;
  totalSeconds: number;
  shortfallSeconds: number;
  /** 非空 = 这样接不行，里面是一句可直接展示的理由 */
  problem: string;
}

export interface MergeArgs {
  assetIds: number[];
  transition?: string;
  transitionSeconds?: number;
  /** 内置配方（与 `sfxAssetId` 二选一） */
  sfx?: string;
  /** 用资产库里自己的一条音频当音效（优先于 `sfx`） */
  sfxAssetId?: number | null;
  /**
   * 字幕版式 key。**空 = 没选过**（用默认版式）；非空 = 用户选的，谁都不许改。
   * 与产物回滚、候选定稿同一个口径。
   */
  subtitleStyle?: string;
  subtitleScale?: number;
  /** 逐段一句字幕，按下标与片段对齐；空串 = 这一段不出字幕 */
  subtitles?: string[];
}

// ---------- 字幕（版式与字体） ----------

/**
 * 字幕版式。**这是用户直接选的**，画风只提供默认值——一旦用户选过，
 * 换画风不许动它（与产物回滚、候选定稿同一个口径）。
 */
export interface SubtitleStyle {
  key: string;
  label: string;
  hint: string;
  /** 用哪款字体（字体清单里的 key） */
  font: string;
  /** 那款字体的显示名，界面直接用，不自己查表 */
  fontLabel: string;
  /** 这款版式要的字体在本机能不能用 */
  fontReady: boolean;
  size: number;
  align: number;
  outline_w: number;
  shadow: number;
}

/** 一款字体。`status`：ok（能用）/ missing（没下过）/ corrupt（下了但校验和不符） */
export interface SubtitleFont {
  key: string;
  label: string;
  family: string;
  files: string[];
  style: string;
  hint: string;
  coverage: string;
  bundled: boolean;
  bytes: number;
  downloadBytes: number;
  status: "ok" | "missing" | "corrupt";
  present: boolean;
}

export interface SubtitleOptions {
  styles: SubtitleStyle[];
  fonts: SubtitleFont[];
  defaultStyle: string;
  /** 画风 key → 推荐版式 key。只在用户没选过时用来给默认值 */
  recommended: Record<string, string>;
  scale: { min: number; max: number; default: number };
  /** 这台机器上的 ffmpeg 有没有 libass */
  filterAvailable: boolean;
  usableFonts: string[];
  /** 空串 = 能烧；非空是一句可直接展示的理由 */
  problem: string;
  previewText: string;
}

export interface SubtitlePreviewResult {
  style: string;
  font: string;
  /** 真渲染出来的那帧，JPEG 的 data URI */
  image: string;
}

// ---------- 配音（语音合成） ----------

/** 音色快捷选项。只是快捷选项、不是白名单：界面允许直接手填别家的音色 id */
export interface SpeechVoicePreset {
  id: string;
  label: string;
}

export interface SpeechVoices {
  presets: SpeechVoicePreset[];
  maxChars: number;
  speedRange: [number, number];
  speedDefault: number;
  /** 把配音挂到一次视频生成上时那句提示（唯一副本在后端，两个入口共用） */
  videoRefHint: string;
}

export interface SpeechResult {
  asset: Asset;
  chars: number;
  seconds: number | null;
  modelKey: string;
  model: string;
  voice: string;
  speed: number;
}

/* ---------------- 配置域（后端注册表驱动，前端不写死清单） ---------------- */

/** 公开配置：键 → 值。如 app.name / defaults.temperature / limits.upload_max_mb */
export type ConfigMap = Record<string, unknown>;

export interface ModalityMeta {
  key: string;
  label: string;
  icon: string;
  sort_order: number;
}

export interface ParamOptionItem {
  id: number;
  value: string;
  label: string;
  meta: Record<string, unknown>;
  sort_order: number;
}

export interface NavMeta {
  key: string;
  label: string;
  icon: string;
  route: string;
  group: string;
  requires_auth: boolean;
}

/** 创作项目（画布重做后挂接画布数据，当前 canvas_ready 恒 false） */
export interface Project {
  id: number;
  name: string;
  description: string;
  status: string;
  canvas_ready: boolean;
  created_at: string;
  updated_at: string;
}

// ---- 画布（契约对齐星云创 TapCanvas） ----

export interface CanvasHandle {
  id: string;
  type: "text" | "image" | "video" | "any";
}

export interface CanvasNodeSchema {
  kind: string;
  category: string;
  label: string;
  description: string;
  features: string[];
  handles: { targets?: CanvasHandle[]; sources?: CanvasHandle[] };
  /** 文档节点可自定义「输入框」标题与占位文案（如「你的想法」/「补充要求」） */
  promptLabel?: string;
  promptPlaceholder?: string;
}

export interface CanvasNodeData {
  prompt?: string;
  model_key?: string;
  size?: string;
  n?: number;
  duration?: number;
  ratio?: string;
  mode?: string;
  workflowId?: number;
  paramValues?: Record<string, unknown>;
  refImages?: { id: number; url: string }[];
  /** 自动链文档节点：块数（章数 / 场数 / 镜头数） */
  chapterCount?: number;
  sceneCount?: number;
  shotCount?: number;
  /** 资产设定图：生成范围（"" 全部 / character 角色 / scene 场景 / prop 道具） */
  assetScope?: string;
  /** 分镜图：本次最多生成几个镜头（0 = 全部） */
  shotLimit?: number;
  /** 静图样片：画幅（source = 跟随图片，其余为固定比例，会裁切填满） */
  sampleRatio?: string;
  /** 静图样片：成片长边像素（1280 / 1920；跟随图片时不会超过原图，避免糊） */
  sampleRes?: number;
  /** 静图样片：分镜表没写时长时，每镜按几秒算 */
  sampleShotSeconds?: number;
  /** 静图样片：分镜表没写运镜时怎么办（空 = 轻微推近 / static = 固定不动） */
  sampleDefaultMove?: string;
  /** 静图样片：把分镜表的「台词」读成旁白合进片子（会额外用掉一次语音合成） */
  sampleNarration?: boolean;
  /** 静图样片旁白的音色；留空 = 用语音服务的默认音色 */
  sampleVoice?: string;
  /**
   * 视频节点的「对白音轨」：一条**音频资产**的 id（配音产物）。
   * 挂上它这次出片会自带声音（数字人 / 口播那条路）；不挂就是无声片子。
   */
  audioRefAssetId?: number | null;
  /**
   * 最近一次出的样片。存在节点上（不是临时状态）——
   * 刷新页面后那条片子还在，不用为了再看一眼重新渲染一次。
   */
  sampleAssetId?: number;
  sampleAssetUrl?: string;
  /** 逐镜出视频：off（整段一个）/ each（每镜一段）/ chain（每镜一段并首尾相连） */
  shotVideo?: string;
  /**
   * 逐镜对白（数字人）：每一镜的「台词」先合成成配音，再作为参考音附给这一镜的视频。
   * 只在逐镜出片时有意义（「整段一个视频」没有「这一镜的台词」这个概念）。
   */
  shotDialogue?: boolean;
  /** 逐镜对白的默认音色；留空 = 用语音服务的默认音色 */
  shotVoice?: string;
  /** 逐镜对白的角色音色表：每行一条 `角色=音色`（认不出格式的行会报错，不静默忽略） */
  shotVoices?: string;
  /** 同场景串联：每镜额外带上同场景上一镜的分镜图当参考 */
  sceneRefs?: boolean;
  /**
   * 回滚到的那一版（产物版本栈）。空 = 跟最新那一版。
   *
   * 只有用户显式点「设为当前」才会写这个字段；生成新产物**不会**动它。
   * 所以「回滚到第 2 版、又生成了第 4 版」之后，节点交付的仍然是第 2 版——
   * 节点上会把这件事标出来（不然就成了静默行为）。
   */
  versionKey?: string | null;
  /**
   * 每一组候选里定稿的那一张（`{组键: 资产 id}`）。组键由后端给（镜号 / 资产名 / 整节点）；
   * 空对象 = 还没定过稿，下游按老行为取（一组全给）。
   *
   * 与 `versionKey` 一样：只有用户点了「定稿」才写，生成新产物不会动它。
   */
  picks?: Record<string, number> | null;
  /** 导演风格卡标识（空 = 不注入风格） */
  styleKey?: string;
  /**
   * 文档节点的「手改正文」：在画布上直接编辑过的内容。
   * 有它就优先给下游用（后端 `_node_inputs` 的覆盖链：手改 > 生成结果 > 节点输入），
   * 所以改几个字不必重跑整条链。清空即恢复用生成结果。
   */
  docText?: string;
  /** 是否自动挂载提示词里提到的资产设定图（false = 关闭，默认开） */
  mentionRefs?: boolean;
  [k: string]: unknown;
}

/**
 * AI 搭画布：一份**未落库**的草稿。
 *
 * `status` 是机器可读的结果（不是给人看的文案），前端据此决定给什么下一步：
 * ok 有草稿可落 / need_input 需求太短 / need_credentials 没有可用的文本模型 /
 * unparsable 模型没给合法 JSON / upstream_error 上游调用失败。
 */
export interface CanvasAgentNode {
  id: string;
  kind: string;
  data: CanvasNodeData;
  /** 后端按拓扑算好的落位（不问模型要坐标，所以它一定是整齐的） */
  position: { x: number; y: number };
}

export interface CanvasAgentDraft {
  status: "ok" | "need_input" | "need_credentials" | "unparsable" | "upstream_error";
  summary: string;
  nodes: CanvasAgentNode[];
  edges: { from: string; to: string }[];
  /** 后端在修复模型输出时做的改动（丢掉的节点/连线、被改回默认的参数） */
  notes: string[];
  /** 下手之前就该知道的事（缺模型、批量节点的花费、视频慢且贵） */
  warnings: string[];
  /** 这份草稿需要哪些能力 */
  requires: string[];
  /** requires 里本机没有的 */
  missing: string[];
  nextAction: string;
}

/* ---------------- 社区分享 ---------------- */

export interface ShareLicense {
  key: string;
  label: string;
}

export interface ShareExportResult {
  snapshot: Record<string, unknown>;
  /** 压缩后的分享码，可以直接贴进聊天窗口 */
  code: string;
  /** 太长就别用分享码了（聊天窗口贴不下、还容易被截断），改用 .json 文件 */
  codeTooLong: boolean;
  codeLength: number;
}

export interface ShareImportResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  title: string;
  description: string;
  license: string;
  /** 授权范围的人话解释（后端给的，前端不自己维护一份） */
  licenseText: string;
  author: string;
  createdAt: string;
  appVersion: string;
  requires: { nodeKinds?: string[]; modalities?: string[] };
  missingNodeKinds: string[];
  missingModalities: string[];
  doc: CanvasDoc;
}

/** 日志与诊断报告 */
export interface LogFileInfo {
  name: string;
  size: number;
  modifiedAt: string;
}

export interface LogsStatus {
  dir: string;
  files: LogFileInfo[];
  totalBytes: number;
  maxBytesPerFile: number;
  backupCount: number;
}

export interface LogExport {
  text: string;
  generatedAt: string;
  errorLines: number;
  files: LogFileInfo[];
  bytes: number;
}

export interface TaskLogs {
  taskId: number;
  count: number;
  lines: string[];
}

/** 导演风格卡（画布节点的风格下拉用；具体内容存在后端配置表里） */
export interface StyleOption {
  key: string;
  name: string;
}

/** 快速接入的一个候选服务商 */
export interface QuickSetupCandidate {
  key: string;
  name: string;
  kind: string;
  baseUrl: string;
  hint: string;
  models: ModelSpec[];
}

/** 快速接入时对某个候选做过的一次连通试探 */
export interface QuickSetupAttempt {
  key: string;
  name: string;
  baseUrl: string;
  ok: boolean;
  message: string;
}

export interface QuickSetupResult {
  ok: boolean;
  /** true = 从 Key 形状认出了归属；false = 没认出来，candidates 是全量候选 */
  recognized: boolean;
  /** true = 连通测试没过但用户坚持保存 */
  forced: boolean;
  service: Provider | null;
  tried: QuickSetupAttempt[];
  candidates: QuickSetupCandidate[];
}

export interface OllamaStatus {
  running: boolean;
  baseUrl: string;
  models: string[];
  /** 探测失败的原因（没装 / 没启动都算正常，这里只是给个说法） */
  error: string;
  connected: boolean;
}

/** 首屏引导状态 */
export interface SetupStatus {
  services: number;
  enabledServices: number;
  modelCounts: Record<string, number>;
  ready: boolean;
  missing: string[];
  servicesDetail: { id: number; name: string; enabled: boolean; models: number }[];
  ollama: OllamaStatus;
}

export interface CanvasNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: CanvasNodeData;
}

export interface CanvasEdge {
  id: string;
  source: string;
  sourceHandle: string | null;
  target: string;
  targetHandle: string | null;
}

export interface CanvasDoc {
  schemaVersion: number;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  viewport: { x?: number; y?: number; zoom?: number };
}

/** 整图执行前的预估（「运行整图」二次确认弹窗用） */
export interface CanvasPreviewNode {
  id: string;
  type: string;
  label: string;
  /** 这一跑会派几个任务；null = 此刻算不出来（要等上游先跑） */
  count: number | null;
  kinds: Record<string, number>;
  /** 已经有产物（整图会把它们重做一遍） */
  hasOutput: boolean;
  outputCount: number;
  /** 条数待定：这些上游节点这次才会产出 */
  waiting: string[];
  /** 已经能判定必挂的原因 */
  error: string;
}

export interface CanvasPreviewNote {
  level: "blocker" | "warning" | "info";
  text: string;
}

export interface CanvasPreview {
  nodes: CanvasPreviewNode[];
  totals: {
    tasks: number;
    /** 调用次数（= 任务数；语义不同，文案要说「次」） */
    calls: number;
    steps: number;
    byKind: Record<string, number>;
    pendingNodes: number;
    rerunNodes: number;
    blockedNodes: number;
    /** 逐镜对白会额外做几次语音合成（不计在 calls 里，但那是真花钱的调用） */
    dialogueCalls?: number;
  };
  /** 预算闸：整图运行是否超过「调用上限」（超了要在弹窗里再确认一次） */
  gate: CanvasRunGate;
  reused: { assetId: number; name: string; count: number }[];
  notes: CanvasPreviewNote[];
  /** 这一跑会不会真调外部模型（全本机 ComfyUI 时为 false） */
  billable: boolean;
}

/**
 * 预算闸状态。
 *
 * `exceeds` 与 `uncertain` 必须分开看：前者是**已经确定**超了（要求再确认一次），
 * 后者只是「还有节点的次数要等上游跑完」。把后者也当成超限，会让一个还没跑过的
 * 项目永远点不动「运行整图」。
 */
export interface CanvasRunGate {
  limit: number;
  calls: number;
  pendingNodes: number;
  exceeds: boolean;
  uncertain: boolean;
  /** 超限时要回传给接口的确认值（就是调用次数） */
  ack: number;
}

/** 跑完之后的对账：这一跑实际调用几次、成了几条、失败几条、出了多少产物 */
export interface CanvasRunSummary {
  runId: string;
  calls: number;
  byKind: Record<string, number>;
  completed: number;
  failed: number;
  running: number;
  /** 只有「已有任务都结束」且「整图本身走完了」才为 true：整图是边跑边派任务的 */
  finished: boolean;
  /** 这一跑还在进行中（含「第一个节点跑完了、后面的还没派」这段空档） */
  active: boolean;
  /** 服务端受理时算出的预估调用次数；刷新页面后靠它把预估带回来 */
  expected: number;
  products: { images: number; videos: number; documents: number; videoSeconds: number };
  nodes: { id: string; label: string; calls: number; failed: number }[];
  startedAt: string | null;
  endedAt: string | null;
  elapsedSec: number | null;
}

/** 分镜静态体检查出的一个问题（形状与生成前的 preflight 告警一致，可复用同一个组件画） */
export interface CanvasLintFinding {
  code: string;
  level: "warn" | "info";
  message: string;
  suggestion: string;
  /** 涉及的镜号（「镜头3」这种），供定位 */
  shots: string[];
}

export interface CanvasLintNode {
  id: string;
  type: string;
  label: string;
  summary: {
    shots: number;
    sizes: Record<string, number>;
    moves: Record<string, number>;
    scenes: number;
    totalSeconds: number;
  };
  findings: CanvasLintFinding[];
}

/** 分镜静态体检结果（零成本：不建任务、不写库、不调模型） */
export interface CanvasLint {
  /** 恒为 false：体检只给建议，不拦你往下走 */
  blocking: boolean;
  /** 拍平后的告警，可直接喂 PreflightNotice */
  warnings: PreflightWarning[];
  nodes: CanvasLintNode[];
  /** 没内容的分镜节点（如实说明，而不是当成「没问题」） */
  skipped: { id: string; label: string; reason: string }[];
  shotTotal: number;
}

/**
 * 静图缓动样片的出片报告。
 *
 * `skipped` / `truncated` 必须显示出来：一条样片少了哪几镜、为什么少，
 * 是判断「这个节奏可不可信」的前提，藏起来就等于骗人。
 */
export interface CanvasAnimaticReport {
  /** 真的进了样片的镜数 */
  shots: number;
  seconds: number;
  /** 有镜头没进来：镜号 + 原因 */
  skipped: { shot: string; reason: string }[];
  /** 因为总时长上限被截掉的镜号 */
  truncated: string[];
  /** 这次用的总时长上限（秒）：面板要说清楚「截在哪里」 */
  maxSeconds: number;
  size: [number, number];
  fps: number;
  bytes: number;
  /** 旁白：关着时只有 enabled=false，其余字段不出现 */
  narration: {
    enabled: boolean;
    assetId?: number;
    url?: string;
    chars?: number;
    /** 念了几句（分镜表里有台词的镜数） */
    lines?: number;
    seconds?: number | null;
    voice?: string;
    /** 旁白比画面长之类的提醒；没有问题就是空串 */
    note?: string;
  };
}

export interface CanvasAnimaticResult {
  asset: Asset;
  report: CanvasAnimaticReport;
}

export interface CanvasNodeStatus {
  taskId: number;
  status: string;
  assetId: number | null;
  assetUrl?: string;
  assetKind?: string;
  assetName?: string;
  /** 文档节点（自动链）产物正文预览 */
  text?: string;
  /** 资产设定图一次运行产出的任务数 */
  taskCount?: number;
  /** 命中的角色提及注入资产名（后端自动挂的参考图） */
  injectedNames?: string[];
  /** 一次运行的全部产物（资产链 / 分镜图节点会有多张） */
  assets?: {
    id: number;
    url: string;
    kind: string;
    name: string;
    category: string;
    /** 网格上的短标签：分镜图是镜号「镜头3」，资产图是资产名；其余产物为空 */
    label?: string;
    /** 悬停说明：镜号+景别运镜+自动挂的参考图，或资产名+类型 */
    title?: string;
    /** 这一张属于哪一组候选（同一组的几张是同一镜 / 同一资产的备选，只能挑一张定稿） */
    candidateKey?: string;
  }[];
  progress?: number;
  error?: string | null;
  /** 当前交付的是第几版 / 共几版（0 表示这个节点还没有产物） */
  versionIndex?: number;
  versionTotal?: number;
  /** 当前交付版本的标识 */
  versionKey?: string;
  /** 当前交付的是不是最新那一版；false = 用户回滚过 */
  versionLatest?: boolean;
}

/** 节点的一版产物（同一节点多次生成各留一版，可回滚） */
export interface CanvasNodeVersion {
  key: string;
  /** 第几版，从 1 开始、从旧往新数 */
  index: number;
  total: number;
  latest: boolean;
  taskCount: number;
  doneCount: number;
  failedCount: number;
  runningCount: number;
  createdAt: string;
  /** 这一版一共有几件产物（`assets` 只带前几张当缩略图） */
  assetCount: number;
  assets: {
    id: number;
    url: string;
    kind: string;
    name: string;
    category: string;
    label?: string;
    title?: string;
  }[];
}

export interface CanvasNodeVersions {
  nodeId: string;
  /** 节点上写着的那个 key（可能因为历史被清而指不到任何一版） */
  pin: string;
  /** 实际生效的那一版 */
  activeKey: string;
  latestKey: string;
  /** 最新的在前 */
  items: CanvasNodeVersion[];
}

// ---- 创作 Agent（自动链） ----

export interface AgentMeta {
  key: string;
  label: string;
  /** 节点浮框里「补充要求」的填写提示 */
  varHint: string;
  /** 块数参数名（chapterCount / sceneCount / shotCount），空串表示单次成文 */
  chunkParam: string;
  /** 块数参数的中文标签 */
  chunkLabel: string;
  /** 是否走「先大纲后逐块续写」 */
  chunked: boolean;
  maxChunks: number;
}

// ---- ComfyUI 工作流 ----

export interface ComfyParamDef {
  key: string;
  label: string;
  type: "prompt" | "text" | "number" | "select" | "image" | "seed";
  nodeId: string;
  field: string;
  default: unknown;
  options?: string[];
  min?: number;
  max?: number;
  step?: number;
}

export interface ComfyWorkflow {
  id: number;
  name: string;
  providerId: number;
  outputKind: "image" | "video" | "mixed";
  paramMap: ComfyParamDef[];
  nodeCount: number;
  createdAt: string | null;
}

/** 提示词模板（公开接口只返回启用项） */
export interface PromptItem {
  id: number;
  key: string;
  title: string;
  modality: string;
  content: string;
  description: string;
  tags: string;
  version?: number;
}

export interface ProviderKindMeta {
  kind: string;
  label: string;
  hint: string;
}

export interface ProviderPresetMeta {
  key: string;
  name: string;
  kind: string;
  base_url: string;
  models: ModelSpec[];
  hint: string;
}

/** 注册表字段描述，用于动态生成管理界面 */
export interface FieldSpecMeta {
  name: string;
  label: string;
  type: "string" | "text" | "int" | "float" | "bool" | "json" | "select";
  required: boolean;
  options: string[];
  default: unknown;
  help: string;
  readonly: boolean;
}

export interface TableSpecMeta {
  name: string;
  label: string;
  description: string;
  group: string;
  has_version: boolean;
  fields: FieldSpecMeta[];
}

export type SchemaRow = Record<string, unknown> & { id: number; version?: number };

export interface AuditLog {
  id: number;
  table_name: string;
  row_id: number | null;
  action: "create" | "update" | "delete" | "rollback";
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  actor: string;
  created_at: string;
}

// ---- 配置导入导出 ----

/** 一个导出范围里包含的配置表 */
export interface ConfigScopeTable {
  name: string;
  label: string;
}

/** 可导出范围（由后端 /api/admin/config/scopes 驱动，后端加范围前端自动出现） */
export interface ConfigScopeMeta {
  name: string;
  label: string;
  description: string;
  /** 默认勾选（「用户资产」那一组） */
  default: boolean;
  tables: ConfigScopeTable[];
}

/** 固定排除项：哪个字段、为什么不带走 */
export interface ConfigExclusion {
  table: string;
  field: string;
  reason: string;
}

/** 导出的 JSON 快照（也是导入接口的请求体） */
export interface ConfigSnapshot {
  format: string;
  schemaVersion: number;
  appVersion: string;
  exportedAt: string;
  scopes: string[];
  tables: Record<string, Record<string, unknown>[]>;
  excluded: ConfigExclusion[];
  warnings: string[];
  notes: string[];
  /** 恒为 false —— 快照里不含任何密钥 */
  containsSecrets: boolean;
  /** 仅导入时带：目前只支持 merge */
  mode?: string;
}

export interface ConfigScopesMeta {
  format: string;
  schemaVersion: number;
  scopes: ConfigScopeMeta[];
  defaultScopes: string[];
  excluded: ConfigExclusion[];
  containsSecrets: boolean;
  notes: string[];
}

export interface ConfigImportCounts {
  created: number;
  updated: number;
  skipped: number;
}

/** 被拒收的一行（脏数据、带密钥、标识重复…），reason 可直接展示 */
export interface ConfigImportConflict {
  table: string;
  row: number | null;
  identity: string;
  reason: string;
}

export interface ConfigImportResult {
  ok: boolean;
  dryRun: boolean;
  mode: string;
  scopes: string[];
  source: { format: string; schemaVersion: number; appVersion: string; exportedAt: string };
  summary: Record<string, ConfigImportCounts>;
  totals: ConfigImportCounts;
  conflicts: ConfigImportConflict[];
  warnings: string[];
  notes: string[];
}

// ---- 在线更新 ----

export interface UpdateStatus {
  /** 当前版本（后端 app.__version__） */
  version: string;
  /** 当前短 commit（无 .git 时为空串） */
  commit: string;
  branch: string;
  /** git 克隆 / docker 容器 / 压缩包解压 */
  installKind: "git" | "docker" | "archive";
  /** 工作区是否有未提交改动；非 git 安装为 null（未知） */
  dirty: boolean | null;
  repoRoot: string;
  gitAvailable: boolean;
  /** 配置的更新源与仓库（owner/repo）；repo 是当前配置源对应的那个 */
  source: string;
  repo: string;
  /** Gitee 专用仓库路径（两个平台账号名不同时单独配） */
  repoGitee: string;
  hasUpdate: boolean;
  latest: string;
  notes: string;
  url: string;
  publishedAt: string;
  checkedAt: string;
  /** 实际取到数据的源与仓库（配置源不通时会回退到另一个） */
  usedSource: string;
  usedRepo: string;
  error?: string;
}

export interface UpdateStep {
  name: string;
  ok: boolean;
  output: string;
}

export interface UpdateRunResult {
  ok: boolean;
  needsRestart: boolean;
  steps: UpdateStep[];
  versions?: { before: string; after: string; branch: string };
  changed?: string[];
  message: string;
}
