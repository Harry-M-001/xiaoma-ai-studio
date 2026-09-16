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
  /** 导演风格卡标识（空 = 不注入风格） */
  styleKey?: string;
  /** 是否自动挂载提示词里提到的资产设定图（false = 关闭，默认开） */
  mentionRefs?: boolean;
  [k: string]: unknown;
}

/** 导演风格卡（画布节点的风格下拉用；具体内容存在后端配置表里） */
export interface StyleOption {
  key: string;
  name: string;
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
  }[];
  progress?: number;
  error?: string | null;
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
