import { useEffect, useState } from "react";
import {
  ArrowRight,
  Clapperboard,
  ImageIcon,
  ListChecks,
  MessageSquare,
  Plus,
  Send,
  Sparkles,
  Video,
} from "lucide-react";
import { api } from "../api";
import { createDemoProject } from "../demoProject";
import type { AgentWorkspaceMeta, Project, SetupStatus } from "../types";
import { formatDate, ModelSelect, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

type Nav = (r: string) => void;

const ENTRIES: { route: string; icon: React.ReactNode; title: string; desc: string }[] = [
  { route: "chat", icon: <MessageSquare />, title: "文本对话", desc: "接任意对话模型，多轮聊天与灵感碰撞" },
  { route: "image", icon: <ImageIcon />, title: "图片生成", desc: "文生图、参考图、批量模式一次出多图" },
  { route: "video", icon: <Video />, title: "视频生成", desc: "文生视频、图生视频，首帧定调更可控" },
  { route: "director", icon: <Clapperboard />, title: "导演台", desc: "本地粗剪：入出点截取、排序合并成片" },
  { route: "prompts", icon: <Sparkles />, title: "提示词库", desc: "10 个常用模板，一键套用开箱即写" },
  { route: "tasks", icon: <ListChecks />, title: "任务中心", desc: "全部任务集中跟踪，失败一键重试" },
];

/**
 * 首页 = 一个「直接开干」的输入框（对齐 dsh 的空状态）。
 *
 * 输入框提交后**交给 Agent**：按所选画布建一条任务、把这句话发出去，然后跳到 Agent 页。
 * 不再让用户在「对话 / 图片 / 视频」之间先选一次——那是功能目录，不是意图。
 */
export default function HomePage({
  onNavigate,
  onOpenCanvas,
  onOpenAgentSession,
  onGoProviders,
  brandName,
  brandSub,
}: {
  onNavigate: Nav;
  onOpenCanvas?: (p: { id: number; name: string }) => void;
  onOpenAgentSession?: (id: number) => void;
  onGoProviders?: () => void;
  brandName: string;
  brandSub: string;
}) {
  const toast = useToast();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [meta, setMeta] = useState<AgentWorkspaceMeta | null>(null);
  const [creating, setCreating] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);
  const [input, setInput] = useState("");
  const [agentProjectId, setAgentProjectId] = useState<number | null>(null);
  const [autoApprove, setAutoApprove] = useState(false);
  const [newProject, setNewProject] = useState<string | null>(null);
  const [modelKey, setModelKey] = useState("");
  const [sending, setSending] = useState(false);

  useEffect(() => {
    let alive = true;
    let timer = 0;
    api
      .listProjects()
      .then(setProjects)
      .catch(() => setProjects([]));
    // Ollama 探测是后台做的：第一次问通常只拿到「检测中」，2.5 秒后再问一次
    const loadSetup = (left: number) => {
      api
        .setupStatus()
        .then((s) => {
          if (!alive) return;
          setSetup(s);
          if (s.ollama?.error === "detecting" && left > 0) {
            timer = window.setTimeout(() => loadSetup(left - 1), 2500);
          }
        })
        .catch(() => {
          if (alive) setSetup(null);
        });
    };
    loadSetup(2);
    api
      .agentMeta()
      .then((m) => {
        setMeta(m);
        setModelKey((prev) => prev || m.models[0]?.key || "");
        setAgentProjectId((prev) => prev ?? m.projects[0]?.id ?? null);
      })
      .catch(() => setMeta(null));
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);

  const ready = Boolean(meta?.ready);

  const quickCreate = async () => {
    const name = `新作品 ${new Date().toLocaleDateString("zh-CN")}`;
    setCreating(true);
    try {
      await api.createProject(name, "");
      setProjects(await api.listProjects());
      toast.success("画布已创建，可以在下面描述要做什么");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  /** 建一张铺好链的示例画布并直接进画布 */
  const startDemo = async () => {
    setDemoBusy(true);
    try {
      const demo = await createDemoProject();
      setProjects(await api.listProjects());
      toast.success(`示例已建好（${demo.levelLabel}）：点画布上方的「运行整图」就能看到产出`);
      onOpenCanvas?.(demo);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "示例创建失败");
      if (e instanceof Error && e.message.includes("模型")) onGoProviders?.();
    } finally {
      setDemoBusy(false);
    }
  };

  /** 一句话交给 Agent：建任务 → 发送 → 跳过去继续 */
  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    if (!ready) {
      toast.error("还没有可用的文本模型，先去「模型服务」接入一个");
      onGoProviders?.();
      return;
    }
    setSending(true);
    try {
      const created = await api.createAgentSession({
        model_key: modelKey || meta?.models[0]?.key || "",
        agent_project_id: agentProjectId,
        auto_approve: autoApprove,
      });
      await api.agentSend(created.session.id, text);
      setInput("");
      onOpenAgentSession?.(created.session.id);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "发送失败");
    } finally {
      setSending(false);
    }
  };

  /** 就地在首页新建一个 Agent 项目，并把它选为当前项目 */
  const createAgentProject = async () => {
    const name = (newProject || "").trim();
    if (!name) {
      setNewProject(null);
      return;
    }
    try {
      const p = await api.createAgentProject({ name });
      setMeta((prev) => (prev ? { ...prev, projects: [p, ...prev.projects] } : prev));
      setAgentProjectId(p.id);
      setNewProject(null);
      toast.success(`项目「${p.name}」已建好`);
    } catch (e) {
      toast.error(`新建项目失败：${(e as Error).message}`);
    }
  };

  return (
    <div className="page home-page">
      <section className="home-composer-wrap">
        <div className="home-composer">
          <div className="home-composer-brand">
            <span className="home-composer-mark">{brandName.slice(0, 1) || "马"}</span>
            <span className="home-composer-name">{brandName}</span>
          </div>

          <div className="home-composer-chips">
            <label className="home-chip-select" title="这次任务归到哪个项目：不同项目的上下文互不串">
              <span className="home-chip-label">项目</span>
              <select
                className="home-chip"
                value={agentProjectId ?? ""}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "__new") {
                    setNewProject("");
                    return;
                  }
                  setAgentProjectId(v ? Number(v) : null);
                }}
              >
                <option value="">不归档</option>
                {(meta?.projects || []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
                <option value="__new">＋ 新建项目…</option>
              </select>
            </label>
            <span className="home-chip home-chip-static">Agent 模式</span>
          </div>

          {newProject !== null && (
            <div className="home-new-project">
              <input
                className="input"
                autoFocus
                placeholder="新项目名，如「短剧 A · 前期」"
                value={newProject}
                onChange={(e) => setNewProject(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void createAgentProject();
                  if (e.key === "Escape") setNewProject(null);
                }}
              />
              <button className="btn btn-primary btn-xs" onClick={() => void createAgentProject()}>
                建
              </button>
            </div>
          )}

          <div className={`home-composer-box ${sending ? "busy" : ""}`}>
            <textarea
              className="home-composer-input"
              rows={2}
              value={input}
              placeholder="说一句想做什么：搭一条从创意到分镜的链 / 把这两个节点连起来 / 跑一次要多少钱"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              disabled={sending}
            />
            <div className="home-composer-bar">
              <button
                type="button"
                className="icon-btn home-composer-new"
                onClick={() => void quickCreate()}
                disabled={creating}
                title="新建画布"
              >
                {creating ? <Spinner /> : <Plus size={16} />}
              </button>
              <div className="home-composer-model">
                <ModelSelect
                  models={meta?.models || []}
                  value={modelKey}
                  onChange={setModelKey}
                  placeholder="选一个文本模型"
                />
              </div>
              <button
                type="button"
                className="btn btn-primary home-composer-send"
                onClick={() => void send()}
                disabled={sending || !input.trim() || !ready}
                title={ready ? "发送给 Agent" : "先接入一个文本模型"}
              >
                {sending ? <Spinner light /> : <Send size={15} />}
              </button>
            </div>
          </div>

          <div className="home-composer-hint">
            <span>
              {ready
                ? "Enter 发送 · Shift+Enter 换行"
                : "还没有可用的文本模型：先去「模型服务」接入一个，Agent 才能动手。"}
            </span>
            <div
              className="agent-approval home-approval"
              title={
                autoApprove
                  ? "自动审批：跑整图 / 跑节点不再停下等确认，直接执行并记账"
                  : "手动审批：跑整图 / 跑节点前停下来，等你看过代价再点确认"
              }
            >
              <button type="button" className={autoApprove ? "" : "on"} onClick={() => setAutoApprove(false)}>
                手动审批
              </button>
              <button
                type="button"
                className={autoApprove ? "on risk" : ""}
                onClick={() => setAutoApprove(true)}
              >
                自动审批
              </button>
            </div>
            {!ready && onGoProviders && (
              <button className="btn btn-ghost btn-xs" onClick={onGoProviders}>
                去模型服务
              </button>
            )}
          </div>
        </div>
      </section>

      <section className="home-section">
        <div className="home-section-head">
          <h2>创作入口</h2>
        </div>
        <div className="home-grid">
          {ENTRIES.map((e, i) => (
            <button
              key={e.route}
              className="home-card card home-enter"
              style={{ animationDelay: `${i * 40}ms` }}
              onClick={() => onNavigate(e.route)}
            >
              <div className="home-card-icon">{e.icon}</div>
              <div className="home-card-title">{e.title}</div>
              <div className="home-card-desc">{e.desc}</div>
              <span className="home-card-go">
                进入
                <ArrowRight size={13} />
              </span>
            </button>
          ))}
        </div>
      </section>

      {projects && projects.length > 0 && (
        <section className="home-section">
          <div className="home-section-head">
            <h2>最近画布</h2>
            <button className="btn btn-ghost btn-sm" onClick={() => onNavigate("projects")}>
              全部画布
              <ArrowRight size={13} />
            </button>
          </div>
          <div className="home-projects">
            {projects.slice(0, 4).map((p, i) => (
              <button
                key={p.id}
                className="home-project card home-enter"
                style={{ animationDelay: `${i * 40}ms` }}
                onClick={() => (onOpenCanvas ? onOpenCanvas(p) : onNavigate("projects"))}
                title={onOpenCanvas ? "打开画布" : "查看全部画布"}
              >
                <div className="home-project-name">{p.name}</div>
                <div className="home-project-desc">{p.description || "暂无描述"}</div>
                <div className="home-project-foot">
                  <span className="home-project-time muted">{formatDate(p.updated_at)}</span>
                  <span className="home-project-go">
                    打开画布
                    <ArrowRight size={12} />
                  </span>
                </div>
              </button>
            ))}
          </div>
        </section>
      )}

      {projects && projects.length === 0 && (
        <section className="home-section">
          <div className="home-section-head">
            <h2>最近画布</h2>
          </div>
          <div className="home-empty card">
            <div className="home-empty-title">还没有作品</div>
            <div className="home-empty-desc">
              这里会列出你最近打开的画布。想要立刻看到成品，「从示例开始」会按你现有的模型铺好一条完整链路
              （创意 → 小说 → 剧本 → 分镜 → 出图），然后点画布上方的「运行整图」就行。
            </div>
            <div className="home-empty-actions">
              <button className="btn btn-primary btn-sm" onClick={startDemo} disabled={demoBusy}>
                {demoBusy ? <Spinner /> : <Sparkles size={14} />}
                铺一条示例
              </button>
              <button className="btn btn-ghost btn-sm" onClick={quickCreate} disabled={creating}>
                {creating ? <Spinner /> : <Plus size={14} />}
                我要自己建
              </button>
            </div>
          </div>
        </section>
      )}

      {setup && !setup.ready && (
        <div className="home-setup-hint muted">
          现在是「没接模型」状态：上面这些入口点进去也只会撞到同一个错误，建议先
          <button className="btn btn-ghost btn-xs" onClick={onGoProviders}>
            接入一个模型
          </button>
        </div>
      )}
    </div>
  );
}
