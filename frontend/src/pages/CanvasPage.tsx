import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  NodeToolbar,
  Position,
  addEdge,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  FileText,
  ImageIcon,
  Images,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Trash2,
  Upload,
  Video,
  Workflow as WorkflowIcon,
  X,
} from "lucide-react";
import { api } from "../api";
import type {
  AgentMeta,
  Asset,
  CanvasDoc,
  CanvasNodeData,
  CanvasNodeSchema,
  CanvasNodeStatus,
  ComfyWorkflow,
  ModelOption,
  StyleOption,
} from "../types";
import { ModelSelect, Spinner } from "../components/common";
import { useToast } from "../components/Toast";
import { prefs } from "../prefs";
// 自动链的拓扑与铺链逻辑在 canvasChain.ts：示例模板要用同一套形状，
// 抽出去之后两边不会各自漂移。
import {
  ASSET_IMAGE_KIND,
  CHAIN_LEVELS,
  STORYBOARD_IMAGE_KIND,
  buildChainNodes,
  missingModelMessage,
  toCanvasDocNodes,
} from "../canvasChain";

/** 自动链文档节点类型（需要调 LLM 写正文） */
const DOC_KINDS = new Set(["idea", "novel", "script", "storyboard", "assetSheet"]);

/** 资产设定图的生成范围档位 */
const ASSET_SCOPES: [string, string][] = [
  ["", "全部"],
  ["character", "仅角色"],
  ["scene", "仅场景"],
  ["prop", "仅道具"],
];

/** 契约驱动的类别图标 */
const CATEGORY_ICON: Record<string, React.ReactNode> = {
  document: <FileText size={13} />,
  image: <ImageIcon size={13} />,
  video: <Video size={13} />,
  tool: <WorkflowIcon size={13} />,
};

/** 类别色点（元素列表用） */
const CATEGORY_DOT: Record<string, string> = {
  document: "#8b8ff7",
  image: "#f07de0",
  video: "#5ad1c6",
  tool: "#f5a742",
};

const STATUS_BADGE: Record<string, string> = {
  pending: "排队中",
  processing: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 节点参考图（图库选择 / 本地上传），后端按 id 取用 */
interface NodeRefImage {
  id: number;
  url: string;
}

/** 灯箱展示项：图库资产与节点产物共用同一个预览层 */
interface LightboxItem {
  url: string;
  kind: string;
  title: string;
  meta?: string;
}

/** 文档正文弹窗的请求参数 */
interface DocEditRequest {
  nodeId: string;
  /** 节点标题，例如「分镜」 */
  label: string;
  /** 打开时放进编辑器的内容（手改正文，没有则用生成结果） */
  text: string;
  /** 当前是否已经存在手改正文 */
  hasOverride: boolean;
  /** 生成结果的下载地址：编辑前用它取全文（节点状态里那份是截断过的预览） */
  url?: string;
}

/** 节点浮框与页面通信（避免把全局态塞进 node.data 被持久化） */
interface NodePanelCtx {
  models: ModelOption[];
  workflows: ComfyWorkflow[];
  agents: AgentMeta[];
  /** 导演风格卡（风格下拉用；只有 key 与名称，实际内容在后端） */
  styles: StyleOption[];
  running: boolean;
  updateNode: (id: string, patch: Partial<CanvasNodeData>) => void;
  /** 把同一个风格套到画布上所有支持风格的节点（省得七八个节点逐个选） */
  applyStyleToAll: (key: string) => void;
  /** 按屏幕像素平移动画布（浮框超出可视区时用来自动让位） */
  nudgeViewport: (dxScreen: number, dyScreen: number) => void;
  /** 放大查看某个产物（浮框里的产物网格用） */
  preview: (url: string, title?: string, kind?: string) => void;
  /** 产物缩略图大小（像素）与修改入口，全局记忆 */
  thumb: number;
  pickThumb: (v: number) => void;
  /** 打开文档正文编辑器（大弹窗） */
  editDoc: (req: DocEditRequest) => void;
  runNode: (id: string) => void;
  openPicker: (nodeId: string, slot?: "first" | "last") => void;
  reloadWorkflows: () => Promise<ComfyWorkflow[]>;
}
const PanelCtx = createContext<NodePanelCtx | null>(null);

let nodeSeq = 0;

/**
 * IME 安全输入：本地 draft 缓冲，中文组词（composition）期间只写本地，
 * 不碰全局 nodes store，避免每键重建打断拼音；组词结束/失焦才提交。
 */
function PromptArea({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState(value);
  const composingRef = useRef(false);
  const focusedRef = useRef(false);

  useEffect(() => {
    if (!focusedRef.current && !composingRef.current) setDraft(value);
  }, [value]);

  return (
    <textarea
      className="textarea"
      rows={4}
      value={draft}
      placeholder={placeholder}
      onFocus={() => {
        focusedRef.current = true;
        setDraft(value);
      }}
      onBlur={() => {
        focusedRef.current = false;
        onChange(draft);
      }}
      onCompositionStart={() => {
        composingRef.current = true;
      }}
      onCompositionEnd={(e) => {
        composingRef.current = false;
        const v = e.currentTarget.value;
        setDraft(v);
        onChange(v);
      }}
      onChange={(e) => {
        const v = e.target.value;
        setDraft(v);
        if (!composingRef.current) onChange(v);
      }}
    />
  );
}

/** ComfyUI 工作流区块：选择/上传工作流 + 按 paramMap 动态渲染参数表单 */
function WorkflowSection({ id, data }: { id: string; data: CanvasNodeData }) {
  const ctx = useContext(PanelCtx);
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  if (!ctx) return null;

  const workflows = ctx.workflows;
  const wf = workflows.find((w) => w.id === Number(data.workflowId ?? 0));
  const paramValues = (data.paramValues as Record<string, unknown>) ?? {};
  const setParam = (key: string, v: unknown) => {
    const next = { ...paramValues };
    if (v === "" || v === undefined || v === null) delete next[key];
    else next[key] = v;
    ctx.updateNode(id, { paramValues: next });
  };

  const pickWorkflow = (w: ComfyWorkflow | undefined) => {
    const patchData: Partial<CanvasNodeData> = { paramValues: {} };
    if (w) {
      patchData.workflowId = w.id;
      // 工作流正向提示词默认值预填（用户已写过提示词则不覆盖）
      const pos = w.paramMap.find((p) => p.type === "prompt");
      const own = String(data.prompt ?? "").trim();
      if (pos && pos.default && !own) patchData.prompt = String(pos.default);
    } else {
      patchData.workflowId = undefined;
    }
    ctx.updateNode(id, patchData);
  };

  const onUploadWf = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(true);
    try {
      const w = await api.uploadComfyWorkflow(files[0]);
      await ctx.reloadWorkflows();
      pickWorkflow(w);
      toast.success(`工作流「${w.name}」已上传（${w.nodeCount} 节点，${w.paramMap.length} 个可调参数）`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "上传失败");
    } finally {
      setBusy(false);
    }
  };

  const onRefreshWf = async () => {
    if (!wf) return;
    setBusy(true);
    try {
      const w = await api.refreshComfyWorkflow(wf.id);
      await ctx.reloadWorkflows();
      toast.success(`已重新解析（${w.paramMap.length} 个可调参数）`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "解析失败");
    } finally {
      setBusy(false);
    }
  };

  const onDeleteWf = async () => {
    if (!wf) return;
    if (!window.confirm(`删除工作流「${wf.name}」？正在使用它的节点将无法运行。`)) return;
    setBusy(true);
    try {
      await api.deleteComfyWorkflow(wf.id);
      await ctx.reloadWorkflows();
      if (Number(data.workflowId ?? 0) === wf.id) ctx.updateNode(id, { workflowId: undefined, paramValues: {} });
      toast.success("已删除");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    } finally {
      setBusy(false);
    }
  };

  // 提示词由节点提示词框（含上游注入）承担；参考图槽位由 refImages（上游/图库）按顺序注入
  const formParams = wf ? wf.paramMap.filter((p) => p.type !== "prompt" && p.type !== "image") : [];
  const imageSlots = wf ? wf.paramMap.filter((p) => p.type === "image").length : 0;

  return (
    <div className="field">
      <label className="field-label">工作流{wf ? `（${wf.outputKind === "mixed" ? "图+视频" : wf.outputKind === "video" ? "视频" : "图片"} · ${wf.nodeCount} 节点）` : ""}</label>
      <div className="canvas-wf-row">
        <select
          className="select"
          value={wf ? String(wf.id) : ""}
          onChange={(e) => pickWorkflow(workflows.find((w) => w.id === Number(e.target.value)))}
        >
          <option value="">选择工作流…</option>
          {workflows.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn btn-ghost btn-xs" disabled={busy} onClick={() => fileRef.current?.click()} title="上传 workflow_api.json">
          {busy ? <Spinner /> : <Upload size={12} />}
          上传
        </button>
        <button type="button" className="btn btn-ghost btn-xs" disabled={busy || !wf} onClick={() => void onRefreshWf()} title="重新解析（ComfyUI 启动后补全模型/采样器选项）">
          <RefreshCw size={12} />
        </button>
        <button type="button" className="btn btn-ghost btn-xs" disabled={busy || !wf} onClick={() => void onDeleteWf()} title="删除工作流">
          <Trash2 size={12} />
        </button>
      </div>
      {!wf && (
        <div className="canvas-float-hint">
          在 ComfyUI 界面点「工作流 → 导出（API）」得到 workflow_api.json，点上方「上传」导入
        </div>
      )}
      <input
        ref={fileRef}
        type="file"
        accept=".json,application/json"
        hidden
        onChange={(e) => {
          void onUploadWf(e.target.files);
          e.target.value = "";
        }}
      />

      {wf && imageSlots > 0 && (
        <div className="canvas-float-hint">参考图 {imageSlots} 个槽位：上游连线或图库选择，按顺序注入 LoadImage</div>
      )}
      {wf && formParams.length > 0 && (
        <div className="canvas-wf-params">
          {formParams.map((p) => (
            <div className="field" key={p.key}>
              <label className="field-label">{p.label}</label>
              {p.type === "number" && (
                <input
                  className="input"
                  type="number"
                  min={p.min}
                  max={p.max}
                  step={p.step}
                  value={String(paramValues[p.key] ?? p.default ?? "")}
                  onChange={(e) => setParam(p.key, e.target.value === "" ? "" : Number(e.target.value))}
                />
              )}
              {p.type === "seed" && (
                <input
                  className="input"
                  type="text"
                  placeholder="留空随机"
                  value={String(paramValues[p.key] ?? "")}
                  onChange={(e) => setParam(p.key, e.target.value)}
                />
              )}
              {p.type === "select" &&
                (p.options && p.options.length > 0 ? (
                  <select
                    className="select"
                    value={String(paramValues[p.key] ?? p.default ?? "")}
                    onChange={(e) => setParam(p.key, e.target.value)}
                  >
                    {String(p.default ?? "") !== "" && !p.options.includes(String(p.default)) && (
                      <option value={String(p.default)}>{String(p.default)}</option>
                    )}
                    {p.options.map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className="input"
                    placeholder="ComfyUI 未启动，手填后可用「刷新」补全选项"
                    value={String(paramValues[p.key] ?? p.default ?? "")}
                    onChange={(e) => setParam(p.key, e.target.value)}
                  />
                ))}
              {p.type === "text" && (
                <PromptArea
                  value={String(paramValues[p.key] ?? p.default ?? "")}
                  onChange={(v) => setParam(p.key, v)}
                  placeholder="输入文本…"
                />
              )}
            </div>
          ))}
        </div>
      )}
      {wf && formParams.length === 0 && imageSlots === 0 && (
        <div className="canvas-float-hint">该工作流没有发现可调参数，将按原始定义执行</div>
      )}
    </div>
  );
}

/**
 * 文档正文大弹窗。
 *
 * 节点上只放预览（浮框那么窄，长文根本没法改），编辑放到这里：
 * 与项目里「节点=卡片，编辑=模态」的一贯做法一致，也和 Toonflow 处理长文本的方式相同。
 */
function DocEditorDialog({
  req,
  onSave,
  onClear,
  onClose,
}: {
  req: DocEditRequest;
  onSave: (text: string) => void;
  onClear: () => void;
  onClose: () => void;
}) {
  const [text, setText] = useState(req.text);
  const dirty = text !== req.text;
  return (
    <div className="canvas-dialog-mask" onMouseDown={onClose}>
      <div className="canvas-dialog wide" onMouseDown={(e) => e.stopPropagation()}>
        <div className="canvas-dialog-head">
          <span className="canvas-dialog-title">
            {req.label} · 正文
            {req.hasOverride ? <em className="canvas-doc-badge">已手改</em> : null}
          </span>
          <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
            <X size={14} />
          </button>
        </div>
        <textarea
          className="canvas-doc-editor-body"
          value={text}
          autoFocus
          spellCheck={false}
          placeholder="正文（Markdown）…"
          onChange={(e) => setText(e.target.value)}
        />
        <div className="canvas-dialog-foot">
          <span className="canvas-dialog-hint">
            {dirty ? "已修改，未保存" : `${text.length} 字`}
            {" · 保存后下游节点读的就是这份，不必重跑"}
          </span>
          {req.hasOverride && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClear}>
              取消手改，用生成结果
            </button>
          )}
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => onSave(text)}>
            保存到节点
          </button>
        </div>
      </div>
    </div>
  );
}

