import { useCallback, useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { api } from "../api";
import { Spinner } from "./common";
import { Dialog } from "./Dialog";
import { useToast } from "./Toast";
import type { McpConfirmation } from "../types";

/**
 * dsh sidecar 的阻塞式确认卡。
 *
 * 与 Agent 页里的确认卡不同：那张是「聊天请求还在等」，这张是 **MCP 工具调用挂起**，
 * 页面可能根本没打开 Agent 页。所以它挂在应用外壳上全局轮询，出现在任何页面上方。
 *
 * 关掉卡片 ≠ 同意：工具调用会继续挂到超时，然后按「没执行」返回给 dsh。
 * 这也是我们不给模型任何「批准」工具的原因——批准只能在这里点。
 */
export default function McpGate() {
  const toast = useToast();
  const [items, setItems] = useState<McpConfirmation[]>([]);
  const [dismissed, setDismissed] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await api.mcpConfirmations();
      setItems(res.items);
    } catch {
      /* 轮询失败不该打扰用户：下一轮继续 */
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let timer = 0;
    const tick = async () => {
      if (alive && !document.hidden) await refresh();
      if (alive) timer = window.setTimeout(tick, 3000);
    };
    void tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [refresh]);

  const item = items.find((it) => !dismissed.includes(it.stepId));

  const decide = async (approve: boolean) => {
    if (!item) return;
    setBusy(true);
    try {
      await api.mcpConfirm(item.stepId, approve);
      toast.success(approve ? "已确认，正在派发任务" : "已拒绝，没有派发任何任务");
      setItems((prev) => prev.filter((it) => it.stepId !== item.stepId));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "确认失败");
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  if (!item) return null;
  const extra = items.filter((it) => it.stepId !== item.stepId && !dismissed.includes(it.stepId)).length;

  return (
    <Dialog
      onClose={() => setDismissed((prev) => [...prev, item.stepId])}
      label="外部 Agent 请求执行画布"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog run-confirm"
    >
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">
          <ShieldAlert size={15} /> 外部 Agent 请求执行画布
        </span>
      </div>

      <div className="canvas-dialog-body">
        <p className="run-confirm-lead">
          DeepSeek Harness（高级模式）想执行画布
          <b>「{item.projectName || item.projectId || "未命名"}」</b>。确认后才会派发任务；
          拒绝或超时都不会执行。
        </p>

        <div className="agent-confirm-grid">
          <div className="agent-confirm-stat">
            <b>{item.calls}</b>
            <span>次调用</span>
          </div>
          <div className="agent-confirm-stat">
            <b>{item.rerun}</b>
            <span>个节点会重做</span>
          </div>
          <div className="agent-confirm-stat">
            <b>{item.blocked}</b>
            <span>个节点被阻塞</span>
          </div>
        </div>

        <div className="run-confirm-hint">
          等待确认还剩约 {Math.max(0, item.remaining)} 秒；关掉这张卡不会执行，超时后会按「没执行」返回给 dsh。
          {extra > 0 ? ` 后面还有 ${extra} 个待确认。` : ""}
        </div>
      </div>

      <div className="canvas-dialog-foot">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => setDismissed((prev) => [...prev, item.stepId])}
          disabled={busy}
        >
          暂不处理
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void decide(false)} disabled={busy}>
          拒绝
        </button>
        <button type="button" className="btn btn-primary btn-sm" onClick={() => void decide(true)} disabled={busy}>
          {busy ? <Spinner light /> : null}
          确认执行
        </button>
      </div>
    </Dialog>
  );
}
