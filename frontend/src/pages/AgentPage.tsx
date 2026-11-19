import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronRight,
  FolderOpen,
  FolderPlus,
  Loader2,
  Plus,
  Send,
  Trash2,
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
  Project,
} from "../types";
import { Empty, Spinner, formatTime, ModelSelect } from "../components/common";
import { useToast } from "../components/Toast";

/**
 * Agent 工作台（2.0）。
 *
 * 三层：**项目 → 任务（会话）→ 步骤**。
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
}: {
  onNavigate?: (route: string) => void;
  onOpenCanvas?: (p: { id: number; name: string }) => void;
  /** 从侧栏 / 首页指定要打开的任务；打开一次后由 App 清掉 */
  openSessionId?: number | null;
  onSessionOpened?: () => void;
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
  const [showTools, setShowTools] = useState(false);
  const [autoApprove, setAutoApprove] = useState(false);
  const [newProjectName, setNewProjectName] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  // 带「指定任务」进来时，默认选择（最近一条）不许覆盖它
  const explicitOpenRef = useRef(openSessionId != null);

  const applyPayload = (payload: AgentPayload) => {
    setSession(payload.session);
    setAgentProject(payload.agentProject);
    setSteps(payload.steps);
    setPending(payload.pending);
    setProjectId(payload.session.agentProjectId);
    setAutoApprove(Boolean(payload.session.autoApprove));
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

  const sessions = meta?.sessions || [];

  const createProject = async () => {
    const name = (newProjectName || "").trim();
    if (!name) {
      setNewProjectName(null);
      return;
    }
    try {
      const p = await api.createAgentProject({ name });
      setNewProjectName(null);
      setProjectId(p.id);
      setSession(null);
      setAgentProject(p);
      setSteps([]);
      setPending(null);
      await refreshMeta();
    } catch (e) {
      toast.error(`新建项目失败：${(e as Error).message}`);
    }
  };

  const removeProject = async (p: AgentProjectInfo) => {
    if (!window.confirm(`删除项目「${p.name}」？里面的 ${p.sessions} 个任务会退回「未归档」，不会被删。`)) {
      return;
    }
    try {
      const r = await api.deleteAgentProject(p.id);
      toast.success(`项目已删除${r.unfiled ? `，${r.unfiled} 个任务退回未归档` : ""}`);
      if (session?.agentProjectId === p.id) {
        setSession(null);
        setSteps([]);
        setPending(null);
      }
      if (projectId === p.id) setProjectId(null);
      await refreshMeta();
    } catch (e) {
      toast.error(`删除项目失败：${(e as Error).message}`);
    }
  };

  const openSession = async (s: AgentSessionInfo) => {
    if (busy) return;
    try {
      applyPayload(await api.getAgentSession(s.id));
    } catch (e) {
      toast.error(`打开任务失败：${(e as Error).message}`);
    }
  };

  const newSession = async () => {
    try {
      const payload = await api.createAgentSession({
        model_key: session?.modelKey || meta?.models[0]?.key || "",
        agent_project_id: projectId,
        auto_approve: autoApprove,
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

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      let sid = session?.id;
      if (!sid) {
        const created = await api.createAgentSession({
          model_key: meta?.models[0]?.key || "",
          agent_project_id: projectId,
          auto_approve: autoApprove,
        });
        applyPayload(created);
        sid = created.session.id;
        await refreshMeta();
      }
      const payload = await api.agentSend(sid, text);
      setInput("");
      applyPayload(payload);
      if (payload.run?.status === "need_confirm") {
        toast.success("有一个不可逆的操作，等你确认");
      }
      await refreshMeta();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
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

  const visibleSessions = useMemo(() => {
    if (projectId === null) return sessions.filter((s) => s.agentProjectId === null);
    return sessions.filter((s) => s.agentProjectId === projectId);
  }, [sessions, projectId]);

  const unarchived = useMemo(() => sessions.filter((s) => s.agentProjectId === null), [sessions]);

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
          <div className="page-desc">
            按项目分上下文：一个项目挂一张画布、下面开若干任务。花钱的操作它会先停下来问你。
          </div>
        </div>
        <div className="row agent-head-row">
          <div className="agent-model">
            <ModelSelect
              models={meta?.models || []}
              value={session?.modelKey || meta?.models?.[0]?.key || ""}
              onChange={(key) => void setSessionModel(key)}
              placeholder="选一个文本模型"
            />
          </div>
          <div
            className="agent-approval"
            title={
              autoApprove
                ? "自动审批：跑整图 / 跑节点不再停下等确认，直接执行并记账"
                : "手动审批：跑整图 / 跑节点前停下来，等你看过代价再点确认"
            }
          >
            <button
              type="button"
              className={autoApprove ? "" : "on"}
              onClick={() => void setApproval(false)}
            >
              手动审批
            </button>
            <button
              type="button"
              className={autoApprove ? "on risk" : ""}
              onClick={() => void setApproval(true)}
            >
              自动审批
            </button>
          </div>
          {agentProject && (
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
        <aside className="card card-pad agent-side">
          <div className="row-between">
            <div className="card-title-sm">项目</div>
            <button
              className="btn btn-ghost btn-xs"
              onClick={() => setNewProjectName(newProjectName === null ? "" : null)}
              disabled={busy}
            >
              <FolderPlus size={13} /> 新建
            </button>
          </div>
          {newProjectName !== null && (
            <div className="agent-new-project">
              <input
                className="input"
                autoFocus
                placeholder="项目名，如「短剧 A · 前期」"
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void createProject();
                  if (e.key === "Escape") setNewProjectName(null);
                }}
              />
              <button className="btn btn-primary btn-xs" onClick={() => void createProject()}>
                建
              </button>
            </div>
          )}

          <div className="agent-project-list">
            {(meta?.projects || []).map((p) => (
              <div key={p.id}>
                <div className={`agent-project ${projectId === p.id ? "on" : ""}`}>
                  <button
                    className="agent-project-main"
                    onClick={() => {
                      setProjectId(p.id);
                      const first = sessions.find((s) => s.agentProjectId === p.id);
                      if (first) void openSession(first);
                      else {
                        setSession(null);
                        setAgentProject(p);
                        setSteps([]);
                        setPending(null);
                      }
                    }}
                    title={p.description || p.name}
                  >
                    <span className="agent-project-name">{p.name}</span>
                    <span className="agent-project-count">{p.sessions} 个任务</span>
                  </button>
                  <button
                    className="icon-btn"
                    onClick={() => void removeProject(p)}
                    aria-label="删除项目"
                    title="删除项目（任务退回未归档）"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
                {projectId === p.id && (
                  <div className="agent-session-list sub">
                    <button className="btn btn-ghost btn-xs agent-new-task" onClick={() => void newSession()}>
                      <Plus size={12} /> 新任务
                    </button>
                    {visibleSessions.map((s) => (
                      <div key={s.id} className={`agent-session ${session?.id === s.id ? "on" : ""}`}>
                        <button className="agent-session-main" onClick={() => void openSession(s)} title={s.title}>
                          <span className="agent-session-title">{s.title || "新任务"}</span>
                          <span className="agent-session-time">{formatTime(s.updatedAt)}</span>
                        </button>
                        <button
                          className="icon-btn agent-session-del"
                          onClick={() => void removeSession(s)}
                          aria-label="删除任务"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                    {visibleSessions.length === 0 && (
                      <div className="agent-session-empty">这个项目还没有任务，点「新任务」开一个。</div>
                    )}
                  </div>
                )}
              </div>
            ))}
            {(meta?.projects || []).length === 0 && (
              <div className="agent-session-empty">
                还没有项目。先建一个——同一件事的多次任务放一起，不同的事互不串上下文。
              </div>
            )}
          </div>

          {unarchived.length > 0 && (
            <div className="agent-unfiled">
              <button
                className={`btn btn-ghost btn-xs ${projectId === null ? "on" : ""}`}
                onClick={() => {
                  setProjectId(null);
                  const first = unarchived[0];
                  if (first) void openSession(first);
                }}
              >
                未归档（{unarchived.length}）
              </button>
            </div>
          )}
        </aside>

        <section className="card agent-main">
          <div className="agent-context">
            <span>{currentProject ? currentProject.name : "未归档"}</span>
            {session && <span className="agent-context-task">· {session.title}</span>}
            {autoApprove && (
              <span className="agent-auto-note" title="跑整图 / 跑节点会直接执行，不再停下来等确认">
                自动审批
              </span>
            )}
            {canvasId && <span className="agent-context-canvas">画布 #{canvasId}</span>}
          </div>
          <div className="agent-timeline">
            {steps.length === 0 && !busy ? (
              <Empty
                icon={<Bot />}
                title={currentProject ? `在「${currentProject.name}」里说一句要做什么` : "说一句要做什么"}
                desc="例如：「看看这张画布现在的流程」「搭一条从创意到分镜图的链」「跑一次要花多少次调用？」"
              />
            ) : (
              steps.map((s) => <StepRow key={s.id} step={s} />)
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

            {busy && (
              <div className="agent-working">
                <Loader2 size={14} className="spin-ic" /> 它正在想 / 正在调用工具…
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          <div className="agent-composer">
            <textarea
              className="textarea agent-input"
              rows={2}
              value={input}
              placeholder="让它去做什么？（Enter 发送，Shift+Enter 换行）"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              disabled={busy}
            />
            <button className="btn btn-primary" onClick={() => void send()} disabled={busy || !input.trim()}>
              {busy ? <Spinner light /> : <Send size={15} />} 发送
            </button>
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
                      <code>load_skill({s.key})</code>
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

/** 一步：用户 / 模型 / 工具。工具那一步默认收成一行，点开看参数与结果原文。 */
function StepRow({ step }: { step: AgentStepInfo }) {
  if (step.role === "user") {
    return (
      <div className="agent-row agent-row-user">
        <div className="agent-bubble agent-bubble-user">{step.content}</div>
      </div>
    );
  }
  if (step.role === "assistant") {
    if (!step.content.trim()) return null;
    return (
      <div className="agent-row">
        <div className="agent-avatar">
          <Bot size={14} />
        </div>
        <div className="agent-bubble">{step.content}</div>
      </div>
    );
  }
  return <ToolRow step={step} />;
}

const STATUS_LABEL: Record<string, string> = {
  ok: "成功",
  error: "失败",
  pending: "等待确认",
  rejected: "已拒绝",
  skipped: "已跳过",
};

function ToolRow({ step }: { step: AgentStepInfo }) {
  const result = (step.toolResult || {}) as Record<string, unknown>;
  const hint = typeof result.hint === "string" ? result.hint : "";
  const error = typeof result.error === "string" ? result.error : "";
  const autoApproved = Boolean(result.autoApproved);
  return (
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
