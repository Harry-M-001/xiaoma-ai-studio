/**
 * 3D 导演台 · 可编辑视口（原生 three.js）
 *
 * 与参考实现（dola-v2 的 StudioCanvas）的关系：**契约照搬，渲染层重写**。
 * 那边跑在 @react-three/fiber + drei 上，本项目的 UI 是纯 CSS、没有 Tailwind，
 * 也没有 `--cv-*` token，逐行搬过来等于同时引入三套新依赖和一层 token 改名。
 * 而这一层真正值钱的东西是**行为契约**，不是 JSX：
 *
 *  - 导演视角（自由环绕）⇄ 机位视角（相机吸附到机位、所见即所得）；
 *  - 点选对象、拖对象在**地面平面**上移动（y 由对象自己的类型决定，不跟鼠标上下跑）；
 *  - 截图必须能取到像素：`preserveDrawingBuffer: true` + 读像素前手动 `render()` 一次，
 *    否则拿到的是黑图；改比例后必须还原尺寸，否则后续画面变形。
 *
 * 三条踩过的坑，写在这里以免重犯：
 *  1. **拖动时一定要先关掉 OrbitControls**。它和对象拖动监听同一个元素，
 *     不关的话「拖角色」会变成「转视角」。所以拾取放在捕获阶段做，先判命中再决定关不关。
 *  2. 场景里的 DOM 标签（CSS2DRenderer）**不在截图里**——这正好是我们想要的
 *     （出图不该带对象名），但要知道这是特性不是 bug。
 *  3. 几何体按「参数键」缓存重建：滑杆一拖会 sync 几十次，每次都 new Geometry 会漏显存。
 */

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import {
  BODY_TYPES,
  focalLengthToFov,
  type ActorObject,
  type CameraObject,
  type DirectorDoc,
  type DirectorObject,
  type PoseState,
  type PropObject,
  type PropShape,
  type Vec3,
} from "./types";
import type { SessionState } from "./store";

const DEFAULT_FOV = 45;
const SELECT_TINT = "#fde047";
const deg = (v: number): number => (v * Math.PI) / 180;

export interface CaptureOptions {
  ratio?: { w: number; h: number };
  scale?: number;
}

export interface ViewSnapshot {
  position: Vec3;
  direction: Vec3;
  fov: number;
}

export interface ViewportCallbacks {
  onSelect: (id: string | null) => void;
  /** 拖动过程（每一帧）——只预览，不记历史 */
  onDragPreview: (id: string, pos: Vec3) => void;
  /** 松手一次——记一条历史 */
  onDragCommit: (id: string, pos: Vec3) => void;
}

/** 色温(K) → 颜色。用经典的近似公式，够预演用；不追求色度学精确 */
function kelvinColor(kelvin: number): THREE.Color {
  const t = Math.min(12000, Math.max(1800, kelvin)) / 100;
  let r: number;
  let g: number;
  let b: number;
  if (t <= 66) {
    r = 255;
    g = 99.4708025861 * Math.log(t) - 161.1195681661;
  } else {
    r = 329.698727446 * Math.pow(t - 60, -0.1332047592);
    g = 288.1221695283 * Math.pow(t - 60, -0.0755148492);
  }
  if (t >= 66) b = 255;
  else if (t <= 19) b = 0;
  else b = 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  const c = (v: number) => Math.min(1, Math.max(0, v / 255));
  return new THREE.Color(c(r), c(g), c(b));
}

interface EnvPreset {
  background: number;
  ground: number;
  ambient: number;
}

function resolveEnvironment(name: string): EnvPreset {
  switch (name) {
    case "day":
      return { background: 0x9fb6cd, ground: 0x6b7280, ambient: 0.55 };
    case "night":
      return { background: 0x10131a, ground: 0x2a2f3a, ambient: 0.22 };
    case "interior":
      return { background: 0x3b332c, ground: 0x6b5b4a, ambient: 0.4 };
    default:
      return { background: 0x2b2f3a, ground: 0x4b5563, ambient: 0.42 };
  }
}

