/**
 * 3D 导演台的文档状态：唯一真源 + 撤销重做 + 自动存档。
 *
 * 为什么不用 `useState` 摊在组件里：导演台的编辑动作有十几种（加对象、改坐标、
 * 调 18 个关节、改灯光、切机位…），散在各处的 setState 迟早会出现「某个动作忘了
 * 记历史」。所以把「改文档」收敛成一个 `commit()`——**所有**编辑都从这里过，
 * 撤销栈与存档就不可能漏。
 *
 * 三条约定：
 *  - `commit(fn, coalesceKey)`：`coalesceKey` 相同且间隔很短的连续改动只记一条历史。
 *    拖滑杆会触发几十次 commit，不合并的话撤销一次只退回一格，用户按到手酸。
 *  - 拖动中的预览用 `preview(fn)`：只改文档不记历史（松手时才 commit 一次）。
 *  - 存档 debounce：每次 commit 都写 localStorage 会在拖滑杆时写几十次，
 *    而 localStorage 是同步 IO，会直接卡住渲染。
 */

import { prefs } from "../prefs";
import {
  DOC_VERSION,
  makeEmptyDoc,
  type DirectorDoc,
  type DirectorObject,
  type PoseState,
  type Vec3,
} from "./types";

export type ViewMode = "director" | "camera";

/** 界面态：不进文档、不进存档（换台机器打开不该带着「上次网格是开的」） */
export interface SessionState {
  selectedId: string | null;
  viewMode: ViewMode;
  showGrid: boolean;
  showFrustum: boolean;
  showGuide: boolean;
  showLights: boolean;
}

export interface StudioState {
  doc: DirectorDoc;
  session: SessionState;
  canUndo: boolean;
  canRedo: boolean;
  /** 本次是「从存档恢复」还是「新开的空场景」——恢复失败要能如实告诉用户 */
  restored: boolean;
  /** 存过存档但读不出来（已被丢弃、退回空场景）。界面需要把这件事说出来 */
  malformed: boolean;
}

const HISTORY_LIMIT = 60;
const COALESCE_MS = 600;
const SAVE_DEBOUNCE_MS = 400;

export class SceneStore {
  private state: StudioState;
  private listeners = new Set<() => void>();
  private past: DirectorDoc[] = [];
  private future: DirectorDoc[] = [];
  private lastKey: string | null = null;
  private lastAt = 0;
  private saveTimer: number | null = null;

  constructor(private scope: string) {
    const saved = prefs.directorScene.load(scope);
    this.state = {
      doc: saved.doc ?? makeEmptyDoc(),
      session: {
        selectedId: null,
        viewMode: "director",
        showGrid: true,
        showFrustum: true,
        showGuide: true,
        showLights: false,
      },
      canUndo: false,
      canRedo: false,
      restored: saved.doc !== null,
      malformed: saved.malformed,
    };
  }

  // ------------------------------------------------------------ 订阅

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = (): StudioState => this.state;

  private emit(): void {
    for (const fn of this.listeners) fn();
  }

  private setDoc(doc: DirectorDoc, extra: Partial<StudioState> = {}): void {
    this.state = {
      ...this.state,
      doc,
      canUndo: this.past.length > 0,
      canRedo: this.future.length > 0,
      ...extra,
    };
    this.emit();
  }

  // ------------------------------------------------------------ 编辑

  /**
   * 提交一次编辑（记历史 + 存档）。
   * `coalesceKey`：同一个 key 的连续改动会合并成一条历史（拖滑杆、拖坐标用）。
   */
  commit(mutate: (doc: DirectorDoc) => DirectorDoc, coalesceKey?: string): void {
    const now = Date.now();
    const merge =
      coalesceKey !== undefined &&
      coalesceKey === this.lastKey &&
      now - this.lastAt < COALESCE_MS &&
      this.past.length > 0;

    if (!merge) {
      this.past.push(this.state.doc);
      if (this.past.length > HISTORY_LIMIT) this.past.shift();
    }
    this.future = [];
    this.lastKey = coalesceKey ?? null;
    this.lastAt = now;

    this.setDoc(mutate(this.state.doc));
    this.scheduleSave();
  }

  /** 拖动中的预览：改文档但不记历史（松手时再 commit 一次收口） */
  preview(mutate: (doc: DirectorDoc) => DirectorDoc): void {
    this.setDoc(mutate(this.state.doc));
  }

  /** 当前这一轮「预览 + 收口」用的合并键，由交互方提供 */
  markInteraction(key: string | null): void {
    this.lastKey = key;
    this.lastAt = Date.now();
  }

  undo(): void {
    const prev = this.past.pop();
    if (!prev) return;
    this.future.push(this.state.doc);
    this.lastKey = null;
    this.setDoc(prev);
    this.scheduleSave();
  }

