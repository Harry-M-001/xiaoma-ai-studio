import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowUp,
  Bot,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  FolderOpen,
  Image as ImageIcon,
  ListTree,
  Loader2,
  Paperclip,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Target,
  FileText,
  Trash2,
  Workflow,
  Wrench,
  X,
} from "lucide-react";
import { api } from "../api";
import type {
  AgentPayload,
  AgentProjectInfo,
  AgentSessionInfo,
  AgentStepInfo,
  AgentWorkspaceMeta,
  Asset,
  CanvasDoc,
  CanvasNodeSchema,
  CanvasNodeStatus,
  Project,
  PromptItem,
} from "../types";
import { Empty, Spinner, ModelSelect, formatSize } from "../components/common";
import { useToast } from "../components/Toast";

/**
 * 输入框里以 `/` 开头的三条命令。
 *
 * spec / plan 是「先出方案、等人确认」——适合要细化、要验收标准的长线任务；
 * goal 是「以目标为导向，持续跑到完成」——适合直出，不用人盯着反复问「怎么样了」。
 */
const SLASH_ITEMS = [
  {
    key: "spec",
    label: "Spec",
    icon: <FileText size={15} />,
    desc: "根据需求细化完整的规范、任务、验收文档，用户确认后再严格执行，适合复杂的长线任务",
  },
  {
    key: "plan",
    label: "Plan",
    icon: <ListTree size={15} />,
    desc: "优先规划任务的执行方向，用户确认后再执行",
  },
  {
    key: "goal",
    label: "Goal",
    icon: <Target size={15} />,
    desc: "启动一个以目标为导向的任务，并持续运行直到完成",
  },
];

/**
 * Agent 工作台（2.0）。
 *
 * 三层：**项目 → 任务（会话）→ 步骤**。项目/任务的树已经收进左侧「工作区」，
 * 这一页只留对话本身。
 * - **项目**的唯一职责是把上下文分开：「选角」和「剪片」是两件事，会话不该互相看见对方的步骤；
 * - **任务**就是一次会话，问什么答什么、每一步都可回放；
 * - **步骤**里能看见工具名、参数、结果与耗时——「它说做完了」不算数。
 *
 * 两条不可退让的规矩：
 * 1. **花钱的调用单独一张确认卡**（跑整图 / 跑单节点），卡片写明会派几次调用、哪些节点会重做；
 * 2. **能力清单可展开**：哪些只读、哪些可撤销、哪些必须人点，写在界面上而不是让人猜。
 */
