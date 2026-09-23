import type {
  AgentMeta,
  AppInfo,
  Asset,
  AuditLog,
  ChatMessage,
  ChatSession,
  ComfyWorkflow,
  ConfigImportResult,
  ConfigMap,
  ConfigScopesMeta,
  ConfigSnapshot,
  EngineJob,
  EngineLocalTts,
  EnginePageData,
  InterpolateOptions,
  InterpolateRequest,
  LogExport,
  LogsStatus,
  ModalityMeta,
  ModelOption,
  ModelSpec,
  MergeArgs,
  MergePreview,
  NavMeta,
  ParamOptionItem,
  PreflightResult,
  PromptItem,
  Provider,
  ProviderInput,
  ProviderKindMeta,
  ProviderPresetMeta,
  ShareExportResult,
  ShareImportResult,
  ShareLicense,
  RemovalBox,
  RemovalFrame,
  RemovalMethod,
  RemovalPreview,
  SubtitleOptions,
  SubtitlePreviewResult,
  TransitionOptions,
  Project,
  CanvasDoc,
  CanvasNodeSchema,
  CanvasNodeStatus,
  CanvasAgentDraft,
  CanvasAnimaticResult,
  CanvasLint,
  CanvasNodeVersions,
  CanvasRunSummary,
  CanvasPreview,
  CameraMoveTable,
  OllamaStatus,
  QuickSetupResult,
  SchemaRow,
  SetupStatus,
  SpeechResult,
  SpeechVoices,
  StyleOption,
  TableSpecMeta,
  Task,
  TaskLogs,
  TaskRetryIn,
  UpdateRunResult,
  UpdateStatus,
  UpscaleOptions,
  UpscaleRequest,
} from "./types";

const TOKEN_KEY = "xm_token";

