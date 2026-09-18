/**
 * 3D 场景文档的序列化与**严格校验**。
 *
 * 这一份刻意不依赖 three：存档要经过 `prefs.ts`（前端 localStorage 的唯一出口），
 * 而 `prefs.ts` 是被首屏加载的，不能因此把 1MB 的 three 拖进主包。
 * 所以这里只有纯数据进出。
 *
 * 为什么读回来要**逐字段校验**而不是 `JSON.parse as DirectorDoc`：
 * 存档是「用户能手动改、旧版本写过、跨版本会残留」的地方（和偏好一个道理）。
 * 少一个字段的后果不是报错，而是场景直接渲染不出来或者某个滑杆显示 NaN——
 * 排查起来完全看不出是存档的问题。所以不合格就整份丢掉、退回空场景，
 * 并在调用侧给一句提示，而不是让半份脏数据流进渲染器。
 */

import {
  ACTOR_COLORS,
  BODY_TYPES,
  DOC_VERSION,
  JOINT_SLIDERS,
  POSE_PRESET_LABEL,
  PROP_SHAPE_LABEL,
  defaultPose,
  makeEmptyDoc,
  v3,
  type ActorObject,
  type BodyType,
  type CameraObject,
  type DirectorDoc,
  type DirectorObject,
  type LightSlotState,
  type PosePreset,
  type PropObject,
  type PropShape,
  type SceneState,
  type Vec3,
} from "./types";

// ---------------------------------------------------------------- 取值小工具

function num(v: unknown, fallback: number): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

/** 数值 + 范围夹取：滑杆越界的存档不该让模型飞到天上去 */
function clampNum(v: unknown, min: number, max: number, fallback: number): number {
  return Math.min(max, Math.max(min, num(v, fallback)));
}

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

function bool(v: unknown, fallback: boolean): boolean {
  return typeof v === "boolean" ? v : fallback;
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[], fallback: T): T {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : fallback;
}

function vec(v: unknown, fallback: Vec3): Vec3 {
  if (!v || typeof v !== "object") return { ...fallback };
  const o = v as Record<string, unknown>;
  return { x: num(o.x, fallback.x), y: num(o.y, fallback.y), z: num(o.z, fallback.z) };
}

// ---------------------------------------------------------------- 各块

function parseScene(v: unknown): SceneState {
  const base = makeEmptyDoc().scene;
  if (!v || typeof v !== "object") return base;
  const o = v as Record<string, unknown>;
  return {
    // 场景尺寸给个下限：0.5 米的世界连一个角色都放不下
    worldSize: {
      x: clampNum((o.worldSize as Vec3 | undefined)?.x, 2, 200, base.worldSize.x),
      y: clampNum((o.worldSize as Vec3 | undefined)?.y, 2, 100, base.worldSize.y),
      z: clampNum((o.worldSize as Vec3 | undefined)?.z, 2, 200, base.worldSize.z),
    },
    groundType: str(o.groundType, base.groundType),
    environment: oneOf(
      o.environment,
      ["studio", "day", "night", "interior"] as const,
      base.environment as "studio",
    ),
    groundOpacity: clampNum(o.groundOpacity, 0, 1, base.groundOpacity),
  };
}

function parseLightSlot(v: unknown, fallback: LightSlotState): LightSlotState {
  if (!v || typeof v !== "object") return { ...fallback, pos: { ...fallback.pos } };
  const o = v as Record<string, unknown>;
  return {
    pos: vec(o.pos, fallback.pos),
    intensity: clampNum(o.intensity, 0, 6, fallback.intensity),
    colorTemperature: clampNum(o.colorTemperature, 1800, 12000, fallback.colorTemperature),
  };
}

function parseLights(v: unknown) {
  const base = makeEmptyDoc().lights;
  if (!v || typeof v !== "object") return base;
  const o = v as Record<string, unknown>;
  return {
    key: parseLightSlot(o.key, base.key),
    fill: parseLightSlot(o.fill, base.fill),
    rim: parseLightSlot(o.rim, base.rim),
    ambientIntensity: clampNum(o.ambientIntensity, 0, 3, base.ambientIntensity),
  };
}

const POSE_PRESETS = Object.keys(POSE_PRESET_LABEL) as PosePreset[];
const BODY_TYPE_KEYS = Object.keys(BODY_TYPES) as BodyType[];
const PROP_SHAPE_KEYS = Object.keys(PROP_SHAPE_LABEL) as PropShape[];

/** 关节名 → 滑杆范围（校验时逐关节夹取，保证存回来的值界面表示得出来） */
const JOINT_RANGE: Record<string, { min: number; max: number }> = Object.fromEntries(
  JOINT_SLIDERS.map((j) => [j.key as string, { min: j.min, max: j.max }]),
);

function baseCommon(o: Record<string, unknown>, id: string, kind: DirectorObject["kind"]) {
  return {
    id,
    kind,
    name: str(o.name, id),
    visible: bool(o.visible, true),
    locked: bool(o.locked, false),
    groupId: typeof o.groupId === "string" ? o.groupId : null,
    pos: vec(o.pos, v3(0, 0, 0)),
    rotY: num(o.rotY, 0),
    scale: clampNum(o.scale, 0.05, 20, 1),
  };
}