/** 角色素模：由身高/体型算出的关节骨架。返回 root + 一个把姿态写进去的函数 */
function buildActor(actor: ActorObject): { root: THREE.Group; applyPose: (pose: PoseState) => void } {
  const H = actor.height;
  const W = BODY_TYPES[actor.bodyType]?.width ?? 1;
  const thick = 0.055 * H * W;
  const torsoW = 0.2 * H * W;
  const torsoD = 0.12 * H * W;
  const shoulderW = 0.22 * H * W;
  const legLen = 0.47 * H;
  const thigh = 0.245 * H;
  const shin = 0.225 * H;
  const armUp = 0.16 * H;
  const armLow = 0.15 * H;
  const headR = 0.065 * H;

  const mat = new THREE.MeshStandardMaterial({ color: actor.color, roughness: 0.72, metalness: 0.04 });
  const limb = (len: number, r: number): THREE.Mesh => {
    const g = new THREE.CapsuleGeometry(r, Math.max(0.02, len - r * 2), 4, 10);
    const m = new THREE.Mesh(g, mat);
    m.castShadow = true;
    return m;
  };

  const root = new THREE.Group();
  const hips = new THREE.Group();
  hips.position.y = legLen;
  root.add(hips);

  // 躯干
  const spine = new THREE.Group();
  hips.add(spine);
  const chest = new THREE.Mesh(new THREE.BoxGeometry(torsoW, 0.3 * H, torsoD), mat);
  chest.position.y = 0.15 * H;
  chest.castShadow = true;
  spine.add(chest);

  // 头颈
  const neck = new THREE.Group();
  neck.position.y = 0.3 * H;
  spine.add(neck);
  const head = new THREE.Mesh(new THREE.SphereGeometry(headR, 16, 12), mat);
  head.position.y = 0.055 * H + headR;
  head.castShadow = true;
  neck.add(head);

  interface Arm {
    shoulder: THREE.Group;
    elbow: THREE.Group;
  }
  const arms: Arm[] = [];
  const legs: { hip: THREE.Group; knee: THREE.Group }[] = [];

  const buildArm = (side: 1 | -1): Arm => {
    const shoulder = new THREE.Group();
    shoulder.position.set(side * shoulderW * 0.5, 0.27 * H, 0);
    const upper = limb(armUp, thick * 0.9);
    upper.position.y = -armUp / 2;
    shoulder.add(upper);
    const elbow = new THREE.Group();
    elbow.position.y = -armUp;
    shoulder.add(elbow);
    const lower = limb(armLow, thick * 0.8);
    lower.position.y = -armLow / 2;
    elbow.add(lower);
    spine.add(shoulder);
    return { shoulder, elbow };
  };
  arms.push(buildArm(-1), buildArm(1));

  const buildLeg = (side: 1 | -1) => {
    const hip = new THREE.Group();
    hip.position.set(side * torsoW * 0.22, 0, 0);
    const upper = limb(thigh, thick);
    upper.position.y = -thigh / 2;
    hip.add(upper);
    const knee = new THREE.Group();
    knee.position.y = -thigh;
    hip.add(knee);
    const lower = limb(shin, thick * 0.85);
    lower.position.y = -shin / 2;
    knee.add(lower);
    hips.add(hip);
    return { hip, knee };
  };
  legs.push(buildLeg(-1), buildLeg(1));

  const applyPose = (pose: PoseState) => {
    // 角度全用度，进 three 时才转弧度；左右镜像靠 Z 轴取反
    neck.rotation.x = deg(pose.headPitch);
    spine.rotation.y = deg(pose.torsoTwist);

    const applyArm = (arm: Arm, side: 1 | -1, raise: number, abduct: number, twist: number, elbow: number) => {
      arm.shoulder.rotation.set(deg(raise), deg(twist), deg(abduct) * -side);
      arm.elbow.rotation.x = deg(-Math.abs(elbow));
    };
    applyArm(arms[0], -1, pose.shoulderL_raise, pose.shoulderL_abduct, pose.shoulderL_twist, pose.elbowL);
    applyArm(arms[1], 1, pose.shoulderR_raise, pose.shoulderR_abduct, pose.shoulderR_twist, pose.elbowR);

    const applyLeg = (leg: { hip: THREE.Group; knee: THREE.Group }, side: 1 | -1, raise: number, abduct: number, twist: number, knee: number) => {
      leg.hip.rotation.set(deg(raise), deg(twist), deg(abduct) * -side);
      leg.knee.rotation.x = deg(-Math.abs(knee));
    };
    applyLeg(legs[0], -1, pose.hipL_raise, pose.hipL_abduct, pose.hipL_twist, pose.kneeL);
    applyLeg(legs[1], 1, pose.hipR_raise, pose.hipR_abduct, pose.hipR_twist, pose.kneeR);
  };

  applyPose(actor.pose);
  return { root, applyPose };
}

/** 元素的几何体参数键：变化才重建（滑杆连续拖动时避免反复 new） */
function shapeKey(shape: PropShape, size: Vec3): string {
  return `${shape}:${size.x.toFixed(3)}:${size.y.toFixed(3)}:${size.z.toFixed(3)}`;
}

