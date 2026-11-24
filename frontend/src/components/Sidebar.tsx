import {
  Boxes,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FolderOpen,
  LogOut,
  Moon,
  MoreHorizontal,
  Plus,
  Sun,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AgentProjectInfo, AgentSessionInfo, NavMeta, Project } from "../types";
import { prefs } from "../prefs";
import { getNavIcon } from "./IconMap";
import { useToast } from "./Toast";

/** 路由标识改为动态字符串：导航菜单由后端配置表驱动，没有固定枚举 */
export type RouteKey = string;

/**
 * 折叠组的显示名。后端只给组 key（`create`），中文名放在前端——
 * 这与「设置」那一节的做法一致（组是前端已知的枚举，而每项的 label 才是用户可改的配置）。
 */
const GROUP_LABELS: Record<string, string> = { create: "创作台" };

/** 折叠组里当前路由所在的那一个，用来决定「收起时该不该把组名点亮」 */
function groupHasRoute(items: NavMeta[], route: RouteKey): boolean {
  return items.some((item) => item.route === route);
}

export default function Sidebar({
  route,
  onNavigate,
  theme,
  onToggleTheme,
  authed,
  onLogout,
  nav,
  brandName,
  brandSub,
  logoText,
  collapsed,
  onToggleCollapsed,
  badgeRoutes,
  onOpenCanvas,
  onOpenAgentSession,
  onAgentProjectCreated,
}: {
  route: RouteKey;
  onNavigate: (r: RouteKey) => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  authed: boolean;
  onLogout: () => void;
  /** 后端返回的导航菜单（仅 enabled 项）；由 App 统一拉取后传入 */
  nav: NavMeta[];
  brandName: string;
  brandSub: string;
  logoText: string;
  /** 抽屉收起状态：只显示图标，点击仍可导航 */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** 需要挂提示红点的路由（如检测到新版本时提示「系统设置」） */
  badgeRoutes?: string[];
  /** 工作区里的画布/任务要能直接打开 */
  onOpenCanvas?: (p: { id: number; name: string }) => void;
  onOpenAgentSession?: (id: number) => void;
  /** 在工作区里新建了 Agent 项目：Agent 页要重新读一次项目列表 */
  onAgentProjectCreated?: () => void;
}) {
  const toast = useToast();
  // main 组直接列出；create 组收进「创作台」折叠组；settings 组收进底部「更多」
  const mainItems = nav.filter((item) => item.group !== "settings" && item.group !== "create");
  const createItems = nav.filter((item) => item.group === "create");
  const settingItems = nav.filter((item) => item.group === "settings");
  // 首页固定第一位；Agent 紧跟首页、排在「创作台」折叠组上面（用户 2026-11-24 指定）。
  // 用 route 钉位置而不是改 sort_order：导航是配置表驱动的，老库里已有的排序不该被一次升级悄悄改掉。
  const [headItem, ...restItems] = mainItems;
  const pinnedItems = restItems.filter((item) => item.route === "agent");
  const tailItems = restItems.filter((item) => item.route !== "agent");

  // 工作区：Agent 项目 → 任务；项目绑定的画布一并显示
  const [wsProjects, setWsProjects] = useState<AgentProjectInfo[]>([]);
  const [wsSessions, setWsSessions] = useState<AgentSessionInfo[]>([]);
  const [canvases, setCanvases] = useState<Project[]>([]);
  const [openWs, setOpenWs] = useState<number | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState<string | null>(null);
  /** 新任务用哪个模型：与 Agent 页同一个默认（目录里第一个可用文本模型） */
  const [modelKey, setModelKey] = useState("");

  const loadWorkspace = useCallback(async () => {
    try {
      const [meta, projects] = await Promise.all([api.agentMeta(), api.listProjects()]);
      setWsProjects(meta.projects || []);
      setWsSessions(meta.sessions || []);
      setCanvases(projects);
      setModelKey((prev) => prev || meta.models[0]?.key || "");
      setOpenWs((prev) => prev ?? meta.projects?.[0]?.id ?? null);
      return meta;
    } catch {
      // 侧栏读不到不影响导航本身；下次路由变化会再试
      return null;
    }
  }, []);

  useEffect(() => {
    if (route === "home" || route === "agent" || route === "projects" || route === "canvas") {
      void loadWorkspace();
    }
  }, [route, loadWorkspace]);

  /** 在某个项目下开一条新任务并打开它——原来 Agent 页左栏那个「新任务」按钮的替身 */
  const createTask = async (projectId: number | null) => {
    try {
      const payload = await api.createAgentSession({
        model_key: modelKey,
        agent_project_id: projectId,
      });
      await loadWorkspace();
      onOpenAgentSession?.(payload.session.id);
    } catch (e) {
      toast.error(`新建任务失败：${(e as Error).message}`);
    }
  };

  /** 新建项目就在工作区标题旁边：不用再绕去 Agent 页左边那一列 */
  const createProject = async () => {
    const name = (newProjectName || "").trim();
    if (!name) {
      setNewProjectName(null);
      return;
    }
    try {
      const p = await api.createAgentProject({ name });
      setNewProjectName(null);
      setOpenWs(p.id);
      await loadWorkspace();
      onAgentProjectCreated?.();
      toast.success(`项目「${p.name}」已建好`);
      onNavigate("agent");
    } catch (e) {
      toast.error(`新建项目失败：${(e as Error).message}`);
    }
  };

  const canvasName = (id: number | null | undefined) =>
    canvases.find((c) => c.id === id)?.name || `画布 #${id}`;

  const sessionsOf = (projectId: number) =>
    wsSessions.filter((s) => s.agentProjectId === projectId);
  const unfiled = wsSessions.filter((s) => s.agentProjectId === null);

  return (
    <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
      <div className="brand" title={collapsed ? brandName : undefined}>
        <div className="brand-logo">{logoText || "马"}</div>
        {!collapsed && (
          <div>
            <div className="brand-name">{brandName || "小马AI工坊"}</div>
            <div className="brand-sub">{brandSub || "个人 AI 创作工作台"}</div>
          </div>
        )}
      </div>

      <button
        className={`nav-item sidebar-collapse-btn ${collapsed ? "open" : ""}`}
        onClick={onToggleCollapsed}
        title={collapsed ? "展开导航" : "收起导航"}
      >
        {collapsed ? <ChevronRight /> : <ChevronLeft />}
        {!collapsed && "收起导航"}
      </button>

      {/* 中间这一段可滚动：品牌与底部「更多」都不动，导航和工作区随滚轮走 */}
      <div className="sidebar-scroll">
        {headItem && (
          <NavRow
            key={headItem.key || headItem.route}
            item={headItem}
            route={route}
            collapsed={collapsed}
            onNavigate={onNavigate}
            badgeRoutes={badgeRoutes}
          />
        )}

        {pinnedItems.map((item) => (
          <NavRow
            key={item.key || item.route}
            item={item}
            route={route}
            collapsed={collapsed}
            onNavigate={onNavigate}
            badgeRoutes={badgeRoutes}
          />
        ))}

        {createItems.length > 0 && (
          <CollapsibleGroup
            groupKey="create"
            items={createItems}
            route={route}
            collapsed={collapsed}
            onNavigate={onNavigate}
            badgeRoutes={badgeRoutes}
          />
        )}

        {tailItems.map((item) => (
          <NavRow
            key={item.key || item.route}
            item={item}
            route={route}
            collapsed={collapsed}
            onNavigate={onNavigate}
            badgeRoutes={badgeRoutes}
          />
        ))}

        {/* 工作区：项目 → 任务；图标态不显示（那里每一样都只是一枚图标，树会失去意义）。
            新建项目放在标题旁边——Agent 页里那一列项目列表已经撤掉，这里是唯一的入口。 */}
        {!collapsed && (
          <div className="nav-workspace">
          <div className="nav-workspace-head">
            <span className="nav-group-label">工作区</span>
            <button
              className="icon-btn ws-add"
              onClick={() => setNewProjectName(newProjectName === null ? "" : null)}
              title="新建项目"
              aria-label="新建项目"
            >
              <Plus size={14} />
            </button>
          </div>
          {newProjectName !== null && (
            <div className="ws-new">
              <input
                className="input ws-new-input"
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
          {wsProjects.map((p) => {
            const sessions = sessionsOf(p.id);
            const open = openWs === p.id;
            return (
              <div key={p.id} className="ws-project-wrap">
                <button
                  className={`ws-project ${open ? "open" : ""}`}
                  onClick={() => setOpenWs(open ? null : p.id)}
                  aria-expanded={open}
                  title={p.description || p.name}
                >
                  <ChevronDown className={`ws-chevron ${open ? "open" : ""}`} />
                  <span className="ws-name">{p.name}</span>
                  <span className="ws-count">{sessions.length}</span>
                </button>
                {open && (
                  <div className="ws-items">
                    {p.canvasProjectId && (
                      <button
                        className="ws-canvas"
                        onClick={() =>
                          onOpenCanvas?.({ id: p.canvasProjectId as number, name: canvasName(p.canvasProjectId) })
                        }
                        title="打开这个项目绑定的画布"
                      >
                        <FolderOpen size={13} />
                        <span>{canvasName(p.canvasProjectId)}</span>
                      </button>
                    )}
                    {sessions.map((s) => (
                      <button
                        key={s.id}
                        className="ws-task"
                        onClick={() => onOpenAgentSession?.(s.id)}
                        title={s.title}
                      >
                        <span className="ws-task-title">{s.title || "新任务"}</span>
                        <span className="ws-task-time">{s.updatedAt.slice(5, 10)}</span>
                      </button>
                    ))}
                    <button className="ws-task ws-task-new" onClick={() => void createTask(p.id)}>
                      <Plus size={11} />
                      <span className="ws-task-title">新任务</span>
                    </button>
                  </div>
                )}
              </div>
            );
          })}
          {wsProjects.length === 0 && newProjectName === null && (
            <div className="ws-empty">还没有项目，点右上角 + 建一个</div>
          )}
          {unfiled.length > 0 && (
            <div className="ws-project-wrap">
              <button
                className={`ws-project ${openWs === -1 ? "open" : ""}`}
                onClick={() => setOpenWs(openWs === -1 ? null : -1)}
                aria-expanded={openWs === -1}
              >
                <ChevronDown className={`ws-chevron ${openWs === -1 ? "open" : ""}`} />
                <span className="ws-name">未归档</span>
                <span className="ws-count">{unfiled.length}</span>
              </button>
              {openWs === -1 && (
                <div className="ws-items">
                  {unfiled.map((s) => (
                    <button
                      key={s.id}
                      className="ws-task"
                      onClick={() => onOpenAgentSession?.(s.id)}
                      title={s.title}
                    >
                      <span className="ws-task-title">{s.title || "新任务"}</span>
                      <span className="ws-task-time">{s.updatedAt.slice(5, 10)}</span>
                    </button>
                  ))}
                  <button className="ws-task ws-task-new" onClick={() => void createTask(null)}>
                    <Plus size={11} />
                    <span className="ws-task-title">新任务</span>
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
      </div>

      <div className="sidebar-footer">
        <button
          className={`nav-item ${moreOpen ? "active" : ""}`}
          onClick={() => setMoreOpen((v) => !v)}
          aria-expanded={moreOpen}
          title="更多设置"
        >
          <MoreHorizontal />
          {!collapsed && "更多"}
        </button>
      </div>

      {moreOpen && (
        <>
          <div className="sidebar-more-backdrop" onClick={() => setMoreOpen(false)} />
          <div className="sidebar-more-menu" role="menu">
            {settingItems.map((item) => {
              const Icon = getNavIcon(item.icon);
              const badge = badgeRoutes?.includes(item.route) ?? false;
              return (
                <button
                  key={item.key || item.route}
                  className="sidebar-more-item"
                  role="menuitem"
                  onClick={() => {
                    onNavigate(item.route);
                    setMoreOpen(false);
                  }}
                >
                  <Icon />
                  <span>{item.label}</span>
                  {badge && <span className="nav-dot" title="有新版本可更新" />}
                </button>
              );
            })}
            {settingItems.length > 0 && <div className="sidebar-more-sep" />}
            <button
              className="sidebar-more-item"
              role="menuitem"
              onClick={() => {
                onToggleTheme();
                setMoreOpen(false);
              }}
            >
              {theme === "dark" ? <Sun /> : <Moon />}
              <span>{theme === "dark" ? "浅色模式" : "深色模式"}</span>
            </button>
            {authed && (
              <button
                className="sidebar-more-item"
                role="menuitem"
                onClick={() => {
                  onLogout();
                  setMoreOpen(false);
                }}
              >
                <LogOut />
                <span>锁定</span>
              </button>
            )}
          </div>
        </>
      )}
    </aside>
  );
}

/** 一个普通的导航项 */
function NavRow({
  item,
  route,
  collapsed,
  onNavigate,
  badgeRoutes,
}: {
  item: NavMeta;
  route: RouteKey;
  collapsed: boolean;
  onNavigate: (r: RouteKey) => void;
  badgeRoutes?: string[];
}) {
  const Icon = getNavIcon(item.icon);
  const badge = badgeRoutes?.includes(item.route) ?? false;
  return (
    <button
      className={`nav-item ${route === item.route ? "active" : ""}`}
      onClick={() => onNavigate(item.route)}
      title={collapsed ? item.label : undefined}
    >
      <Icon />
      {!collapsed && item.label}
      {badge && <span className="nav-dot" title="有新版本可更新" />}
    </button>
  );
}

/**
 * 可折叠的分组。
 *
 * 三条口径：
 * 1. **默认收起**——这一组存在的意义就是让侧边栏一眼不要十几个入口；收起状态下
 *    组名本身可点，所以不存在「找不到」。
 * 2. **展开状态记在 `prefs`**（唯一 localStorage 口子），用户点过之后就完全听用户的。
 * 3. **路由落进组里时自动展开**：不然用户从首页快捷入口点「图片生成」进来，
 *    侧边栏上看不出自己在哪。这个自动展开只在路由变化时跑，所以手动收起不会被顶回来；
 *    而万一收起时人还在组里，组名会被点亮（`active`），仍然看得出位置。
 *
 * 侧边栏处于图标态（`collapsed`）时**不渲染这一层**：那里连文字都没有，再套一层折叠
 * 只是徒增一次点击——「省地方」这件事已经由图标态本身做完了。
 */
function CollapsibleGroup({
  groupKey,
  items,
  route,
  collapsed,
  onNavigate,
  badgeRoutes,
}: {
  groupKey: string;
  items: NavMeta[];
  route: RouteKey;
  collapsed: boolean;
  onNavigate: (r: RouteKey) => void;
  badgeRoutes?: string[];
}) {
  const [open, setOpen] = useState(() => prefs.navGroupOpen.get(groupKey) ?? false);
  const hasActive = groupHasRoute(items, route);

  useEffect(() => {
    setOpen((prev) => prev || groupHasRoute(items, route));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    prefs.navGroupOpen.set(groupKey, next);
  };

  if (collapsed) {
    return (
      <>
        {items.map((item) => (
          <NavRow
            key={item.key || item.route}
            item={item}
            route={route}
            collapsed
            onNavigate={onNavigate}
            badgeRoutes={badgeRoutes}
          />
        ))}
      </>
    );
  }

  return (
    <div className="nav-collapse-group">
      <button
        className={`nav-item nav-group-toggle ${open ? "open" : ""} ${hasActive && !open ? "active" : ""}`}
        onClick={toggle}
        aria-expanded={open}
        title={open ? `收起${GROUP_LABELS[groupKey] ?? groupKey}` : `展开${GROUP_LABELS[groupKey] ?? groupKey}`}
      >
        <Boxes />
        {GROUP_LABELS[groupKey] ?? groupKey}
        <ChevronDown className="nav-group-chevron" />
      </button>
      {open && (
        <div className="nav-group-items">
          {items.map((item) => (
            <NavRow
              key={item.key || item.route}
              item={item}
              route={route}
              collapsed={false}
              onNavigate={onNavigate}
              badgeRoutes={badgeRoutes}
            />
          ))}
        </div>
      )}
    </div>
  );
}
