import { useEffect, useState } from "react";
import {
  ArrowRight,
  Clapperboard,
  ImageIcon,
  ListChecks,
  MessageSquare,
  Play,
  Plus,
  Sparkles,
  Video,
} from "lucide-react";
import { api } from "../api";
import { createDemoProject } from "../demoProject";
import type { Project, SetupStatus } from "../types";
import { formatDate, Spinner } from "../components/common";
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

export default function HomePage({
  onNavigate,
  onOpenCanvas,
  onGoProviders,
  brandName,
  brandSub,
}: {
  onNavigate: Nav;
  onOpenCanvas?: (p: { id: number; name: string }) => void;
  onGoProviders?: () => void;
  brandName: string;
  brandSub: string;
}) {
  const toast = useToast();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [creating, setCreating] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);

  useEffect(() => {
    api
      .listProjects()
      .then(setProjects)
      .catch(() => setProjects([]));
    // 首页要知道「有没有可用模型」，才能把最主要的那个按钮放对
    api
      .setupStatus()
      .then(setSetup)
      .catch(() => setSetup(null));
  }, []);

  const quickCreate = async () => {
    const name = `新作品 ${new Date().toLocaleDateString("zh-CN")}`;
    setCreating(true);
    try {
      await api.createProject(name, "");
      setProjects(await api.listProjects());
      toast.success("画布已创建，到「自由画布」页查看");
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

  /**
   * 首页最主要的那个动作，按「你现在处在哪一步」决定——而不是固定成某个功能的入口。
   *
   * - 已经有作品：接着上一件做（这是老用户九成情况下想干的事）
   * - 还没有作品但有模型：铺一条示例（新手最快看到成果的路径）
   * - 连模型都没有：先去接入（否则点什么都只会撞到同一个错误）
   *
   * 判断依据全部来自已有的接口（画布列表 / setup-status），不额外问用户。
   */
  const recent = projects?.[0];
  const primary = (() => {
    if (recent && onOpenCanvas) {
      return {
        label: `继续「${recent.name}」`,
        icon: <Play size={15} />,
        run: () => onOpenCanvas(recent),
        busy: false,
      };
    }
    if (setup && !setup.ready && onGoProviders) {
      return { label: "先接入一个模型", icon: <Plus size={15} />, run: onGoProviders, busy: false };
    }
    return { label: "从示例开始", icon: <Sparkles size={15} />, run: startDemo, busy: demoBusy };
  })();

  return (
    <div className="page home-page">
      <section className="home-hero card">
        <div className="home-hero-text">
          <h1>
            {brandName}
            <span className="home-hero-sub">{brandSub}</span>
          </h1>
          <p>
            填一个 Base URL 和 API Key，接入任意模型：对话、生图、生视频、本地粗剪，产物自动进本地资产库。
          </p>
          <div className="home-hero-actions">
            <button className="btn btn-primary" onClick={primary.run} disabled={primary.busy}>
              {primary.busy ? <Spinner /> : primary.icon}
              <span className="home-hero-primary-label">{primary.label}</span>
            </button>
            <button className="btn btn-ghost" onClick={() => onNavigate("image")}>
              开始创作
              <ArrowRight size={15} />
            </button>
            <button className="btn btn-ghost" onClick={quickCreate} disabled={creating}>
              {creating ? <Spinner /> : <Plus size={15} />}
              新建画布
            </button>
          </div>
        </div>
        <div className="home-hero-logo">{brandName.slice(0, 1) || "马"}</div>
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
                // 点最近画布就直接进它的画布：以前这里只跳到画布列表，
                // 想继续做的人还得多点一次（组件本来就拿到了 onOpenCanvas）
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
    </div>
  );
}
