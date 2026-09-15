import { useEffect, useState } from "react";
import {
  Plus,
  Pencil,
  Trash2,
  PlugZap,
  Server,
  X,
  KeyRound,
  CircleCheck,
  CircleX,
  BookOpen,
} from "lucide-react";
import { api } from "../api";
import type {
  Modality,
  ModalityMeta,
  ModelSpec,
  Provider,
  ProviderInput,
  ProviderKind,
  ProviderKindMeta,
  ProviderPresetMeta,
} from "../types";
import { Empty, Spinner } from "../components/common";
import { useToast } from "../components/Toast";
import { Modal } from "../components/common";

/** 服务类型接口不可用时的内置回落 */
const FALLBACK_KINDS: ProviderKindMeta[] = [
  { kind: "openai", label: "OpenAI 兼容接口", hint: "需包含 /v1 路径；兼容 OpenAI 协议的中转 / 网关同样可用。" },
  { kind: "ark", label: "火山方舟 Ark", hint: "火山方舟使用统一接入地址，模型名填写具体模型版本 ID。" },
];

/** 能力类型接口不可用时的内置回落 */
const FALLBACK_MODALITIES: ModalityMeta[] = [
  { key: "text", label: "文本", icon: "message", sort_order: 1 },
  { key: "image", label: "图片", icon: "image", sort_order: 2 },
  { key: "video", label: "视频", icon: "video", sort_order: 3 },
];

/** 各协议常用接入地址（用于占位与回填，其它协议留空） */
const COMMON_BASE_URL: Record<string, string> = {
  ark: "https://ark.cn-beijing.volces.com/api/v3",
  comfyui: "http://127.0.0.1:8188",
};

const emptyForm = (): ProviderInput & { id?: number } => ({
  name: "",
  kind: "openai",
  base_url: "",
  api_key: "",
  enabled: true,
  models: [],
});