export const authToken = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface RequestOptions extends RequestInit {
  raw?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const isForm = options.body instanceof FormData;
  if (!isForm && options.body !== undefined) headers.set("Content-Type", "application/json");
  const t = authToken.get();
  if (t) headers.set("Authorization", `Bearer ${t}`);

  const res = await fetch(path, { ...options, headers });

  if (res.status === 401) {
    authToken.clear();
    window.dispatchEvent(new CustomEvent("xm:unauthorized"));
    throw new ApiError("未授权，请重新输入口令", 401);
  }
  if (!res.ok) {
    let detail = `请求失败（${res.status}）`;
    try {
      const data = await res.json();
      if (data?.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const json = (body: unknown) => JSON.stringify(body);

// ---------- 系统 ----------
export const api = {
  appInfo: () => request<AppInfo>("/api/app/info"),
  login: (password: string) =>
    request<{ token: string }>("/api/auth/login", { method: "POST", body: json({ password }) }),
  authCheck: () => request<{ ok: boolean }>("/api/auth/check"),

  // ---------- 模型服务 ----------
  listProviders: () => request<Provider[]>("/api/providers"),
  createProvider: (p: ProviderInput) =>
    request<Provider>("/api/providers", { method: "POST", body: json(p) }),
  updateProvider: (id: number, p: ProviderInput) =>
    request<Provider>(`/api/providers/${id}`, { method: "PUT", body: json(p) }),
  deleteProvider: (id: number) =>
    request<{ ok: boolean }>(`/api/providers/${id}`, { method: "DELETE" }),
  testProvider: (payload: {
    kind: string;
    base_url: string;
    api_key?: string | null;
    service_id?: number | null;
    model?: string | null;
  }) =>
    request<{ ok: boolean; message?: string }>("/api/providers/test", {
      method: "POST",
      body: json(payload),
    }),
  listModels: (modality?: string) =>
    request<ModelOption[]>(`/api/providers/models${modality ? `?modality=${modality}` : ""}`),
  /** 粘贴一个 Key 自动接入（后端会先试一次真实请求，通了才存） */
  quickSetup: (payload: { api_key: string; provider_key?: string; force?: boolean }) =>
    request<QuickSetupResult>("/api/providers/quick-setup", { method: "POST", body: json(payload) }),
  /** 本机 Ollama 检测（服务端探 127.0.0.1:11434，带缓存） */
  ollamaStatus: (refresh = false) =>
    request<OllamaStatus>(`/api/providers/ollama${refresh ? "?refresh=true" : ""}`),
  ollamaConnect: () => request<{ ok: boolean; service: Provider; models: ModelSpec[] }>(
    "/api/providers/ollama",
    { method: "POST" },
  ),
  /** 首屏引导状态：有没有服务、各能力有几个模型、本机有没有 Ollama */
  setupStatus: () => request<SetupStatus>("/api/meta/setup-status"),

  // ---------- 对话 ----------
  listSessions: () => request<ChatSession[]>("/api/chat/sessions"),
  createSession: () =>
    request<ChatSession>("/api/chat/sessions", { method: "POST" }),
  deleteSession: (id: number) =>
    request<{ ok: boolean }>(`/api/chat/sessions/${id}`, { method: "DELETE" }),
  listMessages: (id: number) =>
    request<ChatMessage[]>(`/api/chat/sessions/${id}/messages`),

  // ---------- 任务 ----------
  createImageTask: (payload: {
    model_key: string;
    prompt: string;
    size: string;
    n: number;
    ref_asset_ids: number[];
  }) => request<Task>("/api/images/generations", { method: "POST", body: json(payload) }),
  createImageBatchTasks: (payload: {
    prompts: string[];
    model_keys: string[];
    size: string;
    n: number;
    ref_asset_ids: number[];
  }) => request<Task[]>("/api/images/batch", { method: "POST", body: json(payload) }),

  createVideoTask: (payload: {
    model_key: string;
    prompt: string;
    first_frame_asset_id?: number | null;
    /** 对白音轨（数字人/口播）：一条音频资产的 id，随请求作为参考音频附发 */
    audio_ref_asset_id?: number | null;
    duration: number;
    ratio: string;
    resolution: string;
  }) => request<Task>("/api/videos/generations", { method: "POST", body: json(payload) }),

  // 生成前软校验：只回告警，永远不拦人（后端 blocking 恒为 false）。
  // 这三个调用**失败也不该挡住生成**，所以调用点要自己吞掉异常。
  preflightImage: (payload: {
    model_key: string;
    prompt: string;
    n: number;
    ref_asset_ids: number[];
  }) => request<PreflightResult>("/api/images/preflight", { method: "POST", body: json(payload) }),
  preflightImageBatch: (payload: { prompts: string[]; model_keys: string[]; n: number }) =>
    request<PreflightResult>("/api/images/batch/preflight", { method: "POST", body: json(payload) }),
  preflightVideo: (payload: {
    model_key: string;
    prompt: string;
    first_frame_asset_id?: number | null;
    ratio: string;
    resolution: string;
  }) => request<PreflightResult>("/api/videos/preflight", { method: "POST", body: json(payload) }),

  // 分享：服务端不托管任何内容，这里只做「打包」与「读懂」两件纯事
  shareLicenses: () =>
    request<{ default: string; options: ShareLicense[] }>("/api/share/licenses"),
  shareExport: (payload: {
    doc: unknown;
    title: string;
    description: string;
    author: string;
    license: string;
  }) => request<ShareExportResult>("/api/share/export", { method: "POST", body: json(payload) }),
  shareImport: (text: string) =>
    request<ShareImportResult>("/api/share/import", { method: "POST", body: json({ text }) }),

  listTasks: (kind?: string, limit = 20) =>
    request<Task[]>(`/api/tasks?limit=${limit}${kind ? `&kind=${kind}` : ""}`),
  getTask: (id: number) => request<Task>(`/api/tasks/${id}`),
  cancelTask: (id: number) => request<Task>(`/api/tasks/${id}/cancel`, { method: "POST" }),
  retryTask: (id: number, overrides?: TaskRetryIn) =>
    request<Task>(`/api/tasks/${id}/retry`, {
      method: "POST",
      body: json(overrides ?? {}),
    }),
  revealTask: (id: number) =>
    request<{ ok: boolean; path: string; name: string }>(`/api/tasks/${id}/reveal`, {
      method: "POST",
    }),
  deleteTask: (id: number) => request<{ ok: boolean }>(`/api/tasks/${id}`, { method: "DELETE" }),

  // ---------- 资产 ----------
  listAssets: (params: { kind?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.kind) q.set("kind", params.kind);
    q.set("limit", String(params.limit ?? 60));
    q.set("offset", String(params.offset ?? 0));
    return request<{ items: Asset[]; total: number }>(`/api/assets?${q.toString()}`);
  },
  uploadAsset: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<Asset>("/api/assets/upload", { method: "POST", body: fd });
  },
  deleteAsset: (id: number) =>
    request<{ ok: boolean }>(`/api/assets/${id}`, { method: "DELETE" }),
  // ---- 导演台 ----
  ffmpegStatus: () => request<{ available: boolean; version: string | null }>("/api/director/ffmpeg"),
  listDirectorVideos: () => request<Asset[]>("/api/director/videos"),
  importDirectorVideo: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<Asset>("/api/director/import", { method: "POST", body: fd });
  },
  probeVideo: (assetId: number) =>
    request<{ duration: number | null; width: number | null; height: number | null; codec: string | null }>(
      "/api/director/probe",
      { method: "POST", body: JSON.stringify({ asset_id: assetId }) }
    ),
  extractClip: (assetId: number, start: number, end: number) =>
    request<Asset>("/api/director/extract", {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, start, end }),
    }),
  extractThumbnail: (assetId: number, t: number) =>
    request<Asset>("/api/director/thumbnail", {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, t }),
    }),
  transitionOptions: () => request<TransitionOptions>("/api/director/transitions"),
  /**
   * 合并前算账：成片多长、比硬切短多少、这样接行不行。
   *
   * **纯读**（后端只跑 ffprobe 量时长，不写盘不编码），所以可以放心在每次改动后调，
   * 让「会变短」这件事在**点合并之前**就说出来，而不是等片子出来发现短了。
   */
  mergePreview: (args: MergeArgs) =>
    request<MergePreview>("/api/director/merge/preview", {
      method: "POST",
      body: JSON.stringify({
        asset_ids: args.assetIds,
        transition: args.transition ?? "cut",
        transition_seconds: args.transitionSeconds ?? null,
        sfx: args.sfx ?? "",
        sfx_asset_id: args.sfxAssetId ?? null,
      }),
    }),
  mergeVideos: (args: MergeArgs) =>
    request<Asset>("/api/director/merge", {
      method: "POST",
      body: JSON.stringify({
        asset_ids: args.assetIds,
        transition: args.transition ?? "cut",
        transition_seconds: args.transitionSeconds ?? null,
        sfx: args.sfx ?? "",
        sfx_asset_id: args.sfxAssetId ?? null,
        subtitle_style: args.subtitleStyle ?? "",
        subtitle_scale: args.subtitleScale ?? null,
        subtitles: args.subtitles ?? [],
      }),
    }),
  // ---- 字幕（版式与字体） ----
  /** 版式 + 字体 + 本机能不能烧。**一次给全**，前端不另抄任何一份表 */
  subtitleOptions: () => request<SubtitleOptions>("/api/subtitles/options"),
  /** 下载并安装一款字体（下到本机数据目录，不进程序目录） */
  downloadSubtitleFont: (key: string) =>
    request<{ ok: boolean; message: string; options: SubtitleOptions }>(
      `/api/subtitles/fonts/${encodeURIComponent(key)}/download`,
      { method: "POST" }
    ),
  /**
   * 版式预览：后端**真渲染一帧**回来（JPEG 的 data URI）。
   *
   * 不在前端用 CSS 近似画：版式取决于 libass 的字体选择、字形、描边、缩放、边距，
   * 前端再写一套必然对不上——「预览看着挺好、导出来不一样」是最伤信任的一种不一致。
   */
  subtitlePreview: (args: { style: string; scale?: number; text?: string; width: number; height: number }) =>
    request<SubtitlePreviewResult>("/api/subtitles/preview", {
      method: "POST",
      body: JSON.stringify(args),
    }),
  // ---- 去字幕（v1.1.27） ----
  /**
   * 取一帧用来框选字幕区域，**一次回齐**：帧 + 源视频尺寸 + 推荐框 + 四种手法能不能用。
   *
   * 尺寸回的是源视频像素（框的坐标系），显示用的图后端缩到 1280 以内。
   * 分几次拿会出现「框按旧尺寸画、手法按新尺寸判」这种对不上的中间状态。
   */
  removalFrame: (args: { assetId: number; t?: number }) =>
    request<RemovalFrame>("/api/director/subtitle-removal/frame", {
      method: "POST",
      body: json({ asset_id: args.assetId, t: args.t ?? 0 }),
    }),
  /** 只问「这个框上四种手法各能不能用」——拖动之后可选项会变，而且它不碰 ffmpeg */
  removalCheck: (args: { assetId: number; box: RemovalBox }) =>
    request<{ box: RemovalBox; methods: RemovalMethod[]; defaultMethod: string }>(
      "/api/director/subtitle-removal/check",
      { method: "POST", body: json({ asset_id: args.assetId, box: args.box }) }
    ),
  /** 去字幕预览：后端把同一帧跑一遍同一套滤镜，回 JPEG data URI（不是前端近似画） */
  removalPreview: (args: { assetId: number; t: number; box: RemovalBox; method: string }) =>
    request<RemovalPreview>("/api/director/subtitle-removal/preview", {
      method: "POST",
      body: json({ asset_id: args.assetId, t: args.t, box: args.box, method: args.method }),
    }),
  /** 按这个框与手法处理整段，产物作为**新资产**入库（不动原片） */
  removeSubtitles: (args: { assetId: number; box: RemovalBox; method: string }) =>
    request<Asset>("/api/director/subtitle-removal", {
      method: "POST",
      body: json({ asset_id: args.assetId, box: args.box, method: args.method }),
    }),
  // ---- 超分 / 放大（v1.1.31）----
  /**
   * 一次拿全：这个素材能不能放大、有哪几条路线、每条能用什么模型与倍数、
   * 用哪块显卡、上限是多少。
   *
   * 不在前端拼这份清单：本机引擎装没装、显卡能不能用只有后端知道，
   * 前端猜一次就是一次「界面说行、点下去报错」。
   */
  upscaleOptions: (assetId: number) =>
    request<UpscaleOptions>(`/api/upscale/options?asset_id=${assetId}`),
  /** 图片放大：**同步**（秒级），返回新建的资产（不动原图） */
  upscaleImage: (body: UpscaleRequest) =>
    request<Asset>("/api/upscale/image", { method: "POST", body: json(body) }),
  /** 视频放大：**异步**（逐帧过一遍），返回任务；进度走既有的 getTask */
  upscaleVideo: (body: UpscaleRequest) =>
    request<Task>("/api/upscale/video", { method: "POST", body: json(body) }),
  /**
   * 图片放大走自己的 ComfyUI 工作流：**异步**，返回任务。
   *
   * 与 `upscaleImage` 分开是必须的：ComfyUI 出一张图要排队 + 加载模型，
   * 快的时候也要十几秒，做成同步请求只会让前端一直挂着等。
   */
  upscaleComfy: (body: UpscaleRequest) =>
    request<Task>("/api/upscale/comfy", { method: "POST", body: json(body) }),
  // ---- 补帧（v1.1.32）----
  /**
   * 一次拿全：这段视频能不能补、有哪些模型、每款能补到哪些帧率、用哪块显卡。
   *
   * **能补到多少帧跟着模型走**（只有 v4 系能自定义帧数），所以这份清单必须由后端给：
   * 前端自己列一列「常见帧率」，选到只做 2 倍的模型就会是一个必然被拒的组合。
   * 只对视频有意义（图片会 400——图片该用「放大」）。
   */
  interpolateOptions: (assetId: number) =>
    request<InterpolateOptions>(`/api/interpolate/options?asset_id=${assetId}`),
  /** 补帧：**异步**（逐帧过一遍，几分钟起），返回任务；进度走既有的 getTask */
  interpolateVideo: (body: InterpolateRequest) =>
    request<Task>("/api/interpolate/video", { method: "POST", body: json(body) }),
  // ---- 项目 ----
  listProjects: () =>
    request<Project[]>("/api/projects").then((r) => (Array.isArray(r) ? r : [])),
  createProject: (name: string, description: string) =>
    request<Project>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),
  updateProject: (id: number, patch: { name?: string; description?: string; status?: string }) =>
    request<Project>(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteProject: (id: number) =>
    request<{ ok: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),
  // ---- 画布 ----
  canvasContract: () =>
    request<{
      schemaVersion: number;
      nodeSchemas: Record<string, CanvasNodeSchema>;
    }>("/api/canvas/contract"),
  // AI 搭画布：只回草稿，不动画布。落定由前端做（它本来就有画布状态与自动保存）。
  canvasAgentPlan: (payload: { brief: string; model_key?: string }) =>
    request<CanvasAgentDraft>("/api/canvas/agent/plan", { method: "POST", body: json(payload) }),
  getCanvas: (projectId: number) =>
    request<CanvasDoc>(`/api/canvas/${projectId}`),
  saveCanvas: (projectId: number, doc: CanvasDoc) =>
    request<{ ok: boolean }>(`/api/canvas/${projectId}`, {
      method: "PUT",
      body: JSON.stringify({ nodes: doc.nodes, edges: doc.edges, viewport: doc.viewport }),
    }),
  /**
   * 运行节点 / 整图。
   *
   * `ackCalls` 只在整图运行超过配额上限时才需要：把确认弹窗里看到的调用次数回传过去。
   * 服务端会**按此刻的画布重算一遍**再比（预览之后改过画布的话，旧数字挡不住）。
   */
  runCanvas: (projectId: number, nodeId?: string, ackCalls = 0) =>
    request<{
      mode: string;
      nodeId: string | null;
      /** 这一跑的标识：拿它去查「实际派了多少」（见 runSummary） */
      runId?: string;
      taskId: number | null;
      /** 资产设定图一次会派发多个任务（一行资产一个） */
      taskIds?: number[];
      taskCount?: number;
      estimate?: { calls: number };
    }>(`/api/canvas/${projectId}/run`, {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId ?? null, ack_calls: ackCalls }),
    }),
  /** 跑完之后对一次账：这一跑实际调用几次、成了几条、失败几条、出了多少产物 */
  runSummary: (projectId: number, runId: string) =>
    request<CanvasRunSummary>(
      `/api/canvas/${projectId}/run-summary?run_id=${encodeURIComponent(runId)}`,
    ),
  // 整图执行前的预估：会派多少任务、花多少次调用（纯读，不建任务）
  previewCanvas: (projectId: number) =>
    request<CanvasPreview>(`/api/canvas/${projectId}/preview`),
  // 分镜静态体检：零成本检查镜头语言（纯读，不建任务、不调模型）
  lintCanvas: (projectId: number) =>
    request<CanvasLint>(`/api/canvas/${projectId}/lint`),
  /**
   * 某个节点的历史版本（同一节点多次生成各留一版）。
   *
   * 只读：回滚是把这个节点的 `data.versionKey` 改成要用的那一版，走普通的保存画布那条路
   * ——不另开一个写接口，免得出现两条写画布的路径。
   */
  nodeVersions: (projectId: number, nodeId: string) =>
    request<CanvasNodeVersions>(
      `/api/canvas/${projectId}/nodes/${encodeURIComponent(nodeId)}/versions`,
    ),
  /**
   * 出静图缓动样片：本地 ffmpeg 按分镜表的时长与运镜把分镜图串成一条片子。
   * 不花生成费，但要占 CPU，服务端同时只允许渲染一条（忙时回 409）。
   */
  renderAnimatic: (projectId: number, nodeId: string) =>
    request<CanvasAnimaticResult>(
      `/api/canvas/${projectId}/nodes/${encodeURIComponent(nodeId)}/animatic`,
      { method: "POST" },
    ),
  // ---- 配音（语音合成）----
  /** 音色快捷选项与这一段的上限（音色不做白名单，允许手填别家的 id） */
  speechVoices: (modelKey?: string) =>
    request<SpeechVoices>(
      modelKey ? `/api/audio/voices?model_key=${encodeURIComponent(modelKey)}` : "/api/audio/voices",
    ),
  /** 合成一段语音：同步返回，产物已落成音频资产 */
  speech: (payload: { text: string; model_key: string; voice?: string; speed?: number }) =>
    request<SpeechResult>("/api/audio/speech", { method: "POST", body: json(payload) }),
  canvasStatus: (projectId: number) =>
    request<{ nodes: Record<string, CanvasNodeStatus> }>(`/api/canvas/${projectId}/status`),
  getAgentPrompts: () => request<AgentMeta[]>("/api/meta/agent-prompts"),
  listDirectorStyles: () => request<StyleOption[]>("/api/meta/director-styles"),
  // 运镜词表：分镜表里那一栏的合法写法。体检报「不在词表里」时前端拿它显示可选值，
  // 前端不写死这份清单（与其它 meta 接口同一个口径）。
  listCameraMoves: () => request<CameraMoveTable>("/api/meta/camera-moves"),

  // ---- 本机引擎（批次 9）----
  // 清单、体检、装没装上、下到哪儿了，全在这一个接口里（前端不另抄表）
  listEngines: () => request<EnginePageData>("/api/engines"),
  /** 用户点了「重新检测」（刚插上显卡 / 刚装完驱动时用） */
  refreshEngineHardware: () =>
    request<EnginePageData>("/api/engines/hardware/refresh", { method: "POST" }),
  downloadEngine: (key: string) =>
    request<{ job: EngineJob }>(`/api/engines/${key}/download`, { method: "POST" }),
  cancelEngine: (key: string) =>
    request<{ job: EngineJob }>(`/api/engines/${key}/cancel`, { method: "POST" }),
  removeEngine: (key: string) =>
    request<{ key: string; freed: number; freedText: string }>(
      `/api/engines/${key}/remove`,
      { method: "POST" },
    ),
  verifyEngine: (key: string) =>
    request<{ key: string; ok: boolean; bytes: number; detail: string }>(
      `/api/engines/${key}/verify`,
      { method: "POST" },
    ),
  /** 本机配音：把装好的引擎接成一条音频模型服务（接完配音页就能选「本机跑」） */
  connectLocalTts: () =>
    request<EngineLocalTts>("/api/engines/local-tts/connect", { method: "POST" }),
  disconnectLocalTts: () =>
    request<EngineLocalTts>("/api/engines/local-tts/disconnect", { method: "POST" }),

  // ---- 在线更新 ----
  getUpdateStatus: () => request<UpdateStatus>("/api/update/status"),
  checkUpdate: () => request<UpdateStatus>("/api/update/check", { method: "POST" }),
  runUpdate: () => request<UpdateRunResult>("/api/update/run", { method: "POST" }),

  // ---- 日志与诊断 ----
  logsStatus: () => request<LogsStatus>("/api/logs/status"),
  exportLogs: (errorLines = 200) => request<LogExport>(`/api/logs/export?errorLines=${errorLines}`),
  exportIssue: (errorLines = 200) => request<LogExport>(`/api/logs/issue?errorLines=${errorLines}`),
  taskLogs: (id: number) => request<TaskLogs>(`/api/tasks/${id}/logs`),

  // ---- ComfyUI 工作流 ----
  listComfyWorkflows: () => request<ComfyWorkflow[]>("/api/comfy/workflows"),
  uploadComfyWorkflow: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<ComfyWorkflow>("/api/comfy/workflows", { method: "POST", body: fd });
  },
  deleteComfyWorkflow: (id: number) =>
    request<{ ok: boolean }>(`/api/comfy/workflows/${id}`, { method: "DELETE" }),
  refreshComfyWorkflow: (id: number) =>
    request<ComfyWorkflow>(`/api/comfy/workflows/${id}/refresh`, { method: "POST" }),

  // ---------- 元数据（全部由后端配置表驱动，前端不写死清单） ----------
  getConfig: () => request<ConfigMap>("/api/meta/config"),
  getModalities: () => request<ModalityMeta[]>("/api/meta/modalities"),
  getParamOptions: (kind?: string) =>
    request<Record<string, ParamOptionItem[]>>(
      `/api/meta/param-options${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`
    ),
  getNav: () => request<NavMeta[]>("/api/meta/nav"),
  getPrompts: () => request<PromptItem[]>("/api/meta/prompts"),
  getProviderKinds: () => request<ProviderKindMeta[]>("/api/meta/provider-kinds"),
  getProviderPresets: () => request<ProviderPresetMeta[]>("/api/meta/provider-presets"),

  // ---------- 通用配置管理（schema 注册制，后端加表这里自动多一个） ----------
  listSchemas: () => request<TableSpecMeta[]>("/api/admin/schema"),
  listSchemaRows: (table: string) => request<SchemaRow[]>(`/api/admin/schema/${table}`),
  createSchemaRow: (table: string, payload: Record<string, unknown>) =>
    request<SchemaRow>(`/api/admin/schema/${table}`, { method: "POST", body: json(payload) }),
  updateSchemaRow: (table: string, id: number, payload: Record<string, unknown>) =>
    request<SchemaRow>(`/api/admin/schema/${table}/${id}`, {
      method: "PUT",
      body: json(payload),
    }),
  deleteSchemaRow: (table: string, id: number) =>
    request<{ ok: boolean }>(`/api/admin/schema/${table}/${id}`, { method: "DELETE" }),
  listSchemaAudit: (table: string, limit = 50) =>
    request<AuditLog[]>(`/api/admin/schema/${table}/audit?limit=${limit}`),
  rollbackSchemaRow: (table: string, logId: number) =>
    request<SchemaRow>(`/api/admin/schema/${table}/rollback/${logId}`, {
      method: "POST",
      body: json({}),
    }),

  // ---------- 配置导入导出 ----------
  /** 可用范围 + 固定排除项说明（前端的多选框与「不含 API Key」都由它驱动） */
  configScopes: () => request<ConfigScopesMeta>("/api/admin/config/scopes"),
  exportConfig: (scopes: string[]) =>
    request<ConfigSnapshot>(
      `/api/admin/config/export?scopes=${encodeURIComponent(scopes.join(","))}`,
    ),
  /** dryRun=true 只预览（一行都不写），false 才真正落库 */
  importConfig: (snapshot: ConfigSnapshot, dryRun: boolean) =>
    request<ConfigImportResult>(`/api/admin/config/import?dryRun=${dryRun ? "true" : "false"}`, {
      method: "POST",
      body: json(snapshot),
    }),
};

