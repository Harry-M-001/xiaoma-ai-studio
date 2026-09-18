/**
 * 3D 导演台 · 数据模型
 *
 * 这一份是**可移植的最有价值的部分**：导演台的价值在于「场景描述」这套结构，
 * 而不是某个具体渲染器。所以先把结构定下来，渲染器（three.js）只负责把它画出来。
 *
 * 沿用参考实现（dola-v2 的 DirectorStudio）里被验证过的几条约定：
 *  - 角度**一律用度**：存库、面板、序列化都是度，只有送进 three 的那一刻才转弧度。
 *    混用两套单位是这类编辑器最常见的隐性 bug（改一个滑杆发现模型歪了 57 倍）。
 *  - 语义键 scene / actor / light / camera：一条场景要么整份存，要么整份读，
 *    不做「字段级补丁」，避免出现「读了半份旧数据」的中间态。
 *  - 对象只有三种 kind：actor（素模角色）/ prop（几何元素）/ camera（机位）。
 *    角色与元素的区别只在「有没有骨架与姿态」，渲染与拾取路径完全一致。
 *
 * v1 相对参考实现（dola-v2 的 DirectorStudio）**没有做**的部分，逐条列清（不是忘了）：
 *  - **画布节点形态**：参考实现里一个 3D 场景挂在画布节点上、随项目走；本项目是「导演台页的
 *    第二个页签 + 本机存档」，场景不进项目文件（换机器用导出/导入 JSON）。这是**架构差异**，
 *    不是缺个按钮——要改成节点形态得同时动注册表、节点 schema 与运行链。
 *  - 本地上传 GLB/GLTF 模型：参考实现用 `URL.createObjectURL` 拿 blob 地址，**只在本次会话有效**，
 *    序列化时要剔除地址只留文件名（所以重开显示「模型未加载·请重新上传」，不用占位几何体冒充成功）。
 *    这一整套 blob 生命周期管理没做，所以 `PropShape` 里没有 `model`。
 *  - 空对象占位（参考实现 `PropShape` 里的 `null`：小八面体线框，可选中可定位、无实体视觉）。
 *  - 群众阵列：一次按 行列数 × 间距 × 体型 铺 N×M 个人（参考实现上限 20×20）。
 *  - 对象分组（打组/解组/重命名）：`groupId` 与 `groups` 都**只在数据结构里留着**，界面没做。
 *  - 机位跟随目标：字段留着（`trackTargetId`），界面没做。
 * 留字段不留界面是为了「以后加功能不用迁移老数据」，也为了**导入参考实现导出的场景时这层信息不丢**。
 */

/** 三维向量。three 里到处是这个形状，但**不进渲染层**——数据层不该依赖 three */
export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

export const v3 = (x: number, y: number, z: number): Vec3 => ({ x, y, z });

// ---------------------------------------------------------------- 枚举

export type ObjectKind = "actor" | "prop" | "camera";

/** 姿态预设；custom = 用户手动调过关节角、已经不属于任何预设 */
export type PosePreset =
  | "stand"
  | "tpose"
  | "sit"
  | "walk"
  | "run"
  | "wave"
  | "point"
  | "crouch"
  | "custom";

export type PropShape = "box" | "sphere" | "cylinder" | "cone" | "plane" | "torus" | "pyramid";

/** 体型预设：决定素模的身高与横向比例 */
export type BodyType =
  | "standard_male"
  | "standard_female"
  | "muscular"
  | "slim"
  | "teen"
  | "child"
  | "broad"
  | "chibi";

// ---------------------------------------------------------------- 对象

export interface BaseObject {
  id: string;
  kind: ObjectKind;
  name: string;
  visible: boolean;
  locked: boolean;
  /** 分组 id（v1 界面未开放，字段先留着） */
  groupId: string | null;
  pos: Vec3;
  /** 绕 Y 轴朝向，**单位：度** */
  rotY: number;
  /** 等比缩放 */
  scale: number;
}

