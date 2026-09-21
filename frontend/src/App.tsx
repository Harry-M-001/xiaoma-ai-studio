import { useCallback, useEffect, useState } from "react";
import { Puzzle } from "lucide-react";
import Sidebar, { type RouteKey } from "./components/Sidebar";
import LoginGate from "./components/LoginGate";
import SetupBanner from "./components/SetupBanner";
import { ToastProvider } from "./components/Toast";
import { Empty } from "./components/common";
import { api, authToken, cfgBool, cfgString } from "./api";
import { prefs } from "./prefs";
import type { AppInfo, ConfigMap, NavMeta } from "./types";
import ChatPage from "./pages/ChatPage";
import ImagePage from "./pages/ImagePage";
import VideoPage from "./pages/VideoPage";
import SpeechPage from "./pages/SpeechPage";
import EnginesPage from "./pages/EnginesPage";
import AssetsPage from "./pages/AssetsPage";
import PromptsPage from "./pages/PromptsPage";
import TasksPage from "./pages/TasksPage";
import DirectorPage from "./pages/DirectorPage";
import HomePage from "./pages/HomePage";
import ProjectsPage from "./pages/ProjectsPage";
import CanvasPage from "./pages/CanvasPage";
import ProvidersPage from "./pages/ProvidersPage";
import SettingsPage from "./pages/SettingsPage";

type Theme = "light" | "dark";

/** 兜底配置：后端拿不到时沿用现有硬编码文案 */
const DEFAULT_CONFIG: ConfigMap = {
  "app.name": "小马AI工坊",
  "app.subtitle": "个人 AI 创作工作台",
  "app.logo_text": "马",
};

/** nav 接口失败时的内置默认导航，保证离线可用（与后端初始数据一致） */
const DEFAULT_NAV: NavMeta[] = [
  { key: "home", label: "首页", icon: "home", route: "home", group: "main", requires_auth: false },
  { key: "projects", label: "项目", icon: "folder", route: "projects", group: "main", requires_auth: false },
  { key: "chat", label: "文本对话", icon: "message", route: "chat", group: "main", requires_auth: false },
  { key: "image", label: "图片生成", icon: "image", route: "image", group: "main", requires_auth: false },
  { key: "video", label: "视频生成", icon: "video", route: "video", group: "main", requires_auth: false },
  { key: "speech", label: "配音", icon: "audio", route: "speech", group: "main", requires_auth: false },
  { key: "assets", label: "资产库", icon: "library", route: "assets", group: "main", requires_auth: false },
  { key: "prompts", label: "提示词库", icon: "sparkles", route: "prompts", group: "main", requires_auth: false },
  { key: "tasks", label: "任务中心", icon: "tasks", route: "tasks", group: "main", requires_auth: false },
  { key: "director", label: "导演台", icon: "clapperboard", route: "director", group: "main", requires_auth: false },
  { key: "providers", label: "模型服务", icon: "settings", route: "providers", group: "settings", requires_auth: false },
  { key: "engines", label: "本机引擎", icon: "cpu", route: "engines", group: "settings", requires_auth: false },
  { key: "settings", label: "系统设置", icon: "sliders", route: "settings", group: "settings", requires_auth: false },
];

/** 前端已实现的页面路由；导航里出现的其它 route 视为「未安装模块」 */
const KNOWN_ROUTES = new Set(["home", "projects", "canvas", "chat", "image", "video", "speech", "assets", "prompts", "tasks", "director", "providers", "engines", "settings"]);

/** route → modules.* 开关键，用于按配置关闭模块 */
const MODULE_KEYS: Record<string, string> = {
  home: "modules.home",
  projects: "modules.projects",
  chat: "modules.chat",
  image: "modules.image",
  video: "modules.video",
  assets: "modules.assets",
  prompts: "modules.prompts",
  tasks: "modules.tasks",
  director: "modules.director",
};

/** 占位页：未安装模块 / 已关闭模块共用 */
function Placeholder({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="page">
      <div className="card">
        <Empty icon={<Puzzle />} title={title} desc={desc} />
      </div>
    </div>
  );
}

