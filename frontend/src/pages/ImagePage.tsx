import { useEffect, useMemo, useRef, useState } from "react";
import { ImagePlus, Images, Settings, Sparkles, Upload, X } from "lucide-react";
import { api, cfgBool, cfgNumber, cfgString } from "../api";
import { consumeDraftPrompt } from "../promptDraft";
import type { Asset, ConfigMap, ModelOption, ParamOptionItem, PreflightWarning, Task } from "../types";
import { Empty, Modal, ModelSelect, Spinner, isRunning } from "../components/common";
import { PreflightNotice, runPreflight } from "../components/PreflightNotice";
import { useToast } from "../components/Toast";
import { prefs } from "../prefs";
import TaskCard from "../components/TaskCard";
import Lightbox from "../components/Lightbox";

/** 后端未配置参数档位时的内置回落，保证页面仍然可用 */
const FALLBACK_SIZE: ParamOptionItem[] = [
  { id: -1, value: "1024x1024", label: "1:1", meta: {}, sort_order: 1 },
];
const FALLBACK_COUNT: ParamOptionItem[] = [
  { id: -1, value: "1", label: "1 张", meta: {}, sort_order: 1 },
];

/**
 * 空状态里的示例提示词。
 *
 * 第一次进来的人面对的是「左边一排参数、右边一片空白」，最容易卡在「画面描述该写什么」这一步——
 * 给三条点一下就能用的例子，比再写一句「请描述你想生成的画面」有用得多。
 */
const PROMPT_EXAMPLES = [
  "黄昏的海边，一匹小马站在浅滩上，电影感光影，高细节",
  "赛博朋克城市夜景，霓虹倒影，广角镜头，雨后",
  "水彩风格的山谷日出，柔和色调，画面留白",
];

/** 在档位列表中选中：命中配置值则用它，否则回落到首个档位 */
function pickValue(opts: ParamOptionItem[], preferred: string): string {
  if (opts.length === 0) return preferred;
  return opts.some((o) => o.value === preferred) ? preferred : opts[0].value;
}

type RefAsset = { id: number; url: string };