/* ---------------- 配置读取辅助（带默认值与类型收敛） ---------------- */

export function cfgString(map: ConfigMap, key: string, fallback = ""): string {
  const v = map[key];
  return v === undefined || v === null ? fallback : String(v);
}

export function cfgNumber(map: ConfigMap, key: string, fallback: number): number {
  const v = Number(map[key]);
  return Number.isFinite(v) ? v : fallback;
}

export function cfgBool(map: ConfigMap, key: string, fallback = false): boolean {
  const v = map[key];
  if (v === undefined || v === null) return fallback;
  if (typeof v === "boolean") return v;
  return ["1", "true", "yes", "on"].includes(String(v).toLowerCase());
}

// ---------- SSE 流式对话（用 fetch 以便携带 Authorization） ----------

export type ChatStreamEvent =
  | { type: "meta"; session_id: number; title: string }
  | { type: "delta"; text: string }
  | { type: "done"; model: string }
  | { type: "error"; message: string };

export async function* chatStream(payload: {
  model_key: string;
  messages: { role: string; content: string }[];
  session_id?: number | null;
  temperature?: number;
}): AsyncGenerator<ChatStreamEvent> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const t = authToken.get();
  if (t) headers["Authorization"] = `Bearer ${t}`;

  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!res.ok || !res.body) {
    let msg = `连接失败（${res.status}）`;
    try {
      const d = await res.json();
      if (d?.detail) msg = d.detail;
    } catch {
      /* ignore */
    }
    yield { type: "error", message: msg };
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        yield JSON.parse(line.slice(6)) as ChatStreamEvent;
      } catch {
        /* ignore malformed event */
      }
    }
  }
}