function parseActor(o: Record<string, unknown>, id: string): ActorObject {
  const bodyType = oneOf(o.bodyType, BODY_TYPE_KEYS, "standard_male");
  const preset = oneOf((o.pose as Record<string, unknown> | undefined)?.preset, POSE_PRESETS, "stand");
  const pose = defaultPose(preset === "custom" ? "stand" : preset);
  const rawPose = (o.pose ?? {}) as Record<string, unknown>;
  // 18 个关节逐个夹到**滑杆自己的范围**里：存档里的值必须能被界面表示出来，
  // 否则会出现「读回来 200°，滑杆显示 170°，一碰滑杆就跳」这种别扭状态
  const angles = { ...pose };
  for (const k of Object.keys(angles) as (keyof typeof angles)[]) {
    if (k === "preset") continue;
    const spec = JOINT_RANGE[k];
    if (!spec) continue;
    angles[k] = clampNum(rawPose[k], spec.min, spec.max, angles[k]);
  }
  return {
    ...baseCommon(o, id, "actor"),
    kind: "actor",
    height: clampNum(o.height, 0.3, 4, BODY_TYPES[bodyType].height),
    bodyType,
    color: str(o.color, ACTOR_COLORS[0]),
    costume: str(o.costume, ""),
    pose: { ...angles, preset: o.pose && typeof o.pose === "object" ? preset : "stand" },
  };
}

function parseProp(o: Record<string, unknown>, id: string): PropObject {
  const size = vec(o.size, v3(0.8, 0.8, 0.8));
  return {
    ...baseCommon(o, id, "prop"),
    kind: "prop",
    shape: oneOf(o.shape, PROP_SHAPE_KEYS, "box"),
    size: {
      x: clampNum(size.x, 0.02, 50, 0.8),
      y: clampNum(size.y, 0.02, 50, 0.8),
      z: clampNum(size.z, 0.02, 50, 0.8),
    },
    color: str(o.color, "#8a93a0"),
    };
}

function parseCamera(o: Record<string, unknown>, id: string): CameraObject {
  return {
    ...baseCommon(o, id, "camera"),
    kind: "camera",
    lookAt: vec(o.lookAt, v3(0, 1, 0)),
    focalLength: clampNum(o.focalLength, 8, 300, 35),
    composition: str(o.composition, "rule_of_thirds"),
    shotType: str(o.shotType, "medium_shot"),
    roll: clampNum(o.roll, -45, 45, 0),
    // 指向哪个对象要等所有对象都解析完才知道，这一步先只判类型（见 parseDirectorDoc 里的收口）
    trackTargetId: typeof o.trackTargetId === "string" ? o.trackTargetId : null,
  };
}

function parseObject(v: unknown): DirectorObject | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const id = str(o.id);
  if (!id) return null; // 没有 id 的对象没法选中/删除，直接丢
  if (o.kind === "actor") return parseActor(o, id);
  if (o.kind === "prop") return parseProp(o, id);
  if (o.kind === "camera") return parseCamera(o, id);
  return null;
}

// ---------------------------------------------------------------- 出口

/** 存档字符串 → 文档；不合格返回 null（调用方退回空场景并提示） */
export function parseDirectorDoc(raw: string): DirectorDoc | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const o = data as Record<string, unknown>;
  // 版本不认就整份丢掉：宁可回到空场景，也不要用错版本的结构去渲染
  if (num(o.version, -1) !== DOC_VERSION) return null;
  if (!Array.isArray(o.objects)) return null;

  const objects = o.objects.map(parseObject).filter((x): x is DirectorObject => x !== null);
  const ids = new Set(objects.map((x) => x.id));
  const groups = Array.isArray(o.groups)
    ? o.groups
        .map((g) => {
          if (!g || typeof g !== "object") return null;
          const go = g as Record<string, unknown>;
          const gid = str(go.id);
          return gid ? { id: gid, name: str(go.name, gid) } : null;
        })
        .filter((g): g is { id: string; name: string } => g !== null)
    : [];

  // 激活机位指向一个已经不在的对象时退回第一个机位，避免「机位视角」按钮留着但点不动
  const activeRaw = typeof o.activeCameraId === "string" ? o.activeCameraId : null;
  const firstCam = objects.find((x) => x.kind === "camera") ?? null;
  const activeCameraId =
    activeRaw && ids.has(activeRaw) ? activeRaw : firstCam ? firstCam.id : null;

  // 机位的「跟随目标」同理：只能指向角色或元素。悬空引用留着不会报错，
  // 只会在渲染时静默失效——那种问题查起来最费劲，所以在这里一次性清干净。
  const followable = new Set(objects.filter((x) => x.kind !== "camera").map((x) => x.id));
  const cleaned = objects.map((x) =>
    x.kind === "camera" && x.trackTargetId && !followable.has(x.trackTargetId)
      ? { ...x, trackTargetId: null }
      : x,
  );

  return {
    version: DOC_VERSION,
    scene: parseScene(o.scene),
    lights: parseLights(o.lights),
    objects: cleaned,
    groups,
    activeCameraId,
  };
}

export function serializeDirectorDoc(doc: DirectorDoc): string {
  return JSON.stringify(doc);
}

/**
 * 导入用：接受「存档字符串」或「已经解析好的对象」（用户可能直接粘一段 JSON）。
 * 这里刻意复用同一套校验，不给导入开后门。
 */
export function importDirectorDoc(text: string): DirectorDoc | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  return parseDirectorDoc(trimmed);
}
