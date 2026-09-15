import { useEffect, useState, type ReactNode } from "react";
import { Inbox, Pencil, Plus, Trash2 } from "lucide-react";
import { api, ApiError } from "../api";
import type { FieldSpecMeta, SchemaRow, TableSpecMeta } from "../types";
import { Empty, Modal, Spinner } from "./common";
import { useToast } from "./Toast";

/** 表单里的原始值：布尔走开关，其余统一以字符串暂存，提交时再按字段类型转换 */
type FormState = Record<string, string | boolean>;

/** 把任意后端值安全地转成可展示文本 */
function stringifyValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v) ?? "";
  } catch {
    return String(v);
  }
}

/** 按字段描述构造表单初值（新增用 default，编辑用当前行值） */
function buildForm(fields: FieldSpecMeta[], row?: SchemaRow): FormState {
  const form: FormState = {};
  for (const field of fields) {
    const raw = row ? row[field.name] : undefined;
    const value = raw === undefined || raw === null ? field.default : raw;
    if (field.type === "bool") {
      form[field.name] = Boolean(value);
    } else if (field.type === "json") {
      form[field.name] =
        value === undefined || value === null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
    } else {
      form[field.name] = value === undefined || value === null ? "" : String(value);
    }
  }
  return form;
}

/** 按字段类型把表单值序列化成后端可接受的载荷；返回错误文案表示校验未通过 */
function serialize(fields: FieldSpecMeta[], form: FormState): { payload: Record<string, unknown>; error?: string } {
  const payload: Record<string, unknown> = {};
  for (const field of fields) {
    if (field.readonly) continue;
    const raw = form[field.name];

    if (field.type === "bool") {
      payload[field.name] = Boolean(raw);
      continue;
    }

    const text = typeof raw === "string" ? raw.trim() : stringifyValue(raw).trim();
    if (text === "") {
      if (field.required) return { payload, error: `「${field.label}」不能为空` };
      continue; // 可选字段留空则不提交，避免覆盖后端默认值
    }

    if (field.type === "int") {
      const n = Number(text);
      if (!Number.isFinite(n) || !Number.isInteger(n)) {
        return { payload, error: `「${field.label}」需要填写整数` };
      }
      payload[field.name] = n;
    } else if (field.type === "float") {
      const n = Number(text);
      if (!Number.isFinite(n)) return { payload, error: `「${field.label}」需要填写数字` };
      payload[field.name] = n;
    } else if (field.type === "json") {
      try {
        payload[field.name] = JSON.parse(text);
      } catch {
        return { payload, error: `「${field.label}」不是合法的 JSON，请检查引号与括号` };
      }
    } else {
      payload[field.name] = text;
    }
  }
  return { payload };
}

