/**
 * 3D 导演台 · 外壳（懒加载入口）
 *
 * 这个组件是「唯一会 import three 的地方」的下游，所以它自己被 `React.lazy` 包着：
 * 首屏不该为一个多数人用不到的 3D 视口多下载 1MB。同理它只 import 两样东西——
 * 数据模型（纯数据）与视口（three）。别在这里顺手 import 别的大模块。
 *
 * 分工：
 *  - `store.ts`  管文档与撤销重做、存档；
 *  - `viewport.ts` 管 three 场景，只接受「文档 + 界面态」，回调里说「用户点了谁/拖到哪」；
 *  - 这里只做布局与把两边接起来。渲染循环、几何体、拾取都不在这。
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import "./director3d.css";
import {
  Box,
  Camera as CameraIcon,
  Eye,
  EyeOff,
  Lock,
  LockOpen,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Redo2,
  RotateCcw,
  Trash2,
  Undo2,
  User,
  X,
} from "lucide-react";
import { api } from "../api";
import { useToast } from "../components/Toast";
import { Spinner } from "../components/common";
import { SceneStore, type StudioState } from "./store";
import { StudioViewport } from "./viewport";
import { importDirectorDoc } from "./persist";
import {
  BODY_TYPES,
  CAMERA_PRESETS,
  COMPOSITION_OPTIONS,
  ENVIRONMENT_OPTIONS,
  JOINT_SLIDERS,
  POSE_PRESET_LABEL,
  PROP_SHAPE_LABEL,
  SHOT_RATIOS,
  SHOT_TYPE_OPTIONS,
  actorsOf,
  camerasOf,
  defaultPose,
  fovToFocalLength,
  makeActor,
  makeCameraFromPreset,
  makeProp,
  nextIndex,
  propsOf,
  type ActorObject,
  type BodyType,
  type CameraObject,
  type PosePreset,
  type PropObject,
  type PropShape,
  type Vec3,
} from "./types";

/** dataURL → File（上传到资产库需要 File） */
function dataUrlToFile(dataUrl: string, filename: string): File | null {
  const m = /^data:([^;]+);base64,(.*)$/.exec(dataUrl);
  if (!m) return null;
  try {
    const bin = atob(m[2]);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], filename, { type: m[1] });
  } catch {
    return null;
  }
}

function downloadJson(text: string, filename: string): void {
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const r2 = (v: number): number => Math.round(v * 100) / 100;

function Num({
  label,
  value,
  step = 0.1,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  min?: number;
  max?: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="d3d-field">
      <span>{label}</span>
      <input
        type="number"
        value={r2(value)}
        step={step}
        min={min}
        max={max}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isFinite(n)) onChange(n);
        }}
      />
    </label>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="d3d-slider" title={`${label}：${Math.round(value)}°`}>
      <span>{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={1}
        value={Math.round(value)}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <b>{Math.round(value)}°</b>
    </label>
  );
}

function VecFields({
  label,
  value,
  step = 0.1,
  onChange,
}: {
  label: string;
  value: Vec3;
  step?: number;
  onChange: (v: Vec3) => void;
}) {
  return (
    <div className="d3d-vec">
      <span className="d3d-vec-label">{label}</span>
      <div className="d3d-vec-row">
        <Num label="X" value={value.x} step={step} onChange={(x) => onChange({ ...value, x })} />
        <Num label="Y" value={value.y} step={step} onChange={(y) => onChange({ ...value, y })} />
        <Num label="Z" value={value.z} step={step} onChange={(z) => onChange({ ...value, z })} />
      </div>
    </div>
  );
}

interface Props {
  /** 存档作用域：项目 id 或 demo。换作用域时请用 key 强制重挂载 */
  scope: string;
  onClose: () => void;
}

