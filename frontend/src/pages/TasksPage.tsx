import { useCallback, useEffect, useMemo, useState } from "react";
import { ListChecks, RefreshCw } from "lucide-react";
import { api } from "../api";
import type { Task, TaskStatus } from "../types";
import { Empty, Spinner, isDone, isRunning } from "../components/common";
import TaskCard from "../components/TaskCard";
import Lightbox from "../components/Lightbox";
import { useToast } from "../components/Toast";

type KindFilter = "all" | "image" | "video";
type StatusFilter = "all" | "running" | "completed" | "failed";

const STATUS_MATCH: Record<Exclude<StatusFilter, "all">, (s: TaskStatus) => boolean> = {
  running: (s) => isRunning(s),
  completed: (s) => isDone(s),
  failed: (s) => s === "failed" || s === "cancelled",
};

export default function TasksPage() {
  const toast = useToast();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [preview, setPreview] = useState<{ url: string; kind: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setTasks(await api.listTasks(undefined, 200));
    } catch {
      toast.error("任务列表加载失败");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 有进行中的任务时自动轮询刷新
  const runningCount = tasks.filter((t) => isRunning(t.status)).length;
  useEffect(() => {
    if (runningCount === 0) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [runningCount, load]);

  const filtered = useMemo(
    () =>
      tasks.filter((t) => {
        if (kindFilter !== "all" && t.kind !== kindFilter) return false;
        if (statusFilter !== "all" && !STATUS_MATCH[statusFilter](t.status)) return false;
        return true;
      }),
    [tasks, kindFilter, statusFilter]
  );

  const stats = useMemo(
    () => ({
      running: runningCount,
      done: tasks.filter((t) => isDone(t.status)).length,
      failed: tasks.filter((t) => t.status === "failed" || t.status === "cancelled").length,
    }),
    [tasks, runningCount]
  );

  const retry = async (t: Task) => {
    try {
      const nt = await api.retryTask(t.id);
      setTasks((prev) => [nt, ...prev]);
      toast.success("已按原参数重新发起");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "重试失败");
    }
  };

  const cancel = async (t: Task) => {
    try {
      const updated = await api.cancelTask(t.id);
      setTasks((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      toast.success("已取消");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "取消失败");
    }
  };

  const remove = async (t: Task) => {
    if (!window.confirm("删除该任务记录？已生成的产物会保留在资产库。")) return;
    try {
      await api.deleteTask(t.id);
      setTasks((prev) => prev.filter((x) => x.id !== t.id));
      toast.success("已删除");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">任务中心</div>
          <div className="page-desc">
            全部图片 / 视频生成任务集中在这里：进行中自动刷新，失败可一键按原参数重试。
          </div>
        </div>
        <button className="btn btn-ghost" onClick={load} title="刷新">
          <RefreshCw size={15} />
          刷新
        </button>
      </div>

      <div className="prompt-toolbar">
        <div className="segmented">
          {(
            [
              ["all", `全部 ${tasks.length}`],
              ["running", `进行中 ${stats.running}`],
              ["completed", `已完成 ${stats.done}`],
              ["failed", `失败/取消 ${stats.failed}`],
            ] as [StatusFilter, string][]
          ).map(([f, label]) => (
            <button key={f} className={statusFilter === f ? "active" : ""} onClick={() => setStatusFilter(f)}>
              {label}
            </button>
          ))}
        </div>
        <div className="segmented">
          {(
            [
              ["all", "全部类型"],
              ["image", "图片"],
              ["video", "视频"],
            ] as [KindFilter, string][]
          ).map(([f, label]) => (
            <button key={f} className={kindFilter === f ? "active" : ""} onClick={() => setKindFilter(f)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="loading-page">
          <Spinner />
        </div>
      ) : filtered.length === 0 ? (
        <div className="card">
          <Empty
            icon={<ListChecks />}
            title={tasks.length === 0 ? "还没有生成任务" : "没有匹配的任务"}
            desc={
              tasks.length === 0
                ? "到「图片生成」或「视频生成」发起第一次创作，任务会自动出现在这里。"
                : "换个筛选条件试试。"
            }
          />
        </div>
      ) : (
        <div className="task-list">
          {filtered.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              onPreview={(url, kind) => setPreview({ url, kind })}
              onRetry={retry}
              onCancel={cancel}
              onDelete={remove}
            />
          ))}
        </div>
      )}

      {preview && <Lightbox url={preview.url} kind={preview.kind} onClose={() => setPreview(null)} />}
    </div>
  );
}
