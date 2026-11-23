import { useEffect, useMemo, useState } from "react";
import type { Edge, Node } from "@xyflow/react";
import { Download, Play, Save, Sparkles } from "lucide-react";
import { api } from "../api";
import { toCanvasDocNodes } from "../canvasChain";
import type {
  CanvasNodeData,
  TemplatePreflight,
  WorkflowTemplateMeta,
} from "../types";
import { Empty, Modal, Spinner } from "./common";
import { useToast } from "./Toast";

const MODALITY_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  audio: "音频",
};

/**
 * 工作流模板。
 *
 * 两件事：把画布上选中的一段存成模板；把内置/自己存的模板插进当前画布。
 * 插入前可以先跑一次「依赖预检」——模板再好，缺文本/图片/视频模型时一样跑不动，
 * 这件事必须在点「插入」之前说清楚，而不是等运行整图才发现。
 */
export default function WorkflowTemplates({
  projectId,
  revision,
  nodes,
  edges,
  onClose,
  onInserted,
}: {
  projectId: number;
  /** 当前画布的服务端修订号（插入也走比较并交换） */
  revision: number;
  nodes: Node<CanvasNodeData>[];
  edges: Edge[];
  onClose: () => void;
  onInserted: (revision: number) => void;
}) {
  const toast = useToast();
  const [items, setItems] = useState<WorkflowTemplateMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [values, setValues] = useState<Record<string, Record<string, string>>>({});
  const [checked, setChecked] = useState<Record<string, TemplatePreflight>>({});
  const [saveName, setSaveName] = useState("");

  const selectedNodes = useMemo(() => {
    const picked = nodes.filter((n) => n.selected);
    return picked.length > 0 ? picked : nodes;
  }, [nodes]);

  const load = async () => {
    setItems(await api.listWorkflowTemplates());
  };

  useEffect(() => {
    (async () => {
      try {
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "读取模板失败");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const preflight = async (tpl: WorkflowTemplateMeta) => {
    setBusy(tpl.id);
    try {
      const r = await api.preflightWorkflowTemplate(tpl.id);
      setChecked((prev) => ({ ...prev, [tpl.id]: r }));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "预检失败");
    } finally {
      setBusy("");
    }
  };

  const insert = async (tpl: WorkflowTemplateMeta) => {
    setBusy(tpl.id);
    try {
      const r = await api.insertWorkflowTemplate(
        projectId,
        tpl.id,
        values[tpl.id] ?? {},
        revision,
      );
      toast.success(`已插入「${tpl.name}」（${r.nodeIds.length} 个节点）`);
      onInserted(r.revision);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "插入失败");
    } finally {
      setBusy("");
    }
  };

  const saveSelection = async () => {
    const name = saveName.trim();
    if (!name) {
      toast.info("先给模板起个名字");
      return;
    }
    const ids = new Set(selectedNodes.map((n) => n.id));
    setBusy("__save__");
    try {
      await api.createWorkflowTemplate({
        name,
        description: "",
        nodes: toCanvasDocNodes(selectedNodes),
        edges: edges
          .filter((e) => ids.has(e.source) && ids.has(e.target))
          .map((e) => ({
            id: e.id,
            source: e.source,
            sourceHandle: e.sourceHandle ?? null,
            target: e.target,
            targetHandle: e.targetHandle ?? null,
          })),
        inputs: [],
      });
      setSaveName("");
      toast.success("已存为模板");
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存模板失败");
    } finally {
      setBusy("");
    }
  };

  return (
    <Modal
      title="工作流模板"
      onClose={onClose}
      wide
      footer={
        <>
          <span className="canvas-dialog-hint">
            模板只是把一段画布放进来；模型依赖可以先「预检」再插入。
          </span>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
        </>
      }
    >
      <div className="tpl-save">
        <input
          className="input"
          placeholder="把选中节点存成模板（不选则存整张画布）"
          value={saveName}
          onChange={(e) => setSaveName(e.target.value)}
        />
        <button className="btn btn-ghost btn-sm" onClick={() => void saveSelection()} disabled={!!busy}>
          {busy === "__save__" ? <Spinner /> : <Save size={14} />}
          存为模板
        </button>
        <span className="muted" style={{ fontSize: 12 }}>
          将存 {selectedNodes.length} 个节点
        </span>
      </div>

      {loading ? (
        <div style={{ padding: 28, textAlign: "center" }}>
          <Spinner />
        </div>
      ) : items.length === 0 ? (
        <Empty icon={<Sparkles />} title="还没有模板" desc="先选中一段画布存成模板，或直接使用内置模板。" />
      ) : (
        <div className="tpl-list">
          {items.map((tpl) => {
            const check = checked[tpl.id];
            return (
              <div className="tpl-card" key={tpl.id}>
                <div className="tpl-head">
                  <div>
                    <b>{tpl.name}</b>
                    <span className={`badge ${tpl.source === "builtin" ? "badge-primary" : ""}`}>
                      {tpl.source === "builtin" ? "内置" : `自存 v${tpl.version}`}
                    </span>
                  </div>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {tpl.nodeCount} 个节点
                    {tpl.requirements?.modalities?.length
                      ? ` · 需要 ${tpl.requirements.modalities
                          .map((m) => MODALITY_LABEL[m] ?? m)
                          .join(" / ")} 模型`
                      : ""}
                  </span>
                </div>
                <div className="tpl-desc">{tpl.description || "—"}</div>

                {tpl.inputs.length > 0 && (
                  <div className="tpl-inputs">
                    {tpl.inputs.map((inp) => (
                      <label key={inp.key}>
                        <span>{inp.label}</span>
                        <input
                          className="input"
                          placeholder={inp.hint || ""}
                          value={values[tpl.id]?.[inp.key] ?? ""}
                          onChange={(e) =>
                            setValues((prev) => ({
                              ...prev,
                              [tpl.id]: { ...(prev[tpl.id] ?? {}), [inp.key]: e.target.value },
                            }))
                          }
                        />
                      </label>
                    ))}
                  </div>
                )}

                {check && (
                  <div className={`tpl-check ${check.ok ? "ok" : "warn"}`}>
                    {check.ok ? (
                      <>依赖齐全（可用：{check.available.map((m) => MODALITY_LABEL[m] ?? m).join(" / ")}）</>
                    ) : (
                      <>
                        还缺：
                        {check.missingModalities.length > 0 &&
                          ` ${check.missingModalities.map((m) => MODALITY_LABEL[m] ?? m).join(" / ")} 模型`}
                        {check.missingNodes.length > 0 &&
                          ` ${check.missingNodes.map((n) => `「${n.label}」(${n.model || "未选"})`).join("、")}`}
                        。插入后到「模型服务」补齐即可。
                      </>
                    )}
                  </div>
                )}

                <div className="tpl-actions">
                  <button
                    className="btn btn-ghost btn-xs"
                    onClick={() => void preflight(tpl)}
                    disabled={!!busy}
                  >
                    {busy === tpl.id ? <Spinner /> : <Download size={12} />}
                    依赖预检
                  </button>
                  <button
                    className="btn btn-primary btn-xs"
                    onClick={() => void insert(tpl)}
                    disabled={!!busy}
                  >
                    <Play size={12} />
                    插入画布
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
