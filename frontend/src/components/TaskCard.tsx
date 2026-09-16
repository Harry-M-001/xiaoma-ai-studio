import { useState } from "react";
import {
  Ban,
  Download,
  FileText,
  Maximize2,
  ImagePlus,
  AlertTriangle,
  RotateCcw,
  ScrollText,
  Trash2,
} from "lucide-react";
import type { Task } from "../types";
import { Modal, Spinner, StatusBadge, formatTime, isRunning } from "./common";
import { api, authToken } from "../api";

/** 任务类型中文名（文档 = 自动链的 Markdown 产物） */
const KIND_LABEL: Record<string, string> = {
  image: "图片",
  video: "视频",
  text: "文档",
  workflow: "工作流",
};

export function downloadUrl(assetId: number) {
  const t = authToken.get();
  return `/api/assets/${assetId}/download${t ? `?access_token=${encodeURIComponent(t)}` : ""}`;
}

export default function TaskCard({
  task,
  onPreview,
  onUseRef,
  onRetry,
  onCancel,
  onDelete,
}: {
  task: Task;
  onPreview?: (url: string, kind: string, downloadUrl?: string) => void;
  onUseRef?: (asset: { id: number; url: string }) => void;
  onRetry?: (task: Task) => void;
  onCancel?: (task: Task) => void;
  onDelete?: (task: Task) => void;
}) {
  const running = isRunning(task.status);
  const retryable = !running;

  const [logOpen, setLogOpen] = useState(false);
  const [logLines, setLogLines] = useState<string[] | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  /** 任务日志：先看这里，多半不用去问作者 */
  const openLogs = async () => {
    setLogOpen(true);
    if (logLines !== null) return;
    setLogLoading(true);
    try {
      const r = await api.taskLogs(task.id);
      setLogLines(r.lines);
    } catch {
      setLogLines([]);
    } finally {
      setLogLoading(false);
    }
  };

  return (
    <div className="card task-card">
      <div className="task-head">
        <StatusBadge status={task.status} />
        <span className="task-prompt" title={task.prompt}>
          {task.prompt}
        </span>
        <span className="muted" style={{ fontSize: 11.5, flexShrink: 0 }}>
          {formatTime(task.created_at)}
        </span>
      </div>

      <div className="task-meta">
        <span className="task-kind">{KIND_LABEL[task.kind] ?? task.kind}</span>
        <span className="task-model" title={task.model}>
          {task.model}
        </span>
      </div>

      {running && (
        <div className="progress mt8">
          <div className="progress-bar" style={{ width: `${task.status === "processing" ? Math.max(task.progress, 15) : 4}%` }} />
        </div>
      )}

      {task.status === "failed" && task.error && (
        <div className="task-error">
          <AlertTriangle size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
          {task.error}
        </div>
      )}

      {task.assets.length > 0 && task.kind === "image" && (
        <div className="gen-grid">
          {task.assets.map((a) => (
            <div key={a.id} className="gen-item" onClick={() => onPreview?.(a.url, "image", downloadUrl(a.id))}>
              <img src={a.url} alt={task.prompt} loading="lazy" />
              <div className="hover-bar" onClick={(e) => e.stopPropagation()}>
                {onUseRef && (
                  <button title="用作参考图" onClick={() => onUseRef(a)}>
                    <ImagePlus />
                  </button>
                )}
                <a title="下载" href={downloadUrl(a.id)}>
                  <Download />
                </a>
                <button title="放大查看" onClick={() => onPreview?.(a.url, "image", downloadUrl(a.id))}>
                  <Maximize2 />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {task.assets.length > 0 && task.kind === "video" && (
        <div className="video-result mt8">
          {task.assets.map((a) => (
            <video key={a.id} src={a.url} controls />
          ))}
          <div className="row mt8">
            {task.assets.map((a) => (
              <a key={a.id} className="btn btn-ghost btn-sm" href={downloadUrl(a.id)}>
                <Download size={14} />
                下载视频
              </a>
            ))}
          </div>
        </div>
      )}

      {task.assets.some((a) => a.kind === "document") && (
        <div className="doc-result mt8">
          {task.assets
            .filter((a) => a.kind === "document")
            .map((a) => (
              <div key={a.id} className="doc-result-row">
                <FileText size={14} />
                <span className="doc-result-name">Markdown 文稿</span>
                {onPreview && (
                  <button className="btn btn-ghost btn-sm" onClick={() => onPreview(a.url, "document", downloadUrl(a.id))}>
                    查看正文
                  </button>
                )}
                <a className="btn btn-ghost btn-sm" href={downloadUrl(a.id)}>
                  <Download size={14} />
                  下载
                </a>
              </div>
            ))}
        </div>
      )}

      <div className="task-actions">
        <button className="btn btn-ghost btn-sm" onClick={() => void openLogs()}>
          <ScrollText size={14} />
          日志
        </button>
        {onCancel && running && (
          <button className="btn btn-ghost btn-sm" onClick={() => onCancel(task)}>
            <Ban size={14} />
            取消
          </button>
        )}
        {onRetry && retryable && (
          <button className="btn btn-ghost btn-sm" onClick={() => onRetry(task)}>
            <RotateCcw size={14} />
            重新生成
          </button>
        )}
        {onDelete && (
          <button className="btn btn-ghost btn-sm task-delete" onClick={() => onDelete(task)}>
            <Trash2 size={14} />
            删除
          </button>
        )}
      </div>

      {logOpen && (
        <Modal title={`任务 #${task.id} 的日志`} onClose={() => setLogOpen(false)}>
          {logLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spinner />
            </div>
          ) : logLines && logLines.length > 0 ? (
            <>
              <div className="about-note" style={{ marginBottom: 10 }}>
                接口密钥、URL 查询参数与系统用户名已替换为占位符，可以直接复制。
              </div>
              <pre className="log-preview">{logLines.join("\n")}</pre>
            </>
          ) : (
            <div className="about-note">
              这个任务没有留下日志。服务重启之后内存里的任务日志会清空；
              完整记录仍在日志文件里，「系统设置 → 关于与更新 → 导出日志」可以看到。
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
