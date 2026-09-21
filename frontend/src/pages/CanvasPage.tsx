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
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileText,
  Film,
  ImageIcon,
  Images,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Plus,
  RefreshCw,
  Save,
  ScanSearch,
  Share2,
  Sparkles,
  Trash2,
  Upload,
  Video,
  Wand2,
  Workflow as WorkflowIcon,
  X,
} from "lucide-react";
import { api } from "../api";
import type {
  AgentMeta,
  Asset,
  CanvasAgentDraft,
  CanvasAnimaticReport,
  CanvasRunSummary,
  CanvasDoc,
  CanvasLint,
  CanvasNodeData,
  CanvasNodeSchema,
  CanvasNodeStatus,
  CanvasNodeVersions,
  CanvasPreview,
  CameraMoveTable,
  ComfyWorkflow,
  ModelOption,
  ShareExportResult,
  ShareImportResult,
  ShareLicense,
  StyleOption,
} from "../types";
import { formatTime, ModelSelect, Spinner } from "../components/common";
import AudioRefPicker from "../components/AudioRefPicker";
import { Dialog } from "../components/Dialog";
import { PreflightNotice } from "../components/PreflightNotice";
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
  modelKeyForNodeType,
  toCanvasDocNodes,
  type DocNodeInput,
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
  /** 当前项目 id（节点版本列表要按项目 + 节点查） */
  projectId: number;
  models: ModelOption[];
  workflows: ComfyWorkflow[];
  agents: AgentMeta[];
  /** 导演风格卡（风格下拉用；只有 key 与名称，实际内容在后端） */
  styles: StyleOption[];
  running: boolean;
  updateNode: (id: string, patch: Partial<CanvasNodeData>) => void;
  /**
   * 切换某个节点交付的产物版本（`null` = 回到跟最新）。
   *
   * 单独给一个入口而不是让浮框自己 patch：产物网格与角标都是后端按**已保存的**画布算的，
   * 所以改完要等这次自动保存落库，再补刷一次节点状态——否则会出现
   * 「标签已经是第 1 版、缩略图还是第 2 版」。
   */
  pickVersion: (id: string, key: string | null) => void;
  /**
   * 给某一组候选定稿（`assetId` 传 `null` = 取消这一组的定稿）。
   *
   * 组键由后端在产物里给出（`candidateKey`：镜号 / 资产名 / 整节点），**前端不自己分组**——
   * 两处各判一次会出现「界面上分在一组的，后端其实不是一组」。
   */
  pickCandidate: (id: string, groupKey: string, assetId: number | null) => void;
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
  /** 正在出样片的节点 id（空串 = 没有）；同时只允许一条，服务端也会挡 */
  sampling: string;
  /** 出静图样片（本地渲染，不花生成费）；失败时已弹提示，返回 null */
  renderAnimatic: (id: string) => Promise<CanvasAnimaticReport | null>;
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
    <Dialog
      onClose={onClose}
      label={`${req.label} · 正文`}
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog wide"
    >
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
    </Dialog>
  );
}

/* ---------------- 社区分享 ---------------- */

