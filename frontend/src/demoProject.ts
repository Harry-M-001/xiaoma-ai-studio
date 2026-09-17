import { api } from "./api";
import { CHAIN_LEVELS, buildChainNodes, requiredModalities, toCanvasDocNodes } from "./canvasChain";

/**
 * 内置示例项目：打开就有一条**跑得动**的链。
 *
 * 第一次用的人面对一张空白画布无从下手——不知道节点能连成什么、也不知道该先点哪儿。
 * 这里先存一张画布，并在第一个节点里写好一句创意，点「运行整图」就能看到层层产出。
 *
 * 档位是**按现有模型自动选的**：只有文本模型（比如刚接入本机 Ollama，这也是最典型的新手状态）
 * 就铺 L1 文本链，有图片模型再铺到 L2/L3。第一版固定铺 L2，结果恰恰把「没有云 Key、
 * 只有本机 Ollama」的人挡在门外——而那正是最需要看到示例的人。
 *
 * 一次性动作，而不是「装完就自动塞一个项目」：凭空多出一个项目比没有示例更让人困惑，
 * 而且用户删掉它之后不该再自己长回来。
 */

/** 示例用的那句话：具体、有画面、一眼能看出会产出什么 */
export const DEMO_IDEA =
  "外卖小哥其实是隐退的顶级保镖：雨天送单时被三个混混堵在巷口，他撑伞的手没有抖一下。";

const BASE_NAME = "示例：一句话到分镜";

export interface DemoProject {
  id: number;
  name: string;
  /** 实际铺出来的档位，例如 "L2" */
  level: string;
  /** 档位的中文名，例如 "L2 +资产链" */
  levelLabel: string;
}

/** 从最高档往下找第一个「现有模型撑得起来」的档位 */
function pickLevel(models: { modality: string }[]): string {
  const have = new Set(models.map((m) => m.modality));
  const ordered = [...CHAIN_LEVELS].reverse(); // L4 → L1
  const fit = ordered.find((level) =>
    requiredModalities(level.key).every((need) => have.has(need)),
  );
  return fit?.key ?? "L1";
}

export async function createDemoProject(): Promise<DemoProject> {
  const [contract, models, projects] = await Promise.all([
    api.canvasContract(),
    api.listModels(),
    api.listProjects(),
  ]);

  const levelKey = pickLevel(models);
  const built = buildChainNodes(levelKey, contract.nodeSchemas, models);
  if (built.unknownTypes.length > 0) {
    throw new Error("画布节点契约不完整，请确认后端已升级到最新版本");
  }
  if (built.missing.length > 0) {
    // 连文本模型都没有：任何链都跑不动，先把话说清楚，别留一张空画布让人猜
    throw new Error("示例需要至少一个文本模型，请先到「模型服务」接入（也可以直接接入本机 Ollama）");
  }

  const level = CHAIN_LEVELS.find((l) => l.key === levelKey) ?? CHAIN_LEVELS[0];
  const first = built.nodes[0];
  if (first) first.data.prompt = DEMO_IDEA;

  // 名字撞了就加序号：用户可能想同时留着两份对比不同写法
  const taken = new Set(projects.map((p) => p.name));
  let name = `${BASE_NAME}（${level.key}）`;
  for (let i = 2; taken.has(name); i += 1) name = `${BASE_NAME}（${level.key}·${i}）`;

  const project = await api.createProject(
    name,
    `内置示例：已铺好「${level.label}」——${level.hint}。第一个节点里写了一句话，点画布上方「运行整图」即可看到产出。`,
  );
  try {
    await api.saveCanvas(project.id, {
      schemaVersion: contract.schemaVersion,
      // 必须过一遍整形：builder 产出的是「画布内部形状」（type 统一是 contract），
      // 直接把那个存进去，后端会判「未知节点类型」——示例存下来了但打不开
      nodes: toCanvasDocNodes(built.nodes),
      edges: built.edges,
      viewport: { x: 40, y: 60, zoom: 0.75 },
    });
  } catch (e) {
    // 画布没存上就别留一个空项目在那儿让人猜
    await api.deleteProject(project.id).catch(() => undefined);
    throw e;
  }
  return { id: project.id, name: project.name, level: level.key, levelLabel: level.label };
}