/** 节点属性浮框：features 驱动，渲染在被选中节点下方（dola-v2 交互） */
function NodeFloatingPanel({ id, data }: { id: string; data: CanvasNodeData }) {
  const ctx = useContext(PanelCtx);
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const schema = data.schema as CanvasNodeSchema;
  const status = data.status as CanvasNodeStatus | undefined;
  const panelRef = useRef<HTMLDivElement>(null);
  const statusKey = `${status?.taskId ?? ""}:${status?.status ?? ""}`;

  // 浮框超出画布可视区时把画布平移一点，让整块浮框都落在屏幕内
  // （节点在画布任何位置都能看全，不用手动拖画布）
  useEffect(() => {
    const nudge = ctx?.nudgeViewport;
    if (!nudge) return;

    const measure = () => {
      const el = panelRef.current;
      if (!el) return;
      // 边界取画布容器而不是整个窗口：左侧还有导航与元素面板
      const flow = el.closest(".react-flow");
      const box = flow?.getBoundingClientRect() ?? {
        top: 0,
        bottom: window.innerHeight,
        left: 0,
        right: window.innerWidth,
      };
      const margin = 12;
      // 画布比浮窗还窄时（窗口小 / 元素面板占位）自动收窄，否则横向怎么放都会被切
      const availW = Math.max(280, Math.round(box.right - box.left - margin * 2));
      if (el.style.maxWidth !== `${availW}px`) el.style.maxWidth = `${availW}px`;

      const r = el.getBoundingClientRect();
      let dy = 0;
      let dx = 0;
      if (r.bottom > box.bottom - margin) dy = r.bottom - (box.bottom - margin);
      else if (r.top < box.top + margin) dy = -(box.top + margin - r.top);
      if (r.right > box.right - margin) dx = r.right - (box.right - margin);
      else if (r.left < box.left + margin) dx = -(box.left + margin - r.left);
      // 已经完整可见就不动，避免来回抖
      if (Math.abs(dx) >= 6 || Math.abs(dy) >= 6) nudge(dx, dy);
    };

    // 「定位到节点」有 300ms 平移动画，必须等它停下来再量，否则量到的是中途位置；
    // 收窄后高度会变、位置会再变，所以补两次（已经完整可见时不会重复平移）。
    const timers = [420, 950, 1500].map((ms) => window.setTimeout(measure, ms));
    return () => timers.forEach((t) => window.clearTimeout(t));
    // 只在切换节点 / 任务状态变化时重新测量：打字时不该反复平移画布
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, statusKey]);

  if (!ctx || !schema) return null;
  const features = schema.features ?? [];
  // 图片产物（文档 / 视频不走这个网格）
  const imageProducts = (status?.assets ?? []).filter((a) => a.kind === "image");
  // 文档正文：手改的覆盖 > 生成结果（后端也按这个顺序取，两边口径要一致）
  const docOverride = String(data.docText ?? "").trim();
  const bodyText = docOverride || status?.text || "";
  const nodeType = String(data.nodeType);
  const isVideo = nodeType === "video";
  const isDoc = DOC_KINDS.has(nodeType);
  const videoMode = String(data.mode ?? "text2video");
  // 逐镜出片：off 是普通单段视频；each / chain 会按上游分镜表一镜一段
  const shotVideo = String(data.shotVideo ?? "off");
  const shotMode = shotVideo !== "off";
  const isShotVideoNode = isVideo && shotMode;
  // 文档节点要选文本模型；视频节点选视频模型；其余按图片
  const wantedModality = isDoc ? "text" : isVideo ? "video" : "image";
  const nodeModels = ctx.models.filter((m) => m.modality === wantedModality);
  const agent = isDoc ? ctx.agents.find((a) => a.key === nodeType) : undefined;
  const patch = (p: Partial<CanvasNodeData>) => ctx.updateNode(id, p);
  const refImages = (data.refImages as NodeRefImage[] | undefined) ?? [];
  const chunkParam = agent?.chunkParam ?? "";
  // 分块单位：章 / 场 / 镜（用于浮框提示文案）
  const CHUNK_UNIT: Record<string, string> = {
    chapterCount: "章",
    sceneCount: "场",
    shotCount: "镜",
  };

  const removeRef = (rid: number) =>
    patch({ refImages: refImages.filter((r) => r.id !== rid) });

  const onUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    try {
      const a = await api.uploadAsset(files[0]);
      if (!refImages.some((r) => r.id === a.id)) {
        // 首尾帧模式只保留两个槽位
        const next = [...refImages, { id: a.id, url: a.url }];
        patch({ refImages: videoMode === "first_last" ? next.slice(0, 2) : next });
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "上传失败");
    }
  };

  return (
    <div ref={panelRef} className="canvas-float nodrag nowheel" onDoubleClick={(e) => e.stopPropagation()}>
      <div className="canvas-float-title">
        <span className="canvas-node-icon">{CATEGORY_ICON[schema.category]}</span>
        {schema.label}
      </div>
      {schema.description && <div className="canvas-float-desc">{schema.description}</div>}

      {features.includes("prompt") && (
        <div className="field">
          <label className="field-label">{schema.promptLabel ?? "提示词"}</label>
          <PromptArea
            value={String(data.prompt ?? "")}
            onChange={(v) => patch({ prompt: v })}
            placeholder={schema.promptPlaceholder ?? "描述要生成的内容…"}
          />
          {agent?.varHint ? <div className="field-hint">{agent.varHint}</div> : null}
        </div>
      )}

      {features.includes("workflowSelect") && <WorkflowSection id={id} data={data} />}

      {features.includes("modelSelect") && (
        <div className="field">
          <label className="field-label">模型</label>
          <ModelSelect
            models={nodeModels}
            value={String(data.model_key ?? "")}
            onChange={(v) => patch({ model_key: v })}
            placeholder="选择模型"
          />
        </div>
      )}

      {features.includes("styleSelect") && (
        <div className="field">
          <label className="field-label">风格</label>
          <div className="canvas-inline">
            <select
              className="select"
              value={String(data.styleKey ?? "")}
              onChange={(e) => patch({ styleKey: e.target.value })}
            >
              <option value="">无（不注入风格）</option>
              {ctx.styles.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => ctx.applyStyleToAll(String(data.styleKey ?? ""))}
              title="把当前风格套用到画布上所有支持风格的节点"
            >
              <Sparkles size={12} />
              同步全链
            </button>
          </div>
          <div className="field-hint">
            {isDoc
              ? "追加到该阶段 Agent 的系统提示词；改这里就等于改写作风格"
              : "只取技法与约束词，导演名不会写进生图 / 生视频提示词"}
          </div>
        </div>
      )}

      {features.includes("assetScope") && (
        <div className="field">
          <label className="field-label">生成范围</label>
          <div className="canvas-modetabs">
            {ASSET_SCOPES.map(([value, label]) => (
              <button
                key={value || "all"}
                type="button"
                className={`canvas-modetab ${String(data.assetScope ?? "") === value ? "active" : ""}`}
                onClick={() => patch({ assetScope: value })}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="field-hint">按资产表的「类型」筛选：一行一张图，先只出角色最省钱</div>
        </div>
      )}

      {features.includes("videoMode") && (
        <div className="field">
          <label className="field-label">模式</label>
          <div className="canvas-modetabs">
            {(
              [
                ["text2video", "文生视频"],
                ["first_last", "首尾帧"],
                ["omni_ref", "全能参考"],
              ] as [string, string][]
            ).map(([m, label]) => (
              <button
                key={m}
                type="button"
                className={`canvas-modetab ${videoMode === m ? "active" : ""}`}
                onClick={() => patch({ mode: m })}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {features.includes("shotVideo") && (
        <div className="field">
          <label className="field-label">逐镜出视频</label>
          <select
            className="input"
            value={shotVideo}
            onChange={(e) => patch({ shotVideo: e.target.value })}
          >
            <option value="off">关闭（整段一个视频）</option>
            <option value="each">每镜一段 · 用该镜分镜图当首帧</option>
            <option value="chain">每镜一段 · 并用下一镜首帧收尾（首尾相连）</option>
          </select>
        </div>
      )}

      {isShotVideoNode && (
        <div className="field">
          <label className="canvas-check">
            <input
              type="checkbox"
              checked={data.sceneRefs !== false}
              onChange={(e) => patch({ sceneRefs: e.target.checked })}
            />
            <span>同场景串联</span>
          </label>
          <div className="canvas-float-hint">
            {videoMode === "omni_ref"
              ? "开启后，每一镜会额外带上同一场景上一镜的分镜图当参考，让一个场景内的多个镜头保持连贯"
              : "「首尾帧」模式下连贯性已经由首尾帧保证，这项只对「全能参考」模式生效"}
          </div>
        </div>
      )}

      {features.includes("imageUpload") && videoMode !== "text2video" && !isShotVideoNode && (
        <div className="field">
          {videoMode === "first_last" ? (
            <>
              <label className="field-label">首帧 / 尾帧（第 1 张为首帧，第 2 张为尾帧）</label>
              <div className="canvas-flframe-row">
                {([0, 1] as const).map((idx) => {
                  const r = refImages[idx];
                  if (r) {
                    return (
                      <span key={idx} className="canvas-refthumb canvas-flslot">
                        <span className="canvas-flslot-tag">{idx === 0 ? "首帧" : "尾帧"}</span>
                        <img src={r.url} alt="" loading="lazy" />
                        <button
                          type="button"
                          className="canvas-refremove"
                          onClick={() => removeRef(r.id)}
                          title="移除"
                        >
                          <X size={9} />
                        </button>
                      </span>
                    );
                  }
                  const locked = idx === 1 && refImages.length === 0;
                  return (
                    <button
                      key={idx}
                      type="button"
                      className="canvas-flslot-btn"
                      disabled={locked}
                      onClick={() => ctx.openPicker(id, idx === 0 ? "first" : "last")}
                      title={locked ? "请先选择首帧" : undefined}
                    >
                      <Plus size={14} />
                      <span>{locked ? "先选首帧" : idx === 0 ? "选首帧" : "选尾帧"}</span>
                    </button>
                  );
                })}
              </div>
            </>
          ) : (
            <>
              {/* 标题与两个按钮并成一行，省一层高度 */}
              <div className="canvas-inline canvas-refhead">
                <label className="field-label">
                  参考图{refImages.length > 0 ? `（${refImages.length}）` : "（上游自动收集）"}
                </label>
                <button type="button" className="btn btn-ghost btn-xs" onClick={() => ctx.openPicker(id)}>
                  <Images size={12} />
                  图库
                </button>
                <button type="button" className="btn btn-ghost btn-xs" onClick={() => fileRef.current?.click()}>
                  <Upload size={12} />
                  上传
                </button>
              </div>
              {refImages.length > 0 && (
                <div className="canvas-refstrip">
                  {refImages.map((r) => (
                    <span key={r.id} className="canvas-refthumb">
                      <img src={r.url} alt="" loading="lazy" />
                      <button type="button" className="canvas-refremove" onClick={() => removeRef(r.id)} title="移除参考">
                        <X size={9} />
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => {
              void onUpload(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
      )}

      {features.includes("imageUpload") && (
        <label className="canvas-check">
          <input
            type="checkbox"
            checked={data.mentionRefs !== false}
            onChange={(e) => patch({ mentionRefs: e.target.checked })}
          />
          <span>
            自动挂载提示词里提到的资产
            <em className="canvas-check-hint">（角色 / 场景设定图，最多 3 张）</em>
          </span>
        </label>
      )}

      <div className="canvas-float-grid">
        {features.includes("imageSize") && (
          <div className="field">
            <label className="field-label">尺寸</label>
            <select
              className="select"
              value={String(data.size ?? "1024x1024")}
              onChange={(e) => patch({ size: e.target.value })}
            >
              {["1024x1024", "1024x1792", "1792x1024"].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        )}
        {features.includes("sampleCount") && (
          <div className="field">
            <label className="field-label">张数</label>
            <input
              className="input"
              type="number"
              min={1}
              max={4}
              value={Number(data.n ?? 1)}
              onChange={(e) => patch({ n: Number(e.target.value) || 1 })}
            />
          </div>
        )}
        {features.includes("duration") && (
          <div className="field">
            <label className="field-label">时长（秒）</label>
            <input
              className="input"
              type="number"
              min={1}
              max={15}
              value={Number(data.duration ?? 5)}
              onChange={(e) => patch({ duration: Number(e.target.value) || 5 })}
            />
          </div>
        )}
        {features.includes("ratio") && (
          <div className="field">
            <label className="field-label">画幅</label>
            <select
              className="select"
              value={String(data.ratio ?? "16:9")}
              onChange={(e) => patch({ ratio: e.target.value })}
            >
              {["16:9", "9:16", "1:1"].map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
        )}
        {features.includes("shotLimit") && (!isVideo || shotMode) && (
          <div className="field">
            <label className="field-label">生成镜数</label>
            <input
              className="input"
              type="number"
              min={0}
              max={24}
              value={Number(data.shotLimit ?? 6)}
              onChange={(e) =>
                patch({ shotLimit: Math.max(0, Math.min(24, Number(e.target.value) || 0)) })
              }
            />
          </div>
        )}
        {chunkParam && (
          <div className="field">
            <label className="field-label">{agent?.chunkLabel || chunkParam}</label>
            <input
              className="input"
              type="number"
              min={1}
              max={agent?.maxChunks ?? 30}
              value={Number(data[chunkParam] ?? 1)}
              onChange={(e) =>
                patch({ [chunkParam]: Math.max(1, Math.min(agent?.maxChunks ?? 30, Number(e.target.value) || 1)) })
              }
            />
          </div>
        )}
      </div>

      {isDoc && agent?.chunked && (
        <div className="canvas-float-hint">
          长文会自动分块续写：先出大纲，再逐{CHUNK_UNIT[chunkParam] ?? "块"}生成，避免被截断
        </div>
      )}

      {features.includes("shotLimit") && (!isVideo || shotMode) && (
        <div className="canvas-float-hint">
          {isShotVideoNode
            ? "按上游分镜表逐镜出片，填 0 = 全部（上限 24 镜）。镜号对得上才出，对不上的会在任务日志里列出来"
            : "逐镜出图，填 0 = 全部（上限 24 镜）；每镜只挂它自己提到的资产，上游整批图片不带进来"}
        </div>
      )}

      {(status?.text || docOverride) ? (
        <div className="field">
          <div className="canvas-inline canvas-refhead">
            <label className="field-label">
              正文
              {docOverride ? <em className="canvas-doc-badge">已手改</em> : null}
              <em className="canvas-doc-count">（{bodyText.length} 字）</em>
            </label>
            <button
              type="button"
              className="btn btn-ghost btn-xs"
              onClick={() =>
                ctx.editDoc({
                  nodeId: id,
                  label: schema.label,
                  text: bodyText,
                  hasOverride: Boolean(docOverride),
                  url: status?.assetUrl,
                })
              }
              title="在弹窗里编辑正文；保存后下游读的就是你改过的这份"
            >
              <FileText size={12} />
              编辑
            </button>
            {status?.assetUrl && (
              <a className="btn btn-ghost btn-xs" href={status.assetUrl} target="_blank" rel="noreferrer">
                下载
              </a>
            )}
          </div>
          <pre className="canvas-doc-preview">{bodyText}</pre>
          {docOverride ? (
            <div className="field-hint">下游读的是你手改的这份，不必重跑；点「编辑」可改回生成结果</div>
          ) : null}
        </div>
      ) : null}

      {imageProducts.length > 0 && (
        <div className="field">
          <div className="canvas-inline canvas-refhead">
            <label className="field-label">
              产物<em className="canvas-doc-count">（{imageProducts.length} 张）</em>
            </label>
            {imageProducts.length > 1 && (
              <input
                className="canvas-zoom"
                type="range"
                min={56}
                max={176}
                step={8}
                value={ctx.thumb}
                onChange={(e) => ctx.pickThumb(Number(e.target.value))}
                title="缩略图大小"
                aria-label="缩略图大小"
              />
            )}
          </div>
          <div className="canvas-prodgrid" style={{ "--thumb": `${ctx.thumb}px` } as React.CSSProperties}>
            {imageProducts.map((a, i) => (
              <button
                key={a.id}
                type="button"
                className="canvas-prodcell"
                onClick={() => ctx.preview(a.url, a.title || a.name)}
                title={a.title || a.name}
              >
                <img src={a.url} alt={a.label || a.name} loading="lazy" />
                <span className="canvas-prodlabel">{a.label || `#${i + 1}`}</span>
              </button>
            ))}
          </div>
          <div className="field-hint">
            {String(data.nodeType) === ASSET_IMAGE_KIND
              ? "点开可放大；资产名已入库，下游提到名字会自动挂图"
              : "点开可放大；格子上的标签就是镜号"}
          </div>
        </div>
      )}

      {status?.injectedNames && status.injectedNames.length > 0 && (
        <div className="canvas-float-hint">
          已自动挂载参考图：{status.injectedNames.join("、")}
        </div>
      )}

      {/* 运行按钮与状态吸在底部：浮框内容长时不用滚到底才能运行 */}
      <div className="canvas-float-actions">
        {String(data.nodeType) === "text" ? (
          <div className="canvas-float-hint">文本节点无需运行，保存后直接供下游使用</div>
        ) : (
          <button
            className="btn btn-primary btn-block"
            disabled={ctx.running}
            onClick={() => ctx.runNode(id)}
          >
            {ctx.running ? <Spinner light /> : <Play size={14} />}
            运行此节点
          </button>
        )}

        {status && (
          <div className="canvas-float-status">
            任务 #{status.taskId} · {STATUS_BADGE[status.status] ?? status.status}
            {status.taskCount && status.taskCount > 1 ? ` · 共 ${status.taskCount} 个任务` : ""}
            {status.error ? <div className="canvas-float-error">{status.error}</div> : null}
            {status.assetUrl && status.assetKind !== "document" && (
              <a className="btn btn-ghost btn-sm" href={status.assetUrl} target="_blank" rel="noreferrer">
                查看产物
              </a>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** 自定义节点：契约端口渲染 + 状态角标 + 参考计数 + 选中时下方浮框 */
function ContractNode({ id, data, selected }: NodeProps) {
  const ctx = useContext(PanelCtx);
  const schema = data.schema as CanvasNodeSchema;
  const status = data.status as CanvasNodeStatus | undefined;
  const refCount = ((data.refImages as NodeRefImage[] | undefined) ?? []).length;
  const nodeImages = (status?.assets ?? []).filter((a) => a.kind === "image");
  const docOverride = String(data.docText ?? "").trim();
  const docBody = docOverride || status?.text || "";
  // 节点上选了风格就在卡片上标出来：一条链上七八个节点，一眼能看出谁在用哪套风格
  const styleName = data.styleKey
    ? ctx?.styles.find((s) => s.key === data.styleKey)?.name ?? ""
    : "";
  if (!schema) return null;
  return (
    <>
      <NodeToolbar isVisible={!!selected} position={Position.Bottom} offset={10}>
        <NodeFloatingPanel id={id} data={data as CanvasNodeData} />
      </NodeToolbar>
      <div className={`canvas-node ${selected ? "selected" : ""} cat-${schema.category}`}>
        {schema.handles.targets?.map((h) => (
          <Handle
            key={h.id}
            id={h.id}
            type="target"
            position={Position.Left}
            className={`handle-port t-${h.type}`}
            data-handle-type={h.type}
          />
        ))}
        <div className="canvas-node-head">
          <span className="canvas-node-icon">{CATEGORY_ICON[schema.category]}</span>
          <span className="canvas-node-label">{schema.label}</span>
          {refCount > 0 && <span className="canvas-node-refcount">参考 {refCount}</span>}
          {status && (
            <span className={`canvas-node-badge s-${status.status}`}>
              {STATUS_BADGE[status.status] ?? status.status}
            </span>
          )}
        </div>
        {styleName && <div className="canvas-node-style">风格 · {styleName}</div>}
        {docOverride && <div className="canvas-node-style edited">正文已手改</div>}
        {status?.assetKind === "document" ? (
          <div className="canvas-node-doc">
            {docBody.split("\n").filter((l) => l.trim()).slice(0, 3).join("\n")}
          </div>
        ) : nodeImages.length > 1 ? (
          // 批量节点一次产出多张：铺三张 + 剩余数量，每格角上标镜号 / 资产名
          <div className="canvas-node-thumbs">
            {nodeImages.slice(0, 3).map((a, i) => (
              <span key={a.id} className="canvas-node-thumbcell">
                <img src={a.url} alt={a.label || a.name} title={a.title || a.name} loading="lazy" />
                <em>{a.label || `#${i + 1}`}</em>
              </span>
            ))}
            {nodeImages.length > 3 && (
              <span className="canvas-node-thumbs-more">+{nodeImages.length - 3}</span>
            )}
          </div>
        ) : status?.assetUrl ? (
          <img
            className="canvas-node-thumb"
            src={status.assetUrl}
            alt="产物"
            onError={(e) => (e.currentTarget.style.display = "none")}
            onLoad={(e) => (e.currentTarget.style.display = "")}
          />
        ) : null}
        {data.prompt ? (
          <div className="canvas-node-prompt">{String(data.prompt).slice(0, 60)}</div>
        ) : null}
        {schema.handles.sources?.map((h) => (
          <Handle
            key={h.id}
            id={h.id}
            type="source"
            position={Position.Right}
            className={`handle-port t-${h.type}`}
            data-handle-type={h.type}
          />
        ))}
      </div>
    </>
  );
}

const NODE_TYPES = { contract: ContractNode };

type AssetKind = "all" | "image" | "video" | "document";

/** 本地连线校验：契约驱动，不请求后端（后端按已保存文档校验，未保存的新节点必报"节点不存在"） */
function canConnectLocal(
  schemas: Record<string, CanvasNodeSchema>,
  nodes: Node[],
  edges: Edge[],
  source: string,
  sourceHandle: string | null,
  target: string,
  targetHandle: string | null
): { ok: boolean; reason?: string } {
  if (!source || !target || !sourceHandle || !targetHandle) return { ok: false };
  if (source === target) return { ok: false, reason: "不允许自连接" };
  const srcNode = nodes.find((n) => n.id === source);
  const tgtNode = nodes.find((n) => n.id === target);
  if (!srcNode || !tgtNode) return { ok: false, reason: "节点不存在" };
  const srcSchema = schemas[String(srcNode.data.nodeType)];
  const tgtSchema = schemas[String(tgtNode.data.nodeType)];
  if (!srcSchema || !tgtSchema) return { ok: false, reason: "未知节点类型" };
  const sh = srcSchema.handles.sources?.find((h) => h.id === sourceHandle);
  const th = tgtSchema.handles.targets?.find((h) => h.id === targetHandle);
  if (!sh || !th) return { ok: false, reason: "端口不存在" };
  if (sh.type !== th.type && th.type !== "any" && sh.type !== "any") {
    return { ok: false, reason: `类型不兼容：${sh.type} → ${th.type}` };
  }
  if (edges.some((e) => e.source === source && e.target === target)) {
    return { ok: false, reason: "重复连线" };
  }
  // 环检测：加边后从 target 沿邻接走能否回到 source
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    adj.set(e.source, [...(adj.get(e.source) ?? []), e.target]);
  }
  adj.set(source, [...(adj.get(source) ?? []), target]);
  const seen = new Set<string>();
  const stack = [target];
  while (stack.length) {
    const u = stack.pop()!;
    if (u === source) return { ok: false, reason: "画布中存在环，请检查连线" };
    if (seen.has(u)) continue;
    seen.add(u);
    for (const v of adj.get(u) ?? []) stack.push(v);
  }
  return { ok: true };
}

/** 资产网格（抽屉与选择弹窗共用） */
function AssetGrid({
  assets,
  selectedIds,
  onToggle,
  onPreview,
}: {
  assets: Asset[];
  selectedIds?: Set<number>;
  onToggle?: (a: Asset) => void;
  onPreview?: (a: Asset) => void;
}) {
  return (
    <div className="canvas-asset-grid">
      {assets.map((a) => {
        const active = selectedIds?.has(a.id) ?? false;
        // 文档（自动链产物）不能当参考图，直接开新标签页看/下载
        if (a.kind === "document") {
          return (
            <a
              key={a.id}
              className="canvas-asset-cell"
              href={a.url}
              target="_blank"
              rel="noreferrer"
              title={a.original_name}
            >
              <span className="canvas-asset-thumb canvas-asset-doc">
                <FileText size={16} />
              </span>
              <span className="canvas-asset-name">{a.original_name.slice(0, 14)}</span>
            </a>
          );
        }
        return (
          <button
            key={a.id}
            type="button"
            className={`canvas-asset-cell ${active ? "active" : ""}`}
            title={a.original_name}
            onClick={() => (onToggle ? onToggle(a) : onPreview?.(a))}
          >
            {a.kind === "video" ? (
              <span className="canvas-asset-thumb canvas-asset-video">
                <Video size={16} />
              </span>
            ) : (
              <img className="canvas-asset-thumb" src={a.url} alt={a.original_name} loading="lazy" />
            )}
            {active && <span className="canvas-asset-check" />}
            <span className="canvas-asset-name">{a.original_name.slice(0, 14)}</span>
          </button>
        );
      })}
    </div>
  );
}

/** 从资产库选择参考弹窗：多选模式（图库）或单选模式（首帧/尾帧槽位） */
function AssetPickerDialog({
  slot,
  onConfirm,
  onClose,
}: {
  slot?: "first" | "last";
  onConfirm: (assets: Asset[]) => void;
  onClose: () => void;
}) {
  const single = slot !== undefined;
  const [kind, setKind] = useState<AssetKind>("image");
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const selRef = useRef<Map<number, Asset>>(new Map());

  useEffect(() => {
    let alive = true;
    setAssets(null);
    api
      .listAssets({ limit: 200, kind: kind === "all" ? undefined : kind })
      .then((r) => {
        // 文档是自动链产物，不能当参考图，选择器里不出现
        if (alive) setAssets(r.items.filter((a) => a.kind !== "document"));
      })
      .catch(() => {
        if (alive) setAssets([]);
      });
    return () => {
      alive = false;
    };
  }, [kind]);

  const toggle = (a: Asset) => {
    if (single) {
      onConfirm([a]);
      return;
    }
    setSel((prev) => {
      const next = new Set(prev);
      if (next.has(a.id)) {
        next.delete(a.id);
        selRef.current.delete(a.id);
      } else {
        next.add(a.id);
        selRef.current.set(a.id, a);
      }
      return next;
    });
  };

  return (
    <div className="canvas-dialog-mask" onMouseDown={onClose}>
      <div className="canvas-dialog" onMouseDown={(e) => e.stopPropagation()}>
        <div className="canvas-dialog-head">
          <span className="canvas-dialog-title">
            {single
              ? `选择${slot === "first" ? "首帧" : "尾帧"}图片`
              : "从资产库选择参考（可多选）"}
          </span>
          <button type="button" className="canvas-dialog-close" onClick={onClose}>
            <X size={14} />
          </button>
        </div>
        <div className="canvas-dialog-kindtabs">
          {(
            [
              ["all", "全部"],
              ["image", "图片"],
              ["video", "视频"],
            ] as [AssetKind, string][]
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              className={`canvas-kindtab ${kind === k ? "active" : ""}`}
              onClick={() => setKind(k)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="canvas-dialog-body">
          {assets === null && <div className="canvas-palette-hint">加载中…</div>}
          {assets !== null && assets.length === 0 && (
            <div className="canvas-palette-hint">暂无资产，先在图片/视频页生成或上传</div>
          )}
          {assets !== null && assets.length > 0 && (
            <AssetGrid assets={assets} selectedIds={sel} onToggle={toggle} />
          )}
        </div>
        {!single && (
          <div className="canvas-dialog-foot">
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={sel.size === 0}
              onClick={() => onConfirm(Array.from(sel).map((id) => selRef.current.get(id)!).filter(Boolean))}
            >
              添加所选（{sel.size}）
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function CanvasInner({ projectId, projectName, onBack }: { projectId: number; projectName: string; onBack: () => void }) {
  const toast = useToast();
  const { screenToFlowPosition, toObject, setCenter, getZoom, getViewport, setViewport } =
    useReactFlow();
  const wrapper = useRef<HTMLDivElement>(null);

  const [schemas, setSchemas] = useState<Record<string, CanvasNodeSchema>>({});
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  nodesRef.current = nodes;
  edgesRef.current = edges;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [statusMap, setStatusMap] = useState<Record<string, CanvasNodeStatus>>({});
  const [models, setModels] = useState<ModelOption[]>([]);
  const [agents, setAgents] = useState<AgentMeta[]>([]);
  const [styles, setStyles] = useState<StyleOption[]>([]);
  const [workflows, setWorkflows] = useState<ComfyWorkflow[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [assetKind, setAssetKind] = useState<AssetKind>("all");
  const [railTab, setRailTab] = useState<"elements" | "assets">("elements");
  const [addMenu, setAddMenu] = useState<{ x: number; y: number } | null>(null);
  const [pickerFor, setPickerFor] = useState<{ nodeId: string; slot?: "first" | "last" } | null>(null);
  const [lightbox, setLightbox] = useState<LightboxItem | null>(null);
  const [editorFor, setEditorFor] = useState<DocEditRequest | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedTick, setSavedTick] = useState(0);
  const [running, setRunning] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(() => prefs.paletteOpen.get() ?? true);
  // 自动链档位：铺多远（记忆上次选择，避免每次都要重选）。
  // 只认当前版本真实存在的档位——老版本的 key 可能已经没了
  const [chainLevel, setChainLevel] = useState<string>(
    () => prefs.chainLevel.get(CHAIN_LEVELS.map((l) => l.key)) ?? CHAIN_LEVELS[0].key,
  );

  const pickChainLevel = useCallback((key: string) => {
    setChainLevel(key);
    prefs.chainLevel.set(key);
  }, []);

  const togglePalette = useCallback((open: boolean) => {
    setPaletteOpen(open);
    prefs.paletteOpen.set(open);
  }, []);

  // 输入框聚焦时按 Backspace/Delete 不冒泡到 ReactFlow，防误删节点
  useEffect(() => {
    const guard = (e: KeyboardEvent) => {
      if (e.key !== "Backspace" && e.key !== "Delete") return;
      const el = e.target as HTMLElement | null;
      if (
        el &&
        (el.tagName === "INPUT" ||
          el.tagName === "TEXTAREA" ||
          el.tagName === "SELECT" ||
          el.isContentEditable)
      ) {
        e.stopPropagation();
      }
    };
    window.addEventListener("keydown", guard, true);
    return () => window.removeEventListener("keydown", guard, true);
  }, []);

  // Esc 关闭灯箱 / 图库弹窗 / 添加菜单 / 正文编辑器
  useEffect(() => {
    if (!lightbox && !pickerFor && !addMenu && !editorFor) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setLightbox(null);
      setPickerFor(null);
      setAddMenu(null);
      setEditorFor(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox, pickerFor, addMenu, editorFor]);

  useEffect(() => {
    (async () => {
      const [contract, doc, imageModels, videoModels, textModels, agentList, styleList] =
        await Promise.all([
          api.canvasContract(),
          api.getCanvas(projectId),
          api.listModels("image").catch(() => []),
          api.listModels("video").catch(() => []),
          api.listModels("text").catch(() => []),
          api.getAgentPrompts().catch(() => []),
          api.listDirectorStyles().catch(() => []),
        ]);
      setSchemas(contract.nodeSchemas);
      setModels([...imageModels, ...videoModels, ...textModels]);
      setAgents(agentList);
      setStyles(styleList);
      if (doc.nodes?.length) {
        setNodes(
          doc.nodes.map((n) => ({
            id: n.id,
            type: "contract",
            position: n.position,
            data: { ...n.data, schema: contract.nodeSchemas[n.type], nodeType: n.type } as CanvasNodeData,
          }))
        );
        setEdges(doc.edges as Edge[]);
      }
      setLoaded(true);
      try {
        const st = await api.canvasStatus(projectId);
        setStatusMap(st.nodes);
      } catch {
        /* 状态加载失败不阻塞画布 */
      }
      api.listAssets({ limit: 200 }).then((r) => setAssets(r.items)).catch(() => {});
      void reloadWorkflows();
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // 有运行中节点时轮询状态
  useEffect(() => {
    if (!loaded) return;
    const hasRunning = Object.values(statusMap).some(
      (s) => s.status === "pending" || s.status === "processing"
    );
    if (!hasRunning) return;
    const t = setTimeout(async () => {
      try {
        const st = await api.canvasStatus(projectId);
        setStatusMap(st.nodes);
      } catch {
        /* 轮询失败忽略 */
      }
    }, 2500);
    return () => clearTimeout(t);
  }, [statusMap, loaded, projectId]);

  const refreshStatus = useCallback(async () => {
    const st = await api.canvasStatus(projectId);
    setStatusMap(st.nodes);
  }, [projectId]);

  const reloadWorkflows = useCallback(async () => {
    const list = await api.listComfyWorkflows().catch(() => []);
    setWorkflows(list);
    return list;
  }, []);

  // 同步状态到节点 data（渲染角标）
  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => ({ ...n, data: { ...n.data, status: statusMap[n.id] ?? null } }))
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusMap]);

  const addNodeAt = useCallback(
    (type: string, screenX: number, screenY: number) => {
      const schema = schemas[type];
      if (!schema) return;
      nodeSeq += 1;
      const id = `n${Date.now().toString(36)}${nodeSeq}`;
      const position = screenToFlowPosition({ x: screenX, y: screenY });
      setNodes((prev) => [
        // 新加的节点直接选中（浮框随之打开），其余节点取消选中
        ...prev.map((n) => ({ ...n, selected: false })),
        {
          id,
          type: "contract",
          position,
          selected: true,
          data: { prompt: "", schema, nodeType: type } as CanvasNodeData,
        },
      ]);
      setSelectedId(id);
      setDirty(true);
    },
    [schemas, screenToFlowPosition, setNodes]
  );

  // 一键铺自动链：拓扑是固定模板，不需要 LLM 生成
  // L1 = 创意 → 小说 → 剧本 → 分镜；L2 接资产表 → 资产设定图；L3 再接分镜图（有分叉）；
  // L4 再接视频（逐镜出片，首尾相连）。形状在 canvasChain.ts，这里只管落位与提示。
  const buildAutoChain = useCallback(
    (levelKey: string) => {
      const level = CHAIN_LEVELS.find((l) => l.key === levelKey) ?? CHAIN_LEVELS[0];
      const built = buildChainNodes(level.key, schemas, models);
      if (built.unknownTypes.length > 0) {
        toast.error("自动链节点未就绪，请检查后端是否已升级");
        return;
      }
      if (built.missing.length > 0) {
        toast.error(missingModelMessage(built.missing));
        return;
      }
      setNodes((prev) => [...prev, ...(built.nodes as Node<CanvasNodeData>[])]);
      setEdges((prev) => [...prev, ...(built.edges as Edge[])]);
      const first = built.nodes[0];
      if (first) setSelectedId(first.id);
      setDirty(true);
      toast.success(
        `已铺好自动链（${level.label}）：${level.nodes
          .map((n) => schemas[n.type].label)
          .join(" → ")}`,
      );
    },
    [schemas, models, setNodes, setEdges, toast]
  );

  // 双击空白画布弹添加菜单（dola-v2 交互）；双击节点不弹
  const onPaneDoubleClick = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest(".react-flow__node")) return;
    setAddMenu({ x: e.clientX, y: e.clientY });
  }, []);

  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target || !c.sourceHandle || !c.targetHandle) return;
      const r = canConnectLocal(schemas, nodes, edges, c.source, c.sourceHandle, c.target, c.targetHandle);
      if (!r.ok) {
        toast.error(r.reason || "连接无效");
        return;
      }
      setEdges((eds) =>
        addEdge(
          { id: `e${Date.now().toString(36)}`, source: c.source, target: c.target, sourceHandle: c.sourceHandle, targetHandle: c.targetHandle },
          eds
        )
      );
      setDirty(true);
    },
    [schemas, nodes, edges, setEdges, toast]
  );

  // 拖拽预览时实时禁止非法连线（读 ref 避免闭包过期）
  const isValidConnection = useCallback(
    (c: Connection | Edge) => {
      if (!c.source || !c.target || !c.sourceHandle || !c.targetHandle) return false;
      return canConnectLocal(
        schemas,
        nodesRef.current,
        edgesRef.current,
        c.source,
        c.sourceHandle,
        c.target,
        c.targetHandle
      ).ok;
    },
    [schemas]
  );

  const saveDoc = useCallback(async (): Promise<boolean> => {
    setSaving(true);
    try {
      const doc: CanvasDoc = {
        schemaVersion: 1,
        // 与「示例项目」共用同一份整形逻辑（见 canvasChain.toCanvasDocNodes）：
        // type 要用真实节点类型而不是画布内部的 "contract"，schema 属运行时缓存不落库
        nodes: toCanvasDocNodes(nodes),
        edges: edges.map((e) => ({
          id: e.id,
          source: e.source,
          sourceHandle: e.sourceHandle ?? null,
          target: e.target,
          targetHandle: e.targetHandle ?? null,
        })),
        viewport: toObject().viewport as Viewport,
      };
      await api.saveCanvas(projectId, doc);
      setDirty(false);
      setSavedTick(Date.now());
      return true;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
      return false;
    } finally {
      setSaving(false);
    }
  }, [nodes, edges, projectId, toObject, toast]);

  // 自动保存：编辑停止 1s 后落库（业界惯例 debounce 1000ms，Langflow 同款）
  useEffect(() => {
    if (!dirty || !loaded || saving) return;
    const t = window.setTimeout(() => {
      void saveDoc();
    }, 1000);
    return () => window.clearTimeout(t);
  }, [dirty, loaded, saving, saveDoc]);

  // "已保存"指示 2s 后淡出
  useEffect(() => {
    if (!savedTick) return;
    const t = window.setTimeout(() => setSavedTick(0), 2000);
    return () => window.clearTimeout(t);
  }, [savedTick]);

  // 未保存离开页面时拦截
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  const runNode = useCallback(
    async (nodeId: string) => {
      const node = nodes.find((n) => n.id === nodeId);
      if (!node) return;
      if (String(node.data.nodeType) === "text") {
        toast.info("文本节点无需运行，保存后直接供下游使用");
        return;
      }
      if (dirty && !(await saveDoc())) return;
      setRunning(true);
      try {
        const r = await api.runCanvas(projectId, nodeId);
        toast.success(
          (r.taskCount ?? 1) > 1
            ? `已按资产表派发 ${r.taskCount} 个任务`
            : `节点已运行（任务 #${r.taskId}）`
        );
        await refreshStatus();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "运行失败");
      } finally {
        setRunning(false);
      }
    },
    [dirty, nodes, projectId, refreshStatus, saveDoc, toast]
  );

  const runAll = async () => {
    if (dirty && !(await saveDoc())) return;
    setRunning(true);
    try {
      await api.runCanvas(projectId);
      toast.success("整图执行已启动");
      await refreshStatus();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "启动失败");
    } finally {
      setRunning(false);
    }
  };

  const deleteSelected = () => {
    if (!selectedId) return;
    setNodes((prev) => prev.filter((n) => n.id !== selectedId));
    setEdges((prev) => prev.filter((e) => e.source !== selectedId && e.target !== selectedId));
    setSelectedId(null);
    setDirty(true);
  };

  const updateNodeData = useCallback((id: string, patch: Partial<CanvasNodeData>) => {
    setNodes((prev) => prev.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
    setDirty(true);
  }, []);

  // 一个风格套全链：只改支持 styleSelect 的节点，其余节点不动
  const applyStyleToAll = useCallback(
    (key: string) => {
      setNodes((prev) =>
        prev.map((n) => {
          const features = (n.data.schema as CanvasNodeSchema | undefined)?.features ?? [];
          return features.includes("styleSelect")
            ? { ...n, data: { ...n.data, styleKey: key } }
            : n;
        })
      );
      setDirty(true);
    },
    [setNodes]
  );

  /**
   * 定位到某个节点：选中 + 平移到视口中央。
   *
   * 画布开了 onlyRenderVisibleElements（大画布下只渲染可见节点），
   * 所以「元素」列表点一下必须真的把视图移过去，否则节点既看不见也点不着。
   */
  const focusNode = useCallback(
    (nodeId: string) => {
      const node = nodesRef.current.find((n) => n.id === nodeId);
      if (!node) return;
      setSelectedId(nodeId);
      setNodes((prev) => prev.map((n) => ({ ...n, selected: n.id === nodeId })));
      const w = node.measured?.width ?? node.width ?? 220;
      const h = node.measured?.height ?? node.height ?? 140;
      // 浮框在节点下方展开，所以把节点落在视口偏上的位置（往下多推约 1/8 屏），浮框才有地方放
      const zoom = getZoom() || 1;
      const vh = wrapper.current?.clientHeight ?? window.innerHeight;
      setCenter(node.position.x + w / 2, node.position.y + h / 2 + (vh * 0.12) / zoom, {
        zoom,
        duration: 300,
      });
    },
    [getZoom, setCenter, setNodes]
  );

  const nudgeViewport = useCallback(
    (dxScreen: number, dyScreen: number) => {
      const { x, y, zoom } = getViewport();
      // 平移量与内容位移反向：浮框被右/下沿切掉，就要让内容往左/上走
      setViewport({ x: x - dxScreen, y: y - dyScreen, zoom }, { duration: 220 });
    },
    [getViewport, setViewport]
  );

  // 图库资产的预览（带尺寸与体积信息）
  const previewAsset = useCallback(
    (a: Asset) =>
      setLightbox({
        url: a.url,
        kind: a.kind,
        title: a.original_name,
        meta: `${a.width && a.height ? `${a.width}×${a.height} · ` : ""}${(a.size / 1024).toFixed(0)} KB`,
      }),
    []
  );

  // 节点产物的预览（浮框网格里点开）
  const previewProduct = useCallback(
    (url: string, title?: string, kind = "image") => setLightbox({ url, kind, title: title || "产物" }),
    []
  );

  // 打开文档正文编辑器（大弹窗）与保存
  const openDocEditor = useCallback(async (req: DocEditRequest) => {
    // 节点状态里那份正文是后端截断过的预览（6000 字），编辑必须拿全文，
    // 否则一保存就把后半段截没了
    let payload = req;
    if (!req.hasOverride && req.url) {
      try {
        const res = await fetch(req.url);
        if (res.ok) {
          const full = await res.text();
          if (full.trim()) payload = { ...req, text: full };
        }
      } catch {
        /* 取不到全文就退回预览 */
      }
    }
    setEditorFor(payload);
  }, []);

  // 保存手改正文（空串 = 取消手改，恢复用生成结果）
  const saveDocText = useCallback(
    (nodeId: string, text: string) => {
      updateNodeData(nodeId, { docText: text.trim() ? text : "" });
      setEditorFor(null);
      toast.success(text.trim() ? "已保存到节点，下游会读这份正文" : "已恢复为生成结果");
    },
    [updateNodeData, toast]
  );

  // 产物缩略图大小（用户拖一次就记住，跟 Toonflow 的分镜网格一个思路）
  // 默认值要落在滑杆的步进网格上（56 + n*8），否则滑杆显示会与初值不一致
  const [thumb, setThumb] = useState(() => prefs.thumbSize.get());
  const pickThumb = useCallback((v: number) => {
    setThumb(v);
    prefs.thumbSize.set(v);
  }, []);

  const openPicker = useCallback(
    (nodeId: string, slot?: "first" | "last") => setPickerFor({ nodeId, slot }),
    []
  );

  const confirmPick = useCallback(
    (picked: Asset[]) => {
      if (!pickerFor) return;
      const { nodeId, slot } = pickerFor;
      const node = nodes.find((n) => n.id === nodeId);
      if (node && picked.length > 0) {
        const cur = (node.data.refImages as NodeRefImage[] | undefined) ?? [];
        const a = picked[0];
        if (slot) {
          // 首尾帧槽位：紧凑数组 [首帧, 尾帧?]；尾帧必须先有首帧
          if (a.kind !== "image") {
            toast.error("首尾帧只能选择图片");
          } else if (slot === "first") {
            const last = cur[1];
            updateNodeData(nodeId, {
              refImages: last ? [{ id: a.id, url: a.url }, last] : [{ id: a.id, url: a.url }],
            });
          } else if (cur.length === 0) {
            toast.error("请先选择首帧");
          } else {
            updateNodeData(nodeId, { refImages: [cur[0], { id: a.id, url: a.url }] });
          }
        } else {
          const add = picked
            .filter((x) => x.kind === "image" && !cur.some((c) => c.id === x.id))
            .map((x) => ({ id: x.id, url: x.url }));
          if (add.length > 0) updateNodeData(nodeId, { refImages: [...cur, ...add] });
        }
      }
      setPickerFor(null);
    },
    [nodes, pickerFor, toast, updateNodeData]
  );

  const panelCtx = useMemo<NodePanelCtx>(
    () => ({
      models,
      workflows,
      agents,
      styles,
      running,
      updateNode: updateNodeData,
      applyStyleToAll,
      nudgeViewport,
      preview: previewProduct,
      thumb,
      pickThumb,
      editDoc: openDocEditor,
      runNode,
      openPicker,
      reloadWorkflows,
    }),
    [
      models,
      workflows,
      agents,
      styles,
      running,
      updateNodeData,
      applyStyleToAll,
      nudgeViewport,
      previewProduct,
      thumb,
      pickThumb,
      openDocEditor,
      runNode,
      openPicker,
      reloadWorkflows,
    ]
  );

  const elements = useMemo(
    () =>
      nodes.map((n) => {
        const schema = schemas[String(n.data.nodeType)];
        const prompt = String(n.data.prompt ?? "");
        const st = statusMap[n.id];
        const summary =
          st?.assetUrl
            ? "已生成结果"
            : st
              ? STATUS_BADGE[st.status] ?? st.status
              : prompt
                ? `${prompt.slice(0, 16)}${prompt.length > 16 ? "…" : ""}`
                : schema?.label ?? "";
        return { id: n.id, label: schema?.label ?? String(n.data.nodeType), category: schema?.category ?? "document", summary };
      }),
    [nodes, schemas, statusMap]
  );

  const drawerAssets = useMemo(
    () => (assetKind === "all" ? assets : assets.filter((a) => a.kind === assetKind)),
    [assets, assetKind]
  );

  return (
    <div className="page canvas-page">
      <PanelCtx.Provider value={panelCtx}>
        <div className="canvas-toolbar">
          <button className="btn btn-ghost btn-sm" onClick={onBack}>
            ← {projectName}
          </button>
          <div className="canvas-toolbar-title">
            <WorkflowIcon size={15} />
            画布
            {saving ? (
              <em className="canvas-save-state saving">保存中…</em>
            ) : dirty ? (
              <em className="canvas-dirty">未保存</em>
            ) : savedTick ? (
              <em className="canvas-save-state saved">已保存</em>
            ) : null}
          </div>
          <div className="canvas-toolbar-actions">
            <select
              className="select canvas-chain-level"
              value={chainLevel}
              onChange={(e) => pickChainLevel(e.target.value)}
              title={CHAIN_LEVELS.find((l) => l.key === chainLevel)?.hint}
            >
              {CHAIN_LEVELS.map((l) => (
                <option key={l.key} value={l.key}>
                  {l.label}
                </option>
              ))}
            </select>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => buildAutoChain(chainLevel)}
              title={CHAIN_LEVELS.find((l) => l.key === chainLevel)?.hint}
            >
              <Sparkles size={14} />
              自动链
            </button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setAddMenu({ x: window.innerWidth / 2 - 110, y: 180 })}
              title="添加节点"
            >
              <Plus size={14} />
              添加节点
            </button>
            <button className="btn btn-ghost btn-sm" onClick={refreshStatus} title="刷新节点状态">
              <RefreshCw size={14} />
            </button>
            <button className="btn btn-ghost btn-sm" onClick={deleteSelected} disabled={!selectedId} title="删除选中节点">
              <Trash2 size={14} />
            </button>
            <button className="btn btn-primary btn-sm" onClick={() => void saveDoc()} disabled={saving || !loaded}>
              {saving ? <Spinner light /> : <Save size={14} />}
              保存
            </button>
            <button className="btn btn-primary btn-sm" onClick={runAll} disabled={running || !loaded}>
              {running ? <Spinner light /> : <Play size={14} />}
              运行整图
            </button>
          </div>
        </div>

        <div className={`canvas-layout ${paletteOpen ? "" : "palette-collapsed"}`}>
          {paletteOpen && (
            <div className="canvas-palette">
              <div className="canvas-palette-head">
                <div className="canvas-rail-tabs">
                  <button
                    className={`canvas-rail-tab ${railTab === "elements" ? "active" : ""}`}
                    onClick={() => setRailTab("elements")}
                  >
                    元素
                  </button>
                  <button
                    className={`canvas-rail-tab ${railTab === "assets" ? "active" : ""}`}
                    onClick={() => setRailTab("assets")}
                  >
                    资产
                  </button>
                </div>
                <button
                  className="canvas-palette-toggle"
                  onClick={() => togglePalette(false)}
                  title="收起面板"
                >
                  <PanelLeftClose size={14} />
                </button>
              </div>

              {railTab === "elements" ? (
                <div className="canvas-rail-list">
                  {elements.length === 0 && <div className="canvas-palette-hint">暂无节点，双击画布空白处添加</div>}
                  {elements.map((el) => (
                    <button
                      key={el.id}
                      className={`canvas-element-item ${el.id === selectedId ? "active" : ""}`}
                      onClick={() => focusNode(el.id)}
                    >
                      <span className="canvas-element-dot" style={{ background: CATEGORY_DOT[el.category] }} />
                      <span className="canvas-element-body">
                        <span className="canvas-element-label">{el.label}</span>
                        <span className="canvas-element-summary">{el.summary}</span>
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="canvas-rail-assets">
                  <div className="canvas-kindtabs">
                    {(
                      [
                        ["all", "全部"],
                        ["image", "图片"],
                        ["video", "视频"],
                        ["document", "文档"],
                      ] as [AssetKind, string][]
                    ).map(([k, label]) => (
                      <button
                        key={k}
                        className={`canvas-kindtab ${assetKind === k ? "active" : ""}`}
                        onClick={() => setAssetKind(k)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <div className="canvas-rail-list">
                    {drawerAssets.length === 0 && (
                      <div className="canvas-palette-hint">暂无资产，生成的图片/视频会自动归档</div>
                    )}
                    <AssetGrid assets={drawerAssets} onPreview={previewAsset} />
                  </div>
                </div>
              )}

              <div className="canvas-palette-hint">双击画布空白处添加节点；点节点在下方配置与运行。</div>
            </div>
          )}

          <div
            className="canvas-flow"
            ref={wrapper}
            onDoubleClick={onPaneDoubleClick}
          >
            {!paletteOpen && (
              <button
                className="canvas-palette-open"
                onClick={() => togglePalette(true)}
                title="展开面板"
              >
                <PanelLeftOpen size={14} />
              </button>
            )}
            {loaded && (
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={NODE_TYPES}
                onNodesChange={(c) => {
                  onNodesChange(c);
                  if (c.some((x) => x.type !== "select" && "position" in x)) setDirty(true);
                }}
                onEdgesChange={(c) => {
                  onEdgesChange(c);
                  if (c.some((x) => x.type === "remove")) setDirty(true);
                }}
                onConnect={onConnect}
                isValidConnection={isValidConnection}
                onNodeClick={(_, n) => setSelectedId(n.id)}
                onPaneClick={() => setSelectedId(null)}
                zoomOnDoubleClick={false}
                onlyRenderVisibleElements
                connectionRadius={28}
                fitView
                proOptions={{ hideAttribution: true }}
              >
                <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
                <Controls showInteractive={false} />
              </ReactFlow>
            )}
          </div>

          {addMenu && (
            <>
              <div className="canvas-addmenu-mask" onClick={() => setAddMenu(null)} />
              <div
                className="canvas-addmenu"
                style={{
                  left: Math.min(addMenu.x, window.innerWidth - 200),
                  top: Math.min(addMenu.y, window.innerHeight - 240),
                }}
              >
                <div className="canvas-addmenu-title">添加节点</div>
                {Object.entries(schemas).map(([type, s]) => (
                  <button
                    key={type}
                    className="canvas-addmenu-item"
                    onClick={() => {
                      addNodeAt(type, addMenu.x, addMenu.y);
                      setAddMenu(null);
                    }}
                  >
                    {CATEGORY_ICON[s.category]}
                    <span>{s.label}</span>
                    <em>{s.description}</em>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        {pickerFor && (
          <AssetPickerDialog
            slot={pickerFor.slot}
            onConfirm={confirmPick}
            onClose={() => setPickerFor(null)}
          />
        )}

        {editorFor && (
          <DocEditorDialog
            req={editorFor}
            onSave={(text) => saveDocText(editorFor.nodeId, text)}
            onClear={() => saveDocText(editorFor.nodeId, "")}
            onClose={() => setEditorFor(null)}
          />
        )}

        {lightbox && (
          <div className="canvas-lightbox" onMouseDown={() => setLightbox(null)}>
            <div className="canvas-lightbox-body" onMouseDown={(e) => e.stopPropagation()}>
              {lightbox.kind === "video" ? (
                <video src={lightbox.url} controls autoPlay />
              ) : (
                <img src={lightbox.url} alt={lightbox.title} />
              )}
              <div className="canvas-lightbox-meta">
                <span className="canvas-lightbox-name">{lightbox.title}</span>
                <span>
                  {lightbox.kind === "video" ? "视频" : "图片"}
                  {lightbox.meta ? ` · ${lightbox.meta}` : ""}
                </span>
              </div>
              <button type="button" className="canvas-dialog-close" onClick={() => setLightbox(null)}>
                <X size={16} />
              </button>
            </div>
          </div>
        )}
      </PanelCtx.Provider>
    </div>
  );
}

export default function CanvasPage({
  projectId,
  projectName,
  onBack,
}: {
  projectId: number;
  projectName: string;
  onBack: () => void;
}) {
  return (
    <ReactFlowProvider>
      <CanvasInner projectId={projectId} projectName={projectName} onBack={onBack} />
    </ReactFlowProvider>
  );
}
