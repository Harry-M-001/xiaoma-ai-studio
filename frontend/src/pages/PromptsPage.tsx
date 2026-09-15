import { useEffect, useMemo, useState } from "react";
import { ArrowRight, BookOpen, Copy, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { api } from "../api";
import { setDraftPrompt, type DraftTarget } from "../promptDraft";
import type { PromptItem } from "../types";
import { Empty, Modal, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

const MODALITY_LABEL: Record<string, string> = { text: "文本", image: "图片", video: "视频" };
const MODALITY_ROUTE: Record<string, DraftTarget> = { text: "chat", image: "image", video: "video" };
const MODALITY_USE_LABEL: Record<string, string> = {
  text: "去对话",
  image: "去图片生成",
  video: "去视频生成",
};

type Filter = "all" | "text" | "image" | "video";

interface EditorState {
  id: number | null;
  version: number | null;
  title: string;
  modality: string;
  tags: string;
  description: string;
  content: string;
}

const EMPTY_EDITOR: EditorState = {
  id: null,
  version: null,
  title: "",
  modality: "image",
  tags: "",
  description: "",
  content: "",
};

export default function PromptsPage({ onNavigate }: { onNavigate: (route: string) => void }) {
  const toast = useToast();
  const [items, setItems] = useState<PromptItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      setItems(await api.getPrompts());
    } catch {
      toast.error("提示词加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return items.filter((p) => {
      if (filter !== "all" && p.modality !== filter) return false;
      if (!kw) return true;
      return (
        p.title.toLowerCase().includes(kw) ||
        p.content.toLowerCase().includes(kw) ||
        p.tags.toLowerCase().includes(kw)
      );
    });
  }, [items, filter, q]);

  const copy = async (p: PromptItem) => {
    try {
      await navigator.clipboard.writeText(p.content);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动选择内容复制");
    }
  };

  const useIn = (p: PromptItem) => {
    const target = MODALITY_ROUTE[p.modality];
    if (!target) return;
    setDraftPrompt(target, p.content);
    onNavigate(target);
  };

  const openCreate = () => setEditor({ ...EMPTY_EDITOR, modality: filter === "all" ? "image" : filter });

  const openEdit = (p: PromptItem) =>
    setEditor({
      id: p.id,
      version: p.version ?? null,
      title: p.title,
      modality: p.modality,
      tags: p.tags,
      description: p.description,
      content: p.content,
    });

  const remove = async (p: PromptItem) => {
    if (!window.confirm(`删除提示词「${p.title}」？此操作可在「系统设置 → 变更审计」中回滚。`)) return;
    try {
      await api.deleteSchemaRow("prompts", p.id);
      toast.success("已删除");
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  const save = async () => {
    if (!editor) return;
    if (!editor.title.trim() || !editor.content.trim()) {
      toast.error("标题和提示词内容不能为空");
      return;
    }
    setSaving(true);
    try {
      const payload: Record<string, unknown> = {
        title: editor.title.trim(),
        modality: editor.modality,
        tags: editor.tags.trim(),
        description: editor.description.trim(),
        content: editor.content,
        enabled: true,
      };
      if (editor.id == null) {
        payload.key = `custom-${Date.now()}`;
        payload.sort_order = 100;
        await api.createSchemaRow("prompts", payload);
      } else {
        payload.version = editor.version;
        await api.updateSchemaRow("prompts", editor.id, payload);
      }
      toast.success("已保存");
      setEditor(null);
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">提示词库</div>
          <div className="page-desc">
            可复用的提示词模板。{"{花括号}"} 为占位符，使用时替换；也可直接复制全文。
          </div>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>
          <Plus size={16} />
          新建提示词
        </button>
      </div>

      <div className="prompt-toolbar">
        <div className="select-wrap prompt-search">
          <Search size={15} className="prompt-search-ic" />
          <input
            className="input"
            placeholder="搜索标题 / 内容 / 标签"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="segmented">
          {(["all", "text", "image", "video"] as Filter[]).map((f) => (
            <button key={f} className={filter === f ? "active" : ""} onClick={() => setFilter(f)}>
              {f === "all" ? "全部" : MODALITY_LABEL[f]}
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
            icon={<BookOpen />}
            title={items.length === 0 ? "还没有提示词模板" : "没有匹配的提示词"}
            desc={
              items.length === 0
                ? "点击右上角「新建提示词」，把常用写法沉淀成模板，一次编写处处复用。"
                : "换个关键词或筛选条件试试。"
            }
          />
        </div>
      ) : (
        <div className="prompt-grid">
          {filtered.map((p) => (
            <div key={p.id} className="card prompt-card">
              <div className="prompt-card-head">
                <div className="prompt-title" title={p.title}>
                  {p.title}
                </div>
                <span className="badge badge-primary">{MODALITY_LABEL[p.modality] ?? p.modality}</span>
              </div>
              {p.description && <div className="prompt-desc">{p.description}</div>}
              <div className="prompt-content">{p.content}</div>
              {p.tags && (
                <div className="prompt-tags">
                  {p.tags
                    .split(/[,，]/)
                    .map((t) => t.trim())
                    .filter(Boolean)
                    .map((t) => (
                      <span key={t} className="prompt-tag">
                        {t}
                      </span>
                    ))}
                </div>
              )}
              <div className="prompt-actions">
                <button className="btn btn-primary btn-sm" onClick={() => useIn(p)}>
                  {MODALITY_USE_LABEL[p.modality] ?? "去使用"}
                  <ArrowRight size={14} />
                </button>
                <button className="btn btn-ghost btn-sm" onClick={() => copy(p)}>
                  <Copy size={14} />
                  复制
                </button>
                <button className="icon-btn" title="编辑" onClick={() => openEdit(p)}>
                  <Pencil size={15} />
                </button>
                <button className="icon-btn" title="删除" onClick={() => remove(p)}>
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {editor && (
        <Modal
          title={editor.id == null ? "新建提示词" : "编辑提示词"}
          onClose={() => setEditor(null)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setEditor(null)} disabled={saving}>
                取消
              </button>
              <button className="btn btn-primary" onClick={save} disabled={saving}>
                {saving ? <Spinner light /> : null}
                {saving ? "保存中…" : "保存"}
              </button>
            </>
          }
        >
          <div className="field">
            <label className="field-label">标题</label>
            <input
              className="input"
              placeholder="如：电影感人像摄影"
              value={editor.title}
              onChange={(e) => setEditor({ ...editor, title: e.target.value })}
            />
          </div>
          <div className="field">
            <label className="field-label">适用场景</label>
            <div className="segmented">
              {(["text", "image", "video"] as const).map((m) => (
                <button
                  key={m}
                  className={editor.modality === m ? "active" : ""}
                  onClick={() => setEditor({ ...editor, modality: m })}
                >
                  {MODALITY_LABEL[m]}
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <label className="field-label">标签（可选）</label>
            <input
              className="input"
              placeholder="逗号分隔，如：人像,摄影,电影感"
              value={editor.tags}
              onChange={(e) => setEditor({ ...editor, tags: e.target.value })}
            />
          </div>
          <div className="field">
            <label className="field-label">使用说明（可选）</label>
            <input
              className="input"
              placeholder="如：把 {主体} 替换成想画的人或角色"
              value={editor.description}
              onChange={(e) => setEditor({ ...editor, description: e.target.value })}
            />
          </div>
          <div className="field">
            <label className="field-label">提示词内容</label>
            <textarea
              className="textarea"
              rows={7}
              placeholder="模板正文，可用 {占位符} 标记需要替换的部分"
              value={editor.content}
              onChange={(e) => setEditor({ ...editor, content: e.target.value })}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}