  redo(): void {
    const next = this.future.pop();
    if (!next) return;
    this.past.push(this.state.doc);
    this.lastKey = null;
    this.setDoc(next);
    this.scheduleSave();
  }

  /** 整份换掉（导入 / 清空）。history=true 时把当前这份也记进历史，可撤销回去 */
  replaceDoc(doc: DirectorDoc, history = true): void {
    if (history) {
      this.past.push(this.state.doc);
      if (this.past.length > HISTORY_LIMIT) this.past.shift();
    }
    this.future = [];
    this.lastKey = null;
    this.setDoc(doc, { restored: true });
    this.scheduleSave();
  }

  // ------------------------------------------------------------ 界面态

  setSession(patch: Partial<SessionState>): void {
    this.state = { ...this.state, session: { ...this.state.session, ...patch } };
    this.emit();
  }

  select(id: string | null): void {
    if (this.state.session.selectedId === id) return;
    this.setSession({ selectedId: id });
  }

  selected(): DirectorObject | null {
    const id = this.state.session.selectedId;
    if (!id) return null;
    return this.state.doc.objects.find((o) => o.id === id) ?? null;
  }

  // ------------------------------------------------------------ 对象动作

  addObject(obj: DirectorObject): void {
    this.commit((doc) => ({ ...doc, objects: [...doc.objects, obj] }));
    this.select(obj.id);
  }

  removeObject(id: string): void {
    this.commit((doc) => {
      const objects = doc.objects.filter((o) => o.id !== id);
      const wasActive = doc.activeCameraId === id;
      return {
        ...doc,
        objects,
        // 删掉的正好是激活机位时，顺手把激活位挪到剩下的第一个机位
        activeCameraId: wasActive
          ? objects.find((o) => o.kind === "camera")?.id ?? null
          : doc.activeCameraId,
      };
    });
    if (this.state.session.selectedId === id) this.select(null);
  }

  duplicateObject(id: string): void {
    const src = this.state.doc.objects.find((o) => o.id === id);
    if (!src) return;
    const copy: DirectorObject = {
      ...structuredClone(src),
      id: `${src.kind}-${Date.now().toString(36)}copy`,
      name: `${src.name} 副本`,
      pos: { x: src.pos.x + 0.6, y: src.pos.y, z: src.pos.z + 0.6 },
    };
    this.commit((doc) => ({ ...doc, objects: [...doc.objects, copy] }));
    this.select(copy.id);
  }

  updateObject(id: string, patch: Partial<DirectorObject>, coalesceKey?: string): void {
    this.commit(
      (doc) => ({
        ...doc,
        objects: doc.objects.map((o) =>
          o.id === id ? ({ ...o, ...patch } as DirectorObject) : o,
        ),
      }),
      coalesceKey,
    );
  }

  updatePos(id: string, pos: Vec3, coalesceKey?: string): void {
    this.updateObject(id, { pos } as Partial<DirectorObject>, coalesceKey);
  }

  updatePose(id: string, patch: Partial<PoseState>, coalesceKey?: string): void {
    this.commit(
      (doc) => ({
        ...doc,
        objects: doc.objects.map((o) =>
          o.id === id && o.kind === "actor"
            ? { ...o, pose: { ...o.pose, ...patch } }
            : o,
        ),
      }),
      coalesceKey,
    );
  }

  updateScene(patch: Partial<DirectorDoc["scene"]>, coalesceKey?: string): void {
    this.commit((doc) => ({ ...doc, scene: { ...doc.scene, ...patch } }), coalesceKey);
  }

  updateLights(patch: Partial<DirectorDoc["lights"]>, coalesceKey?: string): void {
    this.commit((doc) => ({ ...doc, lights: { ...doc.lights, ...patch } }), coalesceKey);
  }

  setActiveCamera(id: string | null): void {
    this.commit((doc) => ({ ...doc, activeCameraId: id }));
  }

  /** 清空回空场景（保留历史，能撤销回来） */
  clear(): void {
    this.replaceDoc({ ...makeEmptyDoc(), version: DOC_VERSION });
    this.select(null);
  }

  /** 组件卸载时把还没落盘的存档立刻写掉，避免「拖完滑杆立刻切页」丢最后一次改动 */
  flush(): void {
    if (this.saveTimer !== null) {
      window.clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    prefs.directorScene.set(this.scope, this.state.doc);
  }

  private scheduleSave(): void {
    if (this.saveTimer !== null) window.clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => {
      this.saveTimer = null;
      prefs.directorScene.set(this.scope, this.state.doc);
    }, SAVE_DEBOUNCE_MS);
  }
}