/** 单个字段的编辑控件，按 type 与 options 渲染 */
function FieldControl({
  field,
  value,
  onChange,
}: {
  field: FieldSpecMeta;
  value: string | boolean;
  onChange: (v: string | boolean) => void;
}) {
  const disabled = field.readonly;

  if (field.type === "bool") {
    return (
      <label className="switch">
        <input
          type="checkbox"
          checked={Boolean(value)}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="track" />
      </label>
    );
  }

  if (field.type === "text" || field.type === "json") {
    return (
      <textarea
        className={`textarea ${field.type === "json" ? "schema-json" : ""}`}
        value={String(value)}
        disabled={disabled}
        placeholder={field.type === "json" ? '例如 {"ratio":"1:1"} 或 []' : ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  if (field.type === "select") {
    return (
      <div className="select-wrap">
        <select
          className="select"
          value={String(value)}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">请选择</option>
          {field.options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      </div>
    );
  }

  if (field.type === "int" || field.type === "float") {
    return (
      <input
        className="input"
        type="number"
        step={field.type === "float" ? 0.1 : 1}
        value={String(value)}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <input
      className="input"
      value={String(value)}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

export default function SchemaTable({ spec }: { spec: TableSpecMeta }) {
  const toast = useToast();
  const [rows, setRows] = useState<SchemaRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ id?: number; version?: number; form: FormState } | null>(null);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setRows(await api.listSchemaRows(spec.name));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  };

  // 切换注册表时重新拉取
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.name]);

  const openCreate = () => setEditing({ form: buildForm(spec.fields) });

  const openEdit = (row: SchemaRow) =>
    setEditing({ id: row.id, version: row.version, form: buildForm(spec.fields, row) });

  const setField = (name: string, value: string | boolean) => {
    setEditing((prev) => (prev ? { ...prev, form: { ...prev.form, [name]: value } } : prev));
  };

  const submit = async () => {
    if (!editing) return;
    const { payload, error } = serialize(spec.fields, editing.form);
    if (error) {
      toast.error(error);
      return;
    }
    setSaving(true);
    try {
      if (editing.id !== undefined) {
        // 乐观锁：更新必须带 version，版本不符后端返回 409
        await api.updateSchemaRow(spec.name, editing.id, { ...payload, version: editing.version });
        toast.success("已保存");
      } else {
        await api.createSchemaRow(spec.name, payload);
        toast.success("已新增");
      }
      setEditing(null);
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.error("该记录已被其他操作修改，请刷新后重试");
      } else {
        toast.error(e instanceof Error ? e.message : "保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  const remove = async (row: SchemaRow) => {
    const firstField = spec.fields[0]?.name;
    const name = (firstField ? stringifyValue(row[firstField]) : "") || `#${row.id}`;
    if (!confirm(`确定删除「${name}」这条记录？删除后可在「变更审计」中回滚。`)) return;
    try {
      await api.deleteSchemaRow(spec.name, row.id);
      toast.success("已删除");
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  const cell = (field: FieldSpecMeta, row: SchemaRow): ReactNode => {
    const raw = row[field.name];
    if (field.type === "bool") {
      return <span className={`badge ${raw ? "badge-success" : ""}`}>{raw ? "是" : "否"}</span>;
    }
    const text = stringifyValue(raw);
    if (field.type === "json") {
      return (
        <span className="schema-cell-json" title={text}>
          {text || "—"}
        </span>
      );
    }
    return text || <span className="muted">—</span>;
  };

  return (
    <div className="schema-table-block">
      <div className="schema-toolbar">
        <div className="muted schema-desc">{spec.description || "该注册表由后端提供，字段变化后界面自动跟随。"}</div>
        <button className="btn btn-primary btn-sm" onClick={openCreate}>
          <Plus size={14} />
          新增
        </button>
      </div>

      {loading ? (
        <div className="loading-page">
          <span className="spinner lg" />
        </div>
      ) : rows.length === 0 ? (
        <div className="card">
          <Empty
            icon={<Inbox />}
            title="暂无记录"
            desc="这个注册表还没有数据。点击右上角「新增」添加第一条，保存后立即生效。"
            action={
              <button className="btn btn-primary" onClick={openCreate}>
                <Plus size={15} />
                新增记录
              </button>
            }
          />
        </div>
      ) : (
        <div className="card schema-table">
          <table>
            <thead>
              <tr>
                {spec.fields.map((field) => (
                  <th key={field.name}>{field.label}</th>
                ))}
                <th className="ta-r">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  {spec.fields.map((field) => (
                    <td key={field.name}>{cell(field, row)}</td>
                  ))}
                  <td className="ta-r">
                    <button className="icon-btn" title="编辑" onClick={() => openEdit(row)}>
                      <Pencil size={15} />
                    </button>
                    <button className="icon-btn" title="删除" onClick={() => remove(row)}>
                      <Trash2 size={15} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <Modal
          title={editing.id !== undefined ? `编辑「${spec.label}」` : `新增「${spec.label}」`}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setEditing(null)}>
                取消
              </button>
              <button className="btn btn-primary" onClick={submit} disabled={saving}>
                {saving ? <Spinner /> : null}
                {saving ? "保存中…" : "保存"}
              </button>
            </>
          }
        >
          {spec.fields.map((field) => (
            <div className="field" key={field.name}>
              <label className="field-label">
                {field.label}
                {field.required && <span className="config-req">*</span>}
              </label>
              <FieldControl
                field={field}
                value={editing.form[field.name] ?? ""}
                onChange={(v) => setField(field.name, v)}
              />
              {field.help && <div className="field-hint">{field.help}</div>}
            </div>
          ))}
        </Modal>
      )}
    </div>
  );
}
