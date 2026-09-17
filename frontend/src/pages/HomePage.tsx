import { useEffect, useState } from "react";
import {
  ArrowRight,
  Clapperboard,
  ImageIcon,
  ListChecks,
  MessageSquare,
  Plus,
  Sparkles,
  Video,
} from "lucide-react";
import { api } from "../api";
import { createDemoProject } from "../demoProject";
import type { Project } from "../types";
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
  const [creating, setCreating] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);

  useEffect(() => {
    api
      .listProjects()
      .then(setProjects)
      .catch(() => setProjects([]));
  }, []);

  const quickCreate = async () => {
    const name = `新作品 ${new Date().toLocaleDateString("zh-CN")}`;
    setCreating(true);
    try {
      await api.createProject(name, "");
      setProjects(await api.listProjects());
      toast.success("项目已创建，到「项目」页查看");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  /** 建一个铺好链的示例项目并直接进画布 */
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

  return (
    <div className="page home-page">
      <section className="home-hero card">
        <div className="home-hero-text">
          <h1>
            {brandName}
            <span className="home-hero-sub">{brandSub}</span>
          </h1>
          <p>
            填一个 Base URL 和 API Key，接入任意模型：对话、生图、生视频、本地粗剪，
            产物自动进本地资产库。数据全在你自己手里。
          </p>
          <div className="home-hero-actions">
            <button className="btn btn-primary" onClick={() => onNavigate("image")}>
              开始创作
              <ArrowRight size={15} />
            </button>
            <button className="btn btn-ghost" onClick={startDemo} disabled={demoBusy}>
              {demoBusy ? <Spinner /> : <Sparkles size={15} />}
              从示例开始
            </button>
            <button className="btn btn-ghost" onClick={quickCreate} disabled={creating}>
              {creating ? <Spinner /> : <Plus size={15} />}
              新建项目
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
          {ENTRIES.map((e) => (
            <button key={e.route} className="home-card card" onClick={() => onNavigate(e.route)}>
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
            <h2>最近项目</h2>
            <button className="btn btn-ghost btn-sm" onClick={() => onNavigate("projects")}>
              全部项目
              <ArrowRight size={13} />
            </button>
          </div>
          <div className="home-projects">
            {projects.slice(0, 4).map((p) => (
              <button key={p.id} className="home-project card" onClick={() => onNavigate("projects")}>
                <div className="home-project-name">{p.name}</div>
                <div className="home-project-desc">{p.description || "暂无描述"}</div>
                <div className="home-project-time muted">{formatDate(p.updated_at)}</div>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
