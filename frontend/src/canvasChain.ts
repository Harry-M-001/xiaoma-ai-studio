import type { CanvasEdge, CanvasNode, CanvasNodeData, CanvasNodeSchema, ModelOption } from "./types";

/**
 * 自动链的拓扑模板与「一键铺链」的纯逻辑。
 *
 * 为什么从 CanvasPage 里抽出来：示例模板（首页/项目页的「从示例开始」）要用同一套拓扑
 * 先在服务端存一张画布，再由画布打开。抄一份的下场是——以后改自动链的落位或连线，
 * 示例项目还停在旧形状上，而且这种不一致只在用户点开示例时才看得见。
 *
 * 这里只做纯计算：不碰 React、不弹 toast。「缺哪个模型」「给用户看什么文案」由调用方决定。
 */

/** 越往后越贵（资产设定图一行一张图、分镜图一镜一张图），所以默认只铺 L1。 */
export interface ChainLevel {
  key: string;
  label: string;
  hint: string;
  nodes: { type: string; col: number; row: number; data?: Partial<CanvasNodeData> }[];
  edges: [string, string][];
}

/** 资产链节点：逐行批量出图，产物自动进资产库供下游按名引用 */
export const ASSET_IMAGE_KIND = "assetImage";

/** 分镜图节点：逐镜批量出图 */
export const STORYBOARD_IMAGE_KIND = "storyboardImage";

/**
 * L3 起拓扑不再是直线（分镜图要同时吃分镜表和资产图），
 * 所以连线和落位都显式写出来，不靠「数组相邻即相连」推。
 * L4 是在 L3 基础上再接视频，抽成常量免得抄两遍、以后改一处漏一处。
 */
const L3_NODES: ChainLevel["nodes"] = [
  { type: "idea", col: 0, row: 0 },
  { type: "novel", col: 1, row: 0 },
  { type: "script", col: 2, row: 0 },
  { type: "storyboard", col: 3, row: 0 },
  { type: "assetSheet", col: 4, row: 0 },
  { type: "assetImage", col: 5, row: 0 },
  { type: "storyboardImage", col: 4, row: 1 },
];

const L3_EDGES: ChainLevel["edges"] = [
  ["idea", "novel"],
  ["novel", "script"],
  ["script", "storyboard"],
  ["storyboard", "assetSheet"],
  ["assetSheet", "assetImage"],
  // 分镜图要两个上游：分镜（镜头表）+ 资产图（等它先跑完，提及注入才有图可挂）
  ["storyboard", "storyboardImage"],
  ["assetImage", "storyboardImage"],
];

export const CHAIN_LEVELS: ChainLevel[] = [
  {
    key: "L1",
    label: "L1 文本链",
    hint: "创意 → 小说 → 剧本 → 分镜（只出一份 Markdown，最省钱）",
    nodes: [
      { type: "idea", col: 0, row: 0 },
      { type: "novel", col: 1, row: 0 },
      { type: "script", col: 2, row: 0 },
      { type: "storyboard", col: 3, row: 0 },
    ],
    edges: [
      ["idea", "novel"],
      ["novel", "script"],
      ["script", "storyboard"],
    ],
  },
  {
    key: "L2",
    label: "L2 +资产链",
    hint: "文本链之后再铺 资产表 → 资产设定图，下游提示词提到角色名会自动挂设定图",
    nodes: [
      { type: "idea", col: 0, row: 0 },
      { type: "novel", col: 1, row: 0 },
      { type: "script", col: 2, row: 0 },
      { type: "storyboard", col: 3, row: 0 },
      { type: "assetSheet", col: 4, row: 0 },
      { type: "assetImage", col: 5, row: 0 },
    ],
    edges: [
      ["idea", "novel"],
      ["novel", "script"],
      ["script", "storyboard"],
      ["storyboard", "assetSheet"],
      ["assetSheet", "assetImage"],
    ],
  },
  {
    key: "L3",
    label: "L3 +分镜图",
    hint: "再加「分镜图」：按镜头表逐镜出图，每镜自动挂它提到的角色设定图",
    nodes: L3_NODES,
    edges: L3_EDGES,
  },
  {
    key: "L4",
    label: "L4 +逐镜视频",
    hint: "全链打通：逐镜出图后再逐镜生视频，相邻两镜首尾相连。最贵的一档，建议先跑通 L3 再铺",
    nodes: [
      ...L3_NODES,
      { type: "video", col: 5, row: 1, data: { shotVideo: "chain", mode: "first_last" } },
    ],
    edges: [
      ...L3_EDGES,
      // 视频同样要两个上游：分镜（每镜的动作与时长）+ 分镜图（每镜的首帧图）
      ["storyboard", "video"],
      ["storyboardImage", "video"],
    ],
  },
];

/** 这条链需要哪些能力（text / image / video），用于「缺什么」的提示 */
export function requiredModalities(levelKey: string): string[] {
  const level = CHAIN_LEVELS.find((l) => l.key === levelKey) ?? CHAIN_LEVELS[0];
  const needs = new Set<string>(["text"]);
  for (const n of level.nodes) {
    if (n.type === "video") needs.add("video");
    if (n.type === ASSET_IMAGE_KIND || n.type === STORYBOARD_IMAGE_KIND) needs.add("image");
  }
  return [...needs];
}

