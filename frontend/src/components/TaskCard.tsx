import { useState } from "react";
import {
  Ban,
  Download,
  FileText,
  FolderOpen,
  Maximize2,
  ImagePlus,
  AlertTriangle,
  RotateCcw,
  ScrollText,
  Trash2,
} from "lucide-react";
import type { Task, TaskRetryIn } from "../types";
import { Modal, Spinner, StatusBadge, formatTime, isRunning } from "./common";
import { useToast } from "./Toast";
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
  onRetry?: (task: Task, overrides?: TaskRetryIn) => void;
  onCancel?: (task: Task) => void;
  onDelete?: (task: Task) => void;
}) {
  const toast = useToast();
  const running = isRunning(task.status);
  const retryable = !running;

  const [logOpen, setLogOpen] = useState(false);
  const [logLines, setLogLines] = useState<string[] | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  const [retryOpen, setRetryOpen] = useState(false);
  const [retryForm, setRetryForm] = useState<TaskRetryIn>({});
  const [revealing, setRevealing] = useState(false);

  // 任务自己的参数快照：弹窗直接拿它预填，不需要再向用户问一遍「上次是怎么配的」
  const params = (task.params ?? {}) as Record<string, unknown>;
  const editablePrompt = task.kind === "image" || task.kind === "video";

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

  const openRetry = () => {
    setRetryForm({
      prompt: task.prompt,
      ...(task.kind === "image"
        ? { n: Number(params.n ?? 1), size: String(params.size ?? "1024x1024") }
        : {}),
      ...(task.kind === "video"
        ? {
            duration: Number(params.duration ?? 5),
            ratio: String(params.ratio ?? "16:9"),
            resolution: String(params.resolution ?? "720p"),
          }
        : {}),
    });
    setRetryOpen(true);
  };

  const submitRetry = () => {
    // 预填值本来就来自快照，所以原样发回去等价于「不改」；只有用户真改了才会生效
    const overrides: TaskRetryIn = { ...retryForm };
    if (!editablePrompt) delete overrides.prompt;
    onRetry?.(task, overrides);
    setRetryOpen(false);
  };

  /** 在系统文件管理器里定位产物——自托管应用，「文件到底存哪了」是个真问题 */
  const reveal = async () => {
    setRevealing(true);
    try {
      const r = await api.revealTask(task.id);
      if (!r.ok) toast.error(`没能唤起文件管理器，产物在：${r.path}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "打开目录失败");
    } finally {
      setRevealing(false);
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
        {typeof params.shot_no === "string" && params.shot_no ? (
          <span className="task-shot" title="逐镜任务：这是分镜表里的第几镜">
            镜头 {params.shot_no}
          </span>
        ) : null}
        {task.retry_of_task_id ? (
          <span className="task-retry-of" title="这是「重新生成」出来的任务">
            重跑自 #{task.retry_of_task_id}
          </span>
        ) : null}
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
        {task.assets.length > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={() => void reveal()} disabled={revealing}>
            <FolderOpen size={14} />
            打开目录
          </button>
        )}
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
          <button className="btn btn-ghost btn-sm" onClick={openRetry}>
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

      {retryOpen && (
        <Modal
          title={`按当时的参数重新生成（原任务 #${task.id}）`}
          onClose={() => setRetryOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setRetryOpen(false)}>
                取消
              </button>
              <button className="btn btn-primary" onClick={submitRetry}>
                开始生成
              </button>
            </>
          }
        >
          <div className="about-note" style={{ marginBottom: 12 }}>
            下面填的是这个任务<strong>当时的参数</strong>，改哪项就只改哪项，其余照旧。原任务会保留作对照。
          </div>

          <div className="field">
            <label className="field-label">模型</label>
            <input className="input" value={task.model} readOnly />
          </div>

          {editablePrompt && (
            <div className="field">
              <label className="field-label">提示词</label>
              <textarea
                className="input"
                rows={5}
                value={retryForm.prompt ?? ""}
                onChange={(e) => setRetryForm({ ...retryForm, prompt: e.target.value })}
              />
            </div>
          )}

          {task.kind === "image" && (
            <div className="row" style={{ gap: 12 }}>
              <div className="field" style={{ flex: 1 }}>
                <label className="field-label">尺寸</label>
                <input
                  className="input"
                  value={retryForm.size ?? ""}
                  onChange={(e) => setRetryForm({ ...retryForm, size: e.target.value })}
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label className="field-label">张数</label>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={4}
                  value={retryForm.n ?? 1}
                  onChange={(e) => setRetryForm({ ...retryForm, n: Number(e.target.value) || 1 })}
                />
              </div>
            </div>
          )}

          {task.kind === "video" && (
            <>
              <div className="row" style={{ gap: 12 }}>
                <div className="field" style={{ flex: 1 }}>
                  <label className="field-label">时长（秒）</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={30}
                    value={retryForm.duration ?? 5}
                    onChange={(e) =>
                      setRetryForm({ ...retryForm, duration: Number(e.target.value) || 5 })
                    }
                  />
                </div>
                <div className="field" style={{ flex: 1 }}>
                  <label className="field-label">比例</label>
                  <input
                    className="input"
                    value={retryForm.ratio ?? ""}
                    onChange={(e) => setRetryForm({ ...retryForm, ratio: e.target.value })}
                  />
                </div>
                <div className="field" style={{ flex: 1 }}>
                  <label className="field-label">分辨率</label>
                  <input
                    className="input"
                    value={retryForm.resolution ?? ""}
                    onChange={(e) => setRetryForm({ ...retryForm, resolution: e.target.value })}
                  />
                </div>
              </div>
              {typeof params.shot_no === "string" && params.shot_no ? (
                <div className="about-note">
                  这是逐镜任务，重跑仍会用原来的镜号与首帧（镜头 {String(params.shot_no)}）。
                  想换首帧请回画布上重跑那个节点。
                </div>
              ) : null}
            </>
          )}

          {!editablePrompt && (
            <div className="about-note">
              文档与工作流任务按原参数整段重跑，参数在节点/工作流里调整。
            </div>
          )}
        </Modal>
      )}

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