/** 下载任意文本为文件（走 Blob；设了口令时直链带不上 Authorization） */
function downloadJson(text: string, filename: string) {
  const blob = new Blob([text], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * 分享一个画布。
 *
 * 两条产品上的判断，写在组件里免得被改回去：
 * - **默认走「复制分享码」**：这是「零服务器」下最省事的路径，贴进聊天窗口就完事。
 *   码太长时（后端会告诉我们）主动建议改用文件，而不是让用户复制一段必然被截断的文本。
 * - **导入先看清再落**：标题、作者、授权范围、缺什么，都摆出来之后才给「导入到画布」。
 *   别人分享的东西是什么、能不能用、允许你怎么用，这三件事不该混在一起让人猜。
 */
function ShareDialog({
  doc,
  defaultTitle,
  onApply,
  onClose,
}: {
  doc: CanvasDoc;
  defaultTitle: string;
  onApply: (doc: CanvasDoc) => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [tab, setTab] = useState<"export" | "import">("export");

  const [licenses, setLicenses] = useState<ShareLicense[]>([]);
  const [title, setTitle] = useState(defaultTitle);
  const [description, setDescription] = useState("");
  const [author, setAuthor] = useState("");
  const [license, setLicense] = useState("");
  const [busy, setBusy] = useState(false);
  const [exported, setExported] = useState<ShareExportResult | null>(null);

  const [text, setText] = useState("");
  const [incoming, setIncoming] = useState<ShareImportResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.shareLicenses();
        setLicenses(r.options);
        setLicense((prev) => prev || r.default);
      } catch {
        // 拿不到档位不该拦住分享：留空走后端默认那一档
      }
    })();
  }, []);

  const nodeCount = doc.nodes.length;

  const doExport = async () => {
    if (nodeCount === 0) {
      toast.error("空画布没什么可分享的");
      return;
    }
    setBusy(true);
    try {
      const r = await api.shareExport({ doc, title, description, author, license });
      setExported(r);
      toast.success("已打包，可以复制分享码或下载文件");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "打包失败");
    } finally {
      setBusy(false);
    }
  };

  const copyCode = async () => {
    if (!exported) return;
    try {
      await navigator.clipboard.writeText(exported.code);
      toast.success(`分享码已复制（${exported.codeLength} 字符）`);
    } catch {
      toast.error("复制失败，请手动选中下面的内容");
    }
  };

  const doImport = async (raw: string) => {
    if (!raw.trim()) {
      toast.error("请粘贴分享码或选择 .json 文件");
      return;
    }
    setBusy(true);
    try {
      setIncoming(await api.shareImport(raw));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "读取失败");
    } finally {
      setBusy(false);
    }
  };

  const onPickFile = async (file: File | undefined) => {
    if (!file) return;
    const body = await file.text();
    setText(body);
    await doImport(body);
    if (fileRef.current) fileRef.current.value = "";
  };

  const input = (value: string, set: (v: string) => void, placeholder: string) => (
    <input className="input" value={value} placeholder={placeholder} onChange={(e) => set(e.target.value)} />
  );

  return (
    <Dialog
      onClose={onClose}
      label="分享画布"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog agent"
    >
        <div className="canvas-dialog-head">
          <span className="canvas-dialog-title">分享画布</span>
          <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
            <X size={14} />
          </button>
        </div>

        <div className="segmented share-tabs">
          <button className={tab === "export" ? "active" : ""} onClick={() => setTab("export")}>
            导出给别人
          </button>
          <button className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>
            导入别人的
          </button>
        </div>

        <div className="agent-body">
          {tab === "export" ? (
            <>
              <div className="share-note">
                分享的是「怎么搭」：节点拓扑、每个节点的要求与参数。
                不含产出的图片视频，也不含你的模型服务与密钥。
              </div>
              <div className="field">
                <label className="field-label">标题与说明</label>
                {input(title, setTitle, "例如：60 秒竖屏短剧流水线")}
                {input(description, setDescription, "一句话说明这条链是干什么的（可选）")}
              </div>
              <div className="field">
                <label className="field-label">作者与授权范围</label>
                {input(author, setAuthor, "你的名字或昵称（可选）")}
                <div className="select-wrap">
                  <select className="select" value={license} onChange={(e) => setLicense(e.target.value)}>
                    {licenses.map((l) => (
                      <option key={l.key} value={l.key}>
                        {l.key} · {l.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field-hint">
                  授权范围只影响「别人可以拿它做什么」。不填就会问一次作者。
                </div>
              </div>

              {exported && (
                <div className="agent-result">
                  {exported.codeTooLong ? (
                    <div className="agent-lines warn">
                      <li>
                        分享码有 {exported.codeLength} 字符，贴到聊天窗口里容易被截断。
                        建议下载 .json 文件发给对方。
                      </li>
                    </div>
                  ) : (
                    <>
                      <div className="agent-lines-title">
                        分享码（{exported.codeLength} 字符，直接贴给对方）
                      </div>
                      <textarea className="input share-code" readOnly rows={4} value={exported.code} />
                    </>
                  )}
                </div>
              )}
            </>
          ) : (
            <>
              <div className="field">
                <label className="field-label">粘贴分享码，或选择对方发来的 .json 文件</label>
                <textarea
                  className="input share-code"
                  rows={5}
                  value={text}
                  placeholder={'XMS1:… 或 {"format":"xiaoma-share",…}'}
                  onChange={(e) => setText(e.target.value)}
                />
                <div className="share-pick">
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => fileRef.current?.click()}>
                    <Upload size={14} />
                    选择 .json 文件
                  </button>
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".json,application/json,text/plain"
                    hidden
                    onChange={(e) => void onPickFile(e.target.files?.[0])}
                  />
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={busy}
                    onClick={() => void doImport(text)}
                  >
                    {busy ? <Spinner /> : null}
                    读一读内容
                  </button>
                </div>
              </div>

              {incoming && (
                <div className="agent-result">
                  {incoming.ok ? (
                    <div className="share-meta">
                      <div className="share-meta-title">{incoming.title || "未命名画布"}</div>
                      {incoming.description ? (
                        <div className="share-meta-desc">{incoming.description}</div>
                      ) : null}
                      <div className="share-meta-line">
                        {incoming.author ? `作者：${incoming.author} · ` : ""}
                        {incoming.createdAt ? `${incoming.createdAt.replace("T", " ")} · ` : ""}
                        {`${incoming.doc.nodes.length} 个节点 / ${incoming.doc.edges.length} 条连线`}
                      </div>
                      <div className="share-meta-line">
                        授权：{incoming.license || "未声明"}
                        {incoming.licenseText ? `（${incoming.licenseText}）` : ""}
                      </div>
                    </div>
                  ) : (
                    <div className="agent-fail">
                      <div className="agent-fail-title">这份分享用不了</div>
                      <ul className="agent-lines">
                        {incoming.errors.map((e, i) => (
                          <li key={i}>{e}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {incoming.warnings.length > 0 && (
                    <ul className="agent-lines warn">
                      {incoming.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        <div className="canvas-dialog-foot">
          <span className="canvas-dialog-hint">
            {tab === "export"
              ? `${nodeCount} 个节点 · 服务端不存任何分享内容`
              : "导入只读取，点确认才落到画布上"}
          </span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
          {tab === "export" ? (
            <>
              {exported && (
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => void copyCode()}>
                  复制分享码
                </button>
              )}
              {exported && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => downloadJson(JSON.stringify(exported.snapshot, null, 2), `${title || "canvas"}.xiaoma-share.json`)}
                >
                  下载 .json
                </button>
              )}
              <button type="button" className="btn btn-primary btn-sm" onClick={() => void doExport()} disabled={busy}>
                {busy ? <Spinner light /> : null}
                {exported ? "重新打包" : "打包"}
              </button>
            </>
          ) : (
            incoming?.ok && (
              <button type="button" className="btn btn-primary btn-sm" onClick={() => onApply(incoming.doc)}>
                导入到画布
              </button>
            )
          )}
        </div>
    </Dialog>
  );
}

/* ---------------- AI 搭画布 ---------------- */

const AGENT_EXAMPLES = [
  "做一个 60 秒竖屏短剧，三幕结构，从一句话创意开始",
  "只要文字产出：小说到分镜，先不要出图",
  "角色要立得住：先出资产表做设定图，再逐镜出图",
];

/** 预览卡片与间距：固定尺寸而不是按后端坐标缩放——7 列的链缩到 460px 宽会糊成一坨 */
const MINI_COL_W = 106;
const MINI_ROW_H = 38;
const MINI_CARD_W = 88;
const MINI_CARD_H = 26;

/**
 * 草稿的迷你预览。
 *
 * 不按后端坐标等比缩放，而是取「列序 / 行序」重新摆：后端布局本来就是整齐的网格，
 * 取序号之后无论 3 个节点还是 12 个节点，预览都一样看得清。
 * 形状忠实（分叉能看出来），尺寸不忠实——这是有意的取舍。
 */
function DraftMiniMap({
  draft,
  labelOf,
}: {
  draft: CanvasAgentDraft;
  labelOf: (kind: string) => string;
}) {
  const xs = [...new Set(draft.nodes.map((n) => n.position.x))].sort((a, b) => a - b);
  const colOf = new Map(xs.map((x, i) => [x, i]));
  const byCol = new Map<number, CanvasAgentDraft["nodes"]>();
  for (const n of draft.nodes) {
    const c = colOf.get(n.position.x) ?? 0;
    byCol.set(c, [...(byCol.get(c) ?? []), n]);
  }
  const place = new Map<string, { x: number; y: number }>();
  let maxRows = 1;
  for (const [col, list] of byCol) {
    const sorted = [...list].sort((a, b) => a.position.y - b.position.y);
    maxRows = Math.max(maxRows, sorted.length);
    sorted.forEach((n, row) => place.set(n.id, { x: col * MINI_COL_W, y: row * MINI_ROW_H }));
  }
  const width = Math.max(1, xs.length) * MINI_COL_W;
  const height = maxRows * MINI_ROW_H;

  return (
    <div className="agent-mini">
      <div className="agent-mini-inner" style={{ width, height }}>
        <svg className="agent-mini-edges" width={width} height={height}>
          {draft.edges.map((e, i) => {
            const a = place.get(e.from);
            const b = place.get(e.to);
            if (!a || !b) return null;
            const y1 = a.y + MINI_CARD_H / 2;
            const y2 = b.y + MINI_CARD_H / 2;
            const mid = (a.x + MINI_CARD_W + b.x) / 2;
            return (
              <path
                key={i}
                d={`M ${a.x + MINI_CARD_W} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${b.x} ${y2}`}
              />
            );
          })}
        </svg>
        {draft.nodes.map((n) => {
          const p = place.get(n.id)!;
          return (
            <div
              key={n.id}
              className="agent-mini-node"
              style={{ left: p.x, top: p.y, width: MINI_CARD_W, height: MINI_CARD_H }}
              title={String(n.data.prompt ?? "")}
            >
              {labelOf(n.kind)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 状态 → 这块面板该怎么说话。文案集中在表里，免得散在 JSX 里各写一遍。 */
const AGENT_FAIL_TITLE: Record<CanvasAgentDraft["status"], string> = {
  ok: "",
  need_input: "需求再具体一点",
  need_credentials: "还没有可用的文本模型",
  unparsable: "模型没有按格式回答",
  upstream_error: "调用模型失败",
};

function AgentDialog({
  textModels,
  schemas,
  onApply,
  onClose,
}: {
  textModels: ModelOption[];
  schemas: Record<string, CanvasNodeSchema>;
  onApply: (draft: CanvasAgentDraft) => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [brief, setBrief] = useState("");
  const [modelKey, setModelKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<CanvasAgentDraft | null>(null);

  const labelOf = (kind: string) => schemas[kind]?.label ?? kind;

  const run = async () => {
    if (brief.trim().length < 4) {
      toast.error("先说说你想做什么");
      return;
    }
    setBusy(true);
    try {
      setDraft(await api.canvasAgentPlan({ brief: brief.trim(), model_key: modelKey }));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "生成失败");
    } finally {
      setBusy(false);
    }
  };

  const ok = draft?.status === "ok";

  return (
    <Dialog
      onClose={onClose}
      label="AI 搭画布"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog agent"
    >
        <div className="canvas-dialog-head">
          <span className="canvas-dialog-title">AI 搭画布</span>
          <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
            <X size={14} />
          </button>
        </div>

        <div className="agent-body">
          <div className="field">
            <label className="field-label">你想要什么</label>
            <textarea
              className="input agent-brief"
              rows={2}
              autoFocus
              value={brief}
              placeholder="例如：做一个 60 秒竖屏短剧，三幕结构，从一句话创意开始"
              onChange={(e) => setBrief(e.target.value)}
            />
            <div className="agent-examples">
              {AGENT_EXAMPLES.map((ex) => (
                <button key={ex} type="button" className="agent-example" onClick={() => setBrief(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">用哪个模型搭（可留空，自动挑一个可用的文本模型）</label>
            <ModelSelect models={textModels} value={modelKey} onChange={setModelKey} placeholder="自动选择" />
          </div>

          {draft && (
            <div className="agent-result">
              {ok ? (
                <>
                  <div className="agent-summary">{draft.summary || "已按你的需求搭好草稿"}</div>
                  <DraftMiniMap draft={draft} labelOf={labelOf} />
                  <div className="agent-node-list">
                    {draft.nodes.map((n) => (
                      <div key={n.id} className="agent-node-row">
                        <span className="agent-node-kind">{labelOf(n.kind)}</span>
                        <span className="agent-node-prompt">{String(n.data.prompt ?? "") || "（未写要求）"}</span>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="agent-fail">
                  <div className="agent-fail-title">{AGENT_FAIL_TITLE[draft.status]}</div>
                  {draft.nextAction ? <div className="agent-fail-next">{draft.nextAction}</div> : null}
                </div>
              )}

              {draft.warnings.length > 0 && (
                <ul className="agent-lines warn">
                  {draft.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
              {draft.notes.length > 0 && (
                <>
                  <div className="agent-lines-title">我对模型给的东西做了这些改动</div>
                  <ul className="agent-lines">
                    {draft.notes.map((n, i) => (
                      <li key={i}>{n}</li>
                    ))}
                  </ul>
                </>
              )}
              {ok && (
                <div className="agent-caveat">
                  这只是草稿：落定之后每个节点的要求都可以再改，也可以先只跑前面几步看看效果。
                </div>
              )}
            </div>
          )}
        </div>

        <div className="canvas-dialog-foot">
          <span className="canvas-dialog-hint">
            {ok ? `${draft.nodes.length} 个节点 · 还没放到画布上` : "生成不影响现有画布"}
          </span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void run()} disabled={busy}>
            {busy ? <Spinner /> : <Sparkles size={14} />}
            {draft ? "重新生成" : "生成草稿"}
          </button>
          {ok && (
            <button type="button" className="btn btn-primary btn-sm" onClick={() => onApply(draft)}>
              放到画布上
            </button>
          )}
        </div>
    </Dialog>
  );
}

/* ---------------- 运行整图的二次确认（扣费前先说清楚） ---------------- */

const PREVIEW_KIND_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  workflow: "本机工作流",
};

function kindSummary(kinds: Record<string, number>): string {
  return Object.entries(kinds)
    .map(([k, n]) => `${PREVIEW_KIND_LABEL[k] ?? k} ${n}`)
    .join(" · ");
}

/**
 * 运镜词表参考（体检弹窗里那条「不在运镜词表里」的配套）。
 *
 * 为什么非要有它：体检只会说「这个说法不在词表里，最接近的是 X」。用户要改，得先知道
 * **词表里到底有什么**——尤其「主观镜头」那类被点名的写法，光看一条建议是猜不出全貌的。
 * 词表由后端下发（`services/camera_moves.py` 一处为真），前端不抄一份。
 *
 * 默认收起：找上门来的人（被点名的人）才展开，没被点名的人不必看一张 35 行的表。
 */
function CameraMoveReference() {
  const [table, setTable] = useState<CameraMoveTable | null>(null);
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open || table || failed) return;
    void api.listCameraMoves().then(setTable).catch(() => setFailed(true));
  }, [open, table, failed]);

  return (
    <div className="lint-vocab">
      <button type="button" className="lint-vocab-head" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        运镜词表
        <span className="lint-vocab-tag">
          分镜表「运镜」那一栏的合法写法{table ? `（${table.moves.length} 种）` : ""}
        </span>
      </button>

      {open && failed && (
        <div className="lint-vocab-fail">
          词表没取到——刷新页面再试一次。体检本身的结果不受影响。
        </div>
      )}

      {open && table && (
        <div className="lint-vocab-body">
          {table.groups.map((g) => {
            const items = table.moves.filter((m) => m.group === g.key);
            if (items.length === 0) return null;
            return (
              <div key={g.key} className="lint-vocab-row">
                <span className="lint-vocab-group">{g.label}</span>
                <span className="lint-vocab-words">
                  {items.map((m, i) => (
                    // 悬停看这一条是干什么用的：知道有哪些词，还得知道什么时候用哪个
                    <span key={m.key}>
                      {i > 0 ? "、" : ""}
                      <span className="lint-vocab-word" title={m.hint}>
                        {m.label}
                      </span>
                    </span>
                  ))}
                </span>
              </div>
            );
          })}
          <div className="lint-vocab-row lint-vocab-row-note">
            <span className="lint-vocab-group">不是运镜</span>
            <span className="lint-vocab-words">
              {table.notMoves.join("、")}
              <span className="lint-vocab-hint">
                ——这些是机位 / 镜头类型，写在画面描述里；运镜那一栏另选一个运动方式。
              </span>
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * 分镜静态体检的结果弹窗。
 *
 * 与「运行整图」那个确认弹窗的分工：那个回答**要花多少钱**，这个回答**拍出来会不会难看**。
 * 所以它不放在「点生成」的那条路上，而是一个随时可点、零成本的检查——体检本身
 * 不建任务、不调模型，也就没有任何理由拦着人往下走（`blocking` 恒为 false）。
 */
function LintDialog({ report, onClose }: { report: CanvasLint; onClose: () => void }) {
  const warns = report.warnings.filter((w) => w.level === "warn").length;
  // 有运镜相关的问题才显示词表参考：它正是给「被点名的人」看的
  const moveTrouble = report.warnings.some(
    (w) => w.code.startsWith("move") || w.code === "missing_move",
  );
  // 注意：不能把「有提醒 / 没查出问题」两种情况都塞进 PreflightNotice 的 headline——
  // 它在 warnings 为空时直接返回 null，那句「没查出问题」会永远显示不出来。
  const head =
    `有 ${report.warnings.length} 条提醒（其中 ${warns} 条建议改）。都不影响生成，` +
    "你可以照原样往下走，也可以先改分镜表——现在改一分钱都不用花。";

  return (
    <Dialog
      onClose={onClose}
      label="分镜体检"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog lint-dialog"
    >
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">
          <ScanSearch size={15} /> 分镜体检
        </span>
        <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
          <X size={14} />
        </button>
      </div>

      <div className="lint-dialog-body">
        <div className="lint-dialog-total">
          检查了 <b>{report.nodes.length}</b> 个分镜节点、共 <b>{report.shotTotal}</b> 个镜头。
          <span className="lint-dialog-note">零成本：不调模型、不建任务。</span>
        </div>

        <PreflightNotice warnings={report.warnings} headline={head} />

        {/* 报「不在运镜词表里」时，把词表摆出来——不然用户只知道错了，不知道改成什么 */}
        {moveTrouble && <CameraMoveReference />}

        {report.warnings.length === 0 && report.nodes.length > 0 && (
          <div className="lint-dialog-ok">
            <CheckCircle2 size={15} />
            <span>没查出问题：镜头语言和字段都是齐的，可以直接往下跑。</span>
          </div>
        )}

        {report.nodes.length > 0 && (
          <div className="lint-dialog-nodes">
            {report.nodes.map((n) => (
              <div key={n.id} className="lint-dialog-node">
                <div className="lint-dialog-node-head">
                  <span className="lint-dialog-node-name">{n.label}</span>
                  <span className="lint-dialog-node-meta">
                    {n.summary.shots} 镜 · {n.summary.totalSeconds}s
                    {n.summary.scenes > 1 ? ` · ${n.summary.scenes} 个场景` : ""}
                    {n.findings.length > 0 ? ` · ${n.findings.length} 条` : " · 没问题"}
                  </span>
                </div>
                {Object.keys(n.summary.sizes).length > 0 && (
                  <div className="lint-dialog-node-row">
                    景别：{Object.entries(n.summary.sizes).map(([k, v]) => `${k}×${v}`).join(" ")}
                  </div>
                )}
                {Object.keys(n.summary.moves).length > 0 && (
                  <div className="lint-dialog-node-row">
                    运镜：{Object.entries(n.summary.moves).map(([k, v]) => `${k}×${v}`).join(" ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {report.skipped.length > 0 && (
          <ul className="lint-dialog-skipped">
            {report.skipped.map((s) => (
              <li key={s.id}>
                「{s.label}」{s.reason}
              </li>
            ))}
          </ul>
        )}

        {report.nodes.length === 0 && report.skipped.length === 0 && (
          <div className="lint-dialog-empty">
            画布上还没有能解析出镜头表的内容。先跑一个「分镜」节点，或者把镜头表贴进它的正文里再来。
          </div>
        )}

        <div className="lint-dialog-foot">
          <button type="button" className="btn btn-primary btn-sm" onClick={onClose}>
            知道了
          </button>
        </div>
      </div>
    </Dialog>
  );
}

/**
 * 静图缓动样片：把已出的分镜图按分镜表的「时长 + 运镜」串成一条能看的片子。
 *
 * 这是「先审节奏再花钱」那一步：出视频是真金白银，而节奏对不对、运镜方案成不成立，
 * 在静图阶段就能看出来——本地渲染一条，零生成成本。
 *
 * 两个刻意的取舍：
 * 1. **运镜照分镜表来**。写「固定」就真的不动，那样才能看出「这场戏全是固定镜头，
 *    是不是太闷」；给所有镜头套同一个推近，等于把分镜表的运镜栏当废纸。
 * 2. **少了哪几镜要说出来**。`skipped` / `truncated` 直接摆在下面，
 *    不然用户会拿一条缺了两镜的样片去判断节奏。
 */
function AnimaticSection({ id, data }: { id: string; data: CanvasNodeData }) {
  const ctx = useContext(PanelCtx);
  const [report, setReport] = useState<CanvasAnimaticReport | null>(null);
  const [broken, setBroken] = useState(false);
  const busy = ctx?.sampling === id;
  const url = String(data.sampleAssetUrl ?? "");
  const ratio = String(data.sampleRatio ?? "source");
  const res = Number(data.sampleRes ?? 1280);
  const seconds = Number(data.sampleShotSeconds ?? 3);
  const holdStill = String(data.sampleDefaultMove ?? "") === "static";
  const narrate = Boolean(data.sampleNarration);
  const voice = String(data.sampleVoice ?? "");

  const run = async () => {
    if (!ctx || busy) return;
    setBroken(false);
    setReport(await ctx.renderAnimatic(id));
  };

  const skipped = report?.skipped ?? [];
  const truncated = report?.truncated ?? [];
  const narration = report?.narration;

  return (
    <div className="canvas-sample">
      <div className="canvas-inline canvas-refhead">
        <label className="field-label">静图样片</label>
        {/* 开了旁白就不是「零成本」了：这句话不能含糊，它决定用户点不点 */}
        <span className="canvas-sample-tag">{narrate ? "本地渲染 + 1 次配音" : "零生成成本"}</span>
      </div>
      <div className="canvas-float-hint">
        按分镜表的时长与运镜把上面这些图串成一条片子，先看节奏再决定要不要出视频。
        本地渲染，不调模型、不花钱{narrate ? "；开了旁白会额外调用一次语音合成（按所选服务计费）" : ""}。
      </div>

      <div className="canvas-inline canvas-sample-opts">
        <label>
          画幅
          <select
            className="select"
            value={ratio}
            onChange={(e) => ctx?.updateNode(id, { sampleRatio: e.target.value })}
            title="跟随图片不会裁切；选固定比例时按比例裁切填满"
          >
            <option value="source">跟随图片</option>
            <option value="16:9">16:9</option>
            <option value="9:16">9:16</option>
            <option value="1:1">1:1</option>
            <option value="4:3">4:3</option>
          </select>
        </label>
        <label>
          清晰度
          <select
            className="select"
            value={String(res)}
            onChange={(e) => ctx?.updateNode(id, { sampleRes: Number(e.target.value) })}
            title="720p 够看节奏，而且快得多；跟随图片时不会放大超过原图"
          >
            <option value="1280">720p</option>
            <option value="1920">1080p</option>
          </select>
        </label>
        <label>
          兜底时长
          <input
            className="input"
            type="number"
            min={1}
            max={12}
            value={seconds}
            onChange={(e) =>
              ctx?.updateNode(id, {
                sampleShotSeconds: Math.max(1, Math.min(12, Number(e.target.value) || 3)),
              })
            }
            title="分镜表没写「几秒」时，每镜按这个时长算"
          />
        </label>
        <label>
          没写运镜时
          <select
            className="select"
            value={holdStill ? "static" : ""}
            onChange={(e) => ctx?.updateNode(id, { sampleDefaultMove: e.target.value })}
            title="分镜表的运镜栏是空的、或写了认不出来的词时怎么办"
          >
            <option value="">轻微推近</option>
            <option value="static">固定不动</option>
          </select>
        </label>
      </div>

      <div className="canvas-inline canvas-sample-opts">
        <label className="canvas-sample-check" title="把分镜表里的「台词」读成一条旁白，合进样片">
          <input
            type="checkbox"
            checked={narrate}
            onChange={(e) => ctx?.updateNode(id, { sampleNarration: e.target.checked })}
          />
          加旁白
        </label>
        {narrate && (
          <label>
            音色
            <input
              className="input"
              value={voice}
              placeholder="留空 = 服务默认音色"
              onChange={(e) => ctx?.updateNode(id, { sampleVoice: e.target.value })}
              title="填服务商的音色名（如 alloy / zh-CN-XiaoxiaoNeural）；留空用服务默认音色"
            />
          </label>
        )}
      </div>

      {narrate && (
        <div className="canvas-float-hint">
          旁白念的是分镜表里每镜的「台词」那一栏（写成「（无）」的镜头会跳过），
          整段念成一条音轨、不与镜头逐一对齐——逐句对齐留给「按角色配音」那一步。
          没写台词的分镜表会直接报错提醒你先去补。
        </div>
      )}

      <button
        type="button"
        className="btn btn-ghost btn-block"
        disabled={busy || !ctx}
        onClick={() => void run()}
      >
        {busy ? <Spinner /> : <Film size={14} />}
        {busy ? "正在渲染…" : url ? "重新出一版样片" : "出样片"}
      </button>

      {busy && (
        <div className="canvas-float-hint">
          本地逐镜渲染，几十秒内出结果；同时只允许渲染一条，另一条要等它出完
        </div>
      )}

      {report && (
        <div className="canvas-sample-report">
          <span>
            {report.shots} 镜 · {report.seconds}s · {report.size[0]}×{report.size[1]} · {report.fps}fps
          </span>
          {skipped.length > 0 && (
            <span className="warn">
              跳过：{skipped.map((s) => `镜头${s.shot}（${s.reason}）`).join("、")}
            </span>
          )}
          {truncated.length > 0 && (
            <span className="warn">
              超过 {report.maxSeconds}s 上限，没收录：
              {truncated.map((t) => `镜头${t}`).join("、")}
            </span>
          )}
          {narration?.enabled && (
            <span>
              旁白：{narration.lines} 句 · {narration.chars} 字
              {narration.seconds ? ` · ${narration.seconds.toFixed(1)}s` : ""}（音色 {narration.voice}）
            </span>
          )}
          {narration?.enabled && narration.note ? (
            <span className="warn">{narration.note}</span>
          ) : null}
        </div>
      )}

      {url && !broken && (
        <div className="canvas-sample-video">
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video
            src={url}
            controls
            preload="metadata"
            onError={() => setBroken(true)}
            title="静图样片"
          />
          <div className="canvas-inline canvas-sample-actions">
            <a className="btn btn-ghost btn-xs" href={url} target="_blank" rel="noreferrer">
              下载
            </a>
            <span className="canvas-float-hint">已存进资产库（可直接在导演台里当素材用）</span>
          </div>
        </div>
      )}

      {/* 产物被删掉时给一句人话，而不是一个点不动的播放器 */}
      {url && broken && (
        <div className="canvas-float-hint">
          这条样片的文件已经不在了（可能被清理过）。重新出一版即可，点上面的按钮。
        </div>
      )}
    </div>
  );
}

/**
 * 「运行整图」前的确认弹窗。
 *
 * 整图会把图里每个节点都重跑一遍 —— 包括已经有产物的那些，每条都是真花钱的调用。
 * 所以点下去之前先把「会派几个任务、哪些会被重做、哪个现在就会挂」摆出来，
 * 而不是等跑完在任务中心里才发现花多了。
 *
 * **超配额时还要多一步**：勾选之后才能按「开始运行」，并把调用次数回传给接口
 * （服务端会按此刻的画布再算一遍，预览之后改过画布的话旧数字挡不住）。
 */
function RunConfirmDialog({
  preview,
  busy,
  onConfirm,
  onClose,
}: {
  preview: CanvasPreview;
  busy: boolean;
  onConfirm: (ackCalls: number) => void;
  onClose: () => void;
}) {
  const { totals, gate } = preview;
  const [acked, setAcked] = useState(false);
  // 只列需要解释的节点：一次多条（批量扇出）、已有产物（会被重做）、必挂、条数待定
  const rows = preview.nodes.filter(
    (n) => n.error || n.count === null || (n.count ?? 0) > 1 || n.hasOutput
  );
  const detail: Record<string, string> = {};
  rows.forEach((n) => {
    if (n.error) detail[n.id] = "现在就会失败";
    else if (n.count === null) detail[n.id] = "条数待定";
    else detail[n.id] = `${n.count} 条 · ${kindSummary(n.kinds)}`;
  });
  const needAck = Boolean(gate?.exceeds);

  return (
    <Dialog
      onClose={onClose}
      label="运行整图 · 先看一眼花费"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog run-confirm"
    >
        <div className="canvas-dialog-head">
          <span className="canvas-dialog-title">运行整图 · 先看一眼花费</span>
          <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
            <X size={14} />
          </button>
        </div>

        <div className="run-confirm-body">
          <div className="run-confirm-total">
            这一跑会执行 <b>{totals.steps}</b> 个节点
            {totals.calls > 0 ? (
              <>
                ，至少 <b>{totals.calls}</b> 次模型调用
              </>
            ) : null}
            {totals.pendingNodes > 0 ? (
              <>，另有 {totals.pendingNodes} 个节点的次数要等上游跑完才知道</>
            ) : null}
            。
          </div>

          {needAck && (
            <label className="run-confirm-gate">
              <input
                type="checkbox"
                checked={acked}
                onChange={(e) => setAcked(e.target.checked)}
              />
              <span>
                这一跑已知就要调用 <b>{gate.calls}</b> 次，超过你设的上限{" "}
                <b>{gate.limit}</b> 次（「系统设置 → 安全 → 整图运行调用上限」里可调）。
                我知道，仍然要跑。
              </span>
            </label>
          )}

          {preview.notes.length > 0 && (
            <ul className="agent-lines warn">
              {preview.notes.map((n, i) => (
                <li key={i}>{n.text}</li>
              ))}
            </ul>
          )}

          {rows.length > 0 && (
            <div className="run-confirm-nodes">
              {rows.map((n) => (
                <div key={n.id} className={`run-confirm-row${n.error ? " bad" : ""}`}>
                  <span className="run-confirm-label">{n.label}</span>
                  <span className="run-confirm-detail">{detail[n.id]}</span>
                  {n.hasOutput ? (
                    <span className="run-confirm-badge">已有 {n.outputCount} 个产物，会重做</span>
                  ) : null}
                </div>
              ))}
            </div>
          )}

          <div className="run-confirm-caveat">
            {totals.blockedNodes > 0
              ? "标了「现在就会失败」的节点这一跑会跳过，其余照跑；建议先补齐配置再跑。"
              : "想省钱的话，可以先用单个节点上的「运行」只补跑没出好的那几步。"}
          </div>
        </div>

        <div className="canvas-dialog-foot">
          <span className="canvas-dialog-hint">预估与实际派发用的是同一段逻辑</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            先不跑
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => onConfirm(needAck ? gate.ack : 0)}
            disabled={busy || (needAck && !acked)}
          >
            {busy ? <Spinner /> : <Play size={14} />}
            开始运行
          </button>
        </div>
    </Dialog>
  );
}

/**
 * 跑完之后的「实际账」。
 *
 * 为什么要单独看一眼：预估是「至少几次」，实际可能因为重试、上游切块、某个环节被跳过
 * 而不同。差得多的时候必须说出来，不然用户对这张表的信任就停在第一次「怎么和我算的不一样」。
 */
function RunReportDialog({
  summary,
  estimate,
  onClose,
}: {
  summary: CanvasRunSummary;
  estimate: number;
  onClose: () => void;
}) {
  const delta = summary.calls - estimate;
  const level = summary.failed > 0 || Math.abs(delta) >= 3 ? "warn" : "ok";
  const products = [
    summary.products.images ? `${summary.products.images} 张图` : "",
    summary.products.videos
      ? `${summary.products.videos} 段视频${summary.products.videoSeconds ? `（共 ${summary.products.videoSeconds} 秒）` : ""}`
      : "",
    summary.products.documents ? `${summary.products.documents} 份正文` : "",
  ].filter(Boolean);

  return (
    <Dialog
      onClose={onClose}
      label="这一跑的实际账"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog run-report"
    >
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">这一跑的实际账</span>
        <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
          <X size={14} />
        </button>
      </div>

      <div className="run-confirm-body">
        <div className={`run-report-headline ${level}`}>
          {level === "ok" ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
          <span>
            实际调用 <b>{summary.calls}</b> 次
            {estimate > 0 ? `（预估 ${estimate} 次）` : ""}
            {delta === 0
              ? "，和预估一致。"
              : delta > 0
                ? `，比预估多 ${delta} 次。`
                : `，比预估少 ${-delta} 次。`}
          </span>
        </div>

        <div className="run-confirm-total">
          成功 <b>{summary.completed}</b> 条
          {summary.failed > 0 ? (
            <>
              ，失败 <b className="run-report-bad">{summary.failed}</b> 条
            </>
          ) : (
            "，没有失败"
          )}
          {products.length > 0 ? <>，产出 {products.join("、")}</> : null}
          {summary.elapsedSec !== null ? <>，耗时 {formatDuration(summary.elapsedSec)}</> : null}。
        </div>

        {summary.failed > 0 && (
          <div className="run-confirm-caveat">
            失败的任务不会因为失败就不计费 —— 去「任务中心」看那几条的报错，
            多半是模型或参数的问题，改完单独重跑那一步即可。
          </div>
        )}

        {summary.nodes.length > 1 && (
          <div className="run-confirm-nodes">
            {summary.nodes.map((n) => (
              <div key={n.id} className={`run-confirm-row${n.failed ? " bad" : ""}`}>
                <span className="run-confirm-label">{n.label}</span>
                <span className="run-confirm-detail">{n.calls} 次</span>
                {n.failed ? (
                  <span className="run-confirm-badge">{n.failed} 条失败</span>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="canvas-dialog-foot">
        <span className="canvas-dialog-hint">账按我们自己发出去的调用算，不含供应商侧的用量上报</span>
        <button type="button" className="btn btn-primary btn-sm" onClick={onClose}>
          知道了
        </button>
      </div>
    </Dialog>
  );
}

/** 秒数说成人话：90 → 「1 分 30 秒」 */
function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${s % 60} 秒`;
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

  /**
   * 产物版本栈：这个节点历次生成各留一版，可以回滚到旧的那一版。
   *
   * 随 `statusKey` 重拉（刚跑完就多一版）。**拉不到不算致命**：节点上「已回滚」这件事
   * 记在 `data.versionKey` 里，本地就有，所以即使列表没取回来，浮框照样会如实标出
   * 「已回滚到旧版本」——不会因为一次网络抖动就装作没回滚过。
   */
  const [versions, setVersions] = useState<CanvasNodeVersions | null>(null);
  const projectId = ctx?.projectId ?? 0;
  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    api
      .nodeVersions(projectId, id)
      .then((v) => {
        if (alive) setVersions(v);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [projectId, id, statusKey]);

  /**
   * 浮框超出画布可视区时把画布平移一点，让整块浮框都落在屏幕内
   * （节点在画布任何位置都能看全，不用手动拖画布）
   *
   * 但「什么时候才该对齐」有讲究，否则就是「拖节点时画布乱窜」：
   *   浮框刚挂载（= 刚选中一个节点）、或浮框变高变矮了（产物出来了）→ 对齐
   *   只是浮框的位置变了、尺寸没变 → 不动。位置变而尺寸不变，说明移动的是
   *   节点或画布本身（都是用户自己拖的），这时候再把画布拽回去就是画布在乱走。
   */
  const lastAlignHeight = useRef(0);

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
      // 初值 0 保证「刚挂载」这一趟必对齐；之后只有尺寸真的变了才再对齐
      const resized = Math.abs(r.height - lastAlignHeight.current) >= 6;
      lastAlignHeight.current = r.height;
      if (!resized) return;

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
  /**
   * 候选与定稿：同一组的几张是同一镜 / 同一资产的备选，挑一张定稿给下游用。
   *
   * **组键来自后端**（产物里的 `candidateKey`），前端只负责按它数个数——
   * 分组规则重写一遍的话，会出现「界面上分在一组的，后端其实不是一组」。
   */
  const picks = (data.picks as Record<string, number> | undefined) ?? {};
  const groupSize = new Map<string, number>();
  for (const a of imageProducts) {
    const k = a.candidateKey ?? "";
    groupSize.set(k, (groupSize.get(k) ?? 0) + 1);
  }
  /** 有多张可挑的组数（只有一张的组不给「定稿」按钮——没得挑） */
  const pickableGroups = [...groupSize.values()].filter((n) => n > 1).length;
  const pickedGroups = Object.keys(picks).filter((k) => groupSize.has(k)).length;
  /** 定稿指不到当前这一版的任何一张（回滚到了定稿之前、或换了版本）——要如实说 */
  const pickMissing = Object.keys(picks).filter((k) => !groupSize.has(k)).length;
  // 产物版本栈：节点上回滚到的那一版（空 = 跟最新），与后端读的是同一个字段
  const versionPin = String(data.versionKey ?? "");
  const versionItems = versions?.items ?? [];
  /**
   * 当前这一版**以节点上的字段为准**，不看接口回的 `activeKey`。
   *
   * 理由：用户刚在下拉里改完、画布还没保存时，接口回的仍是上一次保存时的算法结果，
   * 界面就会「选了半天没反应」（浏览器走查时正是这样）。本地字段才是这次要保存下去的值，
   * 拿它算，选完立刻就对。指不到任何一版时退回最新那一版（与后端同口径）。
   */
  const activeVersion =
    (versionPin ? versionItems.find((v) => v.key === versionPin) : undefined) ?? versionItems[0];
  // 回滚到的那一版已经不在了（历史任务被清理过）——要如实说，不能装作没事
  const pinMissing = Boolean(versionPin) && versionItems.length > 0
    && !versionItems.some((v) => v.key === versionPin);
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

      {features.includes("shotDialogue") && isShotVideoNode && (
        // 逐镜对白（数字人）：每镜的「台词」合成成配音，再作为参考音附给这一镜的视频。
        // 只在逐镜出片时才有意义——「整段一个视频」没有「这一镜的台词」这个概念。
        // 与其余区块同一套写法：能力由注册表的 features 声明，面板照着画。
        <div className="field">
          <label className="canvas-check">
            <input
              type="checkbox"
              checked={data.shotDialogue === true}
              onChange={(e) => patch({ shotDialogue: e.target.checked })}
            />
            <span>逐镜对白（数字人）</span>
          </label>
          <div className="canvas-float-hint">
            每一镜的「台词」先合成成配音，再作为参考音附给这一镜的视频——画面里的人就说这句话。
            读不完这一镜的台词不会出片（那一镜跳过，并在日志里说明是哪儿的问题），
            台词写成「（无）」的镜头照常出无声片子。
          </div>
          {data.shotDialogue === true && (
            <>
              <label className="field-label" style={{ marginTop: 10 }}>
                默认音色
                <span className="field-hint-inline">留空 = 用语音服务的默认音色</span>
              </label>
              <input
                className="input"
                value={String(data.shotVoice ?? "")}
                placeholder="留空 = 服务默认音色"
                onChange={(e) => patch({ shotVoice: e.target.value })}
              />
              <label className="field-label" style={{ marginTop: 10 }}>
                角色音色（可选，每行一条）
                <span className="field-hint-inline">小焰=nova</span>
              </label>
              <textarea
                className="textarea"
                rows={2}
                value={String(data.shotVoices ?? "")}
                placeholder={"小焰=nova\n阿澈=echo"}
                onChange={(e) => patch({ shotVoices: e.target.value })}
              />
              <div className="canvas-float-hint">
                角色名取自台词前的标签（分镜表里写「小焰：你终于来了」就认「小焰」），
                没列出来的角色用上面的默认音色。音色名填所选服务商那一套。
              </div>
            </>
          )}
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
        {features.includes("audioRef") && (
          // 对白音轨（数字人 / 口播）：挂一条配音，让画面里的人说出这句话。
          // 刻意放在「时长」上面：这两项是联动的——配音比时长长会被后端拦下，
          // 摆在一起用户才看得见「要么调时长、要么换短一点的配音」。
          <div className="field">
            <label className="field-label">
              对白音轨（可选）
              <span className="field-hint-inline">挂一段配音，出片自带声音</span>
            </label>
            <AudioRefPicker
              value={data.audioRefAssetId ? Number(data.audioRefAssetId) : null}
              // 节点上只存 id：资产本身在库里，节点数据里放一份副本迟早不一致
              onChange={(asset) => ctx?.updateNode(id, { audioRefAssetId: asset ? asset.id : null })}
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

      {(versionItems.length > 1 || versionPin) && (
        // 产物版本栈：同一节点多次生成各留一版，可以回滚。只有一版时不占地方。
        <div className="field">
          <div className="canvas-inline canvas-refhead">
            <label className="field-label">
              产物版本
              <em className="canvas-doc-count">
                {activeVersion
                  ? `（第 ${activeVersion.index} / ${activeVersion.total} 版）`
                  : "（已回滚）"}
              </em>
            </label>
            {versionPin && (
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={() => ctx.pickVersion(id, null)}
              >
                切到最新
              </button>
            )}
          </div>
          {versionItems.length > 0 ? (
            <select
              className="input"
              value={activeVersion?.key ?? ""}
              onChange={(e) => {
                // 选「最新」= 不再钉住（跟最新走），不能把最新的 key 写进去——
                // 否则下次生成出新版时，节点还钉在旧的「最新」上，像是回滚没生效
                const picked = versionItems.find((v) => v.key === e.target.value);
                ctx.pickVersion(id, picked?.latest ? null : e.target.value);
              }}
            >
              {versionItems.map((v) => {
                const bits = [`第 ${v.index} 版`];
                if (v.latest) bits.push("最新");
                bits.push(`${v.assetCount} 件产物`);
                if (v.failedCount) bits.push(`${v.failedCount} 条失败`);
                if (v.createdAt) bits.push(formatTime(v.createdAt));
                return (
                  <option key={v.key} value={v.key}>
                    {bits.join(" · ")}
                  </option>
                );
              })}
            </select>
          ) : (
            <div className="field-hint danger">
              版本列表没取回来（后端或网络异常），但节点上仍然记着「已回滚」——
              刷新一下或点「切到最新」都能回到跟最新。
            </div>
          )}
          {pinMissing ? (
            <div className="field-hint danger">
              你回滚到的那一版已经不在了（历史任务被清理过），现在给下游的是最新这版。
            </div>
          ) : versionPin && activeVersion && !activeVersion.latest ? (
            <div className="field-hint danger">
              下游现在读的是第 {activeVersion.index} 版（不是最新那版）。想让新版重新生效就选它，
              或者点「切到最新」。
            </div>
          ) : (
            <div className="field-hint">
              下游读的是选中的这一版；每生成一次就多一版，旧的都留着。
              回滚在「单跑节点」时生效——整图运行会把每个节点重跑一遍，那时一律按最新算。
            </div>
          )}
        </div>
      )}

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
            {imageProducts.map((a, i) => {
              const key = a.candidateKey ?? "";
              // 只有「这一组不止一张」时才给定稿按钮：一张没什么可挑的
              const many = (groupSize.get(key) ?? 0) > 1;
              const picked = many && picks[key] === a.id;
              return (
                <div key={a.id} className={picked ? "canvas-prodcell is-picked" : "canvas-prodcell"}>
                  <button
                    type="button"
                    className="canvas-prodthumb"
                    onClick={() => ctx.preview(a.url, a.title || a.name)}
                    title={a.title || a.name}
                  >
                    <img src={a.url} alt={a.label || a.name} loading="lazy" />
                    <span className="canvas-prodlabel">{a.label || `#${i + 1}`}</span>
                  </button>
                  {many && (
                    <button
                      type="button"
                      className={picked ? "canvas-pick on" : "canvas-pick"}
                      onClick={() => ctx.pickCandidate(id, key, picked ? null : a.id)}
                      title={picked ? "取消定稿（下游不再只认这一张）" : "定稿：下游只用这一张"}
                    >
                      {picked ? "已定稿" : "定稿"}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
          {pickableGroups > 0 && (
            <div className="field-hint">
              {pickedGroups > 0
                ? `下游只用已定稿的那张（${pickedGroups} 组已定稿）；没定稿的组会把整组都交给下游。`
                : "这几组都有多张候选：点格子右上角「定稿」，下游就只用挑中的那一张；不定稿时下游会拿到整组。"}
            </div>
          )}
          {pickMissing > 0 && (
            <div className="field-hint danger">
              有 {pickMissing} 组定稿指不到当前这一版的图（多半是回滚到了定稿之前的那一版，或又生成了一版）——
              那几组现在会把整组交给下游。重新点一次「定稿」即可。
            </div>
          )}
          <div className="field-hint">
            {String(data.nodeType) === ASSET_IMAGE_KIND
              ? "点开可放大；资产名已入库，下游提到名字会自动挂图"
              : "点开可放大；格子上的标签就是镜号"}
          </div>
        </div>
      )}

      {features.includes("animatic") && <AnimaticSection id={id} data={data} />}

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
    <Dialog
      onClose={onClose}
      label={single ? `选择${slot === "first" ? "首帧" : "尾帧"}图片` : "从资产库选择参考（可多选）"}
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog"
    >
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
    </Dialog>
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
  const [agentOpen, setAgentOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [pickerFor, setPickerFor] = useState<{ nodeId: string; slot?: "first" | "last" } | null>(null);
  const [lightbox, setLightbox] = useState<LightboxItem | null>(null);
  const [editorFor, setEditorFor] = useState<DocEditRequest | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedTick, setSavedTick] = useState(0);
  const [running, setRunning] = useState(false);
  // 整图运行前的预估结果（非 null 时弹确认框）
  const [runPreview, setRunPreview] = useState<CanvasPreview | null>(null);
  // 分镜体检结果（非 null 时弹报告）
  const [lintReport, setLintReport] = useState<CanvasLint | null>(null);
  const [linting, setLinting] = useState(false);
  // 正在出样片的节点 id：渲染是本地 CPU 活，一次只跑一条（服务端也会挡）
  const [sampling, setSampling] = useState("");
  // 整图跑完之后的「实际账」：非 null 时弹出来（预估一起显示，才能看出差多少）
  const [runReport, setRunReport] = useState<CanvasRunSummary | null>(null);
  const [runEstimate, setRunEstimate] = useState(0);
  // 盯这一跑的轮询句柄（组件卸载时要清掉）
  const runWatcher = useRef<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  /** 刚切过产物版本：自动保存落库后要补刷一次节点状态（见 NodePanelCtx.pickVersion） */
  const versionSwitchPending = useRef(false);
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

  /**
   * 把一批外来的节点与连线落到画布上（AI 草稿与分享导入共用这一份）。
   *
   * 三件必须做对的事，两个来源都得满足，所以只写一遍：
   * - **与已有内容错开**：外来坐标都从固定原点算起，直接落上去会正好压在旧节点上，
   *   用户看到的是「画布被搞乱了」。整体平移到现有内容右侧。
   * - **id 重新盖章**：同一张画布可以反复导入，id 撞车会让 ReactFlow 把两批节点搅在一起。
   * - **按类型补模型**：外来内容里没有 model_key（本机的服务编号对别人没意义），
   *   落进来时按节点类型补一个本机可用的。
   * 落定后才 setDirty，自动保存接手——外来内容本身从不经过服务端。
   */
  const mergeCanvasContent = useCallback(
    (
      incomingNodes: { id: string; type: string; position: { x: number; y: number }; data: CanvasNodeData }[],
      incomingEdges: { source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null }[],
    ): number => {
      const usable = incomingNodes.filter((n) => schemas[n.type]);
      if (usable.length === 0) return 0;

      const stamp = Date.now().toString(36);
      const existing = nodesRef.current;
      let shift = { x: 0, y: 0 };
      if (existing.length > 0) {
        const inMinX = Math.min(...usable.map((n) => n.position.x));
        const inMinY = Math.min(...usable.map((n) => n.position.y));
        const targetX = Math.max(...existing.map((n) => n.position.x)) + 340;
        const targetY = Math.min(...existing.map((n) => n.position.y));
        shift = { x: targetX - inMinX, y: targetY - inMinY };
      }
      const newId = (raw: string) => `im_${raw}_${stamp}`;
      const kindOf = new Map(usable.map((n) => [n.id, n.type]));

      const added: Node<CanvasNodeData>[] = usable.map((n) => ({
        id: newId(n.id),
        type: "contract",
        position: { x: n.position.x + shift.x, y: n.position.y + shift.y },
        data: {
          ...n.data,
          model_key: modelKeyForNodeType(n.type, models),
          schema: schemas[n.type],
          nodeType: n.type,
        } as CanvasNodeData,
      }));

      const addedEdges: Edge[] = incomingEdges.flatMap((e, i) => {
        const from = kindOf.get(e.source);
        const to = kindOf.get(e.target);
        if (!from || !to) return [];
        return [
          {
            id: `im_e_${i}_${stamp}`,
            source: newId(e.source),
            sourceHandle: e.sourceHandle || schemas[from].handles.sources?.[0]?.id || "out-text",
            target: newId(e.target),
            targetHandle: e.targetHandle || schemas[to].handles.targets?.[0]?.id || "in-text",
          },
        ];
      });

      setNodes((prev) => [...prev, ...added]);
      setEdges((prev) => [...prev, ...addedEdges]);
      setSelectedId(added[0]?.id ?? null);
      setDirty(true);
      return added.length;
    },
    [models, schemas, setEdges, setNodes]
  );

  const applyAgentDraft = useCallback(
    (draft: CanvasAgentDraft) => {
      const count = mergeCanvasContent(
        draft.nodes.map((n) => ({ id: n.id, type: n.kind, position: n.position, data: n.data })),
        draft.edges.map((e) => ({ source: e.from, target: e.to })),
      );
      setAgentOpen(false);
      toast.success(`已放上 ${count} 个节点，可以逐个改，也可以直接运行`);
    },
    [mergeCanvasContent, toast]
  );

  const applyImportedDoc = useCallback(
    (doc: CanvasDoc) => {
      const count = mergeCanvasContent(
        doc.nodes.map((n) => ({
          id: n.id,
          type: n.type,
          position: n.position,
          data: n.data as CanvasNodeData,
        })),
        doc.edges,
      );
      setShareOpen(false);
      if (count === 0) {
        toast.error("这份分享里没有本版本认识的节点");
        return;
      }
      toast.success(`已导入 ${count} 个节点`);
    },
    [mergeCanvasContent, toast]
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
      void saveDoc().then((ok) => {
        // 刚切过产物版本：这一版交付什么由后端按已保存的画布算，所以要补刷一次状态
        if (ok && versionSwitchPending.current) {
          versionSwitchPending.current = false;
          void refreshStatus();
        }
      });
    }, 1000);
    return () => window.clearTimeout(t);
  }, [dirty, loaded, saving, saveDoc, refreshStatus]);

  // "已保存"指示 2s 后淡出
  useEffect(() => {
    if (!savedTick) return;
    const t = window.setTimeout(() => setSavedTick(0), 2000);
    return () => window.clearTimeout(t);
  }, [savedTick]);

  // 离开画布时把「盯这一跑」的轮询停掉，别让它在后台一直问
  useEffect(() => {
    return () => {
      if (runWatcher.current) window.clearInterval(runWatcher.current);
      runWatcher.current = null;
    };
  }, []);

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

  /**
   * 分镜体检：先把画布存一次再查，否则查的是库里的旧正文，
   * 用户刚改过的分镜表不会被算进去（体检结果与眼前的内容对不上是最容易失去信任的一种失败）。
   */
  const runLint = async () => {
    if (dirty && !(await saveDoc())) return;
    setLinting(true);
    try {
      setLintReport(await api.lintCanvas(projectId));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "体检失败");
    } finally {
      setLinting(false);
    }
  };

  /**
   * 点「运行整图」：先让后端算一遍这一跑的形状（纯读），再弹确认框。
   * 整图会把已有产物也重做一遍 —— 这属于「花出去就回不来」的动作，先给人看一眼。
   */
  const runAll = async () => {
    if (dirty && !(await saveDoc())) return;
    setRunning(true);
    try {
      const preview = await api.previewCanvas(projectId);
      if (preview.totals.steps === 0) {
        toast.info("画布上还没有可执行的节点");
        return;
      }
      setRunPreview(preview);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "预估失败");
    } finally {
      setRunning(false);
    }
  };

  const confirmRunAll = async (ackCalls: number) => {
    setRunning(true);
    try {
      const r = await api.runCanvas(projectId, undefined, ackCalls);
      setRunPreview(null);
      toast.success("整图执行已启动");
      await refreshStatus();
      // 记下这一跑的预估与实际账：约 3 秒看一眼，跑完弹「实际账」
      if (r.runId) {
        setRunEstimate(r.estimate?.calls ?? 0);
        watchRun(r.runId);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "启动失败");
    } finally {
      setRunning(false);
    }
  };

  /**
   * 盯住这一跑：跑完了把「实际账」弹出来。
   *
   * 为什么要等它跑完而不是立刻查：任务还在跑的时候数字只有一半，拿半截数字当结论
   * 比不显示更糟。上限 12 分钟，到点就收手（别让一个卡住的任务把轮询挂一辈子）。
   */
  const watchRun = (runId: string) => {
    if (runWatcher.current) window.clearInterval(runWatcher.current);
    let tries = 0;
    runWatcher.current = window.setInterval(() => {
      tries += 1;
      void (async () => {
        let stop = false;
        try {
          const s = await api.runSummary(projectId, runId);
          if (s.finished) {
            setRunReport(s);
            stop = true;
          }
        } catch {
          // 查不到就别再问了（任务记录被清理、项目被删等等），账只是附赠的一句话
          stop = true;
        }
        if (tries > 240) stop = true; // 12 分钟还跑不完就别再轮询了
        if (stop && runWatcher.current) {
          window.clearInterval(runWatcher.current);
          runWatcher.current = null;
        }
        await refreshStatus();
      })();
    }, 3000);
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

  /**
   * 切换节点交付的产物版本（回滚 / 回到最新）。
   *
   * 与 `updateNodeData` 的区别只在**多打一个标记**：产物网格与画布角标是后端按
   * 已保存的画布算出来的，所以这次自动保存落库之后要补刷一次节点状态，
   * 否则界面会「标签已经是第 1 版、缩略图还是第 2 版」。
   */
  const pickVersion = useCallback((id: string, key: string | null) => {
    setNodes((prev) => prev.map((n) => (n.id === id ? { ...n, data: { ...n.data, versionKey: key } } : n)));
    setDirty(true);
    versionSwitchPending.current = true;
  }, []);

  /**
   * 给某一组候选定稿。
   *
   * 不需要像版本那样补刷状态：产物网格展示的是**全部候选**（不然用户没法挑），
   * 定稿只影响「下游拿哪一张」，所以改完等自动保存落库就够了。
   */
  const pickCandidate = useCallback((id: string, groupKey: string, assetId: number | null) => {
    setNodes((prev) =>
      prev.map((n) => {
        if (n.id !== id) return n;
        const next: Record<string, number> = { ...((n.data.picks as Record<string, number>) ?? {}) };
        if (assetId === null) delete next[groupKey];
        else next[groupKey] = assetId;
        return { ...n, data: { ...n.data, picks: next } };
      })
    );
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

  /**
   * 浮框超出可视区时自动平移画布，让整块浮框都能看见。
   *
   * 但指针按在节点上时**一律不平移**。以前没有这道闸门，于是「拖节点偶尔画布乱窜」：
   * 拖一个还没选中的节点会让它被选中、浮框随之挂载并在 420/950/1500ms 后测量，
   * 这几次测量正好落在拖动过程中；任务状态轮询也会让浮框重新测量。指针还按在节点上、
   * 画布先自己平移走了，看起来就是画布在乱窜。拖动期间挡掉之后，画布的自动平移
   * 只会发生在「刚选中一个节点」与「浮框尺寸变了」这两个时刻。
   */
  const draggingRef = useRef(false);

  const nudgeViewport = useCallback(
    (dxScreen: number, dyScreen: number) => {
      if (draggingRef.current) return;
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

  /**
   * 出静图样片：与体检同理，先存一次——后端读的是库里的画布，
   * 不存就会拿旧的设置和旧的分镜表去渲染，结果与眼前的内容对不上。
   *
   * 产物落进资产库，同时把地址写回节点：刷新页面后那条片子还在，
   * 不用为了再看一眼重新渲染一遍。
   */
  const renderAnimatic = useCallback(
    async (nodeId: string): Promise<CanvasAnimaticReport | null> => {
      if (dirty && !(await saveDoc())) return null;
      setSampling(nodeId);
      try {
        const { asset, report } = await api.renderAnimatic(projectId, nodeId);
        updateNodeData(nodeId, { sampleAssetId: asset.id, sampleAssetUrl: asset.url });
        toast.success(`样片已出：${report.shots} 镜 / ${report.seconds} 秒`);
        return report;
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "出样片失败");
        return null;
      } finally {
        setSampling("");
      }
    },
    [dirty, projectId, saveDoc, toast, updateNodeData]
  );

  const panelCtx = useMemo<NodePanelCtx>(
    () => ({
      projectId,
      models,
      workflows,
      agents,
      styles,
      running,
      updateNode: updateNodeData,
      pickVersion,
      pickCandidate,
      applyStyleToAll,
      nudgeViewport,
      preview: previewProduct,
      thumb,
      pickThumb,
      editDoc: openDocEditor,
      runNode,
      sampling,
      renderAnimatic,
      openPicker,
      reloadWorkflows,
    }),
    [
      projectId,
      models,
      workflows,
      agents,
      styles,
      running,
      sampling,
      renderAnimatic,
      updateNodeData,
      pickVersion,
      pickCandidate,
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
              onClick={() => setAgentOpen(true)}
              title="用一句话描述你想要什么，让 AI 把节点拓扑搭出来（只出草稿，确认后才落到画布上）"
            >
              <Wand2 size={14} />
              AI 搭画布
            </button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setShareOpen(true)}
              title="把这个画布分享给别人（分享码或 .json 文件），或导入别人分享的画布"
            >
              <Share2 size={14} />
              分享
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
            {/* 体检放在「花钱之前」的位置：它零成本，但能省下一整轮逐镜生成的钱 */}
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => void runLint()}
              disabled={linting || !loaded}
              title="分镜体检：检查运镜雷同 / 景别单调 / AI 腔（不调模型、不花钱）"
            >
              {linting ? <Spinner /> : <ScanSearch size={14} />}
              体检
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
                // 拖动期间不让「浮框自动平移」动画布
                onNodeDragStart={() => {
                  draggingRef.current = true;
                  // 顺手掐掉可能正在跑的自动平移动画：剩下那一两百毫秒同样会让用户觉得画布在动
                  setViewport(getViewport(), { duration: 0 });
                }}
                onNodeDragStop={() => {
                  draggingRef.current = false;
                }}
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

        {agentOpen && (
          <AgentDialog
            textModels={models.filter((m) => m.modality === "text")}
            schemas={schemas}
            onApply={applyAgentDraft}
            onClose={() => setAgentOpen(false)}
          />
        )}

        {shareOpen && (
          <ShareDialog
            // 传当前画布状态而不是项目 id：用户想分享的是屏幕上看到的东西
            doc={{
              schemaVersion: 1,
              nodes: toCanvasDocNodes(nodes as DocNodeInput[]),
              edges: edges.map((e) => ({
                id: e.id,
                source: e.source,
                target: e.target,
                sourceHandle: e.sourceHandle ?? null,
                targetHandle: e.targetHandle ?? null,
              })),
              viewport: getViewport(),
            }}
            defaultTitle={projectName}
            onApply={applyImportedDoc}
            onClose={() => setShareOpen(false)}
          />
        )}

        {runPreview && (
          <RunConfirmDialog
            preview={runPreview}
            busy={running}
            onConfirm={confirmRunAll}
            onClose={() => setRunPreview(null)}
          />
        )}

        {runReport && (
          <RunReportDialog
            summary={runReport}
            // 预估取「受理时响应里那个」；刷新过页面就用这一跑自己记着的那个
            estimate={runEstimate || runReport.expected}
            onClose={() => setRunReport(null)}
          />
        )}

        {lintReport && <LintDialog report={lintReport} onClose={() => setLintReport(null)} />}

        {lightbox && (
          <Dialog
            onClose={() => setLightbox(null)}
            label={lightbox.title}
            maskClassName="canvas-lightbox"
            className="canvas-lightbox-body"
          >
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
          </Dialog>
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