export default function ProvidersPage() {
  const toast = useToast();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<(ProviderInput & { id?: number }) | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // 以下清单全部来自后端配置表
  const [presets, setPresets] = useState<ProviderPresetMeta[]>([]);
  const [providerKinds, setProviderKinds] = useState<ProviderKindMeta[]>(FALLBACK_KINDS);
  const [modalities, setModalities] = useState<ModalityMeta[]>(FALLBACK_MODALITIES);
  const [presetHint, setPresetHint] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      setProviders(await api.listProviders());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // 预设 / 服务类型 / 能力类型：任一接口失败都独立回落到内置默认
  useEffect(() => {
    (async () => {
      const [ps, ks, ms] = await Promise.allSettled([
        api.getProviderPresets(),
        api.getProviderKinds(),
        api.getModalities(),
      ]);
      if (ps.status === "fulfilled") setPresets(ps.value);
      if (ks.status === "fulfilled" && ks.value.length > 0) setProviderKinds(ks.value);
      if (ms.status === "fulfilled" && ms.value.length > 0) setModalities(ms.value);
    })();
  }, []);

  const openCreate = () => {
    setTestResult(null);
    setPresetHint("");
    setEditing(emptyForm());
  };

  const openEdit = (p: Provider) => {
    setTestResult(null);
    setPresetHint("");
    setEditing({
      id: p.id,
      name: p.name,
      kind: p.kind,
      base_url: p.base_url,
      api_key: "",
      enabled: p.enabled,
      sort_order: p.sort_order,
      models: p.models.map((m) => ({ ...m })),
    });
  };

  const applyPreset = (preset: ProviderPresetMeta) => {
    if (!editing) return;
    setEditing({
      ...editing,
      kind: preset.kind as ProviderKind,
      base_url: preset.base_url,
      name: editing.name || preset.name,
      models: preset.models.map((m) => ({ ...m })),
    });
    setPresetHint(preset.hint || "");
    setTestResult(null);
  };

  /** 切换服务类型：地址为空时回填该协议常用地址，并清空上一预设提示 */
  const selectKind = (kind: string) => {
    if (!editing) return;
    setEditing({
      ...editing,
      kind: kind as ProviderKind,
      base_url: editing.base_url.trim() ? editing.base_url : COMMON_BASE_URL[kind] ?? "",
    });
    setPresetHint("");
    setTestResult(null);
  };

  const updateModel = (i: number, patch: Partial<ModelSpec>) => {
    if (!editing) return;
    const models = editing.models.map((m, idx) => (idx === i ? { ...m, ...patch } : m));
    setEditing({ ...editing, models });
  };

  const testConnection = async () => {
    if (!editing) return;
    if (!editing.base_url.trim()) {
      toast.error("请先填写接口地址");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      await api.testProvider({
        kind: editing.kind,
        base_url: editing.base_url.trim(),
        api_key: editing.api_key?.trim() || null,
        service_id: editing.id ?? null,
        model: editing.models[0]?.name || null,
      });
      setTestResult({ ok: true, text: "连接正常，接口与密钥可用" });
    } catch (e) {
      setTestResult({ ok: false, text: e instanceof Error ? e.message : "连接失败" });
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    if (!editing) return;
    if (!editing.name.trim()) return toast.error("请填写服务名称");
    if (!editing.base_url.trim()) return toast.error("请填写接口地址");
    if (editing.models.some((m) => !m.name.trim())) return toast.error("模型名不能为空");
    if (!editing.id && editing.models.length === 0)
      return toast.error("至少添加一个模型，否则无法在创作页选择该服务");

    setSaving(true);
    const payload: ProviderInput = {
      name: editing.name.trim(),
      kind: editing.kind,
      base_url: editing.base_url.trim().replace(/\/+$/, ""),
      api_key: editing.api_key?.trim() || null,
      enabled: editing.enabled,
      models: editing.models.map((m) => ({
        name: m.name.trim(),
        modality: m.modality,
        label: m.label?.trim() || "",
      })),
    };
    try {
      if (editing.id) {
        await api.updateProvider(editing.id, payload);
        toast.success("已保存");
      } else {
        await api.createProvider(payload);
        toast.success("服务已添加");
      }
      setEditing(null);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (p: Provider) => {
    try {
      await api.updateProvider(p.id, {
        name: p.name,
        kind: p.kind,
        base_url: p.base_url,
        api_key: null,
        enabled: !p.enabled,
        sort_order: p.sort_order,
        models: p.models,
      });
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "操作失败");
    }
  };

  const quickTest = async (p: Provider) => {
    try {
      await api.testProvider({
        kind: p.kind,
        base_url: p.base_url,
        service_id: p.id,
        model: p.models[0]?.name || null,
      });
      toast.success(`「${p.name}」连接正常`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "连接失败");
    }
  };

  const remove = async (p: Provider) => {
    if (!confirm(`确定删除服务「${p.name}」？相关历史任务记录会保留，但无法再使用该服务生成。`)) return;
    await api.deleteProvider(p.id);
    toast.success("已删除");
    await load();
  };

  const modalityLabel = (key: string) => modalities.find((m) => m.key === key)?.label ?? key;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">模型服务</div>
          <div className="page-desc">通过标准接口地址与 API Key 接入你自己的模型，密钥经加密后保存在本地</div>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>
          <Plus size={16} />
          添加服务
        </button>
      </div>

      <div className="guide">
        <h3>
          <BookOpen size={15} />
          接入三步
        </h3>
        <ol>
          <li>在模型服务商控制台创建 API Key（OpenAI / DeepSeek / Kimi / 通义 / 智谱 / 火山方舟等均可）。</li>
          <li>添加服务，选择类型：OpenAI 兼容接口，或火山方舟 Ark；填入接口地址与 Key。</li>
          <li>
            添加模型并勾选能力类型（<b>文本 / 图片 / 视频</b>），保存后即可在对应创作页选择使用。
          </li>
        </ol>
      </div>

      {loading ? (
        <div className="loading-page">
          <span className="spinner lg" />
        </div>
      ) : providers.length === 0 ? (
        <div className="card">
          <Empty
            icon={<Server />}
            title="还没有添加任何模型服务"
            desc="点击「添加服务」，可直接选用内置的服务商预设，填入 Key 即可开始。所有数据只保存在你自己的电脑上。"
            action={
              <button className="btn btn-primary" onClick={openCreate}>
                <Plus size={15} />
                添加第一个服务
              </button>
            }
          />
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {providers.map((p) => (
            <div key={p.id} className="card provider-card">
              <div className="provider-info">
                <div className="provider-name">
                  {p.name}
                  <span className="badge badge-primary">
                    {providerKinds.find((k) => k.kind === p.kind)?.label ?? p.kind}
                  </span>
                  {!p.enabled && <span className="badge">已停用</span>}
                  {!p.has_api_key && <span className="badge badge-warning">未设密钥</span>}
                </div>
                <div className="provider-url">{p.base_url}</div>
                {p.models.length > 0 && (
                  <div className="provider-models">
                    {p.models.map((m) => (
                      <span key={m.name} className="badge">
                        {m.label || m.name} · {modalityLabel(m.modality)}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="provider-actions">
                <label className="switch" title={p.enabled ? "点击停用" : "点击启用"}>
                  <input
                    type="checkbox"
                    checked={p.enabled}
                    onChange={() => toggleEnabled(p)}
                  />
                  <span className="track" />
                </label>
                <button className="icon-btn" title="测试连接" onClick={() => quickTest(p)}>
                  <PlugZap size={16} />
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

      {editing && (
        <Modal
          wide
          title={editing.id ? "编辑模型服务" : "添加模型服务"}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={testConnection} disabled={testing}>
                {testing ? <Spinner /> : <PlugZap size={15} />}
                测试连接
              </button>
              <div className="flex1" />
              <button className="btn btn-ghost" onClick={() => setEditing(null)}>
                取消
              </button>
              <button className="btn btn-primary" onClick={save} disabled={saving}>
                {saving ? "保存中…" : "保存"}
              </button>
            </>
          }
        >
          <div className="field">
            <label className="field-label">快速选择服务商（自动填充地址与常用模型）</label>
            <div className="quick-presets">
              {presets.map((preset) => (
                <button
                  key={preset.key}
                  className="preset-chip"
                  title={preset.hint || undefined}
                  onClick={() => applyPreset(preset)}
                >
                  {preset.name}
                </button>
              ))}
            </div>
            {presetHint && <div className="field-hint">{presetHint}</div>}
          </div>

          <div className="field">
            <label className="field-label">服务名称</label>
            <input
              className="input"
              placeholder="例如：我的 OpenAI、公司内部代理"
              value={editing.name}
              onChange={(e) => setEditing({ ...editing, name: e.target.value })}
            />
          </div>

          <div className="field">
            <label className="field-label">服务类型</label>
            <div className="segmented">
              {providerKinds.map((k) => (
                <button
                  key={k.kind}
                  className={editing.kind === k.kind ? "active" : ""}
                  onClick={() => selectKind(k.kind)}
                >
                  {k.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">接口地址 Base URL</label>
            <input
              className="input mono"
              placeholder={COMMON_BASE_URL[editing.kind] ?? "https://api.openai.com/v1"}
              value={editing.base_url}
              onChange={(e) => setEditing({ ...editing, base_url: e.target.value })}
            />
            <div className="field-hint">
              {providerKinds.find((k) => k.kind === editing.kind)?.hint || ""}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              <KeyRound size={12} style={{ verticalAlign: -1 }} /> API Key
            </label>
            <input
              className="input mono"
              type="password"
              placeholder={editing.id ? "留空表示保持原 Key 不变" : "sk-...（本地服务可任意填写）"}
              value={editing.api_key ?? ""}
              onChange={(e) => setEditing({ ...editing, api_key: e.target.value })}
            />
          </div>

          <div className="field">
            <label className="field-label">
              模型列表
            </label>
            {editing.models.length === 0 && (
              <div className="muted" style={{ fontSize: 12.5, marginBottom: 8 }}>
                尚未添加模型。可点上方预设自动填充，或手动添加。
              </div>
            )}
            {editing.models.map((m, i) => (
              <div key={i} className="model-row">
                <input
                  className="input"
                  style={{ width: 130, flex: "0 0 130px" }}
                  placeholder="显示名（可选）"
                  value={m.label}
                  onChange={(e) => updateModel(i, { label: e.target.value })}
                />
                <input
                  className="input mono"
                  placeholder="模型名 / 模型 ID"
                  value={m.name}
                  onChange={(e) => updateModel(i, { name: e.target.value })}
                />
                <div className="select-wrap" style={{ width: 100, flexShrink: 0 }}>
                  <select
                    className="select"
                    value={m.modality}
                    onChange={(e) => updateModel(i, { modality: e.target.value as Modality })}
                  >
                    {modalities.map((mo) => (
                      <option key={mo.key} value={mo.key}>
                        {mo.label}
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  className="icon-btn"
                  onClick={() =>
                    setEditing({ ...editing, models: editing.models.filter((_, idx) => idx !== i) })
                  }
                  title="移除模型"
                >
                  <X size={15} />
                </button>
              </div>
            ))}
            <button
              className="btn btn-ghost btn-sm mt8"
              onClick={() =>
                setEditing({
                  ...editing,
                  models: [...editing.models, { name: "", modality: "text", label: "" }],
                })
              }
            >
              <Plus size={14} />
              添加模型
            </button>
          </div>

          <div className="row" style={{ justifyContent: "space-between" }}>
            <label className="row" style={{ gap: 8, cursor: "pointer", fontSize: 13 }}>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={editing.enabled}
                  onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })}
                />
                <span className="track" />
              </label>
              保存后立即启用
            </label>
          </div>

          {testResult && (
            <div
              className="task-error mt16"
              style={
                testResult.ok
                  ? { background: "var(--success-soft)", color: "var(--success)" }
                  : undefined
              }
            >
              {testResult.ok ? <CircleCheck size={14} style={{ verticalAlign: -2, marginRight: 6 }} /> : <CircleX size={14} style={{ verticalAlign: -2, marginRight: 6 }} />}
              {testResult.text}
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