/**
 * 关节级姿态：躯干 2 + 肩髋各 3 自由度 × 左右 + 肘膝各 1 × 左右 = 18 个角度。
 *
 * 为什么不给整只手/整条腿一个角度：预演里最常调的就是「一只手抬起来一点点」和
 * 「身体侧过来一点」，只有拆到肩的前举/外展/扭转三个独立轴才调得出自然的姿势。
 */
export interface PoseState {
  preset: PosePreset;
  headPitch: number;
  torsoTwist: number;
  shoulderL_raise: number;
  shoulderL_abduct: number;
  shoulderL_twist: number;
  shoulderR_raise: number;
  shoulderR_abduct: number;
  shoulderR_twist: number;
  elbowL: number;
  elbowR: number;
  hipL_raise: number;
  hipL_abduct: number;
  hipL_twist: number;
  hipR_raise: number;
  hipR_abduct: number;
  hipR_twist: number;
  kneeL: number;
  kneeR: number;
}

export type PoseAngles = Omit<PoseState, "preset">;

export interface ActorObject extends BaseObject {
  kind: "actor";
  height: number;
  bodyType: BodyType;
  color: string;
  /** 服装说明（只存文字，v1 不做服装建模） */
  costume: string;
  pose: PoseState;
}

export interface PropObject extends BaseObject {
  kind: "prop";
  shape: PropShape;
  size: Vec3;
  color: string;
}

export interface CameraObject extends BaseObject {
  kind: "camera";
  lookAt: Vec3;
  /** 焦段 mm */
  focalLength: number;
  composition: string;
  shotType: string;
  /** 荷兰角倾斜，度。0 = 水平 */
  roll: number;
  /**
   * 跟随目标（角色/元素），v1 界面未开放、字段先留着。
   * 校验时若指向一个不存在的对象会被清成 null——悬空引用留在存档里只会在渲染时静默失效。
   */
  trackTargetId: string | null;
}

export type DirectorObject = ActorObject | PropObject | CameraObject;

export interface DirectorGroup {
  id: string;
  name: string;
}

// ---------------------------------------------------------------- 场景与灯光

export interface SceneState {
  worldSize: Vec3;
  groundType: string;
  environment: string;
  /** 地面不透明度 0~1；做合成素材时希望能透出底图 */
  groundOpacity: number;
}

export interface LightSlotState {
  pos: Vec3;
  intensity: number;
  /** 色温 K */
  colorTemperature: number;
}

export interface LightState {
  key: LightSlotState;
  fill: LightSlotState;
  rim: LightSlotState;
  ambientIntensity: number;
}

export interface DirectorDoc {
  version: 1;
  scene: SceneState;
  lights: LightState;
  objects: DirectorObject[];
  groups: DirectorGroup[];
  activeCameraId: string | null;
}

// ---------------------------------------------------------------- 常量表

export const DOC_VERSION = 1 as const;
export const DIRECTOR_DATA_KEY = "director3d";

export const ACTOR_COLORS = ["#5b8def", "#4fca8f", "#f0a13a", "#e55f8d", "#9a6ef0", "#3fc8e8"];
export const PROP_COLORS = ["#8a93a0", "#c9a35f", "#7f6b55", "#5fa3c9"];

export const PROP_SHAPE_LABEL: Record<PropShape, string> = {
  box: "立方体",
  sphere: "球体",
  cylinder: "圆柱",
  cone: "圆锥",
  plane: "平面",
  torus: "环体",
  pyramid: "棱锥",
};

export const BODY_TYPES: Record<BodyType, { label: string; height: number; width: number }> = {
  standard_male: { label: "标准男", height: 1.75, width: 1.0 },
  standard_female: { label: "标准女", height: 1.65, width: 0.9 },
  muscular: { label: "健硕", height: 1.8, width: 1.18 },
  slim: { label: "纤细", height: 1.7, width: 0.8 },
  teen: { label: "少年", height: 1.5, width: 0.8 },
  child: { label: "儿童", height: 1.2, width: 0.72 },
  broad: { label: "宽厚", height: 1.75, width: 1.28 },
  chibi: { label: "二头身", height: 1.0, width: 0.85 },
};

