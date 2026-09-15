import { useEffect, useState, type ReactNode } from "react";
import { Download, History, Inbox, RefreshCw, RotateCcw, Save, Table2 } from "lucide-react";
import { api, ApiError } from "../api";
import type { AuditLog, SchemaRow, TableSpecMeta, UpdateRunResult, UpdateStatus } from "../types";
import SchemaTable from "../components/SchemaTable";
import { Empty, Modal, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

const AUDIT_TAB = "__audit__";
const ABOUT_TAB = "__about__";

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
