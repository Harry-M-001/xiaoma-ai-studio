import { ChevronLeft, ChevronRight, Moon, Sun, LogOut } from "lucide-react";
import type { NavMeta } from "../types";
import { getNavIcon } from "./IconMap";

/** 路由标识改为动态字符串：导航菜单由后端配置表驱动，没有固定枚举 */
export type RouteKey = string;

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
}) {
  // main 组直接列出；settings 组归到「设置」小节下
  const mainItems = nav.filter((item) => item.group !== "settings");
  const settingItems = nav.filter((item) => item.group === "settings");

  const renderItem = (item: NavMeta) => {
    const Icon = getNavIcon(item.icon);
    const badge = badgeRoutes?.includes(item.route) ?? false;
    return (
      <button
        key={item.key || item.route}
        className={`nav-item ${route === item.route ? "active" : ""}`}
        onClick={() => onNavigate(item.route)}
        title={collapsed ? item.label : undefined}
      >
        <Icon />
        {!collapsed && item.label}
        {badge && <span className="nav-dot" title="有新版本可更新" />}
      </button>
    );
  };

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

      {mainItems.map(renderItem)}

      {settingItems.length > 0 && (
        <>
          <div className="nav-group-label">{collapsed ? "" : "设置"}</div>
          {settingItems.map(renderItem)}
        </>
      )}

      <div className="sidebar-footer">
        <button className="nav-item" onClick={onToggleTheme} title="切换明暗主题">
          {theme === "dark" ? <Sun /> : <Moon />}
          {!collapsed && (theme === "dark" ? "浅色模式" : "深色模式")}
        </button>
        {authed && (
          <button className="nav-item" onClick={onLogout} title="锁定">
            <LogOut />
            {!collapsed && "锁定"}
          </button>
        )}
      </div>
    </aside>
  );
}
