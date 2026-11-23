import { useEffect, useState } from "react";
import { Check, Clapperboard, Play, RotateCcw, X } from "lucide-react";
import { api, thumbUrl } from "../api";
import type { ShotRow, ShotTable } from "../types";
import { Empty, Modal, Spinner } from "./common";
import { useToast } from "./Toast";

const STATUS_LABEL: Record<string, string> = {
  pending: "待审",
  approved: "已通过",
  rejected: "已驳回",
};

/**
 * 镜头工作台（首版）。
 *
 * 它是**现有数据的视图**，不是又一条流水线：身份、首帧、视频候选、对白都来自
 * 已有的项目镜头表与任务产物；这一版只补三件事——稳定 shot_id、审核状态、按镜补跑。
 * 「送往导演台」仍走导演台自己的选片与合并，这里不另造一套排序与导出。
 */
export default function ShotWorkbench({
  projectId,
  onClose,
}: {
  projectId: number;
  onClose: () => void;
}) {
  const toast = useToast();
  const [table, setTable] = useState<ShotTable | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");

  const load = async () => {
    setTable(await api.listShots(projectId));
  };

  useEffect(() => {
    (async () => {
      try {
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "读取镜头失败");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const rerun = async (shot: ShotRow) => {
    setBusy(shot.shotId);
    try {
      const nodeId = table?.videoNodeId || table?.imageNodeId || undefined;
      const r = await api.runShot(projectId, shot.shotId, nodeId);
      toast.success(`镜头 ${shot.no} 已补跑（${r.taskCount} 个任务）`);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "补跑失败");
    } finally {
      setBusy("");
    }
  };

  const review = async (shot: ShotRow, status: string) => {
    setBusy(shot.shotId);
    try {
      await api.setShotStatus(projectId, shot.shotId, status);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存审核状态失败");
    } finally {
      setBusy("");
    }
  };

  const shots = table?.shots ?? [];

  return (
    <Modal
      title="镜头工作台"
      onClose={onClose}
      wide
      footer={
        <>
          <span className="canvas-dialog-hint">
            这一版只做「按镜补跑 + 审核」；选好片后在导演台合并成片。
          </span>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
        </>
      }
    >
      {loading ? (
        <div style={{ padding: 28, textAlign: "center" }}>
          <Spinner />
        </div>
      ) : shots.length === 0 ? (
        <Empty
          icon={<Clapperboard />}
          title="还没有镜头"
          desc="先把「分镜」节点接给逐镜出片或分镜图节点跑一次，这里会按镜号列出每一镜。"
        />
      ) : (
        <div className="shot-table">
          <div className="shot-row shot-head">
            <span>镜头</span>
            <span>首帧</span>
            <span>视频候选</span>
            <span>对白</span>
            <span>审核</span>
            <span />
          </div>
          {shots.map((s) => (
            <div className="shot-row" key={s.shotId}>
              <div className="shot-no">
                <b>#{s.no}</b>
                <span title={s.heading}>{s.heading || "—"}</span>
              </div>
              <div className="shot-frame">
                {s.images[0] ? (
                  <img src={thumbUrl(s.images[0].url, 320)} alt={`镜头 ${s.no} 首帧`} />
                ) : (
                  <span className="muted">—</span>
                )}
                {s.images.length > 1 && <em>+{s.images.length - 1}</em>}
              </div>
              <div className="shot-frames">
                {s.videos.length === 0 ? (
                  <span className="muted">还没有</span>
                ) : (
                  s.videos.slice(0, 3).map((v) => (
                    <a
                      key={v.id}
                      className="shot-video"
                      href={v.url}
                      target="_blank"
                      rel="noreferrer"
                      title={v.title}
                    >
                      <img src={thumbUrl(v.url, 320)} alt={`镜头 ${s.no} 视频候选`} />
                      {v.duration ? <em>{v.duration}s</em> : null}
                    </a>
                  ))
                )}
              </div>
              <div className="shot-dialogue" title={s.dialogue}>
                {s.dialogue || "—"}
              </div>
              <div>
                <span
                  className={`badge ${
                    s.status === "approved"
                      ? "badge-success"
                      : s.status === "rejected"
                        ? "badge-danger"
                        : ""
                  }`}
                >
                  {STATUS_LABEL[s.status] ?? s.status}
                </span>
              </div>
              <div className="shot-actions">
                <button
                  className="btn btn-ghost btn-xs"
                  onClick={() => void rerun(s)}
                  disabled={!!busy}
                  title="只补跑这一镜（其余镜头与已选结果不动）"
                >
                  {busy === s.shotId ? <Spinner /> : <Play size={12} />}
                  补跑
                </button>
                <button
                  className="btn btn-ghost btn-xs"
                  onClick={() => void review(s, s.status === "approved" ? "pending" : "approved")}
                  disabled={!!busy}
                  title={s.status === "approved" ? "取消通过" : "通过这一镜"}
                >
                  {s.status === "approved" ? <RotateCcw size={12} /> : <Check size={12} />}
                  {s.status === "approved" ? "撤销" : "通过"}
                </button>
                <button
                  className="btn btn-ghost btn-xs"
                  onClick={() => void review(s, s.status === "rejected" ? "pending" : "rejected")}
                  disabled={!!busy}
                  title={s.status === "rejected" ? "取消驳回" : "驳回这一镜"}
                >
                  <X size={12} />
                  {s.status === "rejected" ? "撤销" : "驳回"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