export default function AgentPage({
  onNavigate,
  onOpenCanvas,
  openSessionId,
  onSessionOpened,
  projectsRev = 0,
}: {
  onNavigate?: (route: string) => void;
  onOpenCanvas?: (p: { id: number; name: string }) => void;
  /** 从侧栏 / 首页指定要打开的任务；打开一次后由 App 清掉 */
  openSessionId?: number | null;
  onSessionOpened?: () => void;
  /** 侧栏新建项目后 +1：只重读项目列表，不打断当前这段对话 */
  projectsRev?: number;
}) {
  const toast = useToast();
  const [meta, setMeta] = useState<AgentWorkspaceMeta | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [session, setSession] = useState<AgentSessionInfo | null>(null);
  const [agentProject, setAgentProject] = useState<AgentProjectInfo | null>(null);
  const [steps, setSteps] = useState<AgentStepInfo[]>([]);
  const [pending, setPending] = useState<AgentStepInfo | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  /** 随这条消息带上的本地文件：拖进输入框或点回形针上传，发送后才清空 */
  const [files, setFiles] = useState<Asset[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  /** 这一轮用的斜杠命令（空 = 普通消息）；goal 会显示「持续监测中」与停止按钮 */
  const [runMode, setRunMode] = useState("");
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const [approvalOpen, setApprovalOpen] = useState(false);
  /** `+` / `@` 打开的上下文面板：资产与提示词模板 */
  const [ctxOpen, setCtxOpen] = useState(false);
  const [ctxDismissed, setCtxDismissed] = useState(false);
  const [ctxAssets, setCtxAssets] = useState<Asset[]>([]);
  const [ctxPrompts, setCtxPrompts] = useState<PromptItem[]>([]);
  const [ctxLoading, setCtxLoading] = useState(false);
  const [ctxLoaded, setCtxLoaded] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const [autoApprove, setAutoApprove] = useState(false);
  const [thinking, setThinkingState] = useState(true);
  const [schemas, setSchemas] = useState<Record<string, CanvasNodeSchema>>({});
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  // 带「指定任务」进来时，默认选择（最近一条）不许覆盖它
  const explicitOpenRef = useRef(openSessionId != null);

  const applyPayload = (payload: AgentPayload) => {
    setSession(payload.session);
    setAgentProject(payload.agentProject);
    setSteps(payload.steps);
    setPending(payload.pending);
    setProjectId(payload.session.agentProjectId);
    setAutoApprove(Boolean(payload.session.autoApprove));
    setThinkingState(Boolean(payload.session.thinking));
  };

  const refreshMeta = useCallback(async () => {
    try {
      const m = await api.agentMeta();
      setMeta(m);
      return m;
    } catch (e) {
      toast.error(`读 Agent 状态失败：${(e as Error).message}`);
      return null;
    }
  }, [toast]);

  const load = useCallback(async () => {
    const [m, ps] = await Promise.all([
      refreshMeta(),
      api.listProjects().catch(() => [] as Project[]),
    ]);
    setProjects(ps);
    if (!m) return;
    // 默认打开「最近用过的项目 → 它最近的任务」；没有项目就打开最近的任务
    const firstProject = m.projects[0];
    const target =
      (firstProject && m.sessions.find((s) => s.agentProjectId === firstProject.id)) ||
      m.sessions[0] ||
      null;
    if (target && !explicitOpenRef.current) {
      try {
        applyPayload(await api.getAgentSession(target.id));
      } catch {
        /* 会话读不出来就停在空状态 */
      }
    } else if (firstProject && !explicitOpenRef.current) {
      setProjectId(firstProject.id);
    }
  }, [refreshMeta]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * 侧栏新建了项目：只补一次元数据。
   * 不走整套 `load()`——那会把当前正在看的对话替换成「最近一条」，用户刚建完项目回来会莫名换页。
   */
  useEffect(() => {
    if (!projectsRev) return;
    void refreshMeta().then((m) => {
      if (!m) return;
      setProjectId((prev) => prev ?? m.projects[0]?.id ?? null);
    });
  }, [projectsRev, refreshMeta]);

  // 从侧栏 / 首页点某条任务：直接加载那一条
  useEffect(() => {
    if (openSessionId == null) return;
    explicitOpenRef.current = true;
    let alive = true;
    (async () => {
      try {
        const payload = await api.getAgentSession(openSessionId);
        if (alive) applyPayload(payload);
      } catch (e) {
        if (alive) toast.error(`打开任务失败：${(e as Error).message}`);
      } finally {
        if (alive) onSessionOpened?.();
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openSessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [steps.length, pending]);

  // 节点类型的中文名只在契约表里，界面不另抄一份
  useEffect(() => {
    api.canvasContract()
      .then((c) => setSchemas(c.nodeSchemas))
      .catch(() => {});
  }, []);

  /**
   * 一次请求要跑好几分钟（多轮工具 + 慢模型）。这段时间里后端每一步都落库了，
   * 所以按会话轮询就能把「正在想」「刚调完哪个工具」实时铺到时间线上，
   * 而不是让用户对着一句「正在想…」干等。
   */
  useEffect(() => {
    const sid = session?.id;
    // busy = 本标签页发出的请求；goalRunning = 服务端还在跑（刷新 / 换标签页后靠它接上）
    if ((!busy && !session?.goalRunning) || !sid) return;
    let alive = true;
    const timer = window.setInterval(async () => {
      try {
        const payload = await api.getAgentSession(sid);
        if (alive) applyPayload(payload);
      } catch {
        /* 忽略：本轮请求结束时会用最终结果覆盖一次 */
      }
    }, 1500);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, session?.id, session?.goalRunning]);

  /** 服务端还有 `/goal` 在跑（本页刷新后也接得上） */
  const goalRunning = Boolean(session?.goalRunning);

  const newSession = async () => {
    try {
      const payload = await api.createAgentSession({
        model_key: session?.modelKey || meta?.models[0]?.key || "",
        agent_project_id: projectId,
        auto_approve: autoApprove,
        thinking,
      });
      applyPayload(payload);
      await refreshMeta();
    } catch (e) {
      toast.error(`新建任务失败：${(e as Error).message}`);
    }
  };

  const removeSession = async (s: AgentSessionInfo) => {
    if (!window.confirm(`删除任务「${s.title}」？它的步骤记录会一起删掉（画布与产物不受影响）。`)) return;
    try {
      await api.deleteAgentSession(s.id);
      if (session?.id === s.id) {
        setSession(null);
        setSteps([]);
        setPending(null);
      }
      await refreshMeta();
    } catch (e) {
      toast.error(`删除任务失败：${(e as Error).message}`);
    }
  };

  /** 拖拽 / 回形针 / 粘贴进来的本地文件：先落资产库，发送时只传 asset id */
  const uploadFiles = async (list: FileList | File[]) => {
    const arr = Array.from(list);
    if (arr.length === 0 || uploading) return;
    setUploading(true);
    let ok = 0;
    for (const f of arr) {
      try {
        const asset = await api.uploadAgentFile(f);
        setFiles((prev) => [...prev, asset]);
        ok += 1;
      } catch (e) {
        toast.error(`${f.name}：${(e as Error).message}`);
      }
    }
    setUploading(false);
    if (ok) toast.success(`已添加 ${ok} 个文件`);
  };

  const send = async () => {
    const text = input.trim() || (files.length ? "看看这些文件，按你的判断处理。" : "");
    if (!text || busy || goalRunning) return;
    // 斜杠命令只认开头；命令后面的正文不能为空（后端也会拦一次）
    const match = /^\/(spec|plan|goal)\b/i.exec(text);
    const mode = match ? match[1].toLowerCase() : "";
    if (match && !text.slice(match[0].length).trim()) {
      toast.error("在命令后面写一句要做什么");
      return;
    }
    setBusy(true);
    setRunMode(mode);
    setSlashDismissed(true);
    try {
      let sid = session?.id;
      if (!sid) {
        const created = await api.createAgentSession({
          model_key: meta?.models[0]?.key || "",
          agent_project_id: projectId,
          auto_approve: autoApprove,
          thinking,
        });
        applyPayload(created);
        sid = created.session.id;
        await refreshMeta();
      }
      const payload = await api.agentSend(sid, text, files.map((f) => f.id));
      setInput("");
      setFiles([]);
      applyPayload(payload);
      if (payload.run?.status === "need_confirm") {
        toast.success("有一个不可逆的操作，等你确认");
      }
      await refreshMeta();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
      setRunMode("");
    }
  };

  /** 停止 `/goal`：请求会在当前这次模型调用收尾后结束 */
  const stopGoal = async () => {
    if (!session) return;
    try {
      const r = await api.stopAgentGoal(session.id);
      toast.info(r.running ? "已请求停止，等当前这一步收尾" : "没有正在运行的目标");
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  /** 斜杠菜单：输入框以 `/` 开头且还没敲空格时出现 */
  const slashQuery =
    !slashDismissed && input.startsWith("/") && !input.includes(" ")
      ? input.slice(1).toLowerCase()
      : null;
  const slashItems =
    slashQuery === null ? [] : SLASH_ITEMS.filter((item) => item.key.startsWith(slashQuery));
  const slashActive = Math.min(slashIndex, Math.max(0, slashItems.length - 1));

  const selectSlash = (key: string) => {
    setInput(`/${key} `);
    setSlashDismissed(true);
    setSlashIndex(0);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  };

  /**
   * 上下文面板（`+` 按钮 / 输入 `@`）：列最近的资产与提示词模板。
   * 点一项就往输入框插一个可读的引用（`@资产#12「封面图」`），模型能直接用 id 去查。
   */
  const loadContext = useCallback(async () => {
    if (ctxLoaded || ctxLoading) return;
    setCtxLoading(true);
    try {
      const [assets, prompts] = await Promise.all([
        api.listAssets({ limit: 8 }).then((r) => r.items).catch(() => [] as Asset[]),
        api.getPrompts().catch(() => [] as PromptItem[]),
      ]);
      setCtxAssets(assets);
      setCtxPrompts(prompts.slice(0, 8));
      setCtxLoaded(true);
    } finally {
      setCtxLoading(false);
    }
  }, [ctxLoaded, ctxLoading]);

  // 输入 `@`（前面是开头或空格）时就地弹出；`+` 按钮也走同一个面板
  const atMatch = /(^|\s)@([^\s@]*)$/.exec(input);
  const atQuery = atMatch ? atMatch[2].toLowerCase() : null;
  const ctxVisible = (ctxOpen || atQuery !== null) && !ctxDismissed;

  /**
   * 面板开着时，Esc 与「点别处」都要能关掉它。
   * 绑在 document 上：点 `+` 之后焦点在按钮上、输入 `@` 之后焦点虽然在输入框里，
   * 但用户随手点一下时间线时键盘事件根本不会落到 textarea 上。
   */
  useEffect(() => {
    if (!ctxVisible) return;
    const close = () => {
      setCtxDismissed(true);
      setCtxOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const onDown = (e: MouseEvent) => {
      const target = e.target as Element | null;
      if (target && (target.closest(".agent-ctx") || target.closest(".agent-plus"))) return;
      close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [ctxVisible]);

  useEffect(() => {
    if (ctxVisible) void loadContext();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctxVisible]);

  const ctxAssetsShown = ctxAssets.filter(
    (a) =>
      !atQuery ||
      (a.name || "").toLowerCase().includes(atQuery) ||
      (a.original_name || "").toLowerCase().includes(atQuery) ||
      String(a.id) === atQuery
  );
  const ctxPromptsShown = ctxPrompts.filter(
    (p) => !atQuery || p.title.toLowerCase().includes(atQuery) || p.key.toLowerCase().includes(atQuery)
  );

  const insertContext = (token: string) => {
    setInput((prev) => {
      // 输入 @ 触发时，把那段 `@xxx` 一起替换掉，不留半截查询词
      const m = /(^|\s)@([^\s@]*)$/.exec(prev);
      if (!m) return prev ? `${prev} ${token} ` : `${token} `;
      const cut = prev.length - (m[2].length + 1);
      return `${prev.slice(0, cut)}${token} `;
    });
    setCtxOpen(false);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  };

  const confirmStep = async (approve: boolean) => {
    if (!session || !pending || busy) return;
    setBusy(true);
    try {
      applyPayload(await api.agentConfirm(session.id, pending.id, approve));
      if (!approve) toast.success("已取消，没有执行");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const setSessionModel = async (modelKey: string) => {
    if (!session) return;
    try {
      applyPayload(await api.patchAgentSession(session.id, { model_key: modelKey }));
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  /** 切换审批方式：手动 = 花钱前停下等人点；自动 = 直接执行（代价仍记在时间线里）。 */
  const setApproval = async (auto: boolean) => {
    setAutoApprove(auto);
    if (!session) return; // 还没建任务：把这个选择留给下一个新任务
    try {
      applyPayload(await api.patchAgentSession(session.id, { auto_approve: auto }));
    } catch (e) {
      setAutoApprove(!auto);
      toast.error(`切换审批方式失败：${(e as Error).message}`);
    }
  };

  /**
   * 切换思考模式：开 = 先出思维链再答（DeepSeek V4 默认如此）；关 = 直接答，省 token 也快。
   * 还没建任务时只记在本地，新任务创建时带过去。
   */
  const toggleThinking = async (next: boolean) => {
    setThinkingState(next);
    if (!session) return;
    try {
      applyPayload(await api.patchAgentSession(session.id, { thinking: next }));
    } catch (e) {
      setThinkingState(!next);
      toast.error(`切换思考模式失败：${(e as Error).message}`);
    }
  };

  const setProjectCanvas = async (canvasProjectId: number | null) => {
    if (!agentProject) return;
    try {
      const p = await api.patchAgentProject(agentProject.id, { canvas_project_id: canvasProjectId });
      setAgentProject(p);
      await refreshMeta();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const canvasId = session?.canvasId ?? agentProject?.canvasProjectId ?? null;
  const boundProject = useMemo(
    () => projects.find((p) => p.id === canvasId) || null,
    [projects, canvasId]
  );
  const canvasSnap = useCanvasSnapshot(canvasId, steps.length);
  const openCanvas = useCallback(
    (id: number) => {
      const project = projects.find((p) => p.id === id);
      onOpenCanvas?.({ id, name: project?.name || `画布 #${id}` });
    },
    [projects, onOpenCanvas]
  );

  /** 当前大脑能不能开思考模式：判据在后端（provider_store.supports_thinking），前端不猜模型名 */
  const activeModelKey = session?.modelKey || meta?.models?.[0]?.key || "";
  const activeModel = useMemo(
    () => (meta?.models || []).find((m) => m.key === activeModelKey) || null,
    [meta?.models, activeModelKey]
  );
  /**
   * 目录里找不到这个 key 时（老会话绑着 `deepseek-chat` 这类已下线条目）不能直接判死：
   * 名字后半段是 DeepSeek 就说明它背后还是 DeepSeek，开关该能点。
   * 目录里找得到时以目录为准（Ollama / KIE 那些确实不支持）。
   */
  const activeModelName = activeModelKey.includes(":")
    ? activeModelKey.slice(activeModelKey.indexOf(":") + 1)
    : activeModelKey;
  const thinkingCapable = activeModel
    ? Boolean(activeModel.thinking)
    : /^deepseek/i.test(activeModelName);

  const groups = useMemo(() => {
    const tools = meta?.tools || [];
    return {
      read: tools.filter((t) => t.tier === "read"),
      write: tools.filter((t) => t.tier === "write"),
      irreversible: tools.filter((t) => t.tier === "irreversible"),
    };
  }, [meta?.tools]);

  const currentProject = meta?.projects?.find((p) => p.id === projectId) || null;

  return (
    <div className="page agent-page">
      <div className="page-header">
        <div>
          <div className="page-title">Agent</div>
        </div>
        <div className="row agent-head-row">
          {agentProject ? (
            <select
              className="select agent-project-select"
              value={agentProject.canvasProjectId ?? ""}
              onChange={(e) => void setProjectCanvas(e.target.value ? Number(e.target.value) : null)}
              title="这个项目绑定哪张自由画布（项目里所有任务共用）"
            >
              <option value="">未绑定画布</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}（#{p.id}）
                </option>
              ))}
            </select>
          ) : (
            <span className="agent-context-hint">项目就在左侧「工作区」里；点 + 新建</span>
          )}
          {boundProject && onOpenCanvas && (
            <button className="btn btn-ghost btn-sm" onClick={() => onOpenCanvas(boundProject)}>
              <FolderOpen size={14} /> 打开画布
            </button>
          )}
        </div>
      </div>

      {meta && !meta.ready && (
        <div className="studio-note">
          <AlertTriangle size={15} />
          <div>
            <div>{meta.problem}</div>
            <button className="btn btn-ghost btn-xs" onClick={() => onNavigate?.("providers")}>
              去模型服务
            </button>
          </div>
        </div>
      )}

      <div className="agent-grid">
        <section className="card agent-main">
          <div className="agent-context">
            <span>{currentProject ? currentProject.name : "未归档"}</span>
            {session && <span className="agent-context-task">· {session.title}</span>}
            {autoApprove && (
              <span className="agent-auto-note" title="跑整图 / 跑节点会直接执行，不再停下来等确认">
                自动审批
              </span>
            )}
            <div className="agent-context-actions">
              <button className="btn btn-ghost btn-xs" onClick={() => void newSession()} disabled={busy}>
                <Plus size={12} /> 新任务
              </button>
              {session && (
                <button
                  className="icon-btn"
                  onClick={() => void removeSession(session)}
                  disabled={busy}
                  title="删除当前任务（画布与产物不受影响）"
                  aria-label="删除当前任务"
                >
                  <Trash2 size={12} />
                </button>
              )}
            </div>
          </div>
          {canvasId && (
            <CanvasMonitor
              canvasId={canvasId}
              name={boundProject?.name || `画布 #${canvasId}`}
              snap={canvasSnap.snap}
              live={canvasSnap.live}
              onRefresh={canvasSnap.refresh}
              onOpen={() => openCanvas(canvasId)}
            />
          )}
          <div className="agent-timeline">
            {steps.length === 0 && !busy ? (
              <Empty
                icon={<Bot />}
                title={currentProject ? `在「${currentProject.name}」里说一句要做什么` : "说一句要做什么"}
                desc="例如：「看看这张画布现在的流程」「搭一条从创意到分镜图的链」「跑一次要花多少次调用？」"
              />
            ) : (
              steps.map((s) => (
                <StepRow
                  key={s.id}
                  step={s}
                  schemas={schemas}
                  liveCanvasId={canvasId}
                  snap={canvasSnap.snap}
                  polling={canvasSnap.live}
                  onOpenCanvas={openCanvas}
                />
              ))
            )}

            {pending && (
              <div className="agent-confirm">
                <div className="agent-confirm-head">
                  <AlertTriangle size={16} />
                  <span>这一步要你点一下：{pending.tierLabel} · {pending.toolName}</span>
                </div>
                <ConfirmPreview result={pending.toolResult} />
                <div className="agent-confirm-actions">
                  <button className="btn btn-primary btn-sm" onClick={() => void confirmStep(true)} disabled={busy}>
                    {busy ? <Spinner light /> : <Check size={14} />} 确认执行
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => void confirmStep(false)} disabled={busy}>
                    <X size={14} /> 取消
                  </button>
                </div>
              </div>
            )}

            {(busy || goalRunning) && (
              <BusyRow
                steps={steps.length}
                mode={runMode || (goalRunning ? "goal" : "")}
                onStop={
                  runMode === "goal" || goalRunning ? () => void stopGoal() : undefined
                }
              />
            )}
            <div ref={bottomRef} />
          </div>

          <div
            className={`agent-composer ${dragging ? "dragging" : ""}`}
            onDragOver={(e) => {
              if (e.dataTransfer.types.includes("Files")) {
                e.preventDefault();
                setDragging(true);
              }
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (e.dataTransfer.files.length) void uploadFiles(e.dataTransfer.files);
            }}
          >
            {slashItems.length > 0 && (
              <div className="agent-slash">
                {slashItems.map((item, index) => (
                  <button
                    key={item.key}
                    type="button"
                    className={`agent-slash-item ${index === slashActive ? "on" : ""}`}
                    // 用 mousedown：否则点菜单前 textarea 先失焦，菜单可能已经关了
                    onMouseDown={(e) => {
                      e.preventDefault();
                      selectSlash(item.key);
                    }}
                  >
                    <span className="agent-slash-icon">{item.icon}</span>
                    <span className="agent-slash-main">
                      <b>/{item.label}</b>
                      <em>{item.desc}</em>
                    </span>
                  </button>
                ))}
                <div className="agent-slash-foot">↑↓ 选择 · Enter 插入 · Esc 关闭</div>
              </div>
            )}

            {ctxVisible && (
              <div className="agent-slash agent-ctx">
                <div className="agent-ctx-head">添加上下文 · 点一项插入到输入框</div>
                {ctxLoading && <div className="agent-ctx-empty">读取资产…</div>}
                {ctxAssetsShown.length > 0 && (
                  <>
                    <div className="agent-ctx-title">
                      <ImageIcon size={12} /> 最近资产
                    </div>
                    {ctxAssetsShown.map((a) => (
                      <button
                        key={`asset-${a.id}`}
                        type="button"
                        className="agent-slash-item"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          insertContext(`@资产#${a.id}「${a.name || a.original_name || a.kind}」`);
                        }}
                      >
                        <span className="agent-slash-icon">
                          <ImageIcon size={14} />
                        </span>
                        <span className="agent-slash-main">
                          <b>
                            #{a.id} {a.name || a.original_name || "未命名"}
                          </b>
                          <em>
                            {a.kind}
                            {a.width && a.height ? ` · ${a.width}×${a.height}` : ""}
                            {a.created_at ? ` · ${a.created_at.slice(0, 10)}` : ""}
                          </em>
                        </span>
                      </button>
                    ))}
                  </>
                )}
                {ctxPromptsShown.length > 0 && (
                  <>
                    <div className="agent-ctx-title">
                      <Sparkles size={12} /> 提示词模板
                    </div>
                    {ctxPromptsShown.map((p) => (
                      <button
                        key={`prompt-${p.id}`}
                        type="button"
                        className="agent-slash-item"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          insertContext(`@模板#${p.id}「${p.title}」`);
                        }}
                      >
                        <span className="agent-slash-icon">
                          <Sparkles size={14} />
                        </span>
                        <span className="agent-slash-main">
                          <b>{p.title}</b>
                          <em>{p.description || p.modality}</em>
                        </span>
                      </button>
                    ))}
                  </>
                )}
                {!ctxLoading && ctxAssetsShown.length === 0 && ctxPromptsShown.length === 0 && (
                  <div className="agent-ctx-empty">没有匹配的上下文</div>
                )}
                <div className="agent-slash-foot">Esc 关闭</div>
              </div>
            )}

            <div className="agent-composer-box">
              {files.length > 0 && (
                <div className="agent-files">
                  {files.map((f) => (
                    <span className="agent-file-chip" key={f.id} title={`${f.content_type} · ${formatSize(f.size)}`}>
                      {f.kind === "image" ? <ImageIcon size={13} /> : <FileText size={13} />}
                      <b>{f.original_name}</b>
                      <button
                        type="button"
                        title="移除"
                        onClick={() => setFiles((prev) => prev.filter((x) => x.id !== f.id))}
                      >
                        <X size={12} />
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <textarea
                ref={inputRef}
                className="agent-input"
                rows={2}
                value={input}
                placeholder="让它去做什么？输入 @ 添加上下文，/ 使用命令和技能"
                onChange={(e) => {
                  setInput(e.target.value);
                  setSlashDismissed(false);
                  setSlashIndex(0);
                  setCtxDismissed(false);
                  setCtxOpen(false);
                }}
                onKeyDown={(e) => {
                  if (slashItems.length > 0) {
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setSlashIndex((v) => (v + 1) % slashItems.length);
                      return;
                    }
                    if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setSlashIndex((v) => (v - 1 + slashItems.length) % slashItems.length);
                      return;
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setSlashDismissed(true);
                      return;
                    }
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      selectSlash(slashItems[slashActive].key);
                      return;
                    }
                  }
                  if (e.key === "Escape" && ctxVisible) {
                    e.preventDefault();
                    setCtxDismissed(true);
                    setCtxOpen(false);
                    return;
                  }
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
                disabled={busy || goalRunning}
              />
              <div className="agent-composer-bar">
                <button
                  type="button"
                  className="icon-btn agent-plus"
                  onClick={() => {
                    setCtxDismissed(false);
                    setCtxOpen((v) => !v);
                  }}
                  title="添加上下文（资产 / 提示词模板）"
                  aria-label="添加上下文"
                >
                  <Plus size={16} />
                </button>

                {/* 本地文件：点这里选，或直接把文件拖进输入框 */}
                <button
                  type="button"
                  className="icon-btn agent-plus"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading || busy || goalRunning}
                  title="上传本地文件（图片 / 文本 / PDF / 音视频），也可以直接拖进来"
                  aria-label="上传本地文件"
                >
                  {uploading ? <Loader2 size={16} className="spin-ic" /> : <Paperclip size={16} />}
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  hidden
                  onChange={(e) => {
                    if (e.target.files) void uploadFiles(e.target.files);
                    e.target.value = "";
                  }}
                />

                <div className="agent-approval-menu">
                  <button
                    type="button"
                    className={`agent-chip ${autoApprove ? "risk" : ""}`}
                    onClick={() => setApprovalOpen((v) => !v)}
                    title={
                      autoApprove
                        ? "自动审批：跑整图 / 跑节点不再停下等确认，直接执行并记账"
                        : "手动审批：跑整图 / 跑节点前停下来，等你看过代价再点确认"
                    }
                  >
                    <ShieldCheck size={13} />
                    {autoApprove ? "自动审批" : "手动审批"}
                    <ChevronDown size={12} />
                  </button>
                  {approvalOpen && (
                    <>
                      <div className="agent-pop-backdrop" onClick={() => setApprovalOpen(false)} />
                      <div className="agent-pop">
                        <button
                          type="button"
                          className={!autoApprove ? "on" : ""}
                          onClick={() => {
                            void setApproval(false);
                            setApprovalOpen(false);
                          }}
                        >
                          手动审批
                          <em>花钱前停下来，等你看过代价再点</em>
                        </button>
                        <button
                          type="button"
                          className={autoApprove ? "on risk" : ""}
                          onClick={() => {
                            void setApproval(true);
                            setApprovalOpen(false);
                          }}
                        >
                          自动审批
                          <em>直接执行，代价仍记在时间线里</em>
                        </button>
                      </div>
                    </>
                  )}
                </div>

                <button
                  type="button"
                  className={`agent-chip ${thinking && thinkingCapable ? "on" : ""}`}
                  disabled={!thinkingCapable}
                  onClick={() => void toggleThinking(!thinking)}
                  title={
                    !thinkingCapable
                      ? "当前模型不支持思考模式（目前只对 DeepSeek 的 OpenAI 兼容端点生效）"
                      : thinking
                        ? "思考模式：先输出思维链再回答，规划更稳，但更慢、更费 token"
                        : "直接回答，不先生成思维链"
                  }
                >
                  <Brain size={13} />
                  {thinkingCapable ? `思考${thinking ? "开" : "关"}` : "思考不可用"}
                </button>

                <div className="agent-composer-model">
                  <ModelSelect
                    models={meta?.models || []}
                    value={session?.modelKey || meta?.models?.[0]?.key || ""}
                    onChange={(key) => void setSessionModel(key)}
                    placeholder="选一个文本模型"
                  />
                </div>

                <button
                  type="button"
                  className="agent-send"
                  onClick={() => void send()}
                  disabled={busy || goalRunning || (!input.trim() && files.length === 0)}
                  title="发送（Enter）"
                  aria-label="发送"
                >
                  {busy ? <Spinner light /> : <ArrowUp size={16} />}
                </button>
              </div>
            </div>
            {dragging && (
              <div className="agent-drop-hint">
                松开即可添加文件（图片 / 文本 / PDF / 音视频）
              </div>
            )}
          </div>

          <div className="agent-toolbar">
            <button className="btn btn-ghost btn-xs" onClick={() => setShowTools((v) => !v)}>
              <Wrench size={13} /> 它能做什么（{groups.read.length} 只读 / {groups.write.length} 可撤销 /{" "}
              {groups.irreversible.length} 要确认）
              <ChevronRight size={13} className={showTools ? "agent-chevron open" : "agent-chevron"} />
            </button>
          </div>
          {showTools && (
            <div className="agent-tools">
              {(meta?.skills || []).length > 0 && (
                <div className="agent-tools-group">
                  <div className="agent-tools-title">技能（按需加载，{meta?.skills.length}）</div>
                  {(meta?.skills || []).map((s) => (
                    <div key={s.key} className="agent-tool-row">
                      <button
                        type="button"
                        className="btn btn-ghost btn-xs"
                        aria-label={`使用${s.name}技能`}
                        disabled={busy || goalRunning}
                        onClick={() => {
                          const instruction = `请先加载并使用 ${s.key} 技能。`;
                          setInput((draft) => {
                            const text = draft.trimEnd();
                            if (!text) return `${instruction}任务：`;
                            if (SLASH_ITEMS.some(({ key }) => text === `/${key}`)) {
                              return `${text} ${instruction}任务：`;
                            }
                            return `${text}\n\n${instruction}`;
                          });
                          inputRef.current?.focus();
                        }}
                      >
                        使用
                      </button>
                      <span>{s.name}</span>
                      <span className="agent-tool-desc">{s.description}</span>
                    </div>
                  ))}
                </div>
              )}
              {(["read", "write", "irreversible"] as const).map((tier) => (
                <div key={tier} className="agent-tools-group">
                  <div className="agent-tools-title">
                    {meta?.tiers?.[tier] || tier}（{groups[tier].length}）
                  </div>
                  {groups[tier].map((t) => (
                    <div key={t.name} className="agent-tool-row">
                      <code>{t.name}</code>
                      <span>{t.label}</span>
                      <span className="agent-tool-desc">{t.description}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

/**
 * 运行中的活动条：**吸在时间线底部**，滚动到哪儿都看得见。
 *
 * 之前它只是流里的最后一行，长内容一多就被挤出视口，看起来像卡住了；
 * 秒数在走也能证明它没停。
 */
function BusyRow({
  steps,
  mode,
  onStop,
}: {
  steps: number;
  mode: string;
  onStop?: () => void;
}) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setSeconds((v) => v + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const goal = mode === "goal";
  return (
    <div className="agent-working" role="status" aria-live="polite">
      <Loader2 size={14} className="spin-ic" />
      {mode && <span className="agent-working-mode">/{mode}</span>}
      <span className="agent-working-title">
        {goal ? "持续监测中 · 目标完成前不会停" : "它正在想 / 正在调用工具…"}
      </span>
      <span className="agent-working-elapsed">{seconds}s</span>
      {steps > 0 && <span className="agent-working-note">已产生 {steps} 步</span>}
      {onStop && (
        <button className="btn btn-ghost btn-xs agent-working-stop" onClick={onStop}>
          停止
        </button>
      )}
    </div>
  );
}

/** 一步：用户 / 模型 / 工具。工具那一步默认收成一行，点开看参数与结果原文。 */
function StepRow({
  step,
  schemas,
  liveCanvasId,
  snap,
  polling,
  onOpenCanvas,
}: {
  step: AgentStepInfo;
  schemas: Record<string, CanvasNodeSchema>;
  liveCanvasId: number | null;
  snap: CanvasSnapshot | null;
  polling: boolean;
  onOpenCanvas: (id: number) => void;
}) {
  if (step.role === "user") {
    return (
      <div className="agent-row agent-row-user">
        <div className="agent-bubble agent-bubble-user">{step.content}</div>
      </div>
    );
  }
  if (step.role === "assistant") {
    const reasoning = (step.reasoning || "").trim();
    const content = step.content.trim();
    // 模型有时会照抄历史里的「（调用工具 X）」当正文；工具行已经写清楚了，不再重复一个气泡
    const onlyCallNote = /^（调用工具 .+）$/.test(content);
    // 思考模式可能只有思维链、没有正文（模型想完直接调工具），这一步也要留在时间线上
    if (!content && !reasoning) return null;
    if (onlyCallNote && !reasoning) return null;
    return (
      <div className="agent-row">
        <div className="agent-avatar">
          <Bot size={14} />
        </div>
        <div className="agent-assistant-block">
          {reasoning && (
            <details className="agent-think">
              <summary>
                <Brain size={12} /> 思考过程 · {reasoning.length} 字
              </summary>
              <pre>{reasoning}</pre>
            </details>
          )}
          {content && !onlyCallNote && <div className="agent-bubble">{content}</div>}
        </div>
      </div>
    );
  }
  return (
    <ToolRow
      step={step}
      schemas={schemas}
      liveCanvasId={liveCanvasId}
      snap={snap}
      polling={polling}
      onOpenCanvas={onOpenCanvas}
    />
  );
}

const STATUS_LABEL: Record<string, string> = {
  ok: "成功",
  error: "失败",
  pending: "等待确认",
  rejected: "已拒绝",
  skipped: "已跳过",
};

function ToolRow({
  step,
  schemas,
  liveCanvasId,
  snap,
  polling,
  onOpenCanvas,
}: {
  step: AgentStepInfo;
  schemas: Record<string, CanvasNodeSchema>;
  liveCanvasId: number | null;
  snap: CanvasSnapshot | null;
  polling: boolean;
  onOpenCanvas: (id: number) => void;
}) {
  const result = (step.toolResult || {}) as Record<string, unknown>;
  const hint = typeof result.hint === "string" ? result.hint : "";
  const error = typeof result.error === "string" ? result.error : "";
  const autoApproved = Boolean(result.autoApproved);
  const asset = asRecord(result.data);
  const rendersVideo = step.toolName === "render_motion_video" || step.toolName === "render_showcase_video";
  const videoUrl = rendersVideo && step.status === "ok"
    && typeof asset.url === "string" && asset.url.startsWith("/media/") ? asset.url : null;
  return (
    <div className="agent-tool-block">
      <details className={`agent-tool ${step.status === "error" ? "bad" : ""}`}>
        <summary>
          <span className={`agent-tool-dot ${step.status}`} />
          <code>{step.toolName}</code>
          <span className="agent-tool-tier">{step.tierLabel}</span>
          {autoApproved && (
            <span className="agent-tool-auto" title="自动审批：这一步没有停下来等你点">自动审批</span>
          )}
          <span className="agent-tool-status">{STATUS_LABEL[step.status] || step.status}</span>
          {step.durationMs > 0 && <span className="agent-tool-ms">{step.durationMs} ms</span>}
          {hint && <span className="agent-tool-hint">{hint}</span>}
          {error && <span className="agent-tool-error">{error}</span>}
        </summary>
        <div className="agent-tool-body">
          <div className="agent-tool-label">参数</div>
          <pre>{pretty(step.toolArgs)}</pre>
          <div className="agent-tool-label">结果</div>
          <pre>{pretty(step.toolResult)}</pre>
        </div>
      </details>
      {videoUrl && (
        <div className="agent-rendered-video">
          <video src={videoUrl} controls preload="metadata" aria-label={String(asset.name || "程序化视频")} />
          <a href={videoUrl} download>{String(asset.name || "程序化视频")} · 下载 MP4</a>
        </div>
      )}
      <CanvasActivityCard
        step={step}
        schemas={schemas}
        liveCanvasId={liveCanvasId}
        snap={snap}
        polling={polling}
        onOpenCanvas={onOpenCanvas}
      />
    </div>
  );
}

/** draft_canvas 的结果摘要：节点顺序 + 每个节点要它做什么，比 JSON 好读得多 */
function DraftSummary({ result }: { result: Record<string, unknown> }) {
  const data = (result.data || {}) as Record<string, unknown>;
  const draft = (data.draft || {}) as Record<string, unknown>;
  const nodes = (data.nodes || []) as { id: string; label: string; prompt: string }[];
  if (!Array.isArray(nodes) || nodes.length === 0) return null;
  const warnings = (draft.warnings || []) as string[];
  return (
    <div className="agent-draft">
      <div className="agent-draft-head">
        <b>{nodes.length} 个节点</b>
        <span>·</span>
        <span>{Number(data.edge_count || 0)} 条连线</span>
        {draft.summary ? <span className="agent-draft-summary">{String(draft.summary)}</span> : null}
      </div>
      <ol className="agent-draft-nodes">
        {nodes.map((n) => (
          <li key={n.id}>
            <span>{n.label}</span>
            <em>{n.prompt || "（未写要求）"}</em>
          </li>
        ))}
      </ol>
      {warnings.length > 0 && (
        <ul className="agent-lines warn">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ============================================================ 画布活动

type CanvasSnapshot = {
  nodes: CanvasDoc["nodes"];
  edges: CanvasDoc["edges"];
  status: Record<string, CanvasNodeStatus>;
};

const RUNNING_STATUS = new Set(["pending", "processing"]);

/** 会动到画布的工具：它们的结果值得在时间线上画成一张卡，而不是一坨 JSON。 */
const CANVAS_TOOLS = new Set([
  "get_canvas",
  "summarize_canvas",
  "preview_canvas",
  "canvas_status",
  "lint_canvas",
  "draft_canvas",
  "apply_canvas_draft",
  "update_node",
  "connect_nodes",
  "add_storyboard_image",
  "run_canvas",
  "run_node",
]);

const CANVAS_ACTION: Record<string, string> = {
  get_canvas: "读画布",
  summarize_canvas: "画布摘要",
  preview_canvas: "跑前预检",
  canvas_status: "看进度",
  lint_canvas: "分镜体检",
  draft_canvas: "草稿",
  apply_canvas_draft: "落定到画布",
  update_node: "改节点",
  connect_nodes: "连线",
  add_storyboard_image: "加分镜图",
  run_canvas: "跑整图",
  run_node: "跑单节点",
};

/**
 * 画布状态快照：只拉一次，有任务在跑就每 2 秒续一次，全部落定就停。
 *
 * 为什么不在每张卡片里各拉一份：一次对话可能连着读/改十几次画布，
 * 十张卡各开一个轮询就是二十个请求在后台转。这里由页面统一看一张画布，
 * 卡片只读数。
 *
 * `watchKey` 用步骤数：Agent 每落一步就会重新看一眼。跑整图的工具步一出现，
 * 这里就能接上「任务已经在跑」并开始自动刷新，不需要用户手动点。
 */
function useCanvasSnapshot(canvasId: number | null, watchKey: number) {
  const [snap, setSnap] = useState<(CanvasSnapshot & { canvasId: number }) | null>(null);
  const [live, setLive] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!canvasId) return;
    let alive = true;
    let timer: number | undefined;
    let polls = 0;

    const poll = async () => {
      polls += 1;
      let running = false;
      try {
        const [doc, status] = await Promise.all([
          api.getCanvas(canvasId),
          api.canvasStatus(canvasId),
        ]);
        if (!alive) return;
        const statusMap = status.nodes || {};
        setSnap({
          canvasId,
          nodes: doc.nodes || [],
          edges: doc.edges || [],
          status: statusMap,
        });
        running = Object.values(statusMap).some((n) => RUNNING_STATUS.has(String(n.status)));
      } catch {
        /* 画布被删 / 网络断了：保留上一帧，不把错误糊到对话里 */
      }
      if (!alive) return;
      if (running && polls < 900) {
        setLive(true);
        timer = window.setTimeout(poll, 2000);
      } else {
        setLive(false);
      }
    };

    void poll();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [canvasId, tick, watchKey]);

  const matched = Boolean(snap && snap.canvasId === canvasId);
  return {
    snap: matched ? snap : null,
    live: matched && live,
    refresh: () => setTick((v) => v + 1),
  };
}

function CanvasMonitor({
  canvasId,
  name,
  snap,
  live,
  onRefresh,
  onOpen,
}: {
  canvasId: number;
  name: string;
  snap: CanvasSnapshot | null;
  live: boolean;
  onRefresh: () => void;
  onOpen: () => void;
}) {
  const statuses = snap ? Object.values(snap.status) : [];
  const running = statuses.filter((n) => RUNNING_STATUS.has(String(n.status))).length;
  const done = statuses.filter((n) => n.status === "completed").length;
  const failed = statuses.filter((n) => n.status === "failed").length;
  return (
    <div className="agent-canvas-monitor">
      <div className="agent-canvas-monitor-main">
        <Workflow size={14} className="agent-canvas-monitor-icon" />
        <b>{name}</b>
        <span className="agent-canvas-id">#{canvasId}</span>
        <span className="agent-canvas-monitor-meta">
          {snap ? `${snap.nodes.length} 节点 · ${snap.edges.length} 连线` : "读取画布…"}
        </span>
        {snap &&
          (running > 0 ? (
            <span className="agent-canvas-live">
              <i /> 运行中 {running}
            </span>
          ) : (
            <span className="agent-canvas-idle">空闲</span>
          ))}
        {snap && (done > 0 || failed > 0) && (
          <span className="agent-canvas-monitor-meta">
            完成 {done}
            {failed > 0 ? ` · 失败 ${failed}` : ""}
          </span>
        )}
        {live && <span className="agent-canvas-poll">每 2 秒自动刷新</span>}
      </div>
      <div className="agent-canvas-monitor-actions">
        <button className="icon-btn" onClick={onRefresh} title="刷新画布状态">
          <RefreshCw size={13} />
        </button>
        <button className="btn btn-ghost btn-xs" onClick={onOpen}>
          <FolderOpen size={13} /> 打开
        </button>
      </div>
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/**
 * 工具步骤下面那张「它在画布上做了什么」的卡。
 *
 * 只画成功的调用：失败的步骤在标题行已经写明原因，再画一张空卡只会添乱。
 * 画布上的节点状态优先取实时快照（同一张画布时），否则回落到这一步返回的当时状态。
 */
function CanvasActivityCard({
  step,
  schemas,
  liveCanvasId,
  snap,
  polling,
  onOpenCanvas,
}: {
  step: AgentStepInfo;
  schemas: Record<string, CanvasNodeSchema>;
  liveCanvasId: number | null;
  snap: CanvasSnapshot | null;
  polling: boolean;
  onOpenCanvas: (id: number) => void;
}) {
  if (!CANVAS_TOOLS.has(step.toolName) || step.status !== "ok") return null;

  const result = asRecord(step.toolResult);
  const data = asRecord(result.data);
  const preview = asRecord(result.preview);
  const args = asRecord(step.toolArgs);
  const rawId = data.project_id ?? preview.project_id ?? args.project_id;
  const idNum = Number(rawId);
  const canvasId = Number.isFinite(idNum) && idNum > 0 ? idNum : null;
  const labelOf = (kind: string) => schemas[kind]?.label ?? kind;
  const canvasName = String(data.name || preview.project_name || "") || "画布";

  const tool = step.toolName;
  let body: ReactNode = null;

  if (tool === "draft_canvas") {
    body = <DraftSummary result={result} />;
  } else if (tool === "get_canvas") {
    const nodes = (data.nodes || []) as { id: string; label: string; prompt: string }[];
    const edges = (data.edges || []) as unknown[];
    body = (
      <div className="agent-canvas-body">
        <div className="agent-canvas-stats">
          <span>
            <b>{nodes.length}</b> 节点
          </span>
          <span>
            <b>{edges.length}</b> 连线
          </span>
        </div>
        <div className="agent-canvas-chips">
          {nodes.slice(0, 14).map((n) => (
            <span key={n.id} className="agent-canvas-chip" title={n.prompt || n.id}>
              {n.label || n.id}
            </span>
          ))}
          {nodes.length > 14 && <span className="agent-canvas-chip muted">+{nodes.length - 14}</span>}
        </div>
      </div>
    );
  } else if (tool === "summarize_canvas") {
    body = <pre className="agent-canvas-text">{String(data.summary || "")}</pre>;
  } else if (tool === "preview_canvas") {
    const totals = asRecord(data.totals);
    const nodes = (data.nodes || []) as {
      id: string;
      label: string;
      status: string;
      willRun?: boolean;
      reason?: string;
    }[];
    body = (
      <div className="agent-canvas-body">
        <div className="agent-canvas-stats">
          <span>
            <b>{num(totals.calls)}</b> 次调用
          </span>
          <span>
            <b>{num(totals.pendingNodes)}</b> 个待执行
          </span>
          {num(totals.rerunNodes) > 0 && (
            <span>
              <b>{num(totals.rerunNodes)}</b> 个会重做
            </span>
          )}
          {num(totals.blockedNodes) > 0 && (
            <span className="warn">
              <b>{num(totals.blockedNodes)}</b> 个会被跳过
            </span>
          )}
        </div>
        {nodes.length > 0 && (
          <div className="agent-canvas-chips">
            {nodes.slice(0, 12).map((n) => (
              <span
                key={n.id}
                className={`agent-canvas-chip ${n.status === "blocked" || n.reason ? "bad" : ""} ${
                  n.willRun === false ? "muted" : ""
                }`}
                title={n.reason || (n.willRun === false ? "不会执行" : "")}
              >
                {n.label || n.id}
              </span>
            ))}
          </div>
        )}
      </div>
    );
  } else if (tool === "apply_canvas_draft") {
    body = (
      <div className="agent-canvas-stats">
        <span>
          共 <b>{num(data.node_count)}</b> 个节点
        </span>
        <span>
          <b>{num(data.edge_count)}</b> 条连线
        </span>
        <span>
          本次新增 <b>{num(data.added)}</b> 个
        </span>
      </div>
    );
  } else if (tool === "update_node") {
    const ignored = (data.ignored_params || []) as string[];
    body = (
      <div className="agent-canvas-stats">
        <span>
          节点 <code>{String(data.node_id || "")}</code> 已更新
        </span>
        {ignored.length > 0 && <span className="warn">忽略参数：{ignored.join("、")}</span>}
      </div>
    );
  } else if (tool === "connect_nodes") {
    body = (
      <div className="agent-canvas-stats">
        <span>
          <code>{String(data.source || "?")}</code> → <code>{String(data.target || "?")}</code>
        </span>
        {data.duplicated ? <span className="muted">这条线原本就有</span> : null}
      </div>
    );
  } else if (tool === "add_storyboard_image") {
    body = (
      <div className="agent-canvas-stats">
        <span>
          已接在上游 <code>{String(data.source || "?")}</code> 后面
        </span>
        <span>
          新节点 <code>{String(data.node_id || "")}</code>
        </span>
      </div>
    );
  } else if (tool === "lint_canvas") {
    const issues = (data.issues || []) as { level: string; message: string }[];
    body = (
      <div className="agent-canvas-body">
        <div className="agent-canvas-stats">
          <span>
            <b>{num(data.count)}</b> 条体检结论
          </span>
        </div>
        {issues.length > 0 && (
          <ul className="agent-lines warn">
            {issues.slice(0, 3).map((i, idx) => (
              <li key={idx}>{i.message}</li>
            ))}
          </ul>
        )}
      </div>
    );
  } else if (tool === "run_canvas" || tool === "run_node" || tool === "canvas_status") {
    const live = Boolean(snap && canvasId && canvasId === liveCanvasId);
    const focusNode = tool === "run_node" ? String(data.node_id || args.node_id || "") : "";
    const liveRows = live && snap
      ? snap.nodes
          .filter((n) => !focusNode || n.id === focusNode)
          .map((n) => {
            const st = snap.status[n.id];
            return {
              id: n.id,
              label: labelOf(n.type),
              status: st?.status || "idle",
              progress: st?.progress,
              error: st?.error || "",
            };
          })
      : null;
    const staticRows = !live
      ? ((data.nodes || []) as {
          node_id: string;
          label: string;
          status: string;
          progress?: number;
          error?: string;
        }[]).map((n) => ({
          id: n.node_id,
          label: n.label,
          status: n.status,
          progress: n.progress,
          error: n.error || "",
        }))
      : [];
    const rows = liveRows ?? staticRows;
    const runningCount = rows.filter((r) => RUNNING_STATUS.has(String(r.status))).length;
    body = (
      <div className="agent-canvas-body">
        {tool === "run_canvas" && (
          <div className="agent-canvas-stats">
            <span>
              画布上 <b>{rows.length}</b> 个节点
            </span>
            {live && runningCount > 0 && (
              <span className="agent-canvas-live">
                <i /> 运行中 {runningCount}
              </span>
            )}
            {polling && <span className="agent-canvas-poll">每 2 秒自动刷新</span>}
          </div>
        )}
        {tool === "run_node" && (
          <div className="agent-canvas-stats">
            <span>
              节点 <code>{focusNode || "?"}</code> · {num(data.task_count) || 1} 个任务
            </span>
          </div>
        )}
        {rows.length > 0 ? (
          <div className="agent-canvas-status-list">
            {rows.slice(0, 12).map((n) => (
              <div key={n.id} className="agent-canvas-status-row">
                <span className={`agent-canvas-status-dot ${n.status}`} />
                <span className="agent-canvas-status-name">{n.label}</span>
                <span className="agent-canvas-status-text">
                  {n.status === "processing"
                    ? `运行中 ${num(n.progress)}%`
                    : n.status === "completed"
                      ? "完成"
                      : n.status === "failed"
                        ? n.error || "失败"
                        : n.status === "idle"
                          ? "尚未运行"
                          : n.status}
                </span>
              </div>
            ))}
            {rows.length > 12 && (
              <div className="agent-canvas-status-more">还有 {rows.length - 12} 个节点，去画布看</div>
            )}
          </div>
        ) : (
          <div className="agent-canvas-stats muted">
            {tool === "canvas_status" ? "这个画布还没有跑过任务" : "任务已派出，稍等几秒出现进度"}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="agent-canvas-card">
      <div className="agent-canvas-head">
        <span className="agent-canvas-action">{CANVAS_ACTION[tool] || tool}</span>
        <b>{canvasName}</b>
        {canvasId && <span className="agent-canvas-id">#{canvasId}</span>}
        {canvasId && (
          <button className="btn btn-ghost btn-xs agent-canvas-open" onClick={() => onOpenCanvas(canvasId)}>
            <FolderOpen size={12} /> 打开画布
          </button>
        )}
      </div>
      {body}
    </div>
  );
}

function ConfirmPreview({ result }: { result: unknown }) {
  const preview = ((result as Record<string, unknown>)?.preview || {}) as Record<string, unknown>;
  if (preview.error) {
    return <div className="agent-confirm-error">{String(preview.error)}</div>;
  }
  const calls = Number(preview.calls || 0);
  const pendingNodes = Number(preview.pending || 0);
  const redo = Number(preview.redo || 0);
  const blocked = Number(preview.blocked || 0);
  const dialogue = Number(preview.dialogue || 0);
  return (
    <div className="agent-confirm-grid">
      <div>
        <b>{calls}</b>
        <span>次调用（至少）</span>
      </div>
      <div>
        <b>{pendingNodes}</b>
        <span>个节点待执行</span>
      </div>
      <div>
        <b>{redo}</b>
        <span>个节点已有产物、会被重做</span>
      </div>
      <div>
        <b>{blocked}</b>
        <span>个节点缺输入、会被跳过</span>
      </div>
      {dialogue > 0 && (
        <div>
          <b>{dialogue}</b>
          <span>次逐镜对白语音（另算）</span>
        </div>
      )}
      <div>
        <b>{String(preview.project_name || `#${preview.project_id || ""}`)}</b>
        <span>画布</span>
      </div>
    </div>
  );
}

function pretty(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
