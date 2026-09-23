import { Suspense, lazy, useEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Clapperboard,
  Download,
  Eraser,
  Eye,
  Film,
  Gauge,
  Maximize2,
  Scissors,
  Sparkles,
  Upload,
  Wand2,
  X,
} from "lucide-react";
import { api } from "../api";
import { setDraftFirstFrame } from "../promptDraft";
import type {
  Asset,
  MergeArgs,
  MergePreview,
  RemovalBox,
  RemovalFrame,
  RemovalPreview,
  SubtitleOptions,
  TransitionOptions,
} from "../types";
import { downloadUrl } from "../components/TaskCard";
import { Dialog } from "../components/Dialog";
import { UpscaleDialog } from "../components/UpscaleDialog";
import { InterpDialog } from "../components/InterpDialog";
import { Empty, isDone, isRunning, Spinner } from "../components/common";
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

/**
 * 框的最小边（像素）。与后端 `subtitle_removal.MIN_SIDE` 是同一个数：
 * 后端会把越界的框夹回来，但**拖动时就该停住**，不然用户以为自己拖动了、其实没动。
 * 更小也不行——退化成一两条线的框 ffmpeg 不报错，等于什么都没做。
 */
const BOX_MIN = 8;

type BoxMode = "move" | "nw" | "ne" | "sw" | "se";