export const POSE_PRESET_LABEL: Record<PosePreset, string> = {
  stand: "站立",
  tpose: "T 型",
  sit: "坐姿",
  walk: "行走",
  run: "奔跑",
  wave: "挥手",
  point: "指向",
  crouch: "蹲下",
  custom: "自定义",
};

/** 18 个关节的显示名与取值范围（属性面板滑杆直接用这张表） */
export const JOINT_SLIDERS: {
  key: keyof PoseAngles;
  label: string;
  min: number;
  max: number;
}[] = [
  { key: "headPitch", label: "头部俯仰", min: -45, max: 45 },
  { key: "torsoTwist", label: "躯干扭转", min: -60, max: 60 },
  { key: "shoulderL_raise", label: "左肩前举", min: -60, max: 170 },
  { key: "shoulderL_abduct", label: "左肩外展", min: -20, max: 170 },
  { key: "shoulderL_twist", label: "左肩扭转", min: -90, max: 90 },
  { key: "shoulderR_raise", label: "右肩前举", min: -60, max: 170 },
  { key: "shoulderR_abduct", label: "右肩外展", min: -20, max: 170 },
  { key: "shoulderR_twist", label: "右肩扭转", min: -90, max: 90 },
  { key: "elbowL", label: "左肘弯曲", min: 0, max: 150 },
  { key: "elbowR", label: "右肘弯曲", min: 0, max: 150 },
  { key: "hipL_raise", label: "左髋前抬", min: -45, max: 120 },
  { key: "hipL_abduct", label: "左髋外展", min: -30, max: 90 },
  { key: "hipL_twist", label: "左髋扭转", min: -60, max: 60 },
  { key: "hipR_raise", label: "右髋前抬", min: -45, max: 120 },
  { key: "hipR_abduct", label: "右髋外展", min: -30, max: 90 },
  { key: "hipR_twist", label: "右髋扭转", min: -60, max: 60 },
  { key: "kneeL", label: "左膝弯曲", min: 0, max: 150 },
  { key: "kneeR", label: "右膝弯曲", min: 0, max: 150 },
];

const ZERO_ANGLES: PoseAngles = {
  headPitch: 0,
  torsoTwist: 0,
  shoulderL_raise: 0,
  shoulderL_abduct: 0,
  shoulderL_twist: 0,
  shoulderR_raise: 0,
  shoulderR_abduct: 0,
  shoulderR_twist: 0,
  elbowL: 0,
  elbowR: 0,
  hipL_raise: 0,
  hipL_abduct: 0,
  hipL_twist: 0,
  hipR_raise: 0,
  hipR_abduct: 0,
  hipR_twist: 0,
  kneeL: 0,
  kneeR: 0,
};

/** 姿态预设 → 18 个角度（度）。custom 不在表内，取站立为底 */
export const POSE_PRESET_ANGLES: Record<Exclude<PosePreset, "custom">, PoseAngles> = {
  stand: { ...ZERO_ANGLES },
  tpose: { ...ZERO_ANGLES, shoulderL_abduct: 90, shoulderR_abduct: 90 },
  sit: {
    ...ZERO_ANGLES,
    headPitch: 5,
    shoulderL_raise: 25,
    shoulderR_raise: 25,
    elbowL: 20,
    elbowR: 20,
    hipL_raise: 85,
    hipR_raise: 85,
    kneeL: 85,
    kneeR: 85,
  },
  walk: {
    ...ZERO_ANGLES,
    torsoTwist: 6,
    shoulderL_raise: 25,
    shoulderR_raise: -25,
    elbowL: 25,
    elbowR: 25,
    hipL_raise: 22,
    hipR_raise: -22,
    kneeL: 15,
    kneeR: 10,
  },
  run: {
    ...ZERO_ANGLES,
    headPitch: -8,
    torsoTwist: 12,
    shoulderL_raise: 55,
    shoulderR_raise: -55,
    elbowL: 60,
    elbowR: 60,
    hipL_raise: 45,
    hipR_raise: -45,
    kneeL: 60,
    kneeR: 35,
  },
  wave: {
    ...ZERO_ANGLES,
    headPitch: -5,
    torsoTwist: -8,
    shoulderR_raise: 140,
    shoulderR_abduct: 20,
  },
  point: { ...ZERO_ANGLES, torsoTwist: -10, shoulderR_raise: 85 },
  crouch: {
    ...ZERO_ANGLES,
    headPitch: 8,
    shoulderL_raise: -20,
    shoulderR_raise: -20,
    elbowL: 40,
    elbowR: 40,
    hipL_raise: 70,
    hipR_raise: 70,
    kneeL: 140,
    kneeR: 140,
  },
};

