/**
 * 界面偏好的统一读写口。
 *
 * 规矩只有一条：**前端所有 localStorage 读写都必须经过这里**（有单测把这条钉住，
 * 见 backend/tests/test_frontend_prefs.py）。
 *
 * 为什么值得单独收口：localStorage 是「用户能手动改、旧版本写过、跨版本会残留」的地方。
 * 脏数据进来不会报错，只会让界面表现诡异——自动链下拉框显示空白、生成时用了一个早被删掉的
 * 模型、缩略图莫名变成 0。所以这里读回来的每个值都要过一遍合法化：
 * 不合法就**当场丢掉并降级到默认**，而不是原样用出去、让问题在后面某处冒出来。
 */

import { parseDirectorDoc, serializeDirectorDoc } from "./director3d/persist";
import type { DirectorDoc } from "./director3d/types";

const K = {
  theme: "xm_theme",
  sidebar: "xm_sidebar_collapsed",
  navGroups: "xm_nav_groups_open",
  palette: "xm_canvas_palette",
  chainLevel: "xm_canvas_chain_level",
  thumb: "xm_canvas_thumb",
  setupDismissed: "xm_setup_dismissed",
  draftPrompt: "xm_draft_prompt",
  draftFirstFrame: "xm_draft_first_frame",
  model: (modality: string) => `xm_model_${modality}`,
  voice: "xm_speech_voice",
  /**
   * 3D 导演台的场景文档。它严格说不是「界面偏好」而是**用户内容**，
   * 但仍放在这个文件里：规矩的价值在「只有一个口子」，另开一个口子就等于没有规矩。
   * 代价是这里多知道一个业务类型（见文件末尾的说明），换来的是脏数据照样只在一处降级。
   */
  directorScene: (scope: string) => `xm_director3d_${scope}`,
} as const;

type Theme = "light" | "dark";

/** 读一个原始值。localStorage 在隐私模式/配额满时会直接抛异常，界面不该因此崩。 */
function readRaw(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeRaw(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 存不进去就算了：偏好丢了顶多下次回到默认值 */
  }
}

function dropRaw(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* 同上 */
  }
}

/** 读回来做一次校验；返回 null 表示「不合法」。 */
function read<T>(key: string, validate: (raw: string) => T | null): T | null {
  const raw = readRaw(key);
  if (raw === null) return null;
  const value = validate(raw);
  if (value === null) {
    // 不合法就地清掉：留着它只会在每次进页面时重复失败一次
    dropRaw(key);
  }
  return value;
}

const FLAG = (on: string, off: string) => (raw: string) => {
  const v = raw.trim();
  if (v === on) return true;
  if (v === off) return false;
  return null;
};

/**
 * 折叠组状态那张表。形状坏掉（不是对象、是数组、是段垃圾）就整张当空——
 * 少记几个展开状态是小事，把坏数据当配置用出去才是。
 */
function readGroupMap(): Record<string, unknown> {
  return (
    read(K.navGroups, (raw) => {
      try {
        const v: unknown = JSON.parse(raw);
        return v && typeof v === "object" && !Array.isArray(v)
          ? (v as Record<string, unknown>)
          : null;
      } catch {
        return null;
      }
    }) ?? {}
  );
}