function buildPropGeometry(shape: PropShape, size: Vec3): THREE.BufferGeometry {
  const sx = Math.max(0.02, size.x);
  const sy = Math.max(0.02, size.y);
  const sz = Math.max(0.02, size.z);
  switch (shape) {
    case "sphere":
      return new THREE.SphereGeometry(sx / 2, 24, 16);
    case "cylinder":
      return new THREE.CylinderGeometry(sx / 2, sz / 2, sy, 24);
    case "cone":
      return new THREE.ConeGeometry(sx / 2, sy, 24);
    case "plane":
      return new THREE.BoxGeometry(sx, Math.max(0.02, sy), sz);
    case "torus":
      // three 的环体默认立在 XY 平面，摆平后才像「地面上的环」
      return new THREE.TorusGeometry(Math.max(0.06, sx / 2), Math.max(0.03, sy / 2), 12, 28);
    case "pyramid":
      return new THREE.ConeGeometry(sx / 2, sy, 4);
    default:
      return new THREE.BoxGeometry(sx, sy, sz);
  }
}

/** 机位可视化：机身 + 视锥线 + 注视点连线 */
function buildCameraRig(cam: CameraObject): { root: THREE.Group; update: (c: CameraObject) => void } {
  const root = new THREE.Group();
  const bodyMat = new THREE.MeshStandardMaterial({ color: "#cbd5e1", roughness: 0.6 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.2, 0.36), bodyMat);
  root.add(body);

  const frustumMat = new THREE.LineBasicMaterial({ color: "#38bdf8", transparent: true, opacity: 0.85 });
  const gazeMat = new THREE.LineDashedMaterial({ color: "#38bdf8", transparent: true, opacity: 0.5, dashSize: 0.18, gapSize: 0.12 });

  const frustum = new THREE.LineSegments(new THREE.BufferGeometry(), frustumMat);
  const gaze = new THREE.Line(new THREE.BufferGeometry(), gazeMat);
  root.add(frustum, gaze);

  const light = new THREE.PointLight(0xffffff, 0.0, 0.1);
  root.add(light);

  const update = (c: CameraObject) => {
    root.position.set(c.pos.x, c.pos.y, c.pos.z);
    root.lookAt(c.lookAt.x, c.lookAt.y, c.lookAt.z);
    if (c.roll) root.rotateZ(deg(c.roll));

    const fov = focalLengthToFov(c.focalLength);
    const depth = 1.6;
    const h = 2 * Math.tan(deg(fov) / 2) * depth;
    const w = h * (16 / 9);
    const far = [
      new THREE.Vector3(-w / 2, -h / 2, -depth),
      new THREE.Vector3(w / 2, -h / 2, -depth),
      new THREE.Vector3(w / 2, h / 2, -depth),
      new THREE.Vector3(-w / 2, h / 2, -depth),
    ];
    const origin = new THREE.Vector3(0, 0, 0);
    const pts: THREE.Vector3[] = [];
    for (let i = 0; i < 4; i += 1) {
      pts.push(origin.clone(), far[i].clone());
      pts.push(far[i].clone(), far[(i + 1) % 4].clone());
    }
    frustum.geometry.dispose();
    frustum.geometry = new THREE.BufferGeometry().setFromPoints(pts);

    // 注视点连线（在父空间里算，才不会被 root 的旋转重复作用）
    const from = new THREE.Vector3(c.pos.x, c.pos.y, c.pos.z);
    const to = new THREE.Vector3(c.lookAt.x, c.lookAt.y, c.lookAt.z);
    gaze.geometry.dispose();
    gaze.geometry = new THREE.BufferGeometry().setFromPoints([from, to]);
    gaze.computeLineDistances();
  };

  update(cam);
  return { root, update };
}

// ---------------------------------------------------------------- 主类

interface Entry {
  group: THREE.Group;
  object: DirectorObject;
  /** 参数键：变了才重建几何体（滑杆连续拖动时避免反复 new） */
  key: string;
  /** 角色：只更新姿态，不重建骨架 */
  actor?: { applyPose: (pose: PoseState) => void };
  /** 机位：只更新参数（视锥线要重算） */
  cam?: { update: (c: CameraObject) => void };
  /** 可着色材质（选中高亮 + 颜色跟随属性） */
  materials: THREE.MeshStandardMaterial[];
  /** 元素的实体网格（改形状/尺寸时换几何体） */
  propMesh?: THREE.Mesh;
  label: CSS2DObject;
  marker: THREE.Mesh;
}

/** 默认世界尺寸：构造视口时文档还没进来，先用它摆一个能看见全场的机位 */
const DEFAULT_WORLD = { x: 20, y: 6, z: 20 };

export class StudioViewport {
  private renderer: THREE.WebGLRenderer;
  private labelRenderer: CSS2DRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private raycaster = new THREE.Raycaster();
  private pointer = new THREE.Vector2();
  private groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
  private entries = new Map<string, Entry>();
  private frame = 0;
  private resizeObserver: ResizeObserver;