/** 出图比例（含「自适应」= 与视口一致） */
export const SHOT_RATIOS: { key: string; label: string; w: number; h: number }[] = [
  { key: "auto", label: "自适应", w: 0, h: 0 },
  { key: "21:9", label: "21:9 宽银幕", w: 21, h: 9 },
  { key: "16:9", label: "16:9 横屏", w: 16, h: 9 },
  { key: "4:3", label: "4:3 传统", w: 4, h: 3 },
  { key: "1:1", label: "1:1 方形", w: 1, h: 1 },
  { key: "3:4", label: "3:4 竖屏", w: 3, h: 4 },
  { key: "9:16", label: "9:16 竖屏", w: 9, h: 16 },
];

export const SHOT_TYPE_OPTIONS = [
  { value: "extreme_wide_shot", label: "大远景" },
  { value: "wide_shot", label: "远景" },
  { value: "full_shot", label: "全景" },
  { value: "medium_shot", label: "中景" },
  { value: "medium_close_up", label: "近景" },
  { value: "close_up", label: "特写" },
  { value: "extreme_close_up", label: "大特写" },
  { value: "over_the_shoulder", label: "过肩" },
  { value: "two_shot", label: "双人镜头" },
];

export const COMPOSITION_OPTIONS = [
  { value: "rule_of_thirds", label: "三分法" },
  { value: "center", label: "中心构图" },
  { value: "symmetric", label: "对称构图" },
  { value: "leading_room", label: "视线留白" },
];

export const ENVIRONMENT_OPTIONS = [
  { value: "studio", label: "影棚（中灰）" },
  { value: "day", label: "日景（亮）" },
  { value: "night", label: "夜景（暗）" },
  { value: "interior", label: "室内（暖）" },
];

// ---------------------------------------------------------------- 机位预设库

export interface CameraPreset {
  key: string;
  label: string;
  pos: Vec3;
  lookAt: Vec3;
  focalLength: number;
  shotType: string;
  composition: string;
  roll: number;
}

const AT_HEAD = v3(0, 1, 0);

/**
 * 机位预设：位置/注视点都以「角色头部（0,1,0）」为基准。
 * 这一组是影视里最常用的说法（过肩、荷兰角、鸟瞰…），用户不需要懂三维坐标就能选。
 */
