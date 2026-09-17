import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Download,
  History,
  Inbox,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  Table2,
  Upload,
} from "lucide-react";
import { api, ApiError } from "../api";
import type {
  AuditLog,
  ConfigImportResult,
  ConfigScopesMeta,
  ConfigSnapshot,
  LogExport,
  LogsStatus,
  SchemaRow,
  TableSpecMeta,
  UpdateRunResult,
  UpdateStatus,
} from "../types";
import SchemaTable from "../components/SchemaTable";
import { Empty, Modal, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

const AUDIT_TAB = "__audit__";
const ABOUT_TAB = "__about__";
const TRANSFER_TAB = "__transfer__";

/** 安装方式 → 展示名与「一键更新」是否可用 */
const INSTALL_LABEL: Record<string, string> = {
  git: "git 克隆（可一键更新）",
  docker: "Docker 容器",
  archive: "压缩包解压",
};

/** 系统配置分组的中文标题与固定展示顺序 */
const GROUP_ORDER = ["basic", "defaults", "limits", "modules", "safety", "advanced"];
const GROUP_LABEL: Record<string, string> = {
  basic: "基础信息",
  defaults: "默认值",
  limits: "限制",
  modules: "模块开关",
  safety: "安全防护",
  advanced: "其它",
};

/** 配置值类型的中文说明 */
const VALUE_TYPE_LABEL: Record<string, string> = {
  string: "文本",
  int: "整数",
  float: "小数",
  bool: "开关",
  json: "JSON",
};

const ACTION_LABEL: Record<string, string> = {
  create: "新增",
  update: "修改",
  delete: "删除",
  rollback: "回滚",
};
const ACTION_BADGE: Record<string, string> = {
  create: "badge-success",
  update: "badge-primary",
  delete: "badge-danger",
  rollback: "badge-warning",
};

/** 把任意后端值安全转成展示文本 */
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

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 审计记录里「变更前/后」的摘要（节选前几个字段，避免表格被撑爆） */
function summarize(snapshot: Record<string, unknown> | null): string {
  if (!snapshot) return "—";
  const entries = Object.entries(snapshot).filter(([k]) => k !== "version");
  if (entries.length === 0) return "—";
  const text = entries
    .slice(0, 4)
    .map(([k, v]) => `${k}=${stringifyValue(v) || "空"}`)
    .join("，");
  return entries.length > 4 ? `${text} …` : text;
}

/* ---------------- 系统配置（config_items 专用渲染） ---------------- */

function ConfigPanel({ spec, onSaved }: { spec: TableSpecMeta; onSaved?: () => void }) {
  const toast = useToast();
  const [rows, setRows] = useState<SchemaRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState<Record<number, string | boolean>>({});
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setRows(await api.listSchemaRows(spec.name));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "加载配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.name]);

  const rowLabel = (row: SchemaRow) => stringifyValue(row["label"]) || stringifyValue(row["key"]) || `#${row.id}`;

  // 按 value_type 决定控件初始值
  const controlValue = (row: SchemaRow): string | boolean => {
    const v = row["value"];
    if (typeof v === "boolean") return v;
    if (typeof v === "string") return v;
    if (v === null || v === undefined) return "";
    return JSON.stringify(v, null, 2) ?? String(v);
  };

  const currentValue = (row: SchemaRow): string | boolean =>
    row.id in dirty ? dirty[row.id] : controlValue(row);

  const setValue = (id: number, value: string | boolean) =>
    setDirty((prev) => ({ ...prev, [id]: value }));

  const saveRows = async (ids: number[]) => {
    if (ids.length === 0) return;
    setSaving(true);
    const savedIds: number[] = [];
    let conflict = false;

    for (const id of ids) {
      const row = rows.find((r) => r.id === id);
      if (!row) continue;
      const raw = dirty[id];
      const valueType = stringifyValue(row["value_type"]) || "string";

      // 按值类型转换控件输入
      let value: unknown = raw;
      if (typeof raw === "string") {
        const text = raw.trim();
        if (valueType === "int") {
          const n = Number(text);
          if (!Number.isFinite(n)) {
            toast.error(`配置「${rowLabel(row)}」需要填写整数`);
            continue;
          }
          value = Math.trunc(n);
        } else if (valueType === "float") {
          const n = Number(text);
          if (!Number.isFinite(n)) {
            toast.error(`配置「${rowLabel(row)}」需要填写数字`);
            continue;
          }
          value = n;
        } else if (valueType === "json") {
          try {
            value = JSON.parse(text);
          } catch {
            toast.error(`配置「${rowLabel(row)}」不是合法的 JSON`);
            continue;
          }
        } else {
          value = text;
        }
      }

      // 更新载荷：逐项带上各自的 version（乐观锁）
      const payload: Record<string, unknown> = {
        version: row.version,
        value,
        label: row["label"] ?? "",
        description: row["description"] ?? "",
        group_name: row["group_name"] ?? "advanced",
        value_type: valueType,
        is_secret: Boolean(row["is_secret"]),
        sort_order: Number(row["sort_order"] ?? 0),
      };

      try {
        await api.updateSchemaRow(spec.name, id, payload);
        savedIds.push(id);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) conflict = true;
        else toast.error(e instanceof Error ? e.message : `「${rowLabel(row)}」保存失败`);
      }
    }

    setSaving(false);
    if (conflict) {
      toast.error("该配置已被其他操作修改，请刷新后重试");
    } else if (savedIds.length > 0) {
      toast.success(`已保存 ${savedIds.length} 项配置`);
    }

    // 只清除成功项的脏标记
    if (savedIds.length > 0) {
      setDirty((prev) => {
        const next = { ...prev };
        for (const id of savedIds) delete next[id];
        return next;
      });
      onSaved?.();
    }
    await load();
  };

  const renderControl = (row: SchemaRow): ReactNode => {
    const valueType = stringifyValue(row["value_type"]) || "string";
    const value = currentValue(row);

    if (valueType === "bool") {
      return (
        <label className="switch">
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => setValue(row.id, e.target.checked)}
          />
          <span className="track" />
        </label>
      );
    }
    if (valueType === "json") {
      return (
        <textarea
          className="textarea schema-json"
          value={String(value)}
          onChange={(e) => setValue(row.id, e.target.value)}
        />
      );
    }
    if (valueType === "int" || valueType === "float") {
      return (
        <input
          className="input"
          type="number"
          step={valueType === "float" ? 0.1 : 1}
          value={String(value)}
          onChange={(e) => setValue(row.id, e.target.value)}
        />
      );
    }
    return (
      <input className="input" value={String(value)} onChange={(e) => setValue(row.id, e.target.value)} />
    );
  };

  if (loading) {
    return (
      <div className="loading-page">
        <span className="spinner lg" />
      </div>
    );
  }

  // 已出现的分组：先按固定顺序，再补上后端新增的未知分组
  const presentGroups = Array.from(new Set(rows.map((r) => stringifyValue(r["group_name"]) || "advanced")));
  const orderedGroups = [
    ...GROUP_ORDER.filter((g) => presentGroups.includes(g)),
    ...presentGroups.filter((g) => !GROUP_ORDER.includes(g)),
  ];
  const dirtyIds = rows.filter((r) => r.id in dirty).map((r) => r.id);

  if (rows.length === 0) {
    return (
      <div className="card">
        <Empty icon={<Inbox />} title="暂无系统配置" desc="后端 config_items 表还没有数据。" />
      </div>
    );
  }

  return (
    <div className="config-panel">
      {orderedGroups.map((group) => {
        const items = rows
          .filter((r) => (stringifyValue(r["group_name"]) || "advanced") === group)
          .sort((a, b) => Number(a["sort_order"] ?? 0) - Number(b["sort_order"] ?? 0) || a.id - b.id);
        const sectionDirty = items.filter((r) => r.id in dirty).map((r) => r.id);

        return (
          <section className="card config-section" key={group}>
            <div className="config-section-head">
              <div className="config-section-title">{GROUP_LABEL[group] ?? group}</div>
              <button
                className="btn btn-sm btn-ghost"
                disabled={sectionDirty.length === 0 || saving}
                onClick={() => saveRows(sectionDirty)}
              >
                <Save size={14} />
                保存本节
              </button>
            </div>
            <div className="config-section-body">
              {items.map((row) => (
                <div className="config-row" key={row.id}>
                  <div className="config-row-main">
                    <div className="config-row-label">
                      {rowLabel(row)}
                      {Boolean(row["is_secret"]) && <span className="badge badge-warning">敏感</span>}
                      <span className="badge">{VALUE_TYPE_LABEL[stringifyValue(row["value_type"])] ?? "文本"}</span>
                    </div>
                    <div className="config-row-key">{stringifyValue(row["key"])}</div>
                    {Boolean(stringifyValue(row["description"])) && (
                      <div className="config-row-desc">{stringifyValue(row["description"])}</div>
                    )}
                  </div>
                  <div className="config-row-control">{renderControl(row)}</div>
                </div>
              ))}
            </div>
          </section>
        );
      })}

      <div className="settings-savebar">
        <div className="muted">
          {dirtyIds.length > 0 ? `有 ${dirtyIds.length} 项修改尚未保存` : "所有修改已保存"}
        </div>
        <button
          className="btn btn-primary"
          disabled={dirtyIds.length === 0 || saving}
          onClick={() => saveRows(dirtyIds)}
        >
          {saving ? <Spinner /> : <Save size={15} />}
          {saving ? "保存中…" : "保存全部修改"}
        </button>
      </div>
    </div>
  );
}