export const prefs = {
  theme: {
    get: (): Theme | null => read(K.theme, (r) => (r === "dark" || r === "light" ? r : null)),
    set: (v: Theme) => writeRaw(K.theme, v),
  },

  sidebarCollapsed: {
    get: () => read(K.sidebar, FLAG("1", "0")),
    set: (v: boolean) => writeRaw(K.sidebar, v ? "1" : "0"),
  },

  /**
   * 侧边栏折叠组（如「创作台」）的展开状态，按组 key 记。
   *
   * 存成一个对象 `{ create: true }` 而不是一组固定键：将来多几个折叠组时不用再加键、
   * 也不用管老库缺哪个键。**返回 null 表示「用户没设过」**——这个区别很重要：
   * 侧边栏要靠它决定「默认收起」还是「按当前路由自动展开」（见 `Sidebar.tsx`）。
   */
  navGroupOpen: {
    get: (key: string): boolean | null => {
      const v = readGroupMap()[key];
      return typeof v === "boolean" ? v : null;
    },
    set: (key: string, open: boolean) => {
      const all = readGroupMap();
      all[key] = open;
      writeRaw(K.navGroups, JSON.stringify(all));
    },
  },

  paletteOpen: {
    get: () => read(K.palette, FLAG("1", "0")),
    set: (v: boolean) => writeRaw(K.palette, v ? "1" : "0"),
  },

  /**
   * 自动链档位：只接受**当前版本真实存在**的档位。
   * 老版本写过的 key 在新版本里可能已经没了，原样用出去的下场是下拉框显示空白、
   * 或铺链时静默退回第一档（用户以为自己选的是 L4）。
   */
  chainLevel: {
    get: (known: readonly string[]): string | null =>
      read(K.chainLevel, (r) => (known.includes(r) ? r : null)),
    set: (v: string) => writeRaw(K.chainLevel, v),
  },

  /** 缩略图尺寸：限定在滑杆的步进范围内，超出就回默认值。 */
  thumbSize: {
    get: (min = 56, max = 176, fallback = 88): number =>
      read(K.thumb, (r) => {
        const v = Number(r);
        return Number.isFinite(v) && v >= min && v <= max ? v : null;
      }) ?? fallback,
    set: (v: number) => writeRaw(K.thumb, String(v)),
  },

  setupDismissed: {
    get: () => readRaw(K.setupDismissed) ?? "", // 只是个比较用的签名串，没有可校验的形状
    set: (signature: string) => writeRaw(K.setupDismissed, signature),
  },

  /**
   * 记住的模型是**本机配置的引用**，不是纯偏好：服务被删掉或改名后那个 key 就悬空了。
   * 原样用出去的表现是选择框空白、一生成就报「模型服务不存在」。
   */
  modelKey: {
    get: (modality: string, available: readonly string[]): string | null =>
      read(K.model(modality), (r) => (r && available.includes(r) ? r : null)),
    set: (modality: string, key: string) => {
      if (key) writeRaw(K.model(modality), key);
      else dropRaw(K.model(modality));
    },
  },

  /**
   * 上次用的音色（配音页预填用）。
   * 留空是**合法值**：表示「用服务默认音色」，所以空串不写盘、直接清掉键。
   */
  voice: {
    get: () => readRaw(K.voice) ?? "",
    set: (v: string) => {
      if (v) writeRaw(K.voice, v);
      else dropRaw(K.voice);
    },
  },

  /** 提示词库 → 创作页的草稿传递（取走即删，所以读取就是消费） */
  draftPrompt: {
    set: (target: string, text: string) => writeRaw(K.draftPrompt, JSON.stringify({ target, text })),
    take: (target: string): string | null => {
      const raw = readRaw(K.draftPrompt);
      if (raw === null) return null;
      dropRaw(K.draftPrompt);
      try {
        const d = JSON.parse(raw) as { target?: unknown; text?: unknown };
        return d.target === target && typeof d.text === "string" ? d.text : null;
      } catch {
        return null;
      }
    },
  },

  /** 导演台 → 视频生成的「首帧」传递 */
  draftFirstFrame: {
    set: (asset: { id: number; url: string }) =>
      writeRaw(K.draftFirstFrame, JSON.stringify({ id: asset.id, url: asset.url })),
    take: (): { id: number; url: string } | null => {
      const raw = readRaw(K.draftFirstFrame);
      if (raw === null) return null;
      dropRaw(K.draftFirstFrame);
      try {
        const d = JSON.parse(raw) as { id?: unknown; url?: unknown };
        return typeof d.id === "number" && typeof d.url === "string" ? { id: d.id, url: d.url } : null;
      } catch {
        return null;
      }
    },
  },

  /**
   * 3D 场景文档按**作用域**存（项目 id 或 demo），互不干扰。
   *
   * 校验交给 `director3d/persist.ts`：那是一份纯数据模块（不 import three），
   * 所以这里引用它不会把 1MB 的 three 拖进首屏包；而「存档能不能用」这件事
   * 只有那一处说了算，`prefs` 不重复实现半套。
   *
   * `load` 要区分「没存过」和「存过但是坏的」：后者必须让界面说一句，
   * 否则用户看到的是「我昨天搭的场景没了」，而界面表现得像第一次打开。
   */
  directorScene: {
    load: (scope: string): { doc: DirectorDoc | null; malformed: boolean } => {
      const raw = readRaw(K.directorScene(scope));
      if (raw === null) return { doc: null, malformed: false };
      const doc = parseDirectorDoc(raw);
      if (doc === null) {
        dropRaw(K.directorScene(scope));
        return { doc: null, malformed: true };
      }
      return { doc, malformed: false };
    },
    set: (scope: string, doc: DirectorDoc) =>
      writeRaw(K.directorScene(scope), serializeDirectorDoc(doc)),
    drop: (scope: string) => dropRaw(K.directorScene(scope)),
  },
};