export const CAMERA_PRESETS: CameraPreset[] = [
  { key: "front_medium", label: "正面中景", pos: v3(0, 1.6, 4), lookAt: AT_HEAD, focalLength: 50, shotType: "medium_shot", composition: "rule_of_thirds", roll: 0 },
  { key: "front_close", label: "正面特写", pos: v3(0, 1.6, 2.2), lookAt: AT_HEAD, focalLength: 85, shotType: "close_up", composition: "center", roll: 0 },
  { key: "front_wide", label: "正面全景", pos: v3(0, 2.6, 8), lookAt: AT_HEAD, focalLength: 35, shotType: "wide_shot", composition: "rule_of_thirds", roll: 0 },
  { key: "side_track", label: "侧面跟拍", pos: v3(5, 1.5, 0), lookAt: AT_HEAD, focalLength: 50, shotType: "medium_shot", composition: "leading_room", roll: 0 },
  { key: "side_close", label: "侧面近景", pos: v3(3.4, 1.5, 0), lookAt: AT_HEAD, focalLength: 85, shotType: "medium_close_up", composition: "rule_of_thirds", roll: 0 },
  { key: "back_medium", label: "背面中景", pos: v3(0, 1.6, -4), lookAt: AT_HEAD, focalLength: 50, shotType: "medium_shot", composition: "rule_of_thirds", roll: 0 },
  { key: "top_wide", label: "俯拍全景", pos: v3(0, 8, 0.2), lookAt: AT_HEAD, focalLength: 35, shotType: "wide_shot", composition: "center", roll: 0 },
  { key: "angle45_top", label: "45° 俯拍", pos: v3(4, 5, 4), lookAt: AT_HEAD, focalLength: 35, shotType: "medium_shot", composition: "rule_of_thirds", roll: 0 },
  { key: "low_angle", label: "低角度仰拍", pos: v3(0, 0.4, 3), lookAt: v3(0, 1.2, 0), focalLength: 35, shotType: "medium_shot", composition: "rule_of_thirds", roll: 0 },
  { key: "low_wide", label: "低角度广角", pos: v3(0, 0.5, 2), lookAt: v3(0, 1.2, 0), focalLength: 24, shotType: "wide_shot", composition: "rule_of_thirds", roll: 0 },
  { key: "ots_left", label: "过肩镜头（左）", pos: v3(-1.5, 1.6, 2.6), lookAt: v3(0.5, 1, 0), focalLength: 85, shotType: "over_the_shoulder", composition: "rule_of_thirds", roll: 0 },
  { key: "ots_right", label: "过肩镜头（右）", pos: v3(1.5, 1.6, 2.6), lookAt: v3(-0.5, 1, 0), focalLength: 85, shotType: "over_the_shoulder", composition: "rule_of_thirds", roll: 0 },
  { key: "bird", label: "鸟瞰", pos: v3(0, 12, 0.2), lookAt: AT_HEAD, focalLength: 24, shotType: "extreme_wide_shot", composition: "center", roll: 0 },
  { key: "dutch", label: "荷兰角", pos: v3(0, 1.6, 4), lookAt: AT_HEAD, focalLength: 50, shotType: "medium_shot", composition: "rule_of_thirds", roll: 15 },
];

// ---------------------------------------------------------------- 构造

let seq = 0;

export function makeId(kind: ObjectKind): string {
  seq += 1;
  return `${kind}-${Date.now().toString(36)}-${seq.toString(36)}${Math.random().toString(36).slice(2, 5)}`;
}

export function defaultPose(preset: PosePreset = "stand"): PoseState {
  const angles = preset === "custom" ? POSE_PRESET_ANGLES.stand : POSE_PRESET_ANGLES[preset];
  return { preset, ...angles };
}

export function makeActor(index: number, pos: Vec3, bodyType: BodyType = "standard_male"): ActorObject {
  return {
    id: makeId("actor"),
    kind: "actor",
    name: `角色 ${index}`,
    visible: true,
    locked: false,
    groupId: null,
    pos,
    rotY: 0,
    scale: 1,
    height: BODY_TYPES[bodyType].height,
    bodyType,
    color: ACTOR_COLORS[index % ACTOR_COLORS.length],
    costume: "",
    pose: defaultPose("stand"),
  };
}

export function makeProp(index: number, pos: Vec3, shape: PropShape = "box"): PropObject {
  return {
    id: makeId("prop"),
    kind: "prop",
    name: `元素 ${index}`,
    visible: true,
    locked: false,
    groupId: null,
    pos,
    rotY: 0,
    scale: 1,
    shape,
    size: v3(0.8, 0.8, 0.8),
    color: PROP_COLORS[index % PROP_COLORS.length],
  };
}

