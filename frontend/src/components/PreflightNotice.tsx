import { AlertTriangle, Info } from "lucide-react";

import type { PreflightResult, PreflightWarning } from "../types";

/**
 * 跑一次预检，拿不到就当没有告警。
 *
 * 这一层 try/catch 是这个功能能不能成立的关键：预检是个**辅助**，
 * 它自己挂了（网络抖、后端版本旧、接口报错）绝不能连累生成。
 * 所以策略是「静默降级成没有告警」——最坏的结果只是少提示一句，
 * 而不是用户点了生成却什么都没发生。
 */
export async function runPreflight(
  call: () => Promise<PreflightResult>,
): Promise<PreflightWarning[]> {
  try {
    const result = await call();
    return Array.isArray(result?.warnings) ? result.warnings : [];
  } catch {
    return [];
  }
}

/**
 * 生成前的软告警列表。
 *
 * 只负责「把话说清楚」：为什么会有问题、怎么改。是否继续由用户自己按按钮决定——
 * 所以这里不提供任何「阻止」的样式或逻辑，按钮归调用方的弹窗管。
 */
export function PreflightNotice({ warnings }: { warnings: PreflightWarning[] }) {
  if (warnings.length === 0) return null;
  return (
    <div className="preflight">
      <div className="preflight-head">
        <AlertTriangle size={15} />
        <span>
          有 {warnings.length} 条提醒。都不影响提交，你可以照原样生成，也可以先改一改。
        </span>
      </div>
      <ul className="preflight-list">
        {warnings.map((w) => (
          <li key={w.code} className={`preflight-item ${w.level === "info" ? "info" : "warn"}`}>
            <div className="preflight-msg">
              {w.level === "info" ? <Info size={14} /> : <AlertTriangle size={14} />}
              <span>{w.message}</span>
            </div>
            {w.suggestion ? <div className="preflight-hint">{w.suggestion}</div> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
