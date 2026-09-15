import { useEffect, useState } from "react";
import { FolderOpen, MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";
import { api } from "../api";
import type { Project } from "../types";
import { Empty, Modal, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

export default function ProjectsPage({ onOpenCanvas }: { onOpenCanvas?: (p: Project) => void }) {
  const toast = useToast();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [saving, setSaving] = useState(false);
  const [renaming, setRenaming] = useState<Project | null>(null);
  const [renameText, setRenameText] = useState("");
  const [menuFor, setMenuFor] = useState<number | null>(null);

  const load = async () => {
    try {
      setProjects(await api.listProjects());
    } catch {
      toast.error("项目列表加载失败");
      setProjects([]);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openCreate = () => {
    setFormName("");
    setFormDesc("");
    setCreating(true);
  };

  const doCreate = async () => {
    if (!formName.trim()) {
      toast.error("请输入项目名称");
      return;
    }
    setSaving(true);
    try {
      await api.createProject(formName.trim(), formDesc.trim());
      await load();
      toast.success("项目已创建");
      setCreating(false);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "创建失败");
    } finally {
      setSaving(false);
    }
  };

  const doRename = async () => {
    if (!renaming || !renameText.trim()) return;
    try {
      await api.updateProject(renaming.id, { name: renameText.trim() });
      await load();
      toast.success("已重命名");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "重命名失败");
    } finally {
      setRenaming(null);
    }
  };

  const doDelete = async (p: Project) => {
    if (!window.confirm(`删除项目「${p.name}」？此操作不可恢复。`)) return;
    try {
      await api.deleteProject(p.id);
      await load();
      toast.success("已删除");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  const openCanvas = (p: Project) => {
    if (onOpenCanvas) onOpenCanvas(p);
    else toast.info(`「${p.name}」的画布编辑器即将上线`);
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">项目</div>
          <div className="page-desc">一次完整作品的容器。画布编辑器重做后将在这里接入，现阶段可先建项目规划作品。</div>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>
          <Plus size={16} />
          新建项目
        </button>
      </div>

      {projects === null ? (
        <div className="loading-page">
          <Spinner />
        </div>
      ) : projects.length === 0 ? (
        <div className="card">
          <Empty
            icon={<FolderOpen />}
            title="还没有项目"
            desc="新建一个项目，把一次完整创作（角色设定、分镜、成片）组织起来。"
          />
        </div>
      ) : (
        <div className="projects-grid">
          {projects.map((p) => (
            <div key={p.id} className="card project-card">
              <div className="project-card-head">
                <div className="project-card-icon">
                  <FolderOpen size={18} />
                </div>
                <div className="project-card-title" title={p.name}>
                  {p.name}
                </div>
                <div className="project-card-menu">
                  <button className="icon-btn" onClick={() => setMenuFor(menuFor === p.id ? null : p.id)}>
                    <MoreHorizontal size={15} />
                  </button>
                  {menuFor === p.id && (
                    <div className="project-menu">
                      <button
                        onClick={() => {
                          setRenaming(p);
                          setRenameText(p.name);
                          setMenuFor(null);
                        }}
                      >
                        <Pencil size={13} />
                        重命名
                      </button>
                      <button
                        onClick={() => {
                          doDelete(p);
                          setMenuFor(null);
                        }}
                      >
                        <Trash2 size={13} />
                        删除
                      </button>
                    </div>
                  )}
                </div>
              </div>
              <div className="project-card-desc">{p.description || "暂无描述"}</div>
              <div className="project-card-foot">
                <span className="muted">更新于 {p.updated_at.slice(0, 10)}</span>
                <button className="btn btn-ghost btn-sm" onClick={() => openCanvas(p)}>
                  打开画布
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {creating && (
        <Modal
          title="新建项目"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setCreating(false)} disabled={saving}>
                取消
              </button>
              <button className="btn btn-primary" disabled={saving} onClick={doCreate}>
                {saving ? <Spinner light /> : null}
                创建
              </button>
            </>
          }
        >
          <div className="field">
            <label className="field-label">项目名称</label>
            <input
              className="input"
              placeholder="例如：小马宝莉·暮光闪闪的日常"
              value={formName}
              onChange={(e) => setFormName(e.target.value)}
              autoFocus
            />
          </div>
          <div className="field">
            <label className="field-label">描述（可选）</label>
            <textarea
              className="textarea"
              rows={3}
              placeholder="这个项目要做什么、用哪些模型…"
              value={formDesc}
              onChange={(e) => setFormDesc(e.target.value)}
            />
          </div>
        </Modal>
      )}

      {renaming && (
        <Modal
          title="重命名项目"
          onClose={() => setRenaming(null)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setRenaming(null)}>
                取消
              </button>
              <button className="btn btn-primary" onClick={doRename}>
                保存
              </button>
            </>
          }
        >
          <input className="input" value={renameText} onChange={(e) => setRenameText(e.target.value)} autoFocus />
        </Modal>
      )}
    </div>
  );
}