/* ---------------- 变更审计 ---------------- */

function AuditPanel({ schemas }: { schemas: TableSpecMeta[] }) {
  const toast = useToast();
  const [table, setTable] = useState(schemas[0]?.name ?? "");
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);

  const load = async (name: string) => {
    if (!name) return;
    setLoading(true);
    try {
      setLogs(await api.listSchemaAudit(name, 50));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "加载审计记录失败");
      setLogs([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(table);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [table]);

  const rollback = async (log: AuditLog) => {
    if (!confirm(`确定回滚这条「${ACTION_LABEL[log.action] ?? log.action}」记录？回滚后该行会恢复到变更前的状态。`)) return;
    setBusy(log.id);
    try {
      await api.rollbackSchemaRow(table, log.id);
      toast.success("已回滚");
      await load(table);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "回滚失败");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="audit-panel">
      <div className="audit-toolbar">
        <div className="select-wrap" style={{ width: 220 }}>
          <select className="select" value={table} onChange={(e) => setTable(e.target.value)}>
            {schemas.map((s) => (
              <option key={s.name} value={s.name}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <div className="muted">最近 50 条变更记录，可逐条回滚</div>
      </div>

      {loading ? (
        <div className="loading-page">
          <span className="spinner lg" />
        </div>
      ) : logs.length === 0 ? (
        <div className="card">
          <Empty
            icon={<History />}
            title="暂无变更记录"
            desc="对任意注册表做新增、修改或删除后，这里会留下可回滚的审计记录。"
          />
        </div>
      ) : (
        <div className="card audit-list">
          {logs.map((log) => (
            <div className="audit-row" key={log.id}>
              <span className={`badge ${ACTION_BADGE[log.action] ?? ""}`}>
                {ACTION_LABEL[log.action] ?? log.action}
              </span>
              <div className="audit-main">
                <div className="audit-title">
                  <span className="muted">行 ID</span>
                  <b>{log.row_id ?? "—"}</b>
                  <span className="muted">· {log.actor || "system"}</span>
                </div>
                <div className="audit-summary">
                  变更前：{summarize(log.before)}
                  <br />
                  变更后：{summarize(log.after)}
                </div>
              </div>
              <div className="audit-time">{formatDateTime(log.created_at)}</div>
              <button className="btn btn-sm btn-ghost" disabled={busy === log.id} onClick={() => rollback(log)}>
                {busy === log.id ? <Spinner /> : <RotateCcw size={14} />}
                回滚
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- 配置导入导出 ---------------- */

/** 下载任意文本为文件（走 Blob，而不是 <a href>：设了口令时直链带不上 Authorization） */
function downloadText(text: string, filename: string, mime = "application/json") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function snapshotFileName(exportedAt: string): string {
  const stamp = (exportedAt || new Date().toISOString()).replace(/[:+]/g, "-").replace(/\..*$/, "");
  return `xiaoma-config-${stamp}.json`;
}

function countRows(result: ConfigImportResult | null): number {
  if (!result) return 0;
  return result.totals.created + result.totals.updated + result.totals.skipped;
}

/** 快照里的 scopes 可能是数组、逗号串，也可能没有——统一成数组再往后用 */
function snapshotScopes(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map((x) => String(x).trim()).filter(Boolean);
  if (typeof raw === "string") return raw.split(",").map((x) => x.trim()).filter(Boolean);
  return [];
}

/** 导入结果的逐表统计（表名用注册表里的中文名） */
function ImportSummary({
  result,
  tableLabel,
}: {
  result: ConfigImportResult;
  tableLabel: (table: string) => string;
}) {
  const rows = Object.entries(result.summary);
  if (rows.length === 0) return <div className="muted">没有需要写入的行。</div>;
  return (
    <table className="transfer-table">
      <thead>
        <tr>
          <th>配置表</th>
          <th>新增</th>
          <th>更新</th>
          <th>跳过</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([table, c]) => (
          <tr key={table}>
            <td>{tableLabel(table)}</td>
            <td className={c.created ? "transfer-strong" : ""}>{c.created}</td>
            <td className={c.updated ? "transfer-strong" : ""}>{c.updated}</td>
            <td className="muted">{c.skipped}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * 配置导入导出。
 *
 * 三件事必须一眼看明白，否则用户不敢点：
 * 1. 导出的文件里**没有 API Key**（后端也不会接受带 Key 的导入）；
 * 2. 跟这台机器绑定的参数（绝对路径、本机地址、本机服务编号）不会带走，且能展开看清单；
 * 3. 导入先预览（新增/更新/跳过多少行、有哪些行被拒），确认后才写库。
 */
function TransferPanel({ onImported }: { onImported?: () => void }) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement | null>(null);

  const [meta, setMeta] = useState<ConfigScopesMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string[]>([]);
  const [exporting, setExporting] = useState(false);
  const [snapshotText, setSnapshotText] = useState("");
  const [exportedAt, setExportedAt] = useState("");
  const [showNotes, setShowNotes] = useState(false);

  const [incoming, setIncoming] = useState<ConfigSnapshot | null>(null);
  const [incomingName, setIncomingName] = useState("");
  const [importScopes, setImportScopes] = useState<string[]>([]);
  const [preview, setPreview] = useState<ConfigImportResult | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [writing, setWriting] = useState(false);
  const [lastResult, setLastResult] = useState<ConfigImportResult | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const m = await api.configScopes();
        setMeta(m);
        setSelected(m.defaultScopes);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "加载导出范围失败");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tableLabels: Record<string, string> = {};
  for (const s of meta?.scopes ?? []) for (const t of s.tables) tableLabels[t.name] = t.label;
  const tableLabel = (table: string) => tableLabels[table] ?? table;
  const scopeLabel = (name: string) => meta?.scopes.find((s) => s.name === name)?.label ?? name;

  const toggle = (name: string) =>
    setSelected((prev) => (prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name]));

  const doExport = async () => {
    if (selected.length === 0) {
      toast.error("至少勾一个范围");
      return;
    }
    setExporting(true);
    try {
      const snap = await api.exportConfig(selected);
      const text = JSON.stringify(snap, null, 2);
      setSnapshotText(text);
      setExportedAt(snap.exportedAt);
      downloadText(text, snapshotFileName(snap.exportedAt));
      const rows = Object.values(snap.tables).reduce((n, list) => n + list.length, 0);
      toast.success(`已导出 ${rows} 行配置`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导出失败");
    } finally {
      setExporting(false);
    }
  };

  const copySnapshot = async () => {
    if (!snapshotText) return;
    try {
      await navigator.clipboard.writeText(snapshotText);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动选中文本复制");
    }
  };

  /** 快照里出现了哪些表 → 对应哪些范围（后端没写 scopes 时兜底） */
  const scopesOfTables = (tables: string[]): string[] =>
    (meta?.scopes ?? [])
      .filter((s) => s.tables.some((t) => tables.includes(t.name)))
      .map((s) => s.name);

  const runPreview = async (snap: ConfigSnapshot, scopes: string[]) => {
    setPreviewing(true);
    try {
      const r = await api.importConfig({ ...snap, scopes, mode: "merge" }, true);
      setPreview(r);
      setPreviewOpen(true);
    } catch (e) {
      setPreview(null);
      toast.error(e instanceof Error ? e.message : "预览失败");
    } finally {
      setPreviewing(false);
    }
  };

  const onPickFile = async (file: File | undefined) => {
    if (!file) return;
    setLastResult(null);
    setPreview(null);
    try {
      const parsed = JSON.parse(await file.text()) as ConfigSnapshot;
      if (!parsed || typeof parsed !== "object" || !parsed.tables) {
        throw new Error("文件里没有 tables 字段，可能不是本程序导出的配置快照");
      }
      const declared = snapshotScopes(parsed.scopes);
      const scopes = declared.length ? declared : scopesOfTables(Object.keys(parsed.tables));
      setIncoming({ ...parsed, scopes });
      setIncomingName(file.name);
      setImportScopes(scopes);
      await runPreview({ ...parsed, scopes }, scopes);
    } catch (e) {
      setIncoming(null);
      setIncomingName("");
      toast.error(e instanceof Error ? e.message : "读取文件失败");
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const toggleImportScope = (name: string) => {
    if (!incoming) return;
    const next = importScopes.includes(name)
      ? importScopes.filter((x) => x !== name)
      : [...importScopes, name];
    setImportScopes(next);
    void runPreview(incoming, next);
  };

  const confirmImport = async () => {
    if (!incoming) return;
    setWriting(true);
    try {
      const r = await api.importConfig({ ...incoming, scopes: importScopes, mode: "merge" }, false);
      setLastResult(r);
      setPreviewOpen(false);
      if (r.conflicts.length > 0) {
        toast.error(`已写入 ${r.totals.created + r.totals.updated} 行，另有 ${r.conflicts.length} 行被拒`);
      } else {
        toast.success(`导入完成：新增 ${r.totals.created} 行，更新 ${r.totals.updated} 行`);
      }
      // 站点名、模块开关可能被改，通知外层刷新一次元信息
      onImported?.();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    } finally {
      setWriting(false);
    }
  };

  if (loading) {
    return (
      <div className="loading-page">
        <span className="spinner lg" />
      </div>
    );
  }
  if (!meta) return null;

  const exportedRowCount = preview ? countRows(preview) : 0;

  return (
    <div className="config-panel">
      <section className="card config-section">
        <div className="config-section-head">
          <div>
            <div className="config-section-title">导出配置</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 3 }}>
              把提示词库、导演风格、创作 Agent、服务商预设等打包成一个 JSON 快照，可以分享或搬到别的机器
            </div>
          </div>
          <div className="transfer-actions">
            <button className="btn btn-sm btn-ghost" onClick={() => void copySnapshot()} disabled={!snapshotText}>
              复制全文
            </button>
            <button className="btn btn-sm btn-primary" onClick={() => void doExport()} disabled={exporting}>
              {exporting ? <Spinner light /> : <Download size={14} />}
              导出 .json
            </button>
          </div>
        </div>
        <div className="config-section-body">
          <div className="transfer-scopes">
            {meta.scopes.map((s) => (
              <label className={`transfer-scope ${selected.includes(s.name) ? "checked" : ""}`} key={s.name}>
                <input type="checkbox" checked={selected.includes(s.name)} onChange={() => toggle(s.name)} />
                <span className="transfer-scope-body">
                  <span className="transfer-scope-label">{s.label}</span>
                  <span className="transfer-scope-desc">{s.description}</span>
                </span>
              </label>
            ))}
          </div>

          <div className="transfer-note">
            <ShieldCheck size={14} />
            <span>
              <b>不含 API Key</b>：模型服务只导出名称、协议、地址与模型清单；
              密钥在每台机器上单独加密，导出了也解不开，导入时也不会接受。
            </span>
            <button className="btn btn-xs btn-ghost" onClick={() => setShowNotes((v) => !v)}>
              {showNotes ? "收起排除说明" : `排除了什么（${meta.excluded.length} 项）`}
            </button>
          </div>

          {showNotes && (
            <div className="transfer-excluded">
              {meta.excluded.map((e, i) => (
                <div className="transfer-excluded-row" key={i}>
                  <code>{e.table === "*" ? "所有表" : tableLabel(e.table)} · {e.field}</code>
                  <span>{e.reason}</span>
                </div>
              ))}
            </div>
          )}

          {snapshotText && (
            <>
              <div className="transfer-file">
                已导出（{exportedAt.replace("T", " ")}）。下面就是文件内容，也可以直接复制给别人：
              </div>
              <pre className="log-preview">{snapshotText}</pre>
            </>
          )}
        </div>
      </section>

      <section className="card config-section">
        <div className="config-section-head">
          <div>
            <div className="config-section-title">导入配置</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 3 }}>
              先预览「将新增 / 更新 / 跳过多少行、有没有冲突」，确认之后才写库；写入的每一行都会留下可回滚的审计记录
            </div>
          </div>
          <div className="transfer-actions">
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              style={{ display: "none" }}
              onChange={(e) => void onPickFile(e.target.files?.[0])}
            />
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => fileRef.current?.click()}
              disabled={previewing}
            >
              {previewing ? <Spinner /> : <Upload size={14} />}
              {previewing ? "读取中…" : "选择 .json 文件"}
            </button>
          </div>
        </div>
        <div className="config-section-body">
          {!incoming ? (
            <div className="transfer-file muted">
              还没选择文件。选一个导出好的 <code>.json</code> 快照，会立刻给出预览，不会直接改数据。
            </div>
          ) : (
            <div className="transfer-file">
              已读取 <b>{incomingName}</b>
              {incoming.appVersion ? `（导出自 v${incoming.appVersion}` : "（"}
              {incoming.exportedAt ? ` · ${incoming.exportedAt.replace("T", " ")}）` : "）"}
              <div className="muted" style={{ marginTop: 6 }}>
                文件里共 {Object.keys(incoming.tables).length} 张表、{incoming.scopes.map(scopeLabel).join("、") || "—"}
              </div>
            </div>
          )}

          {lastResult && (
            <div className="transfer-result">
              <div className="transfer-result-head">
                上次导入：新增 {lastResult.totals.created} 行、更新 {lastResult.totals.updated} 行、跳过{" "}
                {lastResult.totals.skipped} 行
                {lastResult.conflicts.length > 0 && ` · 被拒 ${lastResult.conflicts.length} 行`}
              </div>
              <ImportSummary result={lastResult} tableLabel={tableLabel} />
              {lastResult.conflicts.length > 0 && (
                <div className="transfer-conflicts">
                  {lastResult.conflicts.map((c, i) => (
                    <div className="transfer-conflict" key={i}>
                      <AlertTriangle size={13} />
                      <span>{c.reason}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </section>

      {previewOpen && preview && incoming && (
        <Modal
          title={preview.dryRun ? "导入预览（还没有写库）" : "导入结果"}
          wide
          onClose={() => setPreviewOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setPreviewOpen(false)}>
                取消
              </button>
              <button
                className="btn btn-primary"
                onClick={() => void confirmImport()}
                disabled={writing || (preview.totals.created === 0 && preview.totals.updated === 0)}
              >
                {writing ? <Spinner light /> : <Upload size={14} />}
                {writing ? "写入中…" : `确认写入（新增 ${preview.totals.created} / 更新 ${preview.totals.updated}）`}
              </button>
            </>
          }
        >
          <div className="transfer-preview">
            <div className="transfer-preview-line">
              快照导出自 v{preview.source.appVersion || "?"} ·{" "}
              {preview.source.exportedAt ? preview.source.exportedAt.replace("T", " ") : "时间未知"} ·
              共 {exportedRowCount} 行
            </div>

            <div className="about-k" style={{ margin: "14px 0 6px" }}>
              导入范围（取消勾选可跳过某些表）
            </div>
            <div className="transfer-scopes transfer-scopes-compact">
              {(incoming.scopes.length ? incoming.scopes : importScopes).map((name) => (
                <label
                  className={`transfer-scope ${importScopes.includes(name) ? "checked" : ""}`}
                  key={name}
                >
                  <input
                    type="checkbox"
                    checked={importScopes.includes(name)}
                    onChange={() => toggleImportScope(name)}
                  />
                  <span className="transfer-scope-body">
                    <span className="transfer-scope-label">{scopeLabel(name)}</span>
                  </span>
                </label>
              ))}
            </div>

            <div className="about-k" style={{ margin: "14px 0 6px" }}>
              将发生什么
            </div>
            <ImportSummary result={preview} tableLabel={tableLabel} />

            {preview.warnings.length > 0 && (
              <div className="transfer-warnings">
                {preview.warnings.map((w, i) => (
                  <div className="transfer-warning" key={i}>
                    <AlertTriangle size={13} />
                    <span>{w}</span>
                  </div>
                ))}
              </div>
            )}

            {preview.conflicts.length > 0 && (
              <>
                <div className="about-k" style={{ margin: "14px 0 6px" }}>
                  被拒收的行（{preview.conflicts.length} 行，其余照常导入）
                </div>
                <div className="transfer-conflicts">
                  {preview.conflicts.map((c, i) => (
                    <div className="transfer-conflict" key={i}>
                      <AlertTriangle size={13} />
                      <span>{c.reason}</span>
                    </div>
                  ))}
                </div>
              </>
            )}

            <div className="about-note">
              合并规则：按各表的唯一标识（如提示词 / 风格的 <code>key</code>、参数选项的{" "}
              <code>kind+value</code>、服务的接口地址）匹配。内容完全一样就跳过——同一份文件导两次不会产生重复行；
              已有的东西不会被删除，本机已保存的 API Key 也不会被覆盖。
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ---------------- 页面主体（关于与更新 + 注册表 Tabs） ---------------- */

/** 关于与更新：版本信息 + 检查更新 + 一键更新 */
function AboutPanel() {
  const toast = useToast();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [result, setResult] = useState<UpdateRunResult | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [logLoading, setLogLoading] = useState(false);
  const [logData, setLogData] = useState<LogExport | null>(null);
  const [logFilesInfo, setLogFilesInfo] = useState<LogsStatus | null>(null);
  const [logMode, setLogMode] = useState<"report" | "issue">("report");

  const loadLogs = async (mode: "report" | "issue") => {
    setLogLoading(true);
    try {
      const data = mode === "issue" ? await api.exportIssue() : await api.exportLogs();
      setLogData(data);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "生成诊断报告失败");
    } finally {
      setLogLoading(false);
    }
  };

  const openLogs = async () => {
    setLogOpen(true);
    setLogMode("report");
    setLogData(null);
    try {
      setLogFilesInfo(await api.logsStatus());
    } catch {
      /* 拿不到文件清单不影响导出 */
    }
    await loadLogs("report");
  };

  const switchLogMode = async (mode: "report" | "issue") => {
    if (mode === logMode && logData) return;
    setLogMode(mode);
    await loadLogs(mode);
  };

  /** 走 Blob 下载，而不是 <a href>：设了口令时直链带不上 Authorization。 */
  const downloadLogs = () => {
    if (!logData) return;
    const blob = new Blob([logData.text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `xiaoma-diagnostic-${logData.generatedAt.replace(/[:T]/g, "-")}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const copyLogs = async () => {
    if (!logData) return;
    try {
      await navigator.clipboard.writeText(logData.text);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动选中文本复制");
    }
  };

  const load = async (force: boolean) => {
    if (force) setChecking(true);
    else setLoading(true);
    try {
      setStatus(force ? await api.checkUpdate() : await api.getUpdateStatus());
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "获取版本信息失败");
    } finally {
      setLoading(false);
      setChecking(false);
    }
  };

  useEffect(() => {
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doUpdate = async () => {
    setConfirmOpen(false);
    setUpdating(true);
    setResult(null);
    try {
      const r = await api.runUpdate();
      setResult(r);
      if (r.ok) toast.success("更新完成，请重启服务");
      else toast.error(r.message);
      await load(false);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "更新失败");
    } finally {
      setUpdating(false);
    }
  };

  if (loading) {
    return (
      <div className="card" style={{ padding: 24, textAlign: "center" }}>
        <Spinner />
      </div>
    );
  }
  if (!status) return null;

  // 一键更新只在「git 克隆 + 工作区干净 + 配了仓库」时才允许，其余情况给出替代做法
  const canUpdate = status.installKind === "git" && status.dirty !== true;

  return (
    <>
      <div className="card" style={{ padding: 20 }}>
        <div className="about-title">版本信息</div>
        <div className="about-grid">
          <div className="about-item">
            <span className="about-k">当前版本</span>
            <span className="about-v">v{status.version}</span>
          </div>
          <div className="about-item">
            <span className="about-k">代码提交</span>
            <span className="about-v">
              {status.commit ? `${status.commit}${status.branch ? ` · ${status.branch}` : ""}` : "—"}
            </span>
          </div>
          <div className="about-item">
            <span className="about-k">安装方式</span>
            <span className="about-v">{INSTALL_LABEL[status.installKind] ?? status.installKind}</span>
          </div>
          <div className="about-item">
            <span className="about-k">安装位置</span>
            <span className="about-v about-v-mono" title={status.repoRoot}>
              {status.repoRoot}
            </span>
          </div>
        </div>
      </div>

      <div className="card mt16" style={{ padding: 20 }}>
        <div className="about-head">
          <div>
            <div className="about-title">在线更新</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              更新源：{status.source}
              {status.repo ? ` · ${status.repo}` : " · 未配置仓库"}
              {status.checkedAt ? ` · 检查于 ${status.checkedAt.replace("T", " ")}` : ""}
            </div>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={() => void load(true)} disabled={checking}>
            {checking ? <Spinner /> : <RefreshCw size={14} />}
            检查更新
          </button>
        </div>

        {!status.repo && (
          <div className="about-note">
            还没配置更新源仓库。到「系统配置」把 <code>update.repo</code> 填成 <code>用户名/仓库名</code>
            （GitHub 与 Gitee 用同一个仓库名即可），这里就能检查新版本了。
          </div>
        )}

        {status.error && <div className="about-note about-note-warn">{status.error}</div>}

        {status.repo && !status.error && (
          <div className={`about-state ${status.hasUpdate ? "has-update" : ""}`}>
            {status.hasUpdate ? (
              <>
                发现新版本 <strong>v{status.latest.replace(/^v/i, "")}</strong>
                {status.usedSource && status.usedSource !== status.source
                  ? `（来自 ${status.usedSource} · ${status.usedRepo}）`
                  : ""}
                {status.publishedAt ? ` · 发布于 ${status.publishedAt.slice(0, 10)}` : ""}
              </>
            ) : (
              <>已是最新版本（上游最新：{status.latest || "无 Release"}）</>
            )}
          </div>
        )}

        {status.hasUpdate && status.notes && (
          <div className="update-notes">
            <div className="about-k" style={{ marginBottom: 6 }}>
              更新说明
            </div>
            <pre>{status.notes}</pre>
            {status.url && (
              <a className="btn btn-ghost btn-xs" href={status.url} target="_blank" rel="noreferrer">
                在浏览器打开 Release 页面
              </a>
            )}
          </div>
        )}

        {status.hasUpdate && (
          <div className="mt12">
            <button
              className="btn btn-primary"
              disabled={!canUpdate || updating}
              onClick={() => setConfirmOpen(true)}
            >
              {updating ? <Spinner light /> : <Download size={14} />}
              一键更新
            </button>
            {!canUpdate && (
              <div className="about-note" style={{ marginTop: 10 }}>
                {status.installKind === "docker"
                  ? "当前跑在容器里，容器内不自我更新。请在宿主机执行：docker compose pull && docker compose up -d"
                  : status.dirty
                    ? "本地有未提交的改动，为避免覆盖你的修改已停用自动更新。请先 git commit 或 git stash。"
                    : "当前不是 git 克隆安装，无法自动更新。请下载新版本解压覆盖（data/ 目录可以直接保留）。"}
              </div>
            )}
          </div>
        )}

        {result && (
          <div className="update-steps">
            <div className={`about-state ${result.ok ? "has-update" : ""}`}>{result.message}</div>
            {result.steps.map((s, i) => (
              <details key={i} className="update-step">
                <summary>
                  {s.ok ? "✓" : "✗"} {s.name}
                </summary>
                <pre>{s.output || "（无输出）"}</pre>
              </details>
            ))}
          </div>
        )}
      </div>

      <div className="card" style={{ padding: 20, marginTop: 16 }}>
        <div className="about-head">
          <div className="about-title">日志与诊断</div>
          <button className="btn btn-ghost" onClick={() => void openLogs()}>
            导出日志
          </button>
        </div>
        <div className="about-note">
          遇到问题时把这份报告整段发给作者即可定位。报告里只有环境信息和错误的文字摘要，
          不包含提示词、作品正文、图片或接口密钥。日志目录：
          <code>{logFilesInfo?.dir ?? "backend/data/logs"}</code>
          （单个文件上限 5 MB、保留 5 份，写盘时已脱敏）。
        </div>
      </div>

      {logOpen && (
        <Modal
          title="诊断报告"
          onClose={() => setLogOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setLogOpen(false)}>
                关闭
              </button>
              <button className="btn btn-ghost" onClick={() => void copyLogs()} disabled={!logData}>
                {logMode === "issue" ? "复制 Issue 内容" : "复制全文"}
              </button>
              <button className="btn btn-primary" onClick={downloadLogs} disabled={!logData}>
                下载 .txt
              </button>
            </>
          }
        >
          <div className="canvas-modetabs" style={{ marginBottom: 12 }}>
            {(
              [
                ["report", "完整报告"],
                ["issue", "Issue 内容"],
              ] as ["report" | "issue", string][]
            ).map(([m, label]) => (
              <button
                key={m}
                type="button"
                className={`canvas-modetab ${logMode === m ? "active" : ""}`}
                onClick={() => void switchLogMode(m)}
              >
                {label}
              </button>
            ))}
          </div>

          {logLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spinner />
            </div>
          ) : logData ? (
            <>
              <div className="about-note" style={{ marginBottom: 10 }}>
                {logMode === "issue" ? (
                  <>
                    已经按仓库的 Issue 模板排好版：把开头两句改成你自己的步骤和现象，
                    整段粘到 <strong>Issues 新建页面</strong>即可提交。
                  </>
                ) : (
                  <>
                    生成时间 {logData.generatedAt}　·　错误行 {logData.errorLines} 条　·　
                    {logData.files.length} 个日志文件
                  </>
                )}
                <br />
                日志中的接口密钥、URL 查询参数、系统用户名与家目录路径已替换为占位符；
                <strong>发出去之前建议自己先扫一眼</strong>。
              </div>
              <pre className="log-preview">{logData.text}</pre>
            </>
          ) : (
            <div className="about-note">生成失败，请重试。</div>
          )}
        </Modal>
      )}

      {confirmOpen && (
        <Modal
          title="确认更新到最新版本"
          onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setConfirmOpen(false)}>
                取消
              </button>
              <button className="btn btn-primary" onClick={() => void doUpdate()}>
                开始更新
              </button>
            </>
          }
        >
          <div style={{ fontSize: 13.5, lineHeight: 1.8 }}>
            将要执行：
            <ol style={{ margin: "8px 0 0 18px" }}>
              <li>
                <code>git pull --ff-only</code> 拉取最新代码
              </li>
              <li>后端依赖清单有变化时才重装依赖</li>
              <li>前端有改动时才重新构建界面</li>
            </ol>
            <div className="about-note" style={{ marginTop: 12 }}>
              更新只改代码，不会动你的 <code>data/</code>（数据库、生成的图片视频都在那里）。
              完成后需要手动重启服务。
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

export default function SettingsPage({ onMetaChanged }: { onMetaChanged?: () => void }) {
  const toast = useToast();
  const [schemas, setSchemas] = useState<TableSpecMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState<string>("");

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        // Tabs 完全由后端注册表生成：后端多注册一张表，前端就多一个 Tab
        const list = await api.listSchemas();
        setSchemas(list);
        setActive((prev) => prev || list[0]?.name || AUDIT_TAB);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "加载配置表清单失败");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeSpec = schemas.find((s) => s.name === active);

  const renderPanel = () => {
    if (active === AUDIT_TAB) return <AuditPanel schemas={schemas} />;
    if (active === ABOUT_TAB) return <AboutPanel />;
    if (active === TRANSFER_TAB) return <TransferPanel onImported={onMetaChanged} />;
    if (!activeSpec) return null;
    // 系统配置是特殊的键值表，字段是 value/value_type，用专用渲染
    if (activeSpec.name === "config_items") {
      return <ConfigPanel spec={activeSpec} onSaved={onMetaChanged} />;
    }
    // 其余注册表统一走通用表格
    return <SchemaTable spec={activeSpec} />;
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">系统设置</div>
          <div className="page-desc">
            站点信息、能力类型、参数档位、导航菜单与模型预设全部由后端配置表驱动，改完即时生效，无需重启或改代码
          </div>
        </div>
      </div>

      {loading ? (
        <div className="loading-page">
          <span className="spinner lg" />
        </div>
      ) : schemas.length === 0 ? (
        <div className="card">
          <Empty icon={<Table2 />} title="后端未注册任何配置表" desc="配置表清单为空，请检查后端 schema 注册。" />
        </div>
      ) : (
        <>
          <div className="segmented settings-tabs">
            {schemas.map((s) => (
              <button key={s.name} className={active === s.name ? "active" : ""} onClick={() => setActive(s.name)}>
                {s.label}
              </button>
            ))}
            <button className={active === AUDIT_TAB ? "active" : ""} onClick={() => setActive(AUDIT_TAB)}>
              变更审计
            </button>
            <button
              className={active === TRANSFER_TAB ? "active" : ""}
              onClick={() => setActive(TRANSFER_TAB)}
            >
              配置导入导出
            </button>
            <button className={active === ABOUT_TAB ? "active" : ""} onClick={() => setActive(ABOUT_TAB)}>
              关于与更新
            </button>
          </div>
          {renderPanel()}
        </>
      )}
    </div>
  );
}
