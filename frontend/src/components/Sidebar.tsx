import { ChevronDown, ChevronLeft, ChevronRight, Moon, Sun, LogOut, Boxes } from "lucide-react";
import { useEffect, useState } from "react";
import type { NavMeta } from "../types";
import { prefs } from "../prefs";
import { getNavIcon } from "./IconMap";

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
  // main 组直接列出；create 组收进「创作台」折叠组；settings 组归到「设置」小节下
  const mainItems = nav.filter((item) => item.group !== "settings" && item.group !== "create");
  const createItems = nav.filter((item) => item.group === "create");
  const settingItems = nav.filter((item) => item.group === "settings");
  // 折叠组排在**首页之后、其余项之前**（用户指定）：创作入口离首页最近，从首页进来第一眼
  // 就该看到它们。实现就是「第一项单独渲染 + 其余照常」——首页被关掉时它自然上移到第一位，
  // 不需要为「首页」这个 key 写死一个特例。
  const [headItem, ...tailItems] = mainItems;

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

      {settingItems.length > 0 && (
        <>
          <div className="nav-group-label">{collapsed ? "" : "设置"}</div>
          {settingItems.map((item) => (
            <NavRow
              key={item.key || item.route}
              item={item}
              route={route}
              collapsed={collapsed}
              onNavigate={onNavigate}
              badgeRoutes={badgeRoutes}
            />
          ))}
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