  private worldBox: THREE.LineSegments | null = null;
  private grid: THREE.GridHelper | null = null;
  private ground: THREE.Mesh | null = null;
  private hemi: THREE.HemisphereLight;
  private ambient: THREE.AmbientLight;
  private lights: { key: THREE.DirectionalLight; fill: THREE.DirectionalLight; rim: THREE.DirectionalLight };
  private lightHelpers: THREE.Object3D[] = [];

  private doc: DirectorDoc | null = null;
  private session: SessionState | null = null;

  // 拖动状态
  private drag: { id: string; moved: boolean; startPos: Vec3; lastPos: Vec3 } | null = null;

  constructor(private container: HTMLElement, private cb: ViewportCallbacks) {
    const size = this.sizeOf();
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      // 读像素的前提；没有它 toDataURL 拿到的是黑图
      preserveDrawingBuffer: true,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(size.w, size.h, false);
    this.renderer.domElement.style.display = "block";
    this.container.appendChild(this.renderer.domElement);

    this.labelRenderer = new CSS2DRenderer();
    this.labelRenderer.setSize(size.w, size.h);
    const ldom = this.labelRenderer.domElement;
    ldom.style.position = "absolute";
    ldom.style.top = "0";
    ldom.style.left = "0";
    // 标签只用来读，不该挡住画布上的拖动
    ldom.style.pointerEvents = "none";
    this.container.appendChild(ldom);

    this.camera = new THREE.PerspectiveCamera(DEFAULT_FOV, size.w / Math.max(1, size.h), 0.1, 800);
    const init = this.overviewPose(DEFAULT_WORLD);
    this.camera.position.copy(init.pos);
    this.camera.lookAt(init.target);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI / 2.02;
    this.controls.target.copy(init.target);

    this.hemi = new THREE.HemisphereLight(0xffffff, 0x1f2937, 0.4);
    this.ambient = new THREE.AmbientLight(0xffffff, 0.35);
    this.lights = {
      key: new THREE.DirectionalLight(0xffffff, 1.2),
      fill: new THREE.DirectionalLight(0xffffff, 0.5),
      rim: new THREE.DirectionalLight(0xffffff, 0.9),
    };
    this.scene.add(this.hemi, this.ambient, this.lights.key, this.lights.fill, this.lights.rim);

    this.renderer.domElement.addEventListener("pointerdown", this.onPointerDown, { capture: true });
    window.addEventListener("pointermove", this.onPointerMove);
    window.addEventListener("pointerup", this.onPointerUp);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);