function clampBox(mode: BoxMode, orig: RemovalBox, dx: number, dy: number,
                  W: number, H: number): RemovalBox {
  const right = orig.x + orig.w;
  const bottom = orig.y + orig.h;
  const cl = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
  let x = orig.x;
  let y = orig.y;
  let w = orig.w;
  let h = orig.h;
  if (mode === "move") {
    x = cl(orig.x + dx, 0, Math.max(0, W - orig.w));
    y = cl(orig.y + dy, 0, Math.max(0, H - orig.h));
  } else {
    if (mode === "nw" || mode === "sw") {
      x = cl(orig.x + dx, 0, right - BOX_MIN);
      w = right - x;
    } else {
      w = cl(orig.w + dx, BOX_MIN, W - orig.x);
    }
    if (mode === "nw" || mode === "ne") {
      y = cl(orig.y + dy, 0, bottom - BOX_MIN);
      h = bottom - y;
    } else {
      h = cl(orig.h + dy, BOX_MIN, H - orig.y);
    }
  }
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

/**
 * 在真帧上拖出「哪一块要处理」。
 *
 * 为什么是拖框而不是填坐标：字幕在画面里的位置只能看，说不出来——填 x/y/宽/高
 * 会让每个人先量三遍。框的坐标全程用**源视频像素**（显示尺寸只是画面被 CSS 缩过），
 * 换算只发生在拖动时取下容器矩形这一步；把显示尺寸当坐标存起来，
 * 换个窗口大小就会「看着框住了、处理的是别处」。
 */
function RemovalBoxPicker({ src, natural, box, onChange, onCommit }: {
  src: string;
  natural: { w: number; h: number };
  box: RemovalBox;
  onChange: (b: RemovalBox) => void;
  /** 松手时回调：可选项随框变，要在这时去问后端（拖动过程中不问，太密） */
  onCommit?: () => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const drag = useRef<{ mode: BoxMode; px: number; py: number; rect: DOMRect; orig: RemovalBox } | null>(null);

  const begin = (mode: BoxMode) => (e: React.PointerEvent) => {
    const rect = holder.current?.getBoundingClientRect();
    if (!rect) return;
    e.preventDefault();
    e.stopPropagation();
    drag.current = { mode, px: e.clientX, py: e.clientY, rect, orig: { ...box } };
    // 捕获指针：拖到画面外再松手也能收到 pointerup（不捕获的话框会「粘」在鼠标上继续动）。
    // try 一下是因为 pointerId 已经失效时它会抛 NotFoundError（快速点一下就可能发生），
    // 那种情况下拖动状态已经设好了，不该因为这一句把整次拖动打断。
    try {
      (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    } catch {
      /* 指针已经没了就算了：下面的 move/up 走的是容器上的监听 */
    }
  };

  const move = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    // 屏幕位移 → 源视频像素
    const dx = ((e.clientX - d.px) / d.rect.width) * natural.w;
    const dy = ((e.clientY - d.py) / d.rect.height) * natural.h;
    onChange(clampBox(d.mode, d.orig, dx, dy, natural.w, natural.h));
  };

  const end = () => {
    if (!drag.current) return;
    drag.current = null;
    onCommit?.();
  };

  const pct = (v: number, total: number) => `${(v / total) * 100}%`;
  const handles: { mode: BoxMode; cls: string }[] = [
    { mode: "nw", cls: "nw" },
    { mode: "ne", cls: "ne" },
    { mode: "sw", cls: "sw" },
    { mode: "se", cls: "se" },
  ];

  return (
    <div
      className="subrm-canvas"
      ref={holder}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
    >
      <img src={src} alt="待处理的一帧" draggable={false} />
      <div
        className="subrm-box"
        style={{
          left: pct(box.x, natural.w),
          top: pct(box.y, natural.h),
          width: pct(box.w, natural.w),
          height: pct(box.h, natural.h),
        }}
        onPointerDown={begin("move")}
        title="拖动移动这一块（拖四个角改大小）"
      >
        {handles.map((h) => (
          <span key={h.mode} className={`subrm-handle subrm-handle-${h.cls}`} onPointerDown={begin(h.mode)} />
        ))}
      </div>
    </div>
  );
}

/**
 * 「去字幕」弹窗：在真帧上框出字幕带，挑一种手法，先看真渲染的一帧，再处理整段。
 *
 * 三件事是这个弹窗存在的理由：
 * 1. **框必须拖**（字幕在哪只能看）；
 * 2. **预览是后端真渲一帧**，与成品同一套滤镜——「预览看着行、导出来不行」最伤信任；
 * 3. **四种手法的代价写在旁边**。ffmpeg 去字幕做不到「无痕」：抹平是竖向拖痕、
 *    遮住是拉出来的一道痕、模糊是一条糊痕、裁掉画面会变矮。用户是拿这些代价
 *    去换「没有那行字」的。真无痕要 AI 补全，那是另一档（本机 VSR，需自己下载）。
 */
function RemoveSubtitlesDialog({ asset, onClose, onReplaced }: {
  asset: Asset;
  onClose: () => void;
  onReplaced: (made: Asset) => void;
}) {
  const toast = useToast();
  const [frame, setFrame] = useState<RemovalFrame | null>(null);
  const [box, setBox] = useState<RemovalBox | null>(null);
  const [method, setMethod] = useState("");
  const [t, setT] = useState(0);
  const [loading, setLoading] = useState(true);
  const [preview, setPreview] = useState<RemovalPreview | null>(null);
  const [stale, setStale] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [problem, setProblem] = useState("");

  const loadFrame = async (at: number) => {
    setLoading(true);
    setProblem("");
    try {
      const data = await api.removalFrame({ assetId: asset.id, t: at });
      setFrame(data);
      // 框在源视频像素里，换帧不会让它失效——字幕在帧间不会跑；
      // 第一次取帧才用推荐框（之后用户拖过就以他的为准）
      if (!boxRef.current) putBox(data.box);
      setMethod((prev) => prev || data.defaultMethod);
      setStale(true);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "取帧失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadFrame(0);
    // 只在打开时取第一帧；换帧由用户点
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asset.id]);

  const runPreview = async (nextMethod?: string) => {
    if (!box) return;
    const use = nextMethod ?? method;
    setPreviewing(true);
    setProblem("");
    try {
      const data = await api.removalPreview({ assetId: asset.id, t, box, method: use });
      setPreview(data);
      setStale(false);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "预览失败");
    } finally {
      setPreviewing(false);
    }
  };

  const checkSeq = useRef(0);
  /**
   * 框的**同步**副本。
   *
   * 为什么不能直接读 state：拖动时 `onChange` 改的是 state，而 React 的更新是异步的
   * （一轮事件处理完才重渲染）。用户**快速一甩**（按下、移动、松手都在同一帧里）时，
   * 松手那一刻闭包里的 `box` 还是旧值——刷新可选项就会把**旧框**发给后端、
   * 再把旧框设回来，表现成「我明明拖了，它自己弹回去了」。
   * 这个 ref 在 onChange 里同步写，所以任何时刻读到的都是最新的框。
   */
  const boxRef = useRef<RemovalBox | null>(null);
  const putBox = (b: RemovalBox) => {
    boxRef.current = b;
    setBox(b);
  };

  /**
   * 框变了就重新问一次「四种手法各能不能用」。
   *
   * 不能只在打开弹窗时算一次：框挪到画面中间时「裁掉」就不能用了，界面若还显示能选，
   * 用户点下去才报错——「界面说行、后端说不行」最伤信任。这个接口不碰 ffmpeg，
   * 所以松手后随便调。拖动过程中不调（太密），只在松手 / 换预设时调。
   */
  const refreshMethods = async (next?: RemovalBox) => {
    const use = next ?? boxRef.current;
    if (!use) return;
    const seq = ++checkSeq.current;
    try {
      const data = await api.removalCheck({ assetId: asset.id, box: use });
      if (seq !== checkSeq.current) return; // 有更新的请求在跑，旧的丢掉
      setFrame((prev) => (prev ? { ...prev, box: data.box, methods: data.methods } : prev));
      putBox(data.box);
      setMethod((prev) => {
        const row = data.methods.find((m) => m.key === prev);
        // 选中的手法变得不可用了就退回默认——留一个「选中但灰掉」的单选更让人困惑
        return row && row.available ? prev : data.defaultMethod;
      });
    } catch {
      /* 刷新失败不挡事：真正的拦截在后端，这里只是少一次同步 */
    }
  };

  const apply = async () => {
    if (!box || applying) return;
    setApplying(true);
    setProblem("");
    try {
      const made = await api.removeSubtitles({ assetId: asset.id, box, method });
      toast.success("已用去字幕后的版本替换这一段（原片仍在资产库）");
      onReplaced(made);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "去字幕失败");
    } finally {
      setApplying(false);
    }
  };

  const pickMethod = (key: string) => {
    setMethod(key);
    // 已经对比过的人换手法就是想比一比——直接给新的那一帧，不用再点一次
    if (preview) void runPreview(key);
    else setStale(true);
  };

  const presets: { label: string; make: (W: number, H: number) => RemovalBox }[] = [
    { label: "底部一条", make: (W, H) => ({ x: 0, y: Math.round(H * 0.84), w: W, h: Math.round(H * 0.16) }) },
    { label: "顶部一条", make: (W, H) => ({ x: 0, y: 0, w: W, h: Math.round(H * 0.16) }) },
    { label: "铺满整幅", make: (W, H) => ({ x: 0, y: 0, w: W, h: H }) },
  ];

  const nat = { w: frame?.width ?? 0, h: frame?.height ?? 0 };

  return (
    <Dialog onClose={onClose} label="去字幕" maskClassName="canvas-dialog-mask"
            className="canvas-dialog subrm-dialog">
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">
          <Eraser size={15} /> 去字幕 · {asset.original_name}
        </span>
        <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
          <X size={14} />
        </button>
      </div>

      <div className="subrm-body">
        <div className="canvas-float-hint">
          在下面这一帧上拖出「字幕所在的那一条」，挑一种手法，先看效果再处理整段。
          <b>去字幕是盖掉像素，做不到无痕</b>：四种手法各自会留下什么，写在手法旁边。
        </div>

        {loading && !frame && (
          <div className="subrm-loading">
            <Spinner /> 正在取一帧…
          </div>
        )}

        {frame && box && (
          <>
            <RemovalBoxPicker
              src={frame.image}
              natural={nat}
              box={box}
              onChange={(b) => {
                putBox(b);
                setStale(true);
              }}
              onCommit={() => void refreshMethods()}
            />

            <div className="subrm-row">
              <label className="field">
                <span>看哪一帧</span>
                <div className="subrm-time">
                  <input
                    type="range"
                    min={0}
                    max={Math.max(0, frame.duration)}
                    step={0.1}
                    value={t}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      setT(v);
                      void loadFrame(v);
                    }}
                  />
                  <span className="muted">
                    {t.toFixed(1)}s / {frame.duration.toFixed(1)}s
                  </span>
                </div>
              </label>
              <div className="subrm-presets">
                {presets.map((p) => (
                  <button
                    key={p.label}
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => {
                      const nb = p.make(nat.w, nat.h);
                      putBox(nb);
                      setStale(true);
                      void refreshMethods(nb);
                    }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              <span className="muted subrm-geo">
                {nat.w}×{nat.h} · 框 {box.x},{box.y} {box.w}×{box.h}
              </span>
            </div>

            <div className="subrm-methods">
              {frame.methods.map((m) => (
                <label
                  key={m.key}
                  className={`subrm-method${m.key === method ? " on" : ""}${m.available ? "" : " off"}`}
                  title={m.available ? m.hint : m.reason}
                >
                  <input
                    type="radio"
                    name="subrm-method"
                    checked={m.key === method}
                    disabled={!m.available}
                    onChange={() => pickMethod(m.key)}
                  />
                  <span className="subrm-method-label">{m.label}</span>
                  <span className="subrm-method-short">{m.available ? m.short : `用不了：${m.reason}`}</span>
                </label>
              ))}
            </div>

            <div className="subrm-compare">
              <figure>
                <img src={frame.image} alt="原帧" />
                <figcaption>原帧（框住的就是要去掉的那一条）</figcaption>
              </figure>
              <figure>
                {preview ? (
                  <img src={preview.image} alt="处理后" />
                ) : (
                  <div className="subrm-placeholder">点「看效果」渲一帧</div>
                )}
                <figcaption>
                  {preview
                    ? `处理后（${preview.label}）${stale ? " · 框或帧改过了，这是旧预览" : ""}`
                    : "处理后"}
                </figcaption>
              </figure>
            </div>

            {preview && <div className="subrm-note">{preview.note}</div>}

            {problem && <div className="director-merge-warn">{problem}</div>}

            <div className="subrm-foot">
              <span className="muted">
                处理整段要重编码一次，时长按片子长度走（这一段 {asset.duration ?? "?"}s）。
              </span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={previewing || applying}
                onClick={() => void runPreview()}
              >
                {previewing ? <Spinner /> : <Eye size={14} />} 看效果
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={applying || previewing}
                onClick={() => void apply()}
              >
                {applying ? <Spinner /> : <Eraser size={14} />} 开始去字幕
              </button>
            </div>
          </>
        )}

        {problem && !frame && <div className="director-merge-warn">{problem}</div>}
      </div>
    </Dialog>
  );
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
  // 正在去字幕的那一段（弹窗里框选 + 选手法 + 看真帧效果）
  const [removing, setRemoving] = useState<Asset | null>(null);
  // 正在放大的那一段（弹窗里挑路线 / 模型 / 倍数 / 显卡）
  const [upscaling, setUpscaling] = useState<Asset | null>(null);
  // 正在补帧的那一段（弹窗里挑模型 / 目标帧率 / 显卡）
  const [interping, setInterping] = useState<Asset | null>(null);
  /**
   * 已派发、还没跑完的**本机重活**：放大与补帧。
   *
   * 两者是同一件事——逐帧过一遍的后台任务，产物要到跑完才出现。记下「哪个任务对应
   * 时间线上哪一段」，等它落地再就地替换。不记的话，用户点完时间线上什么都没变，
   * 只能自己去任务中心把产物捞回来手动补进时间线。
   *
   * 两类共用一套轮询而不是各写一份：除了提示语里的两个字，从「怎么盯」到「怎么换回
   * 时间线」完全一样，写两份迟早在某一份上漏掉一处修正。
   */
  const [localJobs, setLocalJobs] = useState<
    { taskId: number; clipId: number; what: "upscale" | "interp" }[]
  >([]);

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

  /**
   * 盯放大 / 补帧任务的进度，**跑完就地替换时间线上那一段**。
   *
   * 两者都是后台任务（逐帧过一遍，几分钟起），产物要到跑完才出现。不盯的话，
   * 用户点完时间线上什么都没变——他只能自己去任务中心把产物找回来、
   * 手动再截/再排一次，而「跑完会自动换掉」这件事没有任何人告诉他。
   * 轮询节奏与视频页保持一致（4 秒一次，只到有任务在飞的时候才转）。
   */
  const localJobKey = localJobs.map((j) => j.taskId).join(",");
  useEffect(() => {
    if (!localJobKey) return;
    let alive = true;
    const timer = setInterval(async () => {
      const checked = await Promise.all(
        localJobs.map(async (job) => ({
          job,
          // 查一次失败当作「还没结束」：一次网络抖动不该把这一段标成失败
          task: await api.getTask(job.taskId).catch(() => null),
        })),
      );
      if (!alive) return;
      const settled = checked.flatMap((x) =>
        x.task && !isRunning(x.task.status) ? [{ job: x.job, task: x.task }] : [],
      );
      if (settled.length === 0) return;
      setLocalJobs((prev) => prev.filter((j) => !settled.some((s) => s.job.taskId === j.taskId)));

      const done = settled.flatMap((s) =>
        isDone(s.task.status) && s.task.assets[0]
          ? [{ job: s.job, assetId: s.task.assets[0].id }]
          : [],
      );
      if (done.length > 0) {
        // 换上去的必须是**完整**的那条资产（时长、体积、宽高都要对），所以按 id 从视频
        // 素材清单里捞回来，而不是拿任务里的 AssetBrief 拼一个「差不多」的对象塞进时间线
        const fresh = await api.listDirectorVideos();
        if (!alive) return;
        setClips((prev) =>
          prev.map((c) => {
            const hit = done.find((d) => d.job.clipId === c.id);
            const made = hit ? fresh.find((v) => v.id === hit.assetId) : undefined;
            return made ?? c;
          }),
        );
        void loadVideos();
        // 提示语与去字幕那一条同一个口径：换掉的是这一段，原片还在资产库里。
        // 一次只盯着一类活时（绝大多数情况）把具体动作说出来，混着才退回笼统说法。
        const one = done.every((d) => d.job.what === done[0].job.what) ? done[0].job.what : "";
        toast.success(
          one === "interp"
            ? "已用补帧后的版本替换这一段（原片仍在资产库）"
            : one === "upscale"
              ? "已用放大后的版本替换这一段（原片仍在资产库）"
              : "已用处理后的版本替换这一段（原片仍在资产库）",
        );
      }
      if (settled.length > done.length) {
        const failed = settled.filter((s) => !done.some((d) => d.job.taskId === s.job.taskId));
        const one = failed.every((f) => f.job.what === failed[0].job.what) ? failed[0].job.what : "";
        toast.error(
          one === "interp"
            ? "有一段的补帧没成，去任务中心能看到原因"
            : one === "upscale"
              ? "有一段的放大没成，去任务中心能看到原因"
              : "有一段没成，去任务中心能看到原因",
        );
      }
    }, 4000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localJobKey]);

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
                        title="去字幕：框出画面里那一条字幕，用画面把它盖掉（原片不动，产物是新资产）"
                        onClick={() => setRemoving(c)}
                      >
                        <Eraser size={14} />
                      </button>
                      <button
                        className="icon-btn"
                        title="放大（超分）：把这一段放大成新资产，跑完自动换掉时间线上的这一段（原片不动）"
                        onClick={() => setUpscaling(c)}
                      >
                        <Maximize2 size={14} />
                      </button>
                      <button
                        className="icon-btn"
                        title="补帧：把这一段补顺（如 24 帧→60 帧）成新资产，跑完自动换掉时间线上的这一段（原片不动）"
                        onClick={() => setInterping(c)}
                      >
                        <Gauge size={14} />
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

      {/* 去字幕：在时间线上就地替换这一段；原片仍在资产库 */}
      {removing && (
        <RemoveSubtitlesDialog
          asset={removing}
          onClose={() => setRemoving(null)}
          onReplaced={(made) => {
            setClips((prev) => prev.map((c) => (c.id === removing.id ? made : c)));
            setRemoving(null);
          }}
        />
      )}

      {/* 放大：与去字幕同一个口径——产物是新资产、原片不动。
          只是视频这条路是后台跑的，所以派发时先把任务记下来，等它跑完再就地替换这一段 */}
      {upscaling && (
        <UpscaleDialog
          asset={upscaling}
          onClose={() => setUpscaling(null)}
          onQueued={(task) =>
            setLocalJobs((prev) => [...prev, { taskId: task.id, clipId: upscaling.id, what: "upscale" }])
          }
          onDone={(made) => {
            // 图片是同步出的，直接换掉这一段；视频这里拿不到东西（made 为 null），
            // 由上面那个轮询等它跑完再换
            if (made) setClips((prev) => prev.map((c) => (c.id === upscaling.id ? made : c)));
            setUpscaling(null);
          }}
          onGoEngines={() => onNavigate("engines")}
        />
      )}

      {/* 补帧：与放大同一个口径——产物是新资产、原片不动、跑完自动换掉这一段。
          它恒为后台任务，所以派发时先把任务记下来，等它跑完再就地替换 */}
      {interping && (
        <InterpDialog
          asset={interping}
          onClose={() => setInterping(null)}
          onQueued={(task) =>
            setLocalJobs((prev) => [...prev, { taskId: task.id, clipId: interping.id, what: "interp" }])
          }
          onDone={() => setInterping(null)}
          onGoEngines={() => onNavigate("engines")}
        />
      )}
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