export default function ImagePage({ onGoSettings }: { onGoSettings: () => void }) {
  const toast = useToast();
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelKey, setModelKey] = useState("");
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState("1024x1024");
  const [n, setN] = useState(1);
  const [sizeOptions, setSizeOptions] = useState<ParamOptionItem[]>(FALLBACK_SIZE);
  const [countOptions, setCountOptions] = useState<ParamOptionItem[]>(FALLBACK_COUNT);
  const [refs, setRefs] = useState<RefAsset[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmGen, setConfirmGen] = useState(true);
  // 生成前的软告警：只展示，不改变能不能提交
  const [warnings, setWarnings] = useState<PreflightWarning[]>([]);
  const [checking, setChecking] = useState(false);
  const [mode, setMode] = useState<"single" | "batch">("single");
  const [batchText, setBatchText] = useState("");
  const [batchModels, setBatchModels] = useState<string[]>([]);
  const [batchMax, setBatchMax] = useState(20);
  const [preview, setPreview] = useState<{ url: string; kind: string; downloadUrl?: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadTasks = async () => setTasks(await api.listTasks("image", 12));

  useEffect(() => {
    (async () => {
      const [ms, ts] = await Promise.all([api.listModels("image"), api.listTasks("image", 12)]);
      setModels(ms);
      setTasks(ts);
      // 记住的模型可能已经不存在了（服务被删/改名）：合法的沿用，失效的丢掉并退回第一个可用的
      const keys = ms.map((m) => m.key);
      setModelKey((prev) => prev || prefs.modelKey.get("image", keys) || keys[0] || "");
    })();
    // 从提示词库「去图片生成」带过来的模板内容
    const draft = consumeDraftPrompt("image");
    if (draft) setPrompt(draft);
  }, []);

  // 参数档位与默认值全部来自后端配置表；接口失败则回落到内置默认
  useEffect(() => {
    (async () => {
      let options: Record<string, ParamOptionItem[]> = {};
      let config: ConfigMap = {};
      try {
        [options, config] = await Promise.all([api.getParamOptions(), api.getConfig()]);
      } catch {
        // 接口不可用：使用上方内置默认档位
      }
      const sizes = options.image_size?.length ? options.image_size : FALLBACK_SIZE;
      const counts = options.image_count?.length ? options.image_count : FALLBACK_COUNT;
      setSizeOptions(sizes);
      setCountOptions(counts);
      setSize(pickValue(sizes, cfgString(config, "defaults.image_size", "1024x1024")));
      setN(Number(pickValue(counts, String(cfgNumber(config, "defaults.image_count", 1)))));
      setConfirmGen(cfgBool(config, "safety.confirm_before_generate", true));
      setBatchMax(Math.max(1, cfgNumber(config, "limits.batch_max_tasks", 20)));
    })();
  }, []);

  useEffect(() => {
    prefs.modelKey.set("image", modelKey);
  }, [modelKey]);

  // 批量模式：每行一条提示词；与选中的模型做笛卡尔积
  const batchPrompts = useMemo(
    () => batchText.split("\n").map((s) => s.trim()).filter(Boolean),
    [batchText]
  );
  const batchTotal = batchPrompts.length * batchModels.length;

  const toggleBatchModel = (key: string) => {
    setBatchModels((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
    );
  };

  const runningIds = tasks.filter((t) => isRunning(t.status)).map((t) => t.id).join(",");
  useEffect(() => {
    if (!runningIds) return;
    const ids = runningIds.split(",").map(Number);
    const timer = setInterval(async () => {
      const updated = await Promise.all(ids.map((id) => api.getTask(id)));
      setTasks((prev) => prev.map((t) => updated.find((u) => u.id === t.id) ?? t));
      if (updated.some((t) => !isRunning(t.status))) setTimeout(loadTasks, 600);
    }, 2500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningIds]);

  const addFiles = async (files: FileList | File[]) => {
    const room = 4 - refs.length;
    const list = Array.from(files).filter((f) => f.type.startsWith("image/")).slice(0, room);
    for (const f of list) {
      try {
        const a: Asset = await api.uploadAsset(f);
        setRefs((prev) => [...prev, { id: a.id, url: a.url }]);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "上传失败");
      }
    }
  };

  /** 提交前先问一句「你这组合可能不是你要的」，但不拦。
   *
   * 两条规则：**有告警一定弹窗**（哪怕用户关掉了二次确认——关掉二次确认是想少点一次，
   * 不是想被蒙着走）；**没告警就按用户原来的设置**（开了二次确认才弹）。
   */
  const gate = async () => {
    // 预检要占一小段网络往返，别让按钮看起来像没反应
    setChecking(true);
    try {
      const found = await runPreflight(() =>
        mode === "batch"
          ? api.preflightImageBatch({ prompts: batchPrompts, model_keys: batchModels, n })
          : api.preflightImage({
              model_key: modelKey,
              prompt: prompt.trim(),
              n,
              ref_asset_ids: refs.map((r) => r.id),
            }),
      );
      setWarnings(found);
      return found.length > 0 || confirmGen;
    } finally {
      setChecking(false);
    }
  };

  const submit = async () => {
    if (mode === "batch") {
      if (batchModels.length === 0) {
        toast.error("请至少选择一个参与模型");
        return;
      }
      if (batchPrompts.length === 0) {
        toast.error("请输入至少一行提示词");
        return;
      }
      if (batchTotal > batchMax) {
        toast.error(`批量任务数 ${batchTotal} 超过上限 ${batchMax}，请减少提示词或模型`);
        return;
      }
      if (await gate()) {
        setConfirming(true);
        return;
      }
      await doCreateBatch();
      return;
    }
    if (!modelKey) {
      toast.error("请先选择图片模型");
      return;
    }
    if (!prompt.trim()) {
      toast.error("请输入画面描述");
      return;
    }
    if (await gate()) {
      setConfirming(true);
      return;
    }
    await doCreate();
  };

  const doCreate = async () => {
    setSubmitting(true);
    try {
      const t = await api.createImageTask({
        model_key: modelKey,
        prompt: prompt.trim(),
        size,
        n,
        ref_asset_ids: refs.map((r) => r.id),
      });
      setTasks((prev) => [t, ...prev]);
      toast.success("任务已提交");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  const doCreateBatch = async () => {
    setSubmitting(true);
    try {
      const created = await api.createImageBatchTasks({
        prompts: batchPrompts,
        model_keys: batchModels,
        size,
        n,
        ref_asset_ids: refs.map((r) => r.id),
      });
      setTasks((prev) => [...[...created].reverse(), ...prev]);
      toast.success(`已提交 ${created.length} 个任务，可在任务中心跟踪`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "批量提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  if (models.length === 0) {
    return (
      <div className="page">
        <Empty
          icon={<Settings />}
          title="还没有可用的图片模型"
          desc="到「模型服务」添加支持图片生成的服务：OpenAI 兼容接口（如 gpt-image、通义万相）或火山方舟（豆包·Seedream），并添加 image 类型模型。"
          action={
            <button className="btn btn-primary" onClick={onGoSettings}>
              去配置模型服务
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="page">
      <div className="studio-grid">
        <div className="card studio-panel">
          <div className="field">
            <label className="field-label">生成模式</label>
            <div className="segmented">
              <button className={mode === "single" ? "active" : ""} onClick={() => setMode("single")}>
                单次生成
              </button>
              <button className={mode === "batch" ? "active" : ""} onClick={() => setMode("batch")}>
                批量生成
              </button>
            </div>
          </div>

          {mode === "single" ? (
            <>
              <div className="field">
                <label className="field-label">图片模型</label>
                <ModelSelect models={models} value={modelKey} onChange={setModelKey} placeholder="选择图片模型" />
              </div>

              <div className="field">
                <label className="field-label">画面描述</label>
                <textarea
                  className="textarea"
                  rows={5}
                  placeholder="描述你想生成的画面，例如：黄昏的海边，一匹小马站在浅滩上，电影感光影，高细节…"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label className="field-label">参与模型（可多选，对比不同模型）</label>
                <div className="model-chips">
                  {models.map((m) => (
                    <button
                      key={m.key}
                      type="button"
                      className={`model-chip ${batchModels.includes(m.key) ? "active" : ""}`}
                      onClick={() => toggleBatchModel(m.key)}
                    >
                      {m.label || m.name}
                    </button>
                  ))}
                </div>
                <div className="field-hint">已选 {batchModels.length} 个模型，每条提示词会分别用它们各生成一次。</div>
              </div>

              <div className="field">
                <label className="field-label">画面描述（每行一条）</label>
                <textarea
                  className="textarea"
                  rows={6}
                  placeholder={"一行一条提示词，例如：\n黄昏海边，一匹小马站在浅滩上，电影感\n水墨画风格，远山如黛，江上孤舟\n等距 3D 微缩场景，温馨书房"}
                  value={batchText}
                  onChange={(e) => setBatchText(e.target.value)}
                />
                <div className="field-hint">
                  {batchPrompts.length > 0
                    ? `将创建 ${batchTotal} 个任务（${batchPrompts.length} 行 × ${batchModels.length} 模型）`
                    : "每行一条提示词，提交时逐条生成"}
                </div>
              </div>
            </>
          )}

          <div className="field">
            <label className="field-label">画面比例</label>
            <div className="segmented">
              {sizeOptions.map((s) => (
                <button key={s.value} className={size === s.value ? "active" : ""} onClick={() => setSize(s.value)}>
                  {s.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">生成数量</label>
            <div className="segmented">
              {countOptions.map((x) => (
                <button
                  key={x.value}
                  className={n === Number(x.value) ? "active" : ""}
                  onClick={() => setN(Number(x.value))}
                >
                  {x.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              参考图（可选，最多 4 张）
            </label>
            <div className="ref-strip">
              {refs.map((r) => (
                <div key={r.id} className="ref-thumb">
                  <img src={r.url} alt="参考图" />
                  <button
                    onClick={() => setRefs((prev) => prev.filter((x) => x.id !== r.id))}
                    title="移除"
                  >
                    <X size={11} />
                  </button>
                </div>
              ))}
              {refs.length < 4 && (
                // 用 button 而不是带 onClick 的 div：键盘用户也要能上传参考图
                <button
                  type="button"
                  className="upload-tile"
                  onClick={() => fileInput.current?.click()}
                  title="上传参考图"
                >
                  <Upload />
                </button>
              )}
            </div>
            <input
              ref={fileInput}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files) addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <div className="field-hint">部分模型支持参考图（如图生图），不支持时将自动忽略。</div>
          </div>

          <button
            className="btn btn-primary btn-block"
            disabled={submitting || checking}
            onClick={submit}
          >
            {submitting || checking ? <Spinner light /> : <Sparkles size={16} />}
            {submitting
              ? "提交中…"
              : checking
                ? "检查中…"
                : mode === "batch"
                  ? batchTotal > 0
                    ? `批量生成（${batchTotal} 个任务）`
                    : "批量生成"
                  : "生成图片"}
          </button>
        </div>

        <div className="studio-results">
          {tasks.length === 0 && (
            <div className="card">
              <Empty
                icon={<Images />}
                title="作品将在这里出现"
                desc="在左侧写好画面描述，点击「生成图片」。生成的图片会自动保存到资产库。"
                action={
                  mode === "single" ? (
                    <div className="prompt-examples">
                      <span className="prompt-examples-label">不知道写什么？点一条试试：</span>
                      {PROMPT_EXAMPLES.map((ex) => (
                        <button
                          key={ex}
                          type="button"
                          className="prompt-example"
                          onClick={() => setPrompt(ex)}
                        >
                          {ex}
                        </button>
                      ))}
                    </div>
                  ) : null
                }
              />
            </div>
          )}
          {tasks.map((t) => (
            <TaskCard
              key={t.id}
              task={t}
              onPreview={(url, kind, dl) => setPreview({ url, kind, downloadUrl: dl })}
              onUseRef={(a) =>
                setRefs((prev) =>
                  prev.some((x) => x.id === a.id) || prev.length >= 4
                    ? prev
                    : [...prev, { id: a.id, url: a.url }]
                )
              }
            />
          ))}
        </div>
      </div>

      {preview && (
        <Lightbox
          url={preview.url}
          kind={preview.kind}
          downloadUrl={preview.downloadUrl}
          onClose={() => setPreview(null)}
        />
      )}

      {confirming && (
        <Modal
          title={
            warnings.length > 0
              ? `生成前有 ${warnings.length} 条提醒`
              : mode === "batch"
                ? `确认批量生成 ${batchTotal} 个任务？`
                : "确认生成？"
          }
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setConfirming(false)} disabled={submitting}>
                {warnings.length > 0 ? "返回修改" : "取消"}
              </button>
              <button
                className="btn btn-primary"
                disabled={submitting}
                onClick={async () => {
                  setConfirming(false);
                  if (mode === "batch") await doCreateBatch();
                  else await doCreate();
                }}
              >
                {submitting ? <Spinner light /> : null}
                {warnings.length > 0 ? "仍然生成" : "确认生成"}
              </button>
            </>
          }
        >
          <PreflightNotice warnings={warnings} />
          {mode === "batch" ? (
            <div className="confirm-summary">
              <div className="confirm-row">
                <span>模型</span>
                <b>
                  {batchModels
                    .map((k) => models.find((m) => m.key === k)?.label || models.find((m) => m.key === k)?.name || k)
                    .join("、")}
                </b>
              </div>
              <div className="confirm-row">
                <span>任务</span>
                <b>
                  {batchTotal} 个 = {batchPrompts.length} 行提示词 × {batchModels.length} 个模型，每个任务 {n} 张（{size}）
                </b>
              </div>
            </div>
          ) : (
            <div className="confirm-summary">
              <div className="confirm-row">
                <span>模型</span>
                <b>{modelKey}</b>
              </div>
              <div className="confirm-row">
                <span>数量</span>
                <b>
                  {n} 张（{size}）
                  {refs.length > 0 ? `，参考图 ${refs.length} 张` : ""}
                </b>
              </div>
              <div className="confirm-row">
                <span>描述</span>
                <b className="confirm-prompt">{prompt.trim()}</b>
              </div>
            </div>
          )}
          <div className="field-hint">调用外部模型 API 可能产生费用，请确认后再继续。</div>
        </Modal>
      )}
    </div>
  );
}