export default function DirectorStudio3D({ scope, onClose }: Props) {
  const toast = useToast();
  const storeRef = useRef<SceneStore | null>(null);
  if (!storeRef.current) storeRef.current = new SceneStore(scope);
  const store = storeRef.current;

  const state: StudioState = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const vpRef = useRef<StudioViewport | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const [ratioKey, setRatioKey] = useState("16:9");
  const [scale, setScale] = useState(2);
  const [busy, setBusy] = useState<"" | "shot">("");
  const [presetKey, setPresetKey] = useState(CAMERA_PRESETS[0].key);
  /**
   * 两侧面板可收起，且**窄窗口默认收起**。
   *
   * 为什么必须给这个开关：两栏固定宽（216 + 268）在窗口窄时会把中间的 3D 视口挤成 0 宽，
   * 而视口是按容器像素算画布的——0 宽的容器渲染出来就是一片空白，看起来像「功能坏了」。
   * 与其让用户自己发现「窗口拉大就好了」，不如让视口永远优先拿到空间。
   */
  const [showLeft, setShowLeft] = useState(() => window.innerWidth >= 1100);
  const [showRight, setShowRight] = useState(() => window.innerWidth >= 1100);

  /**
   * 窄窗口下两个面板**互斥**（开一个自动关另一个）。
   * 它们是浮层，宽度加起来 484px；窄窗口的视口只有三百来像素，
   * 两个同时打开会把画面整个盖住（实测过一次，等于什么都看不见）。
   */
  const togglePanel = (side: "left" | "right") => {
    const narrow = window.innerWidth < 1100;
    if (side === "left") {
      const next = !showLeft;
      setShowLeft(next);
      if (next && narrow) setShowRight(false);
    } else {
      const next = !showRight;
      setShowRight(next);
      if (next && narrow) setShowLeft(false);
    }
  };

  const { doc, session } = state;
  const selected = useMemo(
    () => doc.objects.find((o) => o.id === session.selectedId) ?? null,
    [doc.objects, session.selectedId],
  );

  // ---- 视口：挂载一次，卸载时销毁（three 的上下文不销毁会一路漏显存）
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const vp = new StudioViewport(host, {
      onSelect: (id) => store.select(id),
      onDragPreview: (id, pos) => {
        // 拖动中只预览不记历史：一次拖动只该在撤销栈里留一条
        store.preview((d) => ({
          ...d,
          objects: d.objects.map((o) => (o.id === id ? { ...o, pos } : o)),
        }));
      },
      onDragCommit: (id, pos) => store.updatePos(id, pos),
    });
    vpRef.current = vp;
    return () => {
      vp.dispose();
      vpRef.current = null;
    };
    // store 在组件生命周期内是同一个实例
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- 状态变化推进场景
  useEffect(() => {
    vpRef.current?.sync(doc, session);
  }, [doc, session]);

  // ---- 卸载前把没落盘的存档写掉（拖完滑杆立刻切页不该丢最后一次改动）
  useEffect(() => {
    return () => store.flush();
  }, [store]);

  // ---- 快捷键：T 导演视角 / Y 机位视角 / Q 重置 / Delete 删除 / Ctrl+Z 撤销
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)
      ) {
        return;
      }
      const k = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && k === "z" && !e.shiftKey) {
        e.preventDefault();
        store.undo();
      } else if ((e.ctrlKey || e.metaKey) && (k === "y" || (k === "z" && e.shiftKey))) {
        e.preventDefault();
        store.redo();
      } else if (k === "t") {
        e.preventDefault();
        store.setSession({ viewMode: "director" });
      } else if (k === "y") {
        e.preventDefault();
        if (doc.activeCameraId) store.setSession({ viewMode: "camera" });
      } else if (k === "q") {
        e.preventDefault();
        vpRef.current?.resetView();
      } else if (e.key === "Delete" && selected && !selected.locked) {
        e.preventDefault();
        store.removeObject(selected.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doc.activeCameraId, selected, store]);

  // ---- 截图：先把像素读出来，再走已有的资产上传口子
  const takeShot = useCallback(async () => {
    const vp = vpRef.current;
    if (!vp) return;
    const ratio = SHOT_RATIOS.find((r) => r.key === ratioKey);
    const url = vp.capture({
      ratio: ratio && ratio.w > 0 ? { w: ratio.w, h: ratio.h } : undefined,
      scale,
    });
    if (!url) {
      toast.error("截图失败：取不到画面（WebGL 上下文可能不可用）");
      return;
    }
    const file = dataUrlToFile(url, `director3d-${Date.now()}.png`);
    if (!file) {
      toast.error("截图失败：图像数据解析异常");
      return;
    }
    setBusy("shot");
    try {
      const asset = await api.uploadAsset(file);
      toast.success(`已存入资产库：${asset.original_name}（可直接当首帧用）`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "上传失败");
    } finally {
      setBusy("");
    }
  }, [ratioKey, scale, toast]);

  /** 把当前视角存成一个机位（不用去算坐标，看好了直接存） */
  const saveViewAsCamera = useCallback(() => {
    const vp = vpRef.current;
    const view = vp?.view();
    if (!view) {
      toast.error("视点还没就绪，稍后再试");
      return;
    }
    const dist = 6;
    const lookAt: Vec3 = {
      x: view.position.x + view.direction.x * dist,
      y: view.position.y + view.direction.y * dist,
      z: view.position.z + view.direction.z * dist,
    };
    const cam = makeCameraFromPreset(
      nextIndex(doc, "camera"),
      {
        key: "custom",
        label: "当前视角",
        pos: view.position,
        lookAt,
        focalLength: Math.round(fovToFocalLength(view.fov)),
        shotType: "medium_shot",
        composition: "rule_of_thirds",
        roll: 0,
      },
    );
    store.addObject(cam);
    store.setActiveCamera(cam.id);
    toast.success(`已存成「${cam.name}」（${cam.focalLength}mm），已切到机位视角`);
  }, [doc, store, toast]);

  const onImportFile = useCallback(
    async (f: File) => {
      const text = await f.text();
      const imported = importDirectorDoc(text);
      if (!imported) {
        toast.error("导入失败：这不是一份能识别的 3D 场景文件");
        return;
      }
      store.replaceDoc(imported);
      toast.success(`已导入场景（${imported.objects.length} 个对象）`);
    },
    [store, toast],
  );

  const addActor = () => {
    const i = nextIndex(doc, "actor");
    // 新对象落在原点附近错开一点，避免叠在一起看不出来
    const spread = ((i - 1) % 5) * 1.1 - 2.2;
    store.addObject(makeActor(i, { x: spread, y: 0, z: 0 }));
  };

  const addProp = () => {
    const i = nextIndex(doc, "prop");
    const spread = ((i - 1) % 5) * 1.1 - 2.2;
    store.addObject(makeProp(i, { x: spread, y: 0, z: 1.6 }));
  };

  const addCameraPreset = () => {
    const preset = CAMERA_PRESETS.find((p) => p.key === presetKey) ?? CAMERA_PRESETS[0];
    const cam = makeCameraFromPreset(nextIndex(doc, "camera"), preset);
    store.addObject(cam);
    store.setActiveCamera(cam.id);
    toast.success(`已加入机位「${cam.name}」`);
  };

  const actors = actorsOf(doc);
  const props = propsOf(doc);
  const cameras = camerasOf(doc);

  return (
    <div className="d3d">
      {/* -------- 顶栏 -------- */}
      <header className="d3d-bar">
        <div className="d3d-brand">
          <span className="d3d-badge">3D</span>
          <b>3D 导演台</b>
          <span className="d3d-save-hint">自动存档到本机</span>
        </div>

        <div className="d3d-seg">
          <button
            type="button"
            className={session.viewMode === "director" ? "on" : ""}
            onClick={() => store.setSession({ viewMode: "director" })}
            title="导演视角（快捷键 T）"
          >
            导演视角
          </button>
          <button
            type="button"
            className={session.viewMode === "camera" ? "on" : ""}
            onClick={() => store.setSession({ viewMode: "camera" })}
            disabled={!doc.activeCameraId}
            title={doc.activeCameraId ? "机位视角（快捷键 Y）" : "先加一个机位"}
          >
            机位视角
          </button>
        </div>

        <button type="button" className="btn btn-ghost btn-sm" onClick={() => vpRef.current?.resetView()} title="重置视角（Q）">
          <RotateCcw size={13} /> 重置视角
        </button>

        <div className="d3d-bar-right">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => togglePanel("left")}
            title={showLeft ? "收起场景树" : "展开场景树"}
          >
            {showLeft ? <PanelLeftClose size={13} /> : <PanelLeftOpen size={13} />}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => togglePanel("right")}
            title={showRight ? "收起属性面板" : "展开属性面板"}
          >
            {showRight ? <PanelRightClose size={13} /> : <PanelRightOpen size={13} />}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => store.undo()} disabled={!state.canUndo} title="撤销（Ctrl+Z）">
            <Undo2 size={13} />
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => store.redo()} disabled={!state.canRedo} title="重做（Ctrl+Y）">
            <Redo2 size={13} />
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => downloadJson(JSON.stringify(doc, null, 2), `scene3d-${Date.now()}.json`)}
            title="把场景导出成 JSON（换台机器/发给人时用这个）"
          >
            导出
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => fileRef.current?.click()} title="导入之前导出的场景 JSON">
            导入
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              if (window.confirm("清空当前场景？（可以 Ctrl+Z 撤销回来）")) store.clear();
            }}
          >
            清空
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            <X size={13} /> 关闭
          </button>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) void onImportFile(f);
          }}
        />
      </header>

      {state.malformed && (
        <div className="d3d-warn">
          上次的场景存档读不出来，已重置为空场景（原始数据不符合当前版本的格式，已丢弃）。
        </div>
      )}

      {/* -------- 主体三栏 -------- */}
      <div className="d3d-body">
        <aside className={showLeft ? "d3d-left" : "d3d-left d3d-hide"}>
          <div className="d3d-sec">场景对象</div>
          <ul className="d3d-tree">
            {doc.objects.length === 0 && <li className="d3d-tree-empty">还没有对象，从下面加一个</li>}
            {[
              { kind: "actor" as const, list: actors, icon: <User size={13} />, label: "角色" },
              { kind: "prop" as const, list: props, icon: <Box size={13} />, label: "元素" },
              { kind: "camera" as const, list: cameras, icon: <CameraIcon size={13} />, label: "机位" },
            ].map((grp) =>
              grp.list.length === 0 ? null : (
                <li key={grp.kind} className="d3d-tree-group">
                  <div className="d3d-tree-head">
                    {grp.icon} {grp.label}（{grp.list.length}）
                  </div>
                  <ul>
                    {grp.list.map((o) => (
                      <li key={o.id}>
                        <div className={`d3d-tree-row${session.selectedId === o.id ? " on" : ""}`}>
                          <button type="button" className="d3d-tree-name" onClick={() => store.select(o.id)}>
                            {o.name}
                            {o.kind === "camera" && doc.activeCameraId === o.id ? <em>当前</em> : null}
                          </button>
                          <button
                            type="button"
                            className="d3d-icon"
                            title={o.visible ? "隐藏" : "显示"}
                            onClick={() => store.updateObject(o.id, { visible: !o.visible })}
                          >
                            {o.visible ? <Eye size={12} /> : <EyeOff size={12} />}
                          </button>
                          <button
                            type="button"
                            className="d3d-icon"
                            title={o.locked ? "解锁（解锁后才能拖动）" : "锁定（锁定后不会被误拖）"}
                            onClick={() => store.updateObject(o.id, { locked: !o.locked })}
                          >
                            {o.locked ? <Lock size={12} /> : <LockOpen size={12} />}
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                </li>
              ),
            )}
          </ul>

          <div className="d3d-add">
            <button type="button" className="btn btn-ghost btn-sm" onClick={addActor}>
              <User size={13} /> 加角色
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={addProp}>
              <Box size={13} /> 加元素
            </button>
            <div className="d3d-add-cam">
              <select value={presetKey} onChange={(e) => setPresetKey(e.target.value)}>
                {CAMERA_PRESETS.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.label}
                  </option>
                ))}
              </select>
              <button type="button" className="btn btn-ghost btn-sm" onClick={addCameraPreset}>
                <CameraIcon size={13} /> 按预设加机位
              </button>
            </div>
          </div>
        </aside>

        <main className="d3d-stage">
          <div ref={hostRef} className="d3d-viewport" />

          <div className="d3d-overlay d3d-overlay-tl">
            {(
              [
                { key: "showGrid", label: "网格" },
                { key: "showFrustum", label: "取景框" },
                { key: "showGuide", label: "构图线" },
                { key: "showLights", label: "灯光位" },
              ] as const
            ).map((t) => (
              <button
                key={t.key}
                type="button"
                className={session[t.key] ? "on" : ""}
                onClick={() => store.setSession({ [t.key]: !session[t.key] })}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="d3d-overlay d3d-overlay-tr">
            角色 {actors.length} · 元素 {props.length} · 机位 {cameras.length}
          </div>

          <div className="d3d-shotbar">
            <span>出图比例</span>
            <select value={ratioKey} onChange={(e) => setRatioKey(e.target.value)}>
              {SHOT_RATIOS.map((r) => (
                <option key={r.key} value={r.key}>
                  {r.label}
                </option>
              ))}
            </select>
            <span>倍率</span>
            <select value={scale} onChange={(e) => setScale(Number(e.target.value))}>
              {[1, 2, 3, 4].map((s) => (
                <option key={s} value={s}>
                  {s}x
                </option>
              ))}
            </select>
            <button type="button" className="btn btn-ghost btn-sm" onClick={saveViewAsCamera}>
              存成机位
            </button>
            <button type="button" className="btn btn-primary btn-sm" disabled={busy !== ""} onClick={() => void takeShot()}>
              {busy === "shot" ? <Spinner light /> : null} 截图存到资产库
            </button>
          </div>
        </main>

        <aside className={showRight ? "d3d-right" : "d3d-right d3d-hide"}>
          {selected ? (
            <>
              <div className="d3d-sec">
                {selected.kind === "actor" ? "角色属性" : selected.kind === "prop" ? "元素属性" : "机位属性"}
              </div>
              <label className="d3d-field">
                <span>名称</span>
                <input
                  type="text"
                  value={selected.name}
                  onChange={(e) => store.updateObject(selected.id, { name: e.target.value }, `name:${selected.id}`)}
                />
              </label>

              {selected.kind === "actor" && <ActorFields actor={selected} store={store} />}
              {selected.kind === "prop" && <PropFields prop={selected} store={store} />}
              {selected.kind === "camera" && <CameraFields cam={selected} store={store} doc={doc} />}

              <div className="d3d-sec">变换</div>
              <VecFields
                label="位置"
                value={selected.pos}
                onChange={(pos) => store.updatePos(selected.id, pos, `pos:${selected.id}`)}
              />
              <Num
                label="朝向 Y°"
                value={selected.rotY}
                step={5}
                onChange={(rotY) => store.updateObject(selected.id, { rotY }, `rot:${selected.id}`)}
              />
              <Num
                label="缩放"
                value={selected.scale}
                step={0.05}
                min={0.05}
                max={20}
                onChange={(scale) => store.updateObject(selected.id, { scale }, `scale:${selected.id}`)}
              />

              <div className="d3d-row">
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => store.duplicateObject(selected.id)}>
                  复制一个
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => store.removeObject(selected.id)}
                >
                  <Trash2 size={13} /> 删除
                </button>
              </div>
            </>
          ) : null}

          <div className="d3d-sec">场景与灯光</div>
          <SceneFields state={state} store={store} />
        </aside>
      </div>

      <footer className="d3d-foot">
        <span>T 导演视角</span>
        <span>Y 机位视角</span>
        <span>Q 重置视角</span>
        <span>拖动对象在地面移动（锁定的对象不动）</span>
        <span>Delete 删除选中</span>
        <span className="d3d-foot-right">只做 3D 空间编辑与截图出图，不触发生成、不扣任何额度</span>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------- 属性分块

function SceneFields({ state, store }: { state: StudioState; store: SceneStore }) {
  const { doc } = state;
  return (
    <>
      <label className="d3d-field">
        <span>环境</span>
        <select
          value={doc.scene.environment}
          onChange={(e) => store.updateScene({ environment: e.target.value })}
        >
          {ENVIRONMENT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <VecFields label="世界尺寸" value={doc.scene.worldSize} step={1} onChange={(worldSize) => store.updateScene({ worldSize }, "world")} />
      <label className="d3d-slider">
        <span>地面不透明度</span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={doc.scene.groundOpacity}
          onChange={(e) => store.updateScene({ groundOpacity: Number(e.target.value) }, "groundOpacity")}
        />
        <b>{doc.scene.groundOpacity.toFixed(2)}</b>
      </label>
      <label className="d3d-slider">
        <span>环境光</span>
        <input
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={doc.lights.ambientIntensity}
          onChange={(e) => store.updateLights({ ambientIntensity: Number(e.target.value) }, "ambient")}
        />
        <b>{doc.lights.ambientIntensity.toFixed(2)}</b>
      </label>
      {(
        [
          { slot: "key" as const, label: "主光" },
          { slot: "fill" as const, label: "辅光" },
          { slot: "rim" as const, label: "轮廓光" },
        ]
      ).map(({ slot, label }) => (
        <div key={slot} className="d3d-light">
          <div className="d3d-light-head">{label}</div>
          <Slider
            label="强度"
            value={doc.lights[slot].intensity}
            min={0}
            max={4}
            onChange={(v) => store.updateLights({ [slot]: { ...doc.lights[slot], intensity: v } }, `light:${slot}`)}
          />
          <Num
            label="色温 K"
            value={doc.lights[slot].colorTemperature}
            step={100}
            min={1800}
            max={12000}
            onChange={(colorTemperature) =>
              store.updateLights({ [slot]: { ...doc.lights[slot], colorTemperature } }, `kelvin:${slot}`)
            }
          />
        </div>
      ))}
    </>
  );
}

function ActorFields({ actor, store }: { actor: ActorObject; store: SceneStore }) {
  return (
    <>
      <label className="d3d-field">
        <span>体型</span>
        <select
          value={actor.bodyType}
          onChange={(e) => {
            const bodyType = e.target.value as BodyType;
            // 换体型同时把身高带到该体型的参考值：只换比例不改身高会出现「儿童 1.8 米」
            store.updateObject(actor.id, { bodyType, height: BODY_TYPES[bodyType].height });
          }}
        >
          {(Object.keys(BODY_TYPES) as BodyType[]).map((k) => (
            <option key={k} value={k}>
              {BODY_TYPES[k].label}
            </option>
          ))}
        </select>
      </label>
      <Num
        label="身高 m"
        value={actor.height}
        step={0.05}
        min={0.3}
        max={4}
        onChange={(height) => store.updateObject(actor.id, { height }, `height:${actor.id}`)}
      />
      <label className="d3d-field">
        <span>颜色</span>
        <input
          type="color"
          value={actor.color}
          onChange={(e) => store.updateObject(actor.id, { color: e.target.value }, `color:${actor.id}`)}
        />
      </label>
      <label className="d3d-field">
        <span>服装说明</span>
        <input
          type="text"
          placeholder="只记录文字，供出图提示词参考"
          value={actor.costume}
          onChange={(e) => store.updateObject(actor.id, { costume: e.target.value }, `costume:${actor.id}`)}
        />
      </label>
      <label className="d3d-field">
        <span>姿态预设</span>
        <select
          value={actor.pose.preset}
          onChange={(e) => {
            const preset = e.target.value as PosePreset;
            // 选预设 = 整体套上那套角度；custom 只是「手动调过」的标记，不套
            store.updatePose(actor.id, preset === "custom" ? { preset } : defaultPose(preset));
          }}
        >
          {(Object.keys(POSE_PRESET_LABEL) as PosePreset[]).map((k) => (
            <option key={k} value={k}>
              {POSE_PRESET_LABEL[k]}
            </option>
          ))}
        </select>
      </label>
      <div className="d3d-sec">关节（拖动即改，整段算一次撤销）</div>
      {JOINT_SLIDERS.map((j) => (
        <Slider
          key={j.key}
          label={j.label}
          value={actor.pose[j.key]}
          min={j.min}
          max={j.max}
          onChange={(v) =>
            // 手动调过就标记 custom：预设下拉不该再显示成「站立」
            store.updatePose(actor.id, { [j.key]: v, preset: "custom" }, `joint:${actor.id}:${j.key}`)
          }
        />
      ))}
    </>
  );
}

function PropFields({ prop, store }: { prop: PropObject; store: SceneStore }) {
  return (
    <>
      <label className="d3d-field">
        <span>形状</span>
        <select
          value={prop.shape}
          onChange={(e) => store.updateObject(prop.id, { shape: e.target.value as PropShape })}
        >
          {(Object.keys(PROP_SHAPE_LABEL) as PropShape[]).map((k) => (
            <option key={k} value={k}>
              {PROP_SHAPE_LABEL[k]}
            </option>
          ))}
        </select>
      </label>
      <VecFields label="尺寸" value={prop.size} onChange={(size) => store.updateObject(prop.id, { size }, `size:${prop.id}`)} />
      <label className="d3d-field">
        <span>颜色</span>
        <input
          type="color"
          value={prop.color}
          onChange={(e) => store.updateObject(prop.id, { color: e.target.value }, `color:${prop.id}`)}
        />
      </label>
    </>
  );
}

function CameraFields({ cam, store, doc }: { cam: CameraObject; store: SceneStore; doc: StudioState["doc"] }) {
  return (
    <>
      <Num
        label="焦段 mm"
        value={cam.focalLength}
        step={1}
        min={8}
        max={300}
        onChange={(focalLength) => store.updateObject(cam.id, { focalLength }, `focal:${cam.id}`)}
      />
      <label className="d3d-field">
        <span>景别</span>
        <select value={cam.shotType} onChange={(e) => store.updateObject(cam.id, { shotType: e.target.value })}>
          {SHOT_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <label className="d3d-field">
        <span>构图</span>
        <select value={cam.composition} onChange={(e) => store.updateObject(cam.id, { composition: e.target.value })}>
          {COMPOSITION_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <Num
        label="荷兰角°"
        value={cam.roll}
        step={5}
        min={-45}
        max={45}
        onChange={(roll) => store.updateObject(cam.id, { roll }, `roll:${cam.id}`)}
      />
      <VecFields
        label="注视点"
        value={cam.lookAt}
        onChange={(lookAt) => store.updateObject(cam.id, { lookAt }, `lookAt:${cam.id}`)}
      />
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        disabled={doc.activeCameraId === cam.id}
        onClick={() => store.setActiveCamera(cam.id)}
      >
        {doc.activeCameraId === cam.id ? "已是当前机位" : "设为当前机位"}
      </button>
    </>
  );
}