    this.loop();
  }

  private sizeOf(): { w: number; h: number } {
    const r = this.container.getBoundingClientRect();
    // 容器还没布局出来（宽高为 0）时先用一个合理的默认值：0 宽的绘图缓冲会让截图为空，
    // 看起来像「WebGL 坏了」。真尺寸由 ResizeObserver（以及每次 sync 的兜底）修正。
    return {
      w: Math.round(r.width) > 0 ? Math.round(r.width) : 640,
      h: Math.round(r.height) > 0 ? Math.round(r.height) : 360,
    };
  }

  private overviewPose(world: { x: number; y: number; z: number }): { pos: THREE.Vector3; target: THREE.Vector3 } {
    const span = Math.max(world.x, world.z, 6);
    const target = new THREE.Vector3(0, Math.min(1.2, world.y / 4), 0);
    return {
      pos: new THREE.Vector3(span * 0.55, Math.max(world.y, 3) * 0.9 + 1.5, span * 0.75),
      target,
    };
  }

  /** 把文档与界面态同步进场景。每次状态变化调用（不是每帧） */
  sync(doc: DirectorDoc, session: SessionState): void {
    this.doc = doc;
    this.session = session;
    const env = resolveEnvironment(doc.scene.environment);

    if (this.scene.background instanceof THREE.Color) this.scene.background.setHex(env.background);
    else this.scene.background = new THREE.Color(env.background);

    // ---- 世界容器：尺寸变了重建（世界盒子/地面/网格）
    const worldKey = `${doc.scene.worldSize.x}:${doc.scene.worldSize.y}:${doc.scene.worldSize.z}:${doc.scene.groundOpacity}:${doc.scene.groundType}`;
    if (this.worldBoxKey !== worldKey) {
      this.worldBoxKey = worldKey;
      this.rebuildWorld(doc, env);
    } else if (this.ground) {
      const m = this.ground.material as THREE.MeshStandardMaterial;
      m.opacity = doc.scene.groundOpacity;
      m.transparent = doc.scene.groundOpacity < 1;
      m.color.setHex(env.ground);
      m.needsUpdate = true;
    }
    if (this.grid) this.grid.visible = session.showGrid;
    if (this.worldBox) this.worldBox.visible = session.showGrid;

    // ---- 灯光
    const setLight = (l: THREE.DirectionalLight, slot: DirectorDoc["lights"]["key"], visible: boolean) => {
      l.position.set(slot.pos.x, slot.pos.y, slot.pos.z);
      l.intensity = slot.intensity;
      l.color.copy(kelvinColor(slot.colorTemperature));
      l.visible = visible;
    };
    setLight(this.lights.key, doc.lights.key, session.showLights);
    setLight(this.lights.fill, doc.lights.fill, session.showLights);
    setLight(this.lights.rim, doc.lights.rim, session.showLights);
    this.hemi.intensity = env.ambient + doc.lights.ambientIntensity * 0.5;
    this.ambient.intensity = doc.lights.ambientIntensity * 0.6;

    // 灯光位置在关掉显示时也想知道「光在哪」——只在开启时画小标记
    for (const h of this.lightHelpers) h.visible = session.showLights;
    if (this.lightHelpers.length === 0 && session.showLights) this.buildLightHelpers();

    // ---- 对象
    const seen = new Set<string>();
    for (const obj of doc.objects) {
      seen.add(obj.id);
      let entry = this.entries.get(obj.id);
      if (!entry) {
        entry = this.createEntry(obj);
        this.entries.set(obj.id, entry);
      }
      this.updateEntry(entry, obj, session);
    }
    for (const [id, entry] of [...this.entries]) {
      if (seen.has(id)) continue;
      this.removeEntry(entry);
      this.entries.delete(id);
    }

    // ---- 相机视角
    const active = doc.objects.find((o) => o.id === doc.activeCameraId);
    const activeCam = active && active.kind === "camera" ? active : null;
    if (session.viewMode === "camera" && activeCam) {
      this.camera.fov = focalLengthToFov(activeCam.focalLength);
      this.camera.position.set(activeCam.pos.x, activeCam.pos.y, activeCam.pos.z);
      this.camera.lookAt(activeCam.lookAt.x, activeCam.lookAt.y, activeCam.lookAt.z);
      if (activeCam.roll) this.camera.rotateZ(deg(activeCam.roll));
      this.camera.updateProjectionMatrix();
      this.controls.enabled = false;
      this.controls.target.set(activeCam.lookAt.x, activeCam.lookAt.y, activeCam.lookAt.z);
    } else {
      if (this.camera.fov !== DEFAULT_FOV) {
        this.camera.fov = DEFAULT_FOV;
        this.camera.updateProjectionMatrix();
      }
      this.controls.enabled = this.drag === null;
    }

    // 每次同步顺手核对一次画布尺寸（见 ensureSize 的说明）
    this.ensureSize();

    // 第一次同步时按对象自动取一次景：打开就能看到人，而不是「一片网格」
    if (!this.framedOnce) {
      this.framedOnce = true;
      this.resetView();
    }
  }

  private framedOnce = false;

  private worldBoxKey = "";

  private rebuildWorld(doc: DirectorDoc, env: EnvPreset): void {
    for (const obj of [this.worldBox, this.ground, this.grid, ...this.lightHelpers]) {
      if (!obj) continue;
      this.scene.remove(obj);
    }
    this.disposeTree(this.worldBox);
    this.disposeTree(this.ground);
    // 灯光位标记也要连几何体一起释放：世界尺寸每改一次都会重建一轮
    for (const h of this.lightHelpers) this.disposeTree(h);
    this.grid?.dispose();
    this.lightHelpers = [];

    const { x, y, z } = doc.scene.worldSize;

    const groundGeo = new THREE.PlaneGeometry(x, z);
    const groundMat = new THREE.MeshStandardMaterial({
      color: env.ground,
      roughness: 0.95,
      metalness: 0,
      transparent: doc.scene.groundOpacity < 1,
      opacity: doc.scene.groundOpacity,
    });
    this.ground = new THREE.Mesh(groundGeo, groundMat);
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.receiveShadow = true;
    this.scene.add(this.ground);

    this.grid = new THREE.GridHelper(Math.max(x, z), Math.max(4, Math.round(Math.max(x, z))), 0x64748b, 0x475569);
    this.grid.position.y = 0.015;
    this.scene.add(this.grid);

    const box = new THREE.BoxGeometry(x, y, z);
    this.worldBox = new THREE.LineSegments(
      new THREE.EdgesGeometry(box),
      new THREE.LineBasicMaterial({ color: 0x64748b, transparent: true, opacity: 0.28 }),
    );
    box.dispose();
    this.worldBox.position.y = y / 2;
    this.scene.add(this.worldBox);
  }

  private buildLightHelpers(): void {
    const mk = (pos: Vec3, color: number) => {
      const g = new THREE.Mesh(
        new THREE.SphereGeometry(0.12, 10, 8),
        new THREE.MeshBasicMaterial({ color, wireframe: true }),
      );
      g.position.set(pos.x, pos.y, pos.z);
      this.scene.add(g);
      this.lightHelpers.push(g);
    };
    const d = this.doc?.lights;
    if (!d) return;
    mk(d.key.pos, 0xfde047);
    mk(d.fill.pos, 0x93c5fd);
    mk(d.rim.pos, 0xf0abfc);
  }

  // ------------------------------------------------------------ 对象条目

  private createEntry(obj: DirectorObject): Entry {
    const group = new THREE.Group();
    group.userData.objectId = obj.id;
    let materials: THREE.MeshStandardMaterial[] = [];
    let actor: Entry["actor"];
    let cam: Entry["cam"];
    let propMesh: THREE.Mesh | undefined;
    // 元素的几何体直接按当前尺寸建好，并记下参数键——否则第一次 sync 会白建一次又拆一次
    let key = "";

    if (obj.kind === "actor") {
      const built = buildActor(obj);
      group.add(built.root);
      actor = { applyPose: built.applyPose };
      materials = this.collectMaterials(group);
    } else if (obj.kind === "prop") {
      const mesh = new THREE.Mesh(
        buildPropGeometry(obj.shape, obj.size),
        new THREE.MeshStandardMaterial({ color: obj.color, roughness: 0.7, metalness: 0.05 }),
      );
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      group.add(mesh);
      propMesh = mesh;
      materials = [mesh.material as THREE.MeshStandardMaterial];
      key = shapeKey(obj.shape, obj.size);
    } else {
      const rig = buildCameraRig(obj);
      group.add(rig.root);
      cam = { update: rig.update };
    }

    const marker = new THREE.Mesh(
      new THREE.TorusGeometry(0.22, 0.03, 8, 20),
      new THREE.MeshBasicMaterial({ color: SELECT_TINT, transparent: true, opacity: 0.9 }),
    );
    marker.rotation.x = -Math.PI / 2;
    marker.position.y = 0.02;
    group.add(marker);

    const el = document.createElement("div");
    el.className = "d3d-label";
    const label = new CSS2DObject(el);
    group.add(label);

    this.scene.add(group);
    return { group, object: obj, key, actor, cam, materials, propMesh, label, marker };
  }

  private updateEntry(entry: Entry, obj: DirectorObject, session: SessionState): void {
    entry.object = obj;
    entry.group.visible = obj.visible;
    entry.group.position.set(obj.pos.x, obj.pos.y, obj.pos.z);
    entry.group.rotation.y = deg(obj.rotY);
    entry.group.scale.setScalar(obj.scale);

    if (obj.kind === "actor") {
      entry.actor?.applyPose(obj.pose);
      for (const m of entry.materials) m.color.set(obj.color);
    } else if (obj.kind === "prop") {
      const key = shapeKey(obj.shape, obj.size);
      if (key !== entry.key && entry.propMesh) {
        entry.propMesh.geometry.dispose();
        entry.propMesh.geometry = buildPropGeometry(obj.shape, obj.size);
        entry.key = key;
      }
      for (const m of entry.materials) m.color.set(obj.color);
    } else {
      entry.cam?.update(obj);
    }

    const selected = session.selectedId === obj.id;
    const tint = selected ? SELECT_TINT : null;
    for (const m of entry.materials) {
      m.emissive.set(tint ?? "#000000");
      m.emissiveIntensity = tint ? 0.35 : 0;
    }
    entry.marker.visible = selected;
    const el = entry.label.element as HTMLDivElement;
    const tag = obj.kind === "actor" ? "角色" : obj.kind === "prop" ? "元素" : "机位";
    el.textContent = `${tag} · ${obj.name}${obj.locked ? " 🔒" : ""}`;
    el.className = `d3d-label${selected ? " d3d-label-on" : ""}${obj.visible ? "" : " d3d-label-off"}`;
    // 标签高度：角色的头顶在身高处，元素在自身高度处，机位就在机身
    const top = obj.kind === "actor" ? obj.height + 0.2 : obj.kind === "prop" ? obj.size.y + 0.25 : 0.35;
    entry.label.position.set(0, top, 0);
  }

  private removeEntry(entry: Entry): void {
    this.scene.remove(entry.group);
    this.disposeTree(entry.group);
    entry.label.element.remove();
  }

  private collectMaterials(root: THREE.Object3D): THREE.MeshStandardMaterial[] {
    const out: THREE.MeshStandardMaterial[] = [];
    root.traverse((o) => {
      if (o instanceof THREE.Mesh && o.material instanceof THREE.MeshStandardMaterial) out.push(o.material);
    });
    return out;
  }

  private disposeTree(root: THREE.Object3D | null): void {
    if (!root) return;
    root.traverse((o) => {
      if (o instanceof THREE.Mesh || o instanceof THREE.LineSegments || o instanceof THREE.Line) {
        o.geometry?.dispose();
        const m = (o as THREE.Mesh).material;
        if (Array.isArray(m)) m.forEach((x) => x.dispose());
        else m?.dispose();
      }
    });
  }

  // ------------------------------------------------------------ 交互

  private hitObject(clientX: number, clientY: number): DirectorObject | null {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const roots = [...this.entries.values()].filter((e) => e.group.visible).map((e) => e.group);
    const hits = this.raycaster.intersectObjects(roots, true);
    for (const h of hits) {
      let node: THREE.Object3D | null = h.object;
      while (node && !node.userData.objectId) node = node.parent;
      if (node?.userData.objectId) {
        const id = String(node.userData.objectId);
        return this.doc?.objects.find((o) => o.id === id) ?? null;
      }
    }
    return null;
  }

  private onPointerDown = (e: PointerEvent): void => {
    if (e.button !== 0) return;
    const hit = this.hitObject(e.clientX, e.clientY);
    if (!hit) {
      this.cb.onSelect(null);
      return;
    }
    this.cb.onSelect(hit.id);
    // 未锁定 + 导演视角下才允许拖动；镜头机位不拖动（位置由机位预设/面板决定）
    const canDrag = this.session?.viewMode === "director" && !hit.locked && hit.kind !== "camera";
    if (!canDrag) return;
    // 关键：必须在捕获阶段先把 OrbitControls 关掉，否则「拖角色」会变成「转视角」
    this.controls.enabled = false;
    this.drag = { id: hit.id, moved: false, startPos: { ...hit.pos }, lastPos: { ...hit.pos } };
  };

  private onPointerMove = (e: PointerEvent): void => {
    if (!this.drag) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hitPoint = new THREE.Vector3();
    if (!this.raycaster.ray.intersectPlane(this.groundPlane, hitPoint)) return;
    // y 保持按下时的值：在地面上拖就只改 x/z，别让对象跟着鼠标上下飞
    const pos: Vec3 = { x: hitPoint.x, y: this.drag.startPos.y, z: hitPoint.z };
    this.drag.moved = true;
    this.drag.lastPos = pos;
    this.cb.onDragPreview(this.drag.id, pos);
  };

  private onPointerUp = (): void => {
    if (!this.drag) return;
    const d = this.drag;
    this.drag = null;
    if (this.session?.viewMode === "director") this.controls.enabled = true;
    // 只有真的移动过才收口：点一下不该在撤销栈里留下一条空记录
    if (d.moved) this.cb.onDragCommit(d.id, d.lastPos);
  };

  // ------------------------------------------------------------ 视角 / 截图

  /**
   * 取「当前该被看到的东西」的包围盒。
   *
   * 为什么要有这个：默认视角如果按「世界盒子」取景，一个 1.75 米的角色在 20 米的世界里
   * 只有画面高度的 12%，还正好落在底部工具条后面——打开就是「什么都没有」的观感。
   * 所以取景优先围着**实际存在的对象**转。
   */
  private frameTargets(): { center: THREE.Vector3; radius: number } {
    const box = new THREE.Box3();
    let any = false;
    // 先只按「内容」（角色/元素）取景：机位是辅助显示，把它的视锥算进来会让镜头
    // 一下子退到很远（默认机位在 z=6，一个角色在原点，取景半径直接从 0.9 变成 3.6）
    for (const e of this.entries.values()) {
      if (!e.group.visible || e.object.kind === "camera") continue;
      box.expandByObject(e.group);
      any = true;
    }
    // 场景里只有机位时（刚打开就是这种情况）再退回来按全部对象取景
    if (!any) {
      for (const e of this.entries.values()) {
        if (!e.group.visible) continue;
        box.expandByObject(e.group);
        any = true;
      }
    }
    if (!any || box.isEmpty()) return { center: new THREE.Vector3(0, 1, 0), radius: 4 };
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    // 下限 2.5 米：只有一个机位时别把镜头怼到机身上，画面上什么都看不出来
    const radius = Math.max(2.5, Math.max(size.x, size.y, size.z) * 0.5);
    return { center, radius };
  }

  /** 把镜头摆到「围着对象、略微俯视」的位置 */
  private frameObjects(): void {
    const { center, radius } = this.frameTargets();
    const dist = Math.max(6, (radius / Math.tan((DEFAULT_FOV * Math.PI) / 360)) * 1.15);
    const dir = new THREE.Vector3(0.55, 0.6, 0.75).normalize();
    const pos = center.clone().add(dir.multiplyScalar(dist));
    this.camera.fov = DEFAULT_FOV;
    this.camera.position.copy(pos);
    this.camera.lookAt(center);
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.update();
  }

  resetView(): void {
    if (!this.doc) return;
    if (this.entries.size > 0) {
      this.frameObjects();
      return;
    }
    const { pos, target } = this.overviewPose(this.doc.scene.worldSize);
    this.camera.fov = DEFAULT_FOV;
    this.camera.position.copy(pos);
    this.camera.lookAt(target);
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(target);
    this.controls.update();
  }

  /** 当前视点（把「我现在看到的」存成一个机位时用） */
  view(): ViewSnapshot | null {
    const dir = new THREE.Vector3();
    this.camera.getWorldDirection(dir);
    return {
      position: { x: this.camera.position.x, y: this.camera.position.y, z: this.camera.position.z },
      direction: { x: dir.x, y: dir.y, z: dir.z },
      fov: this.camera.fov,
    };
  }

  capture(opts: CaptureOptions = {}): string | null {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    if (w <= 0 || h <= 0) return null;
    const k = Math.max(1, Math.min(4, Math.round(opts.scale ?? 1)));
    const r = opts.ratio;
    const outH = Math.round(h * k);
    const outW = Math.round(r && r.h > 0 ? outH * (r.w / r.h) : w * k);

    const prevAspect = this.camera.aspect;
    const prevRatio = this.renderer.getPixelRatio();
    const restore = () => {
      const rw = this.container.clientWidth || w;
      const rh = this.container.clientHeight || h;
      this.renderer.setPixelRatio(prevRatio);
      this.renderer.setSize(rw, rh, false);
      this.camera.aspect = prevAspect;
      this.camera.updateProjectionMatrix();
      this.renderer.render(this.scene, this.camera);
    };
    try {
      // 出图期间把设备像素比压到 1：否则「2x」出来的是 2×dpr 倍（实测要的是 835×470、
      // 拿到的却是 1463×822），而且**换台机器同一份场景出图尺寸还不一样**。
      // 倍率就该是倍率，出图尺寸要可预期。
      this.renderer.setPixelRatio(1);
      this.renderer.setSize(outW, outH, false);
      this.camera.aspect = outW / Math.max(1, outH);
      this.camera.updateProjectionMatrix();
      // 先渲染再读像素，否则拿到黑图
      this.renderer.render(this.scene, this.camera);
      return this.renderer.domElement.toDataURL("image/png") || null;
    } catch (err) {
      console.warn("[3D 导演台] 截图失败（WebGL 上下文可能已丢失）", err);
      return null;
    } finally {
      // 无论成败都要还原尺寸、比例与像素比，否则后续画面变形或者糊掉
      restore();
    }
  }

  private resize(): void {
    const w = Math.round(this.container.clientWidth);
    const h = Math.round(this.container.clientHeight);
    if (w <= 0 || h <= 0) return; // 容器被收起到 0 宽时不缩画布，等它回来
    this.lastW = w;
    this.lastH = h;
    this.renderer.setSize(w, h, false);
    this.labelRenderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  private lastW = 0;
  private lastH = 0;

  /**
   * 兜底：每次 sync 都核对一次画布与容器的尺寸。
   * 只靠 ResizeObserver 会漏一种情况——容器在「被挤成 0 宽」的状态下第一次观察，
   * 之后虽然布局变了但观察者没再报（实测遇到过），于是画布一直停在错误尺寸。
   */
  private ensureSize(): void {
    const w = Math.round(this.container.clientWidth);
    const h = Math.round(this.container.clientHeight);
    if (w > 0 && h > 0 && (w !== this.lastW || h !== this.lastH)) this.resize();
  }

  private loop = (): void => {
    this.frame = window.requestAnimationFrame(this.loop);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labelRenderer.render(this.scene, this.camera);
  };

  dispose(): void {
    window.cancelAnimationFrame(this.frame);
    this.resizeObserver.disconnect();
    this.renderer.domElement.removeEventListener("pointerdown", this.onPointerDown, { capture: true });
    window.removeEventListener("pointermove", this.onPointerMove);
    window.removeEventListener("pointerup", this.onPointerUp);
    this.controls.dispose();
    for (const e of this.entries.values()) this.removeEntry(e);
    this.entries.clear();
    this.disposeTree(this.scene);
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.labelRenderer.domElement.remove();
  }
}
