import { useEffect, useRef, useState } from "react";
import { Clapperboard, Film, Settings, Upload, Video as VideoIcon, X } from "lucide-react";
import { api, cfgBool, cfgNumber, cfgString } from "../api";
import { consumeDraftFirstFrame, consumeDraftPrompt } from "../promptDraft";
import type { Asset, ConfigMap, ModelOption, ParamOptionItem, PreflightWarning, Task } from "../types";
import { Empty, Modal, ModelSelect, Spinner, isRunning } from "../components/common";
import { PreflightNotice, runPreflight } from "../components/PreflightNotice";
import { useToast } from "../components/Toast";
import { prefs } from "../prefs";
import TaskCard from "../components/TaskCard";
import Lightbox from "../components/Lightbox";

/** 后端未配置参数档位时的内置回落，保证页面仍然可用 */
const FALLBACK_DURATION: ParamOptionItem[] = [
  { id: -1, value: "5", label: "5 秒", meta: {}, sort_order: 1 },
];
const FALLBACK_RATIO: ParamOptionItem[] = [
  { id: -1, value: "16:9", label: "16:9", meta: {}, sort_order: 1 },
];
const FALLBACK_RESOLUTION: ParamOptionItem[] = [
  { id: -1, value: "720p", label: "720p", meta: {}, sort_order: 1 },
];

/** 在档位列表中选中：命中配置值则用它，否则回落到首个档位 */
function pickValue(opts: ParamOptionItem[], preferred: string): string {
  if (opts.length === 0) return preferred;
  return opts.some((o) => o.value === preferred) ? preferred : opts[0].value;
}