export interface ChainBuild {
  nodes: (CanvasNode & { selected?: boolean })[];
  edges: CanvasEdge[];
  /** 缺哪些能力：非空就别铺（铺了也是一跑就失败） */
  missing: string[];
  /** 链里引用了但契约里没有的节点类型（后端没升级时会出现） */
  unknownTypes: string[];
}

/**
 * 按档位生成节点与连线。
 *
 * 位置与 id 都**按 stamp 生成**：同一张画布上可以反复铺链，id 撞车会让 ReactFlow 把两条链
 * 搅在一起（而且线上报错很难看懂）。`origin` 让调用方决定铺在哪儿——画布上从鼠标附近铺，
 * 示例项目从左上角铺。
 */
export function buildChainNodes(
  levelKey: string,
  schemas: Record<string, CanvasNodeSchema>,
  models: ModelOption[],
  options: { stamp?: string; origin?: { x: number; y: number } } = {},
): ChainBuild {
  const level = CHAIN_LEVELS.find((l) => l.key === levelKey) ?? CHAIN_LEVELS[0];
  const unknownTypes = level.nodes.map((n) => n.type).filter((t) => !schemas[t]);
  if (unknownTypes.length > 0) {
    return { nodes: [], edges: [], missing: [], unknownTypes };
  }

  const pick = (modality: string) => models.find((m) => m.modality === modality)?.key ?? "";
  const keys = { text: pick("text"), image: pick("image"), video: pick("video") } as Record<string, string>;
  const missing = requiredModalities(level.key).filter((m) => !keys[m]);
  if (missing.length > 0) {
    return { nodes: [], edges: [], missing, unknownTypes: [] };
  }

  const stamp = options.stamp ?? Date.now().toString(36);
  const originX = options.origin?.x ?? 80;
  const originY = options.origin?.y ?? 140;
  const stepX = 300;
  const stepY = 240;
  const idOf = (t: string) => `chain_${t}_${stamp}`;
  const modelKeyFor = (t: string) => {
    if (t === "video") return keys.video;
    if (t === ASSET_IMAGE_KIND || t === STORYBOARD_IMAGE_KIND) return keys.image;
    return keys.text;
  };

  const nodes = level.nodes.map(({ type, col, row, data: extra }, i) => ({
    id: idOf(type),
    type: "contract",
    position: { x: originX + col * stepX, y: originY + row * stepY },
    selected: i === 0,
    data: {
      prompt: "",
      model_key: modelKeyFor(type),
      schema: schemas[type],
      nodeType: type,
      ...extra,
    } as CanvasNodeData,
  }));

  const edges: CanvasEdge[] = level.edges.map(([from, to], i) => ({
    id: `chain_e_${i}_${stamp}`,
    source: idOf(from),
    sourceHandle: schemas[from].handles.sources?.[0]?.id ?? "out-text",
    target: idOf(to),
    targetHandle: schemas[to].handles.targets?.[0]?.id ?? "in-text",
  }));

  return { nodes, edges, missing: [], unknownTypes: [] };
}

/** 缺能力的提示文案（画布与示例入口共用一份，免得两处口径不一样） */
export function missingModelMessage(missing: string[]): string {
  const label: Record<string, string> = { text: "文本", image: "图片", video: "视频" };
  const names = missing.map((m) => label[m] ?? m).join("与");
  return `这条链需要${names}模型，请先到「模型服务」接入（也可以直接接入本机 Ollama）`;
}

/**
 * 落库前的整形：把画布内的节点转成后端认的形状。
 *
 * 两条硬要求，都是踩过的：
 * - `type` 必须是**真实节点类型**（idea/novel/…）。画布内部为了统一渲染，所有节点在
 *   ReactFlow 里都叫 "contract"，把那个名字存进去，后端会回一句「未知节点类型：contract」，
 *   示例项目就是**存下来了但打不开**（前端只看到一句 toast，画布空白）。
 * - `data.schema` 是运行时从契约接口拿的，属于可再生的缓存，不该写进库里；
 *   带上它不仅让文档变胖，还会在契约升级后留下一份旧快照。
 *
 * 入参故意放宽（`type` 可空）：ReactFlow 的 `Node.type` 是可选的，而参数类型写死成
 * 必填就必须在调用处做无意义的断言。
 */
export interface DocNodeInput {
  id: string;
  type?: string;
  position: { x: number; y: number };
  data: CanvasNodeData;
  selected?: boolean;
}

export function toCanvasDocNodes(nodes: DocNodeInput[]): CanvasNode[] {
  return nodes.map((n) => {
    const data = { ...(n.data as Record<string, unknown>) };
    delete data.schema;
    delete data.status;
    return {
      id: n.id,
      type: String(n.data.nodeType ?? ""),
      position: { x: n.position.x, y: n.position.y },
      data: data as CanvasNodeData,
    };
  });
}
