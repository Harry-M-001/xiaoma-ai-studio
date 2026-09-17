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
  RefreshCw,
} from "lucide-react";
import { api } from "../api";
import type {
  Modality,
  ModalityMeta,
  ModelSpec,
  OllamaStatus,
  Provider,
  ProviderInput,
  ProviderKind,
  ProviderKindMeta,
  ProviderPresetMeta,
  QuickSetupResult,
} from "../types";
import { Empty, Spinner } from "../components/common";
import { useToast } from "../components/Toast";
import { notifyProvidersChanged } from "../providerEvents";
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
  // 快速接入（粘贴 Key / 本机 Ollama）
  const [keyInput, setKeyInput] = useState("");
  const [settingUp, setSettingUp] = useState(false);
  const [setupResult, setSetupResult] = useState<QuickSetupResult | null>(null);
  const [ollama, setOllama] = useState<OllamaStatus | null>(null);
  const [ollamaBusy, setOllamaBusy] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setProviders(await api.listProviders());
      // 告诉横幅这类「看有没有模型」的组件：这里变了
      notifyProvidersChanged();
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // 本机 Ollama 检测（走服务端；浏览器自己探不到那个端口）。
  // 失败就当没装——这条信息只是加分项，不该弹错误。
  useEffect(() => {
    api
      .ollamaStatus()
      .then(setOllama)
      .catch(() => setOllama(null));
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

  /** 粘贴一个 Key：后端认归属 → 试真实请求 → 通了才存 */
  const runQuickSetup = async (providerKey = "", force = false) => {
    const key = keyInput.trim();
    if (!key) {
      toast.error("先把 API Key 粘进来");
      return;
    }
    setSettingUp(true);
    try {
      const r = await api.quickSetup({ api_key: key, provider_key: providerKey, force });
      setSetupResult(r);
      if (r.ok) {
        const name = r.service?.name ?? "";
        toast.success(
          r.forced
            ? `已保存「${name}」，但连通测试没通过——记得点一次「测试连接」确认地址与 Key`
            : `已接入「${name}」`,
        );
        setKeyInput("");
        setSetupResult(null);
        await load();
        refreshOllama();
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "接入失败");
    } finally {
      setSettingUp(false);
    }
  };

  const refreshOllama = () => {
    api
      .ollamaStatus(true)
      .then(setOllama)
      .catch(() => setOllama(null));
  };

  const connectOllama = async () => {
    setOllamaBusy(true);
    try {
      const r = await api.ollamaConnect();
      toast.success(`已接入本机 Ollama（${r.models.length} 个模型）`);
      // 直接拿接口返回的结果把本地状态定下来：再发一次查询会有一小段
      // 「刚接入完还显示未接入」的空窗（实测能撞上），而这里的信息本来就够
      setOllama((prev) =>
        prev
          ? { ...prev, connected: true, running: true, models: r.models.map((m) => m.name) }
          : prev,
      );
      await load();
      refreshOllama();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "接入失败");
    } finally {
      setOllamaBusy(false);
    }
  };

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

      <div className="card quick-setup mb16">
        <div>
          <div className="field-label">粘贴一个 Key 就接入</div>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
            自动认出这个 Key 是哪家的，用预设地址发一次<b>真实请求</b>，通了才保存——免得配了半天才发现拿错了服务商。
          </div>
        </div>
        <div className="quick-setup-row">
          <input
            className="input"
            type="password"
            placeholder="sk-... / 32位ID.密钥 / UUID 形状的 Key 都认"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") runQuickSetup();
            }}
          />
          <button className="btn btn-primary" onClick={() => runQuickSetup()} disabled={settingUp}>
            {settingUp ? <Spinner /> : <KeyRound size={15} />}
            识别并接入
          </button>
        </div>

        {setupResult && !setupResult.ok && (
          <div className="quick-setup-attempt">
            {setupResult.tried.map((t) => (
              <div key={t.key}>
                <b>{t.name}</b>：{t.message}
              </div>
            ))}
            <div style={{ marginTop: 6 }}>
              {setupResult.recognized ? "换一个服务商再试：" : "认不出归属，指定一个服务商再试："}
            </div>
            <div className="quick-setup-candidates">
              {setupResult.candidates.map((c) => (
                <button
                  key={c.key}
                  className="btn btn-ghost btn-sm"
                  disabled={settingUp}
                  onClick={() => runQuickSetup(c.key)}
                  title={`${c.baseUrl}${c.hint ? ` · ${c.hint}` : ""}`}
                >
                  {c.name}
                </button>
              ))}
              {setupResult.candidates[0] && (
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={settingUp}
                  onClick={() => runQuickSetup(setupResult.candidates[0].key, true)}
                  title="有些服务的连通测试方式不一样，测不过不代表不能用"
                >
                  测不过也保存为「{setupResult.candidates[0].name}」
                </button>
              )}
            </div>
          </div>
        )}

        <div className="ollama-line">
          {ollama === null ? (
            <span className="muted">本机 Ollama：没查到</span>
          ) : ollama.running ? (
            ollama.connected ? (
              <>
                <CircleCheck size={14} style={{ color: "var(--success)" }} />
                本机 Ollama 已接入，{ollama.models.length} 个模型可用（不需要账号，完全本地跑）
              </>
            ) : (
              <>
                <Server size={14} />
                检测到本机 Ollama，{ollama.models.length} 个模型：
                <span className="muted">{ollama.models.slice(0, 4).join("、")}{ollama.models.length > 4 ? " …" : ""}</span>
                <button className="btn btn-primary btn-sm" onClick={connectOllama} disabled={ollamaBusy}>
                  {ollamaBusy ? <Spinner /> : <PlugZap size={14} />}
                  一键接入
                </button>
              </>
            )
          ) : (
            <>
              <Server size={14} />
              没检测到本机 Ollama（装了的话启动它再点一次检测，那条路不需要任何账号）
            </>
          )}
          <button className="btn btn-ghost btn-sm" onClick={refreshOllama} disabled={ollamaBusy}>
            <RefreshCw size={13} />
            重新检测
          </button>
        </div>
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