export default function VideoPage({ onGoSettings }: { onGoSettings: () => void }) {
  const toast = useToast();
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelKey, setModelKey] = useState("");
  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState(5);
  const [ratio, setRatio] = useState("16:9");
  const [resolution, setResolution] = useState("720p");
  const [durationOptions, setDurationOptions] = useState<ParamOptionItem[]>(FALLBACK_DURATION);
  const [ratioOptions, setRatioOptions] = useState<ParamOptionItem[]>(FALLBACK_RATIO);
  const [resolutionOptions, setResolutionOptions] = useState<ParamOptionItem[]>(FALLBACK_RESOLUTION);
  const [firstFrame, setFirstFrame] = useState<{ id: number; url: string } | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmGen, setConfirmGen] = useState(true);
  // 生成前的软告警：只展示，不改变能不能提交
  const [warnings, setWarnings] = useState<PreflightWarning[]>([]);
  const [checking, setChecking] = useState(false);
  const [preview, setPreview] = useState<{ url: string; kind: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadTasks = async () => setTasks(await api.listTasks("video", 10));

  useEffect(() => {
    (async () => {
      const [ms, ts] = await Promise.all([api.listModels("video"), api.listTasks("video", 10)]);
      setModels(ms);
      setTasks(ts);
      // 记住的模型可能已经不存在了（服务被删/改名）：合法的沿用，失效的丢掉并退回第一个可用的
      const keys = ms.map((m) => m.key);
      setModelKey((prev) => prev || prefs.modelKey.get("video", keys) || keys[0] || "");
    })();
    // 从提示词库「去视频生成」带过来的模板内容
    const draft = consumeDraftPrompt("video");
    if (draft) setPrompt(draft);
    // 从导演台「AI 改造」带过来的首帧片段
    const draftFrame = consumeDraftFirstFrame();
    if (draftFrame) setFirstFrame(draftFrame);
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
      const durations = options.video_duration?.length ? options.video_duration : FALLBACK_DURATION;
      const ratios = options.video_ratio?.length ? options.video_ratio : FALLBACK_RATIO;
      const resolutions = options.video_resolution?.length ? options.video_resolution : FALLBACK_RESOLUTION;
      setDurationOptions(durations);
      setRatioOptions(ratios);
      setResolutionOptions(resolutions);
      setDuration(Number(pickValue(durations, String(cfgNumber(config, "defaults.video_duration", 5)))));
      setRatio(pickValue(ratios, cfgString(config, "defaults.video_ratio", "16:9")));
      setResolution(pickValue(resolutions, cfgString(config, "defaults.video_resolution", "720p")));
      setConfirmGen(cfgBool(config, "safety.confirm_before_generate", true));
    })();
  }, []);

  useEffect(() => {
    prefs.modelKey.set("video", modelKey);
  }, [modelKey]);

  const runningIds = tasks.filter((t) => isRunning(t.status)).map((t) => t.id).join(",");
  useEffect(() => {
    if (!runningIds) return;
    const ids = runningIds.split(",").map(Number);
    const timer = setInterval(async () => {
      const updated = await Promise.all(ids.map((id) => api.getTask(id)));
      setTasks((prev) => prev.map((t) => updated.find((u) => u.id === t.id) ?? t));
      if (updated.some((t) => !isRunning(t.status))) setTimeout(loadTasks, 600);
    }, 4000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningIds]);

  const uploadFrame = async (file: File) => {
    if (!file.type.startsWith("image/")) {
      toast.error("首帧需要是图片文件");
      return;
    }
    try {
      const a: Asset = await api.uploadAsset(file);
      setFirstFrame({ id: a.id, url: a.url });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "上传失败");
    }
  };

  /** 提交前先问一句「你这组合可能不是你要的」，但不拦。
   *
   * 视频这一条尤其值钱：一次出片又慢又贵，而「首帧是 16:9 却选了 9:16」
   * 这种错上游不会报错，只会闷头裁切，等看到成片才知道白花了额度。
   */
  const gate = async () => {
    setChecking(true);
    try {
      const found = await runPreflight(() =>
        api.preflightVideo({
          model_key: modelKey,
          prompt: prompt.trim(),
          first_frame_asset_id: firstFrame?.id ?? null,
          ratio,
          resolution,
        }),
      );
      setWarnings(found);
      return found.length > 0 || confirmGen;
    } finally {
      setChecking(false);
    }
  };

  const submit = async () => {
    if (!modelKey) {
      toast.error("请先选择视频模型");
      return;
    }
    if (!prompt.trim()) {
      toast.error("请输入视频描述");
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
      const t = await api.createVideoTask({
        model_key: modelKey,
        prompt: prompt.trim(),
        first_frame_asset_id: firstFrame?.id ?? null,
        duration,
        ratio,
        resolution,
      });
      setTasks((prev) => [t, ...prev]);
      toast.success("视频任务已提交，通常需要 1-5 分钟");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  if (models.length === 0) {
    return (
      <div className="page">
        <Empty
          icon={<Settings />}
          title="还没有可用的视频模型"
          desc="到「模型服务」添加火山方舟服务并添加 video 类型模型（如豆包·Seedance 视频生成模型），即可开始文生视频 / 图生视频。"
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
            <label className="field-label">视频模型</label>
            <ModelSelect models={models} value={modelKey} onChange={setModelKey} placeholder="选择视频模型" />
          </div>

          <div className="field">
            <label className="field-label">视频描述</label>
            <textarea
              className="textarea"
              rows={4}
              placeholder="描述画面内容与运动，例如：镜头缓缓推近，小马转头看向镜头，鬃毛随风飘动…"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>

          <div className="field">
            <label className="field-label">首帧图片（可选，图生视频）</label>
            {firstFrame ? (
              <div className="ref-thumb" style={{ width: 96, height: 96 }}>
                <img src={firstFrame.url} alt="首帧" />
                <button onClick={() => setFirstFrame(null)} title="移除">
                  <X size={11} />
                </button>
              </div>
            ) : (
              // 用 button 而不是带 onClick 的 div：键盘用户也要能选首帧
              <button
                type="button"
                className="upload-tile"
                style={{ width: 96, height: 96 }}
                onClick={() => fileInput.current?.click()}
              >
                <Upload />
              </button>
            )}
            <input
              ref={fileInput}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => {
                if (e.target.files?.[0]) uploadFrame(e.target.files[0]);
                e.target.value = "";
              }}
            />
          </div>

          <div className="field">
            <label className="field-label">时长</label>
            <div className="segmented">
              {durationOptions.map((d) => (
                <button
                  key={d.value}
                  className={duration === Number(d.value) ? "active" : ""}
                  onClick={() => setDuration(Number(d.value))}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">画幅比例</label>
            <div className="segmented">
              {ratioOptions.map((r) => (
                <button key={r.value} className={ratio === r.value ? "active" : ""} onClick={() => setRatio(r.value)}>
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">清晰度</label>
            <div className="segmented">
              {resolutionOptions.map((r) => (
                <button
                  key={r.value}
                  className={resolution === r.value ? "active" : ""}
                  onClick={() => setResolution(r.value)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            <div className="field-hint">参数支持情况取决于具体模型，不支持的参数会被自动忽略。</div>
          </div>

          <button
            className="btn btn-primary btn-block"
            disabled={submitting || checking}
            onClick={submit}
          >
            {submitting || checking ? <Spinner light /> : <Clapperboard size={16} />}
            {submitting ? "提交中…" : checking ? "检查中…" : "生成视频"}
          </button>
        </div>

        <div className="studio-results">
          {tasks.length === 0 && (
            <div className="card">
              <Empty
                icon={<Film />}
                title="视频任务将在这里出现"
                desc="视频生成耗时较长，提交后可以离开本页，任务在后台运行，完成后自动入库。"
              />
            </div>
          )}
          {tasks.map((t) => (
            <TaskCard key={t.id} task={t} onPreview={(url, kind) => setPreview({ url, kind })} />
          ))}
        </div>
      </div>

      {preview && <Lightbox url={preview.url} kind={preview.kind} onClose={() => setPreview(null)} />}

      {confirming && (
        <Modal
          title={warnings.length > 0 ? `生成前有 ${warnings.length} 条提醒` : "确认生成视频？"}
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
                  await doCreate();
                }}
              >
                {submitting ? <Spinner light /> : null}
                {warnings.length > 0 ? "仍然生成" : "确认生成"}
              </button>
            </>
          }
        >
          <PreflightNotice warnings={warnings} />
          <div className="confirm-summary">
            <div className="confirm-row">
              <span>模型</span>
              <b>{modelKey}</b>
            </div>
            <div className="confirm-row">
              <span>参数</span>
              <b>
                {duration} 秒 · {ratio} · {resolution}
                {firstFrame ? " · 图生视频" : ""}
              </b>
            </div>
            <div className="confirm-row">
              <span>描述</span>
              <b className="confirm-prompt">{prompt.trim()}</b>
            </div>
          </div>
          <div className="field-hint">视频生成通常消耗较多额度，请确认后再继续。</div>
        </Modal>
      )}
    </div>
  );
}