function getInitialTheme(): Theme {
  // 合法化统一在 prefs 里做：非法值会被丢掉并回到系统偏好
  const saved = prefs.theme.get();
  if (saved) return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function Shell() {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [route, setRoute] = useState<RouteKey>("home");
  const [collapsed, setCollapsed] = useState(() => prefs.sidebarCollapsed.get() ?? false);
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [config, setConfig] = useState<ConfigMap>(DEFAULT_CONFIG);
  const [nav, setNav] = useState<NavMeta[]>([]);
  const [authed, setAuthed] = useState(false);
  const [canvasProject, setCanvasProject] = useState<{ id: number; name: string } | null>(null);
  const [updateAvailable, setUpdateAvailable] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    prefs.theme.set(theme);
  }, [theme]);

  useEffect(() => {
    prefs.sidebarCollapsed.set(collapsed);
  }, [collapsed]);

  // 站点配置与导航菜单：系统设置保存后也会调用它即时刷新
  const loadMeta = useCallback(async () => {
    const [configRes, navRes] = await Promise.allSettled([api.getConfig(), api.getNav()]);
    if (configRes.status === "fulfilled") setConfig(configRes.value);
    // 导航为空或失败时回落到内置默认导航
    if (navRes.status === "fulfilled" && navRes.value.length > 0) setNav(navRes.value);
    else setNav(DEFAULT_NAV);
  }, []);

  useEffect(() => {
    (async () => {
      // 启动时并行拉取：应用信息 / 公开配置 / 导航菜单
      const infoPromise = api.appInfo();
      const metaPromise = loadMeta();
      try {
        const data = await infoPromise;
        setInfo(data);
        if (!data.auth_required) {
          setAuthed(true);
        } else if (authToken.get()) {
          try {
            await api.authCheck();
            setAuthed(true);
          } catch {
            authToken.clear();
          }
        }
      } catch {
        // 后端暂不可用时也放行界面，避免一直卡在 loading
        setInfo({ name: "小马AI工坊", version: "", auth_required: false });
        setAuthed(true);
      }
      await metaPromise;
    })();
  }, [loadMeta]);

  useEffect(() => {
    const onUnauthorized = () => setAuthed(false);
    window.addEventListener("xm:unauthorized", onUnauthorized);
    return () => window.removeEventListener("xm:unauthorized", onUnauthorized);
  }, []);

  // 启动时静默检查更新：只在「已配更新源仓库」且用户没关掉开关时查一次，
  // 失败不打扰用户（离线 / 上游 API 不通都属正常）。
  useEffect(() => {
    if (!authed) return;
    if (!cfgBool(config, "update.check_on_start", true)) return;
    if (!cfgString(config, "update.repo", "")) return;
    let alive = true;
    api
      .getUpdateStatus()
      .then((s) => {
        if (alive) setUpdateAvailable(Boolean(s.hasUpdate));
      })
      .catch(() => {
        /* 检查更新失败不提示 */
      });
    return () => {
      alive = false;
    };
  }, [authed, config]);

  // 默认路由：nav 中第一个 main 分组项，拿不到则回落 home
  useEffect(() => {
    if (nav.length === 0) return;
    const first = nav.find((item) => item.group === "main") ?? nav[0];
    setRoute((prev) => (nav.some((item) => item.route === prev) ? prev : first.route));
  }, [nav]);

  if (!info) {
    return (
      <div className="loading-page">
        <span className="spinner lg" />
      </div>
    );
  }

  if (info.auth_required && !authed) {
    return <LoginGate onSuccess={() => setAuthed(true)} />;
  }

  const brandName = cfgString(config, "app.name", "小马AI工坊");
  const brandSub = cfgString(config, "app.subtitle", "个人 AI 创作工作台");
  const logoText = cfgString(config, "app.logo_text", "马");

  const openCanvas = (p: { id: number; name: string }) => {
    setCanvasProject({ id: p.id, name: p.name });
    setRoute("canvas");
  };

  const renderPage = () => {
    // 导航里注册了前端没有实现的模块：显示「尚未安装」占位，而不是崩溃
    if (!KNOWN_ROUTES.has(route)) {
      return (
        <Placeholder
          title="该模块尚未安装"
          desc={`导航里出现了前端尚未实现的路由「${route}」。这是预留模块，可在「系统设置 → 导航菜单」中启用或安装对应模块后使用。`}
        />
      );
    }

    // modules.* 开关：被关闭的模块显示占位
    const moduleKey = MODULE_KEYS[route];
    if (moduleKey && !cfgBool(config, moduleKey, true)) {
      return (
        <Placeholder
          title="该模块已在系统设置中关闭"
          desc="可在「系统设置 → 模块开关」中重新打开该模块，保存后立即生效。"
        />
      );
    }

    switch (route) {
      case "home":
        return (
          <HomePage
            onNavigate={setRoute}
            onOpenCanvas={openCanvas}
            onGoProviders={() => setRoute("providers")}
            brandName={brandName}
            brandSub={brandSub}
          />
        );
      case "projects":
        return (
          <ProjectsPage
            onOpenCanvas={openCanvas}
            onGoProviders={() => setRoute("providers")}
          />
        );
      case "canvas":
        return canvasProject ? (
          <CanvasPage
            projectId={canvasProject.id}
            projectName={canvasProject.name}
            onBack={() => setRoute("projects")}
          />
        ) : null;
      case "chat":
        return <ChatPage onGoSettings={() => setRoute("providers")} />;
      case "image":
        return <ImagePage onGoSettings={() => setRoute("providers")} />;
      case "video":
        return <VideoPage onGoSettings={() => setRoute("providers")} />;
      case "speech":
        return <SpeechPage onGoSettings={() => setRoute("providers")} />;
      case "assets":
        return <AssetsPage />;
      case "prompts":
        return <PromptsPage onNavigate={setRoute} />;
      case "tasks":
        return <TasksPage />;
      case "director":
        return <DirectorPage onNavigate={setRoute} />;
      case "providers":
        return <ProvidersPage />;
      case "engines":
        return <EnginesPage />;
      case "settings":
        return <SettingsPage onMetaChanged={loadMeta} />;
      default:
        return (
          <Placeholder
            title="该模块尚未安装"
            desc={`路由「${route}」还没有对应的前端页面，可在「系统设置 → 导航菜单」中启用或安装对应模块。`}
          />
        );
    }
  };

  return (
    <div className="app-shell">
      <Sidebar
        route={route}
        onNavigate={setRoute}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        authed={info.auth_required}
        onLogout={() => {
          authToken.clear();
          setAuthed(false);
        }}
        nav={nav}
        brandName={brandName}
        brandSub={brandSub}
        logoText={logoText}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        badgeRoutes={updateAvailable ? ["settings"] : undefined}
      />
      <main className="main">
        <SetupBanner route={route} onNavigate={setRoute} />
        {renderPage()}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}
