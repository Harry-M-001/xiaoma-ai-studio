import { Bot, FolderOpen, ShieldCheck, ListChecks } from "lucide-react";
import { Empty } from "../components/common";

/**
 * Agent 工作台（占位）。
 *
 * **这一版只占住导航位置，功能还没做**——路线图里排在批次 12（依赖批次 10 的工具层与确认门）。
 *
 * 为什么先放一个空页而不是干脆不给入口：侧边栏的信息架构本身就是这一次要落地的东西，
 * 缺一项会让结构看着是残的。所以这一页**如实说自己还没做**，同时把已经定下来的形状
 * 摆出来（那四条口径是讨论过的结论，不是许愿），而不是写一句「敬请期待」。
 *
 * 不想看到这个入口：到「系统设置 → 导航菜单」把它关掉即可（`enabled`）。
 */
export default function AgentPage({ onNavigate }: { onNavigate?: (r: string) => void }) {
  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">Agent</div>
          <div className="page-desc">
            一个能在本机干活、也能调工坊工具的协作体。现在还没做，这一页先把位置占住。
          </div>
        </div>
      </div>

      <div className="card">
        <Empty
          icon={<Bot />}
          title="Agent 工作台还没做"
          desc="它排在路线图的批次 12，要等前面的工具层与确认门先落地。在那之前，创作这条线已经能完整跑通。"
          action={
            <button className="btn btn-primary" onClick={() => onNavigate?.("projects")}>
              <FolderOpen size={15} />
              去自由画布
            </button>
          }
        />
      </div>

      <div className="card">
        <div className="page-title" style={{ fontSize: 15, marginBottom: 12 }}>
          已经定下来的形状
        </div>
        <ul className="muted" style={{ lineHeight: 1.9, paddingLeft: 18 }}>
          <li>
            <b>项目 = 本机的一个文件夹</b>：不指定目录就建在默认目录下，默认目录可在「系统设置」里改。
          </li>
          <li>
            <b>项目里派任务，一次任务就是一次会话</b>：每一步做了什么都能看见、能回放。
          </li>
          <li>
            <b>危险的事它先问你</b>：花钱、删除、覆盖这类不可逆操作一律要人点；可撤销的它自己做完。
          </li>
          <li>
            <b>要跑命令时默认最保守</b>：沙箱 + 白名单起步，也可以改成每条都问，或明确放开。
          </li>
        </ul>
        <div className="row mt16" style={{ gap: 8, alignItems: "flex-start", fontSize: 12 }}>
          <span className="muted" style={{ display: "inline-flex", flexShrink: 0, paddingTop: 2 }}>
            <ListChecks size={14} />
          </span>
          <span className="muted">
            它既能动本机文件，也能调工坊自己的工具（画布 / 资产 / 任务 / 引擎）——这是它和别的 Agent 不一样的地方。
          </span>
        </div>
        <div className="row mt16" style={{ gap: 8, alignItems: "flex-start", fontSize: 12 }}>
          <span className="muted" style={{ display: "inline-flex", flexShrink: 0, paddingTop: 2 }}>
            <ShieldCheck size={14} />
          </span>
          <span className="muted">
            工具不会一次全开：第一版只给十来个只读工具，够用再往上加——给模型一百多个接口，选错是迟早的事。
          </span>
        </div>
      </div>
    </div>
  );
}
