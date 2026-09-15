import {
  MessageSquare,
  ImageIcon,
  Video,
  Library,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Workflow,
  Users,
  Music,
  BookOpen,
  Circle,
  ListChecks,
  Clapperboard,
  Home,
  FolderOpen,
} from "lucide-react";

/** 图标组件类型：与 lucide-react 的组件签名保持一致 */
export type IconComponent = typeof MessageSquare;

/**
 * 后端 icon 字符串 → 前端图标组件。
 * 后端导航菜单 / 能力类型表里的 icon 是自由文本，这里做一次映射；
 * 未命中的名称统一回落到 Circle，保证新增图标名时页面不会崩溃。
 */
const ICON_MAP: Record<string, IconComponent> = {
  message: MessageSquare,
  chat: MessageSquare,
  image: ImageIcon,
  video: Video,
  library: Library,
  settings: Settings,
  sliders: SlidersHorizontal,
  sparkles: Sparkles,
  workflow: Workflow,
  community: Users,
  users: Users,
  audio: Music,
  music: Music,
  prompt: BookOpen,
  book: BookOpen,
  tasks: ListChecks,
  list: ListChecks,
  clapperboard: Clapperboard,
  home: Home,
  folder: FolderOpen,
};

/** 按名称取图标组件，未知名称回落到 Circle */
export function getNavIcon(name?: string | null): IconComponent {
  if (!name) return Circle;
  return ICON_MAP[name.trim().toLowerCase()] ?? Circle;
}

/** 便捷组件：按名称渲染一个图标 */
export default function IconMap({ name, size }: { name?: string | null; size?: number }) {
  const Icon = getNavIcon(name);
  return <Icon size={size} />;
}