export function makeCamera(index: number, pos: Vec3, lookAt: Vec3): CameraObject {
  return {
    id: makeId("camera"),
    kind: "camera",
    name: `机位 ${index}`,
    visible: true,
    locked: false,
    groupId: null,
    pos,
    rotY: 0,
    scale: 1,
    lookAt,
    focalLength: 35,
    composition: "rule_of_thirds",
    shotType: "medium_shot",
    roll: 0,
    trackTargetId: null,
  };
}

/** 由机位预设构造机位（同一预设可以加多个，名字带序号） */
export function makeCameraFromPreset(index: number, preset: CameraPreset, origin: Vec3 = v3(0, 0, 0)): CameraObject {
  const c = makeCamera(index, v3(0, 0, 0), v3(0, 0, 0));
  c.name = `${preset.label} ${index}`;
  c.pos = v3(origin.x + preset.pos.x, origin.y + preset.pos.y, origin.z + preset.pos.z);
  c.lookAt = v3(origin.x + preset.lookAt.x, origin.y + preset.lookAt.y, origin.z + preset.lookAt.z);
  c.focalLength = preset.focalLength;
  c.shotType = preset.shotType;
  c.composition = preset.composition;
  c.roll = preset.roll;
  return c;
}

/** 空场景：一个默认机位 + 默认三点布光，打开就能用 */
export function makeEmptyDoc(): DirectorDoc {
  const cam = makeCamera(1, v3(0, 1.7, 6), v3(0, 1, 0));
  return {
    version: DOC_VERSION,
    scene: {
      worldSize: v3(20, 6, 20),
      groundType: "grid",
      environment: "studio",
      groundOpacity: 0.4,
    },
    lights: {
      key: { pos: v3(4, 5, 4), intensity: 1.4, colorTemperature: 5600 },
      fill: { pos: v3(-5, 3, 3), intensity: 0.55, colorTemperature: 6500 },
      rim: { pos: v3(-2, 4.5, -5), intensity: 0.95, colorTemperature: 7200 },
      ambientIntensity: 0.35,
    },
    objects: [cam],
    groups: [],
    activeCameraId: cam.id,
  };
}

// ---------------------------------------------------------------- 查询

export function findObject(doc: DirectorDoc, id: string | null): DirectorObject | null {
  if (!id) return null;
  return doc.objects.find((o) => o.id === id) ?? null;
}

export function actorsOf(doc: DirectorDoc): ActorObject[] {
  return doc.objects.filter((o): o is ActorObject => o.kind === "actor");
}

export function propsOf(doc: DirectorDoc): PropObject[] {
  return doc.objects.filter((o): o is PropObject => o.kind === "prop");
}

export function camerasOf(doc: DirectorDoc): CameraObject[] {
  return doc.objects.filter((o): o is CameraObject => o.kind === "camera");
}

export function activeCamera(doc: DirectorDoc): CameraObject | null {
  const c = findObject(doc, doc.activeCameraId);
  return c && c.kind === "camera" ? c : null;
}

export function nextIndex(doc: DirectorDoc, kind: ObjectKind): number {
  return doc.objects.filter((o) => o.kind === kind).length + 1;
}

/** 焦段(mm) → 垂直视场角(度)。传感器高度按 24mm（全画幅）算 */
export function focalLengthToFov(focal: number, sensorHeightMm = 24): number {
  const f = Number.isFinite(focal) && focal > 0 ? focal : 50;
  return (2 * Math.atan(sensorHeightMm / (2 * f)) * 180) / Math.PI;
}

/** 垂直视场角(度) → 焦段(mm)，上面那个的逆运算（实测视角存成机位时用） */
export function fovToFocalLength(fovDeg: number, sensorHeightMm = 24): number {
  const t = Math.tan((fovDeg * Math.PI) / 360);
  if (!Number.isFinite(t) || t <= 0) return 35;
  return Math.max(8, Math.min(300, sensorHeightMm / (2 * t)));
}
