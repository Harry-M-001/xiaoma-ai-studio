import { useEffect, type ReactNode } from "react";
import { X, Loader2, CheckCircle2, XCircle, Clock, Ban } from "lucide-react";
import type { ModelOption, TaskStatus } from "../types";

/* ---------------- Modal ---------------- */

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal>
        <div className="modal-head">
          <div className="modal-title">{title}</div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/* ---------------- Empty ---------------- */

export function Empty({
  icon,
  title,
  desc,
  action,
}: {
  icon: ReactNode;
  title: string;
  desc?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">{icon}</div>
      <div className="empty-title">{title}</div>
      {desc && <div className="empty-desc">{desc}</div>}
      {action && <div className="mt16">{action}</div>}
    </div>
  );
}

/* ---------------- 任务状态 ---------------- */

/**
 * 任务状态表。
 *
 * 后端写库用的是 `completed`；`succeeded` 只是 provider 层的上游状态词，
 * 历史别名也一并收下。未知状态**不做兜底伪装**——直接显示原始值，
 * 免得状态值对不上时被静默显示成「排队中」。
 */
const DONE_META = { text: "已完成", cls: "badge-success", icon: <CheckCircle2 size={12} /> };

const STATUS_MAP: Record<string, { text: string; cls: string; icon?: ReactNode }> = {
  pending: { text: "排队中", cls: "badge-warning", icon: <Clock size={12} /> },
  processing: { text: "生成中", cls: "badge-primary", icon: <Loader2 size={12} className="spin-ic" /> },
  completed: DONE_META,
  succeeded: DONE_META,
  failed: { text: "失败", cls: "badge-danger", icon: <XCircle size={12} /> },
  cancelled: { text: "已取消", cls: "", icon: <Ban size={12} /> },
};

export function StatusBadge({ status }: { status: TaskStatus }) {
  const s = STATUS_MAP[status] ?? { text: String(status), cls: "" };
  return (
    <span className={`badge ${s.cls}`}>
      {s.icon}
      {s.text}
    </span>
  );
}

export const isRunning = (s: TaskStatus) => s === "pending" || s === "processing";

/** 任务是否已成功结束（两种情况都算：后端写的 completed、历史别名 succeeded）。 */
export const isDone = (s: TaskStatus) => s === "completed" || s === "succeeded";

/* ---------------- 模型选择 ---------------- */

export function ModelSelect({
  models,
  value,
  onChange,
  placeholder = "选择模型",
}: {
  models: ModelOption[];
  value: string;
  onChange: (key: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="select-wrap">
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{placeholder}</option>
        {models.map((m) => (
          <option key={m.key} value={m.key}>
            {m.label || m.name}（{m.service_name}）
          </option>
        ))}
      </select>
    </div>
  );
}

/* ---------------- 小组件 ---------------- */

export function Spinner({ light }: { light?: boolean }) {
  return <span className={`spinner ${light ? "light" : ""}`} />;
}

export function formatTime(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (sameDay) return hm;
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}

export function formatSize(bytes: number) {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
