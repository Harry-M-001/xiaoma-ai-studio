import { Suspense, lazy, useEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Clapperboard,
  Download,
  Film,
  Scissors,
  Sparkles,
  Upload,
  Wand2,
  X,
} from "lucide-react";
import { api } from "../api";
import { setDraftFirstFrame } from "../promptDraft";
import type { Asset, MergeArgs, MergePreview, SubtitleOptions, TransitionOptions } from "../types";
import { downloadUrl } from "../components/TaskCard";
import { Empty, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

/**
 * 3D 导演台**必须懒加载**：它背后是 three（约 1MB），而绝大多数打开导演台的人
 * 是来粗剪视频的。放在这里 lazy 一下，不进主包也不进这个页面本身。
 */
const DirectorStudio3D = lazy(() => import("../director3d/DirectorStudio3D"));

/** 本机场景的作用域。3D 预演暂时是「一台机器一份草稿」，换机器用导出/导入 JSON */
const SCENE_SCOPE = "local";

/**
 * 转场音效下拉里的取值前缀。一个下拉里混着「内置配方」与「我自己的音频」两来源，
 * 得能分开——`""` 表示不加音效。
 */
const BUILTIN_PREFIX = "builtin:";
const ASSET_PREFIX = "asset:";

function fmt(t: number): string {
  if (!Number.isFinite(t)) return "--:--";
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const ms = Math.floor((t % 1) * 10);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${ms}`;
}

/** 视频粗剪：导入 → 入出点 → 截片段 → 排序合并 → AI 改造 */
function ClipEditor({ onNavigate }: { onNavigate: (route: string) => void }) {
  const toast = useToast();
  const videoRef = useRef<HTMLVideoElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const [ffmpeg, setFfmpeg] = useState<{ available: boolean; version: string | null } | null>(null);
  const [videos, setVideos] = useState<Asset[]>([]);
  const [current, setCurrent] = useState<Asset | null>(null);
  const [probe, setProbe] = useState<{ duration: number | null } | null>(null);
  const [curTime, setCurTime] = useState(0);
  const [inPoint, setInPoint] = useState<number | null>(null);
  const [outPoint, setOutPoint] = useState<number | null>(null);
  const [clips, setClips] = useState<Asset[]>([]);
  const [importing, setImporting] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [merging, setMerging] = useState(false);
  const [merged, setMerged] = useState<Asset | null>(null);
  // 正在截首帧的片段 id（AI 改造要先截一帧才能交给视频页）
  const [remaking, setRemaking] = useState<number | null>(null);

  // ---- 转场与转场音效 ----
  // 预设表从后端拿：`services/transitions.py` 是唯一一份，前端不另抄一张——
  // 抄一份就可能出现「界面能选出一种后端不认识的转场」，而报错要到合并时才知道。
  const [trOpts, setTrOpts] = useState<TransitionOptions | null>(null);
  const [transition, setTransition] = useState("cut");
  const [trSeconds, setTrSeconds] = useState(0.5);
  /** "" = 不加；"builtin:whoosh" = 内置配方；"asset:12" = 资产库里自己的一条音频 */
  const [sfxPick, setSfxPick] = useState("");
  const [audioAssets, setAudioAssets] = useState<Asset[]>([]);
  /** 合并前的算账结果（成片多长、比硬切短多少、这样接行不行） */
  const [preview, setPreview] = useState<MergePreview | null>(null);

  // ---- 字幕 ----
  // 版式**是用户直接选的**（画风只给默认值）；空串 = 没选过 → 用 defaultStyle。
  // 与产物回滚、候选定稿同一个口径：用户显式选过之后，别处不许改。
  const [subOptions, setSubOptions] = useState<SubtitleOptions | null>(null);
  const [subtitleStyle, setSubtitleStyle] = useState("");
  const [subtitleScale, setSubtitleScale] = useState(1);
  /** 逐段一句话，按下标与时间线对齐；空串 = 这一段不出字幕 */
  const [subtitles, setSubtitles] = useState<string[]>([]);

  useEffect(() => {
    (async () => {
      const [status, vids] = await Promise.all([api.ffmpegStatus(), api.listDirectorVideos()]);
      setFfmpeg(status);
      setVideos(vids);
    })();
    api
      .transitionOptions()
      .then((o) => {
        setTrOpts(o);
        setTrSeconds(o.seconds.default);
      })
      .catch(() => {
        /* 预设拉不到也不挡着粗剪：默认硬切本来就够用 */
      });
    api
      .listAssets({ kind: "audio", limit: 60 })
      .then((r) => setAudioAssets(r.items))
      .catch(() => setAudioAssets([]));
    api
      .subtitleOptions()
      .then((o) => {
        setSubOptions(o);
        setSubtitleScale(o.scale.default);
      })
      .catch(() => {
        /* 字幕选项拉不到就不显示字幕区，粗剪本身不受影响 */
      });
  }, []);

  const clipIds = clips.map((c) => c.id);

  /**
   * 把下拉的取值翻成接口参数。两条路：内置配方（本机现场合成）与
   * **资产库里自己的一条音频**——后者优先，与后端 `_resolve_sfx` 的顺序一致。
   */
  const sfxArgs = ((): Pick<MergeArgs, "sfx" | "sfxAssetId"> => {
    if (sfxPick.startsWith(BUILTIN_PREFIX)) return { sfx: sfxPick.slice(BUILTIN_PREFIX.length) };
    if (sfxPick.startsWith(ASSET_PREFIX)) return { sfxAssetId: Number(sfxPick.slice(ASSET_PREFIX.length)) };
    return {};
  })();

  /**
   * 合并前先算账：**成片会比硬切短**（`xfade` 是把相邻两段交叠，不是插一段新的——
   * 三段各 5 秒、转场 1 秒，成片 13 秒而不是 15 秒）。这件事必须在点合并**之前**
   * 说出来，而不是等用户拿到片子发现短了再去猜。接口是纯读（只 ffprobe 量时长），
   * 所以每次改动都可以重算。
   */
  useEffect(() => {
    if (clipIds.length < 2) {
      setPreview(null);
      return;
    }
    let alive = true;
    api
      .mergePreview({ assetIds: clipIds, transition, transitionSeconds: trSeconds, ...sfxArgs })
      .then((p) => {
        if (alive) setPreview(p);
      })
      .catch(() => {
        /* 算不出来就不显示这一行，别把粗剪卡住 */
        if (alive) setPreview(null);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clipIds.join(","), transition, trSeconds, sfxPick]);

  const loadVideos = async () => setVideos(await api.listDirectorVideos());

  const pickVideo = async (a: Asset) => {
    setCurrent(a);
    setInPoint(null);
    setOutPoint(null);
    setMerged(null);
    setProbe(null);
    try {
      setProbe(await api.probeVideo(a.id));
    } catch {
      /* probe 失败不阻塞播放 */
    }
  };

  const doImport = async (f: File) => {
    setImporting(true);
    try {
      const a = await api.importDirectorVideo(f);
      await loadVideos();
      await pickVideo(a);
      toast.success("导入成功");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    } finally {
      setImporting(false);
    }
  };

  const doExtract = async () => {
    if (!current || inPoint == null) return;
    const end = outPoint ?? probe?.duration ?? videoRef.current?.duration ?? null;
    if (end == null || end <= inPoint) {
      toast.error("请先设置出点（或等待视频加载完成）");
      return;
    }
    setExtracting(true);
    try {
      const clip = await api.extractClip(current.id, inPoint, end);
      setClips((prev) => [...prev, clip]);
      await loadVideos();
      toast.success("片段已加入时间线");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "截取失败");
    } finally {
      setExtracting(false);
    }
  };

  const moveClip = (idx: number, dir: -1 | 1) => {
    setClips((prev) => {
      const next = [...prev];
      const j = idx + dir;
      if (j < 0 || j >= next.length) return prev;
      [next[idx], next[j]] = [next[j], next[idx]];
      return next;
    });
  };

  const removeClip = (idx: number) => setClips((prev) => prev.filter((_, i) => i !== idx));

  /**
   * 导演台 → 视频生成（图生视频）。
   *
   * 这里手上是**视频片段**，而生成接口的首帧必须是图片：
   * 直接把片段的 id 当首帧带过去，要到参数填完、点下生成之后才被后端挡下
   * （「首帧图片不存在或不是图片」）——用户会以为是模型的问题。
   * 所以先在本机截一帧（t=0，就是这段的起点）存成图片资产，再把它带过去。
   */
  const aiRemake = async (clip: Asset) => {
    setRemaking(clip.id);
    try {
      const frame = await api.extractThumbnail(clip.id, 0);
      setDraftFirstFrame({ id: frame.id, url: frame.url });
      toast.success("已取这段的首帧，去视频页接着做");
      onNavigate("video");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "截取首帧失败");
    } finally {
      setRemaking(null);
    }
  };

  const doMerge = async () => {
    // 放不下时按钮也是灰的，这里再拦一次：按钮状态可能还没跟上最后一次改动
    if (clips.length < 2 || preview?.problem) return;
    setMerging(true);
    try {
      const out = await api.mergeVideos({
        assetIds: clipIds,
        transition,
        transitionSeconds: trSeconds,
        ...sfxArgs,
        subtitleStyle,
        subtitleScale,
        subtitles: clipIds.map((_, i) => subtitles[i] ?? ""),
      });
      setMerged(out);
      await loadVideos();
      toast.success("合并完成，成片已存入资产库");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "合并失败");
    } finally {
      setMerging(false);
    }
  };

  const totalDur = clips.reduce((s, c) => s + (c.duration ?? 0), 0);
  const canExtract = current != null && inPoint != null && !extracting;
  const isCut = transition === "cut";
  const curPreset = trOpts?.presets.find((p) => p.key === transition) ?? null;
  const curSfx =
    trOpts?.sfx.find((s) => s.key === (sfxPick.startsWith(BUILTIN_PREFIX) ? sfxPick.slice(BUILTIN_PREFIX.length) : "")) ??
    null;

  return (
    <>
      <div className="page-header">
        <div>
          <div className="page-title">导演台</div>
          <div className="page-desc">
            导入视频 → 标记入 / 出点 → 截取片段 → 排序合并导出；片段可送去「AI 改造」重新生成。
          </div>
        </div>
        <div>
          <input
            ref={fileInput}
            type="file"
            accept="video/*"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) doImport(f);
              e.target.value = "";
            }}
          />
          <button
            className="btn btn-primary"
            onClick={() => fileInput.current?.click()}
            disabled={importing || ffmpeg?.available === false}
          >
            {importing ? <Spinner light /> : <Upload size={16} />}
            {importing ? "导入中…" : "导入视频"}
          </button>
        </div>
      </div>

      {ffmpeg && !ffmpeg.available && (
        <div className="card ffmpeg-missing">
          <b>服务器未安装 FFmpeg，导演台不可用。</b>
          <div>安装后重启服务即可，各系统命令：</div>
          <code>winget install ffmpeg</code>
          <code>brew install ffmpeg</code>
          <code>sudo apt install ffmpeg</code>
        </div>
      )}

      <div className="director-layout">
        {/* ---- 左：播放器与入出点 ---- */}
        <div className="card director-player">
          {current ? (
            <>
              <video
                ref={videoRef}
                src={current.url}
                controls
                onTimeUpdate={() => {
                  const v = videoRef.current;
                  if (v) setCurTime(v.currentTime);
                }}
                onLoadedMetadata={() => {
                  const v = videoRef.current;
                  if (v && Number.isFinite(v.duration) && v.duration > 0) {
                    setProbe((p) => ({ duration: v.duration || p?.duration || null }));
                  }
                }}
              />
              <div className="director-timeline">
                <div className="director-time">
                  <span>当前 {fmt(curTime)}</span>
                  <span>入点 {inPoint != null ? fmt(inPoint) : "未设"}</span>
                  <span>出点 {outPoint != null ? fmt(outPoint) : "未设"}</span>
                </div>
                <div className="director-marks">
                  <button className="btn btn-ghost btn-sm" onClick={() => setInPoint(curTime)}>
                    设为入点
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setOutPoint(curTime)}>
                    设为出点
                  </button>
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={!canExtract}
                    onClick={doExtract}
                  >
                    {extracting ? <Spinner light /> : <Scissors size={14} />}
                    {extracting ? "截取中…" : "截取片段"}
                  </button>
                  {inPoint != null && outPoint != null && (
                    <span className="director-range">
                      时长 {(outPoint - inPoint).toFixed(1)}s
                    </span>
                  )}
                </div>
              </div>
              <div className="director-src muted">
                素材：{current.original_name}
                {probe?.duration ? ` · 全长 ${fmt(probe.duration)}` : ""}
              </div>
            </>
          ) : (
            <Empty
              icon={<Film />}
              title="先导入一段视频"
              desc="支持 mp4 / webm / mov，导入后即可标记入出点截取片段。也可从下方素材列表选择已有视频。"
            />
          )}
        </div>

        {/* ---- 右：素材列表 + 时间线 ---- */}
        <div className="director-side">
          {videos.length > 0 && (
            <div className="card director-videos">
              <div className="card-title-sm">视频素材（{videos.length}）</div>
              <div className="director-video-list">
                {videos.slice(0, 8).map((v) => (
                  <button
                    key={v.id}
                    className={`director-video-item ${current?.id === v.id ? "active" : ""}`}
                    onClick={() => pickVideo(v)}
                    title={v.original_name}
                  >
                    <Film size={13} />
                    <span className="director-video-name">{v.original_name}</span>
                    {v.duration ? <em>{v.duration}s</em> : null}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="card director-clips">
            <div className="card-title-sm">
              时间线（{clips.length} 段{clips.length > 0 ? ` · 约 ${totalDur}s` : ""}）
            </div>
            {clips.length === 0 ? (
              <div className="muted director-clips-empty">
                在左侧播放器标记入点 / 出点后点「截取片段」，截好的片段会按顺序排在这里。
              </div>
            ) : (
              <div className="director-clip-list">
                {clips.map((c, i) => (
                  <div key={c.id} className="director-clip">
                    <span className="director-clip-no">{i + 1}</span>
                    <video src={c.url} preload="metadata" muted />
                    <div className="director-clip-info">
                      <div className="director-clip-name" title={c.original_name}>
                        {c.original_name}
                      </div>
                      <div className="muted">{c.duration ?? "?"}s · {(c.size / 1024 / 1024).toFixed(1)}MB</div>
                    </div>
                    <div className="director-clip-ops">
                      <button className="icon-btn" title="上移" disabled={i === 0} onClick={() => moveClip(i, -1)}>
                        <ArrowUp size={14} />
                      </button>
                      <button
                        className="icon-btn"
                        title="下移"
                        disabled={i === clips.length - 1}
                        onClick={() => moveClip(i, 1)}
                      >
                        <ArrowDown size={14} />
                      </button>
                      <button
                        className="icon-btn"
                        title="AI 改造：取这段的首帧，去视频页做图生视频"
                        disabled={remaking !== null}
                        onClick={() => aiRemake(c)}
                      >
                        {remaking === c.id ? <Spinner /> : <Wand2 size={14} />}
                      </button>
                      <button className="icon-btn" title="移除" onClick={() => removeClip(i)}>
                        <X size={14} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {clips.length >= 2 && (
              <div className="director-transition">
                <div className="director-transition-row">
                  <label className="field">
                    <span>片段之间怎么接</span>
                    <select
                      className="select"
                      value={transition}
                      onChange={(e) => setTransition(e.target.value)}
                    >
                      {(trOpts?.presets ?? []).map((p) => (
                        <option key={p.key} value={p.key}>
                          {p.label}
                        </option>
                      ))}
                    </select>
                    {curPreset && <span className="field-hint">{curPreset.hint}</span>}
                  </label>
                  <label className="field">
                    <span>转场时长（秒）</span>
                    <input
                      className="input"
                      type="number"
                      step={0.1}
                      min={trOpts?.seconds.min ?? 0.2}
                      max={trOpts?.seconds.max ?? 2}
                      value={trSeconds}
                      disabled={isCut}
                      onChange={(e) => setTrSeconds(Number(e.target.value))}
                    />
                    <span className="field-hint">
                      {isCut
                        ? "硬切不用这个"
                        : `可取 ${trOpts?.seconds.min ?? 0.2}–${trOpts?.seconds.max ?? 2} 秒`}
                    </span>
                  </label>
                  <label className="field">
                    <span>转场处音效</span>
                    <select
                      className="select"
                      value={sfxPick}
                      disabled={isCut}
                      onChange={(e) => setSfxPick(e.target.value)}
                    >
                      <optgroup label="内置音效（本机合成，不下载素材）">
                        {(trOpts?.sfx ?? []).map((s) => (
                          <option key={s.key || "none"} value={s.key ? `${BUILTIN_PREFIX}${s.key}` : ""}>
                            {s.label}
                          </option>
                        ))}
                      </optgroup>
                      {audioAssets.length > 0 && (
                        <optgroup label="用资产库里我自己的音频">
                          {audioAssets.map((a) => (
                            <option key={a.id} value={`${ASSET_PREFIX}${a.id}`}>
                              {a.name || a.original_name}
                              {a.duration ? `（${a.duration} 秒）` : ""}
                            </option>
                          ))}
                        </optgroup>
                      )}
                    </select>
                    <span className="field-hint">
                      {isCut
                        ? "硬切没有转场，也就没有转场音效"
                        : sfxPick.startsWith(ASSET_PREFIX)
                          ? "用你选的那条音频，长度以片子为准自动截断"
                          : (curSfx?.hint ?? "")}
                    </span>
                  </label>
                </div>

                <SubtitlePanel
                  options={subOptions}
                  clips={clips}
                  styleKey={subtitleStyle}
                  onStyleChange={setSubtitleStyle}
                  scale={subtitleScale}
                  onScaleChange={setSubtitleScale}
                  texts={subtitles}
                  onTextsChange={setSubtitles}
                  onOptionsChange={setSubOptions}
                  toastOk={toast.success}
                  toastErr={toast.error}
                />

                {preview?.problem ? (
                  <div className="director-merge-warn">{preview.problem}</div>
                ) : preview ? (
                  <div className="director-merge-note">
                    成片约 <b>{preview.totalSeconds}s</b>
                    {preview.shortfallSeconds > 0
                      ? `——比硬切短 ${preview.shortfallSeconds}s（转场是把相邻两段交叠，不是插一段新的）`
                      : "（硬切，各段时长直接相加）"}
                  </div>
                ) : null}

                <button
                  className="btn btn-primary btn-block"
                  disabled={merging || Boolean(preview?.problem)}
                  onClick={doMerge}
                >
                  {merging ? <Spinner light /> : <Clapperboard size={15} />}
                  {merging ? "合并中…" : `合并导出（${clips.length} 段）`}
                </button>
              </div>
            )}
          </div>

          {merged && (
            <div className="card director-merged">
              <div className="card-title-sm">
                <Sparkles size={13} /> 成片
              </div>
              <video src={merged.url} controls />
              <a className="btn btn-primary btn-sm" href={downloadUrl(merged.id)}>
                <Download size={14} />
                下载成片
              </a>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/**
 * 字幕：版式、字号、逐段一句话，以及**真渲染的版式预览**。
 *
 * 四条设计口径（都来自前面几次踩坑）：
 *
 * 1. **版式是用户直接选的，画风只给默认值。** 导演台没有画风可选，所以这里的取值就是
 *    最终结果；一旦用户选过，别处不许改（与产物回滚、候选定稿同一个口径）。
 * 2. **预览走后端真渲染一帧**，不在前端用 CSS 近似画。版式取决于 libass 的字体选择、
 *    字形、描边、缩放、边距，前端再写一套必然对不上。
 * 3. **字体缺了就地能下**。下到本机数据目录（不进程序目录），下完立即刷新可用状态。
 *    「没下过」与「下坏了」分开说——后者要用户**重新**下载，同一句话会让他以为「我明明下过了」。
 * 4. **字幕内容留空 = 这一段不出字幕**，不是「必填项没填」。所以措辞上说的是
 *    「留空则这一段不出字幕」，而不是报错。
 */
function SubtitlePanel({
  options,
  clips,
  styleKey,
  onStyleChange,
  scale,
  onScaleChange,
  texts,
  onTextsChange,
  onOptionsChange,
  toastOk,
  toastErr,
}: {
  options: SubtitleOptions | null;
  clips: Asset[];
  styleKey: string;
  onStyleChange: (v: string) => void;
  scale: number;
  onScaleChange: (v: number) => void;
  texts: string[];
  onTextsChange: (v: string[]) => void;
  onOptionsChange: (v: SubtitleOptions) => void;
  toastOk: (m: string) => void;
  toastErr: (m: string) => void;
}) {
  const [shot, setShot] = useState<string>("");
  const [rendering, setRendering] = useState(false);
  const [busyFont, setBusyFont] = useState("");

  const style = options?.styles.find((s) => s.key === styleKey) ?? null;
  const filled = texts.filter((t) => (t ?? "").trim()).length;

  // 画面尺寸用第一段素材的；没有就用 1280x720（预览只是个示意，比例对就行）
  const size = { width: clips[0]?.width || 1280, height: clips[0]?.height || 720 };

  const render = async (nextStyle = styleKey, nextScale = scale) => {
    if (!options?.filterAvailable) return;
    setRendering(true);
    try {
      const r = await api.subtitlePreview({
        style: nextStyle,
        scale: nextScale,
        text: "",
        ...size,
      });
      setShot(r.image);
    } catch (e) {
      // 预览失败不该挡住导出：说一句就好，别把它做成一个必须过的关
      toastErr(e instanceof Error ? e.message : "版式预览失败");
    } finally {
      setRendering(false);
    }
  };

  const pickStyle = (v: string) => {
    onStyleChange(v);
    // 这款版式要的字体不在本机时**不去渲染**：后端会拒绝，弹一句错，而下面已经给了
    // 「下载这款字体」的入口——那时候用户要做的是下载，不是看报错。
    const target = options?.styles.find((s) => s.key === v);
    if (target?.fontReady) void render(v, scale);
  };

  const download = async (key: string) => {
    setBusyFont(key);
    try {
      const r = await api.downloadSubtitleFont(key);
      // **整份替换**，不只换字体列表：版式里的 fontReady 也在这一份里。
      // 只换一半的话会出现「下完了但提示还在」（走查时踩到过）。
      onOptionsChange(r.options);
      toastOk(r.message);
      // 下完顺手把当前版式的预览渲出来——用户刚才就是为了看它才下的字体
      const ready = r.options.styles.find((s) => s.key === (styleKey || r.options.defaultStyle));
      if (ready?.fontReady) void render(styleKey || r.options.defaultStyle, scale);
    } catch (e) {
      toastErr(e instanceof Error ? e.message : "字体下载失败");
    } finally {
      setBusyFont("");
    }
  };

  if (!options) return null;

  if (!options.filterAvailable) {
    return (
      <div className="director-merge-warn">
        这台机器上的 ffmpeg 没有字幕滤镜（libass），烧不了字幕。
        换一个完整的 ffmpeg 构建就行——「系统设置 → 环境体检」里能看到当前用的是哪一个。
      </div>
    );
  }

  return (
    <div className="director-subtitles">
      <div className="director-transition-row">
        <label className="field">
          <span>字幕版式</span>
          <select className="select" value={styleKey || options.defaultStyle} onChange={(e) => pickStyle(e.target.value)}>
            {options.styles.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
                {s.fontReady ? "" : `（要下 ${s.fontLabel}）`}
              </option>
            ))}
          </select>
          <span className="field-hint">{style?.hint ?? ""}</span>
        </label>
        <label className="field">
          <span>字号（倍）</span>
          <input
            className="input"
            type="number"
            step={0.1}
            min={options.scale.min}
            max={options.scale.max}
            value={scale}
            onChange={(e) => {
              onScaleChange(Number(e.target.value));
            }}
            onBlur={() => void render(styleKey, scale)}
          />
          <span className="field-hint">
            可取 {options.scale.min}–{options.scale.max} 倍
          </span>
        </label>
      </div>

      {style && !style.fontReady && (
        <div className="director-sub-font-missing">
          <span>
            这款版式要用的「{style.fontLabel}」还没下载到本机
          </span>
          <button
            className="btn btn-sm"
            disabled={busyFont !== ""}
            onClick={() => void download(style.font)}
          >
            {busyFont === style.font ? <Spinner /> : null}
            {busyFont === style.font ? "下载中…" : "下载这款字体"}
          </button>
        </div>
      )}

      <div className="director-sub-shot">
        {shot ? (
          <img src={shot} alt="版式预览" className="director-sub-shot-img" />
        ) : (
          <div className="director-sub-shot-empty">
            {rendering ? "正在渲染预览…" : "点「看看这版式」渲一帧，用的是真字体真描边"}
          </div>
        )}
        <button className="btn btn-sm" disabled={rendering} onClick={() => void render()}>
          {rendering ? <Spinner /> : null}
          看看这版式
        </button>
      </div>

      <div className="director-sub-lines">
        <div className="director-sub-lines-head">
          每段一句话（{filled} / {clips.length} 段有字幕；留空则这一段不出字幕）
        </div>
        {clips.map((c, i) => (
          <div key={c.id} className="director-sub-line">
            <span className="director-clip-no">{i + 1}</span>
            <input
              className="input"
              value={texts[i] ?? ""}
              placeholder={`第 ${i + 1} 段的一句话（${c.duration ?? "?"} 秒）`}
              onChange={(e) => {
                const next = [...texts];
                while (next.length < clips.length) next.push("");
                next[i] = e.target.value;
                onTextsChange(next);
              }}
            />
          </div>
        ))}
        <span className="field-hint">
          时间轴按**成片**里各段的区间算（配了转场会跟着变短），不用手填时间码。
        </span>
      </div>
    </div>
  );
}

/**
 * 导演台（页面壳）：上面两个页签——「视频粗剪」与「3D 预演」。
 *
 * 为什么不把 3D 预演单开一个导航项：导航是数据驱动的（注册表里有种子数据），
 * 加一项要动种子、路由白名单、模块开关三处；而这两件事本来就是同一件事的两半
 * （先摆好空间关系，再剪成片），放在一个入口下更顺。
 */
export default function DirectorPage({ onNavigate }: { onNavigate: (route: string) => void }) {
  const [tab, setTab] = useState<"clip" | "3d">("clip");

  return (
    <div className={tab === "3d" ? "page director-3d-page" : "page"}>
      <div className="director-tabs">
        <button type="button" className={tab === "clip" ? "on" : ""} onClick={() => setTab("clip")}>
          <Scissors size={14} /> 视频粗剪
        </button>
        <button type="button" className={tab === "3d" ? "on" : ""} onClick={() => setTab("3d")}>
          <Clapperboard size={14} /> 3D 预演
        </button>
      </div>

      {tab === "clip" ? (
        <ClipEditor onNavigate={onNavigate} />
      ) : (
        <div className="director-3d-host">
          <Suspense fallback={<div className="director-3d-loading">正在加载 3D 模块…</div>}>
            {/* key 绑定作用域：将来支持多场景时换 scope 会强制重挂载，不会串档 */}
            <DirectorStudio3D key={SCENE_SCOPE} scope={SCENE_SCOPE} onClose={() => setTab("clip")} />
          </Suspense>
        </div>
      )}
    </div>
  );
}
