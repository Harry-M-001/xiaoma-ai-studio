import { useEffect, useState } from "react";
import { ArrowUpRight, Maximize2, X } from "lucide-react";
import { api } from "../api";
import type {
  Asset,
  Task,
  UpscaleModel,
  UpscaleOptions,
  UpscaleRequest,
  UpscaleRoute,
  UpscaleWorkflow,
} from "../types";
import { Dialog } from "./Dialog";
import { Spinner } from "./common";
import { useToast } from "./Toast";

/** 上限写成「兆像素」：33177600 这种数读完也不知道是多大，33.2 兆像素一眼就有概念 */
function megaPixels(v: number): string {
  return `${(v / 1e6).toFixed(1)} 兆像素`;
}

/**
 * 这条路线此刻真能不能选。
 *
 * 契约里 `available` 是后端按**这个素材**算好的总账，`videoOk` 是这条路支不支持视频。
 * 两个都要过：只看 `available`，视频上就可能选中一条只做图片的路线，
 * 而那要等提交被拒才知道。
 */
function usable(kind: string, r: UpscaleRoute): boolean {
  return r.available && (kind !== "video" || r.videoOk);
}

/** 视频上要滤掉不做视频的模型（模型自己的 `video` 才是准的那一份） */
function modelsOf(kind: string, r: UpscaleRoute | null): UpscaleModel[] {
  if (!r) return [];
  return kind === "video" ? r.models.filter((m) => m.video) : r.models;
}

/** 默认模型：后端点名的那个；认不出来（或视频上不能用）就退到第一个能用的 */
function defaultModelOf(kind: string, r: UpscaleRoute): UpscaleModel | null {
  const list = modelsOf(kind, r);
  return list.find((m) => m.key === r.defaultModel) ?? list[0] ?? null;
}

/**
 * 默认倍数：后端点名的那个；这个模型不支持它时退到该模型的第一档。
 * 换模型时也走这里——留着一个新模型没有的倍数，等于让用户提交一个必然被拒的组合。
 */
function defaultScaleOf(r: UpscaleRoute, m: UpscaleModel): number {
  return m.scales.includes(r.defaultScale) ? r.defaultScale : (m.scales[0] ?? 0);
}

/**
 * 「放大」（超分）弹窗：一条路线 + 一款模型 + 一个倍数 + 一块显卡，看清目标尺寸再提交。
 *
 * 四件事是它存在的理由：
 * 1. **能做什么由后端回答**（路线能不能用、有哪些模型与倍数、用哪块卡）。本机引擎装没装、
 *    卡能不能用只有后端知道，前端自己猜一次就是一次「界面说行、点下去报错」。
 * 2. **不能用的路线照样列出来**：灰掉、写清原因，能跳的给个入口。静默消失会让人以为
 *    这个软件不会放大——与去字幕四种手法同一个口径。
 * 3. **目标尺寸当场相乘显示**（宽×倍数）：8192×8192 这种结果必须在点之前看见，
 *    等跑完才发现太大就白等一场。
 * 4. **图片与视频是两条路**：图片秒级同步出资产；视频是后台任务（逐帧过一遍）。
 *    按钮文案跟着变，就是为了让人知道点下去之后该去哪儿找结果。
 *
 * 弹窗骨架、选择卡、底栏都复用去字幕那套 `subrm-*`/`canvas-dialog*` 类名：
 * 形状需求完全一样，另起一套只会让两边日后各自漂移。新写的只有素材信息行、
 * 路线上的跳转提示、推荐小标这三处没有现成等价物的样式。
 */
export function UpscaleDialog({
  asset,
  onClose,
  onDone,
  onQueued,
  onGoEngines,
}: {
  asset: Asset;
  onClose: () => void;
  /** 图片：新建的资产；视频：null（产物要等后台任务跑完，见 onQueued） */
  onDone: (made: Asset | null) => void;
  /** 视频派发成功后拿到任务。调用方要「跑完就地替换」就靠它盯进度 */
  onQueued?: (task: Task) => void;
  /** 路线报「去本机引擎页下载」时用它跳过去；不传就不显示跳转按钮 */
  onGoEngines?: () => void;
}) {
  const toast = useToast();
  const [opt, setOpt] = useState<UpscaleOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState("");
  const [routeKey, setRouteKey] = useState("");
  const [modelKey, setModelKey] = useState("");
  const [scale, setScale] = useState(0);
  /** 只有 route=comfyui 用：跑用户自己的哪一条放大工作流 */
  const [workflowId, setWorkflowId] = useState(0);
  /** -1 = 自动（后端自己挑一块） */
  const [gpu, setGpu] = useState(-1);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setProblem("");
    api
      .upscaleOptions(asset.id)
      .then((data) => {
        if (!alive) return;
        setOpt(data);
        // 默认值一律取后端给的那一份（defaultModel / defaultScale / deviceAuto）：
        // 「默认」只能有一个作者，两边各挑一次迟早会对不上。
        const first = data.routes.find((r) => usable(data.kind, r));
        if (first) {
          setRouteKey(first.key);
          const m = defaultModelOf(data.kind, first);
          setModelKey(m?.key ?? "");
          setScale(m ? defaultScaleOf(first, m) : 0);
          setWorkflowId(first.workflows[0]?.id ?? 0);
        }
        const auto = data.devices.find((d) => d.recommended && d.usable) ?? data.devices.find((d) => d.usable);
        setGpu(data.deviceAuto || !auto ? -1 : auto.id);
      })
      .catch((e) => {
        if (alive) setProblem(e instanceof Error ? e.message : "读取放大选项失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [asset.id]);

  const kind = opt?.kind ?? "image";
  const route = opt?.routes.find((r) => r.key === routeKey) ?? null;
  const models = modelsOf(kind, route);
  const model = models.find((m) => m.key === modelKey) ?? null;
  const scales = model?.scales ?? [];
  /** 走 ComfyUI 这条：没有「模型 / 倍数」可选，改由工作流自己决定 */
  const isComfy = route?.engine === "comfyui";
  const workflows = route?.workflows ?? [];
  const src = opt?.source;
  // ComfyUI 那条路的倍数只是给用户看的说明（真正放大几倍由工作流里的节点定），
  // 所以拿默认倍数算目标尺寸，不假装我们知道它最终会出多大
  const shownScale = isComfy ? (route?.defaultScale ?? 2) : scale;
  const targetW = Math.round((src?.width ?? 0) * shownScale);
  const targetH = Math.round((src?.height ?? 0) * shownScale);
  const overPixels = opt != null && targetW * targetH > opt.limits.maxOutputPixels;
  const overFrames =
    opt?.kind === "video" && opt.limits.maxFrames > 0 && (src?.frames ?? 0) > opt.limits.maxFrames;
  const noRoute = opt != null && !opt.routes.some((r) => usable(opt.kind, r));

  /**
   * 拦在前面的那一条原因；为空才允许提交。
   *
   * 后端也会拒（它会按此刻的参数再算一遍），这里先拦一道是为了**不让用户白等**：
   * 视频逐帧跑几分钟，越界的倍数提交上去要等很久才知道不行。文字与后端那句
   * reason 是同一件事的两种说法，不追求字字相同。
   */
  const blockReason = !opt
    ? ""
    : noRoute
      ? "这台机器上现在没有可用的放大路线：按每条路线写明的原因处理完再回来"
      : !route
        ? "先挑一条能用的路线"
        : isComfy
          ? workflows.length === 0
            ? "这条路线要有一个已经传上来的放大工作流才能跑"
            : workflowId > 0
              ? ""
              : "请选一个放大工作流"
          : !model || scale <= 0
            ? "先挑一条能用的路线与模型"
            : overPixels
              ? `${targetW}×${targetH} 超过一次最多能出的 ${megaPixels(opt.limits.maxOutputPixels)}，换个倍数或换个模型试试`
              : overFrames
                ? `这段有 ${src?.frames ?? 0} 帧，超过一次最多处理的 ${opt.limits.maxFrames} 帧，先截短一点再放大`
                : "";

  const pickRoute = (r: UpscaleRoute) => {
    if (!usable(kind, r)) return;
    setRouteKey(r.key);
    const m = defaultModelOf(kind, r);
    setModelKey(m?.key ?? "");
    setScale(m ? defaultScaleOf(r, m) : 0);
    // 换路线时工作流重置回第一条：留着上一条的 id 会提交一个不属于这条路线的工作流
    setWorkflowId(r.workflows[0]?.id ?? 0);
  };

  const pickModel = (m: UpscaleModel) => {
    setModelKey(m.key);
    // 换模型就回到「这个模型也认的」那一档：上一个模型的 4 倍它可能没有
    setScale((prev) =>
      m.scales.includes(prev) ? prev : route ? defaultScaleOf(route, m) : (m.scales[0] ?? 0),
    );
  };

  const submit = async () => {
    if (!opt || !route || busy || blockReason) return;
    if (!isComfy && (!model || scale <= 0)) return;
    const body: UpscaleRequest = {
      asset_id: asset.id,
      route: route.key,
      model: modelKey,
      // ComfyUI 那条不报倍数给后端（放大几倍由工作流里的节点决定），
      // 但接口要求一个值，就把界面上显示的那一档发过去，权当说明
      scale: isComfy ? shownScale : scale,
      gpu,
      // TTA 是图片那条路的质量开关（视频逐帧跑本来就慢，契约 §3 里视频忽略它）。
      // 交互规格里没有这个控件，所以固定送 false，不自己加一个说不清代价的开关。
      tta: false,
      ...(isComfy ? { workflow_id: workflowId, param_values: {} } : {}),
    };
    setBusy(true);
    setProblem("");
    try {
      if (opt.kind === "video") {
        const task = await api.upscaleVideo(body);
        toast.success("已派发放大任务，可在任务中心看进度");
        onQueued?.(task);
        onDone(null);
        // 视频要跑几分钟，留一个不知道该看哪儿的弹窗没意义，派发完就关
        onClose();
      } else if (isComfy) {
        // 走 ComfyUI 是**异步**的（要排队、要加载模型），所以与视频同一条出口：
        // 派发完就关窗、去任务中心看。不开一个「同步等 ComfyUI 出图」的口子。
        const task = await api.upscaleComfy(body);
        toast.success("已派发到 ComfyUI，可在任务中心看进度");
        onQueued?.(task);
        onDone(null);
        onClose();
      } else {
        const made = await api.upscaleImage(body);
        toast.success("已放大成新资产");
        // 关窗交给调用方：它拿到资产后还要把新资产挂进自己的列表/时间线
        onDone(made);
      }
    } catch (e) {
      // **不关窗**：409（已有一条超分在跑）与「这个倍数后端不认」都是改个数就能重试的，
      // 关掉等于让用户把刚才选的一整套重填一遍
      setProblem(e instanceof Error ? e.message : "放大失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      onClose={onClose}
      label="放大"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog subrm-dialog"
    >
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">
          <Maximize2 size={15} /> 放大 · {asset.original_name}
        </span>
        <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
          <X size={14} />
        </button>
      </div>

      <div className="subrm-body">
        {loading && !opt && (
          <div className="subrm-loading">
            <Spinner /> 正在看这台机器能怎么放大…
          </div>
        )}

        {opt && (
          <>
            <div className="upscale-src">
              <span className="upscale-src-name" title={opt.source.name}>
                {opt.source.name}
              </span>
              <span className="muted">
                原尺寸 {opt.source.width}×{opt.source.height}
                {opt.kind === "video"
                  ? ` · ${opt.source.frames} 帧 / ${Math.round(opt.source.duration)} 秒`
                  : ""}
              </span>
            </div>

            <div className="canvas-float-hint">
              挑一条路线、一款模型、一个倍数。放大是<b>把像素重跑一遍</b>：
              产物是<b>新资产</b>，原图 / 原片都还在，随时可以回头。
            </div>

            <div className="section-label">用哪条路线</div>
            <div className="subrm-methods">
              {opt.routes.map((r) => {
                const ok = usable(opt.kind, r);
                return (
                  <label
                    key={r.key}
                    className={`subrm-method${r.key === routeKey ? " on" : ""}${ok ? "" : " off"}`}
                    title={ok ? r.note || r.engineLabel : r.reason}
                  >
                    <input
                      type="radio"
                      name="upscale-route"
                      checked={r.key === routeKey}
                      disabled={!ok}
                      onChange={() => pickRoute(r)}
                    />
                    <span className="subrm-method-label">
                      {r.label}
                      {r.kindLabel ? `（${r.kindLabel}）` : ""}
                    </span>
                    <span className="subrm-method-short">
                      {ok ? r.engineLabel || r.note : `用不了：${r.reason}`}
                    </span>
                    {!ok && (r.action || r.actionRoute === "engines") && (
                      <span className="upscale-route-action">
                        {r.action}
                        {/* 跳转按钮：只有调用方给了导航能力、且后端说这条能跳时才出现 */}
                        {r.actionRoute === "engines" && onGoEngines && (
                          <button
                            type="button"
                            className="btn btn-ghost btn-xs"
                            onClick={(e) => {
                              // 这个按钮在一个 <label> 里：不拦一下会顺手把这条路线选上
                              e.preventDefault();
                              e.stopPropagation();
                              onClose();
                              onGoEngines();
                            }}
                          >
                            <ArrowUpRight size={12} /> 去本机引擎
                          </button>
                        )}
                      </span>
                    )}
                  </label>
                );
              })}
            </div>

            <div className="section-label">{isComfy ? "用哪条工作流" : "用哪个模型"}</div>
            {isComfy ? (
              workflows.length === 0 ? (
                <div className="muted">ComfyUI 里还没有能出图的工作流，先在画布上上传一份。</div>
              ) : (
                <div className="subrm-methods">
                  {workflows.map((w: UpscaleWorkflow) => (
                    <label
                      key={w.id}
                      className={`subrm-method${w.id === workflowId ? " on" : ""}`}
                      title={`${w.name}（${w.nodeCount} 个节点）`}
                    >
                      <input
                        type="radio"
                        name="upscale-workflow"
                        checked={w.id === workflowId}
                        onChange={() => setWorkflowId(w.id)}
                      />
                      <span className="subrm-method-label">{w.name}</span>
                      <span className="subrm-method-short">
                        {w.nodeCount} 个节点 · 出{w.outputKind === "image" ? "图" : w.outputKind}
                      </span>
                    </label>
                  ))}
                </div>
              )
            ) : models.length === 0 ? (
              <div className="muted">先选一条能用的路线，这里才会列出它的模型。</div>
            ) : (
              <div className="subrm-methods">
                {models.map((m) => (
                  <label
                    key={m.key}
                    className={`subrm-method${m.key === modelKey ? " on" : ""}`}
                    title={m.note}
                  >
                    <input
                      type="radio"
                      name="upscale-model"
                      checked={m.key === modelKey}
                      onChange={() => pickModel(m)}
                    />
                    <span className="subrm-method-label">{m.label}</span>
                    <span className="subrm-method-short">{m.note}</span>
                  </label>
                ))}
              </div>
            )}

            <div className="subrm-row">
              <label className="field">
                <span>放大几倍</span>
                {isComfy ? (
                  // 放大几倍是工作流内部那个节点的参数，我们不知道也不改它。
                  // 这里说清「由工作流决定」比给一个点了没用的按钮诚实。
                  <span className="muted">由工作流决定</span>
                ) : scales.length === 0 ? (
                  <span className="muted">这条路线没有可选的倍数</span>
                ) : (
                  <div className="segmented">
                    {scales.map((s) => (
                      <button
                        key={s}
                        type="button"
                        className={s === scale ? "active" : ""}
                        onClick={() => setScale(s)}
                      >
                        {s} 倍
                      </button>
                    ))}
                  </div>
                )}
              </label>
              {/* 目标尺寸在前端相乘：为它再发一次请求，就多了一个「界面显示的和后端算的不一样」的机会 */}
              <span className="muted subrm-geo">
                目标尺寸{" "}
                <b>
                  {isComfy ? "约 " : "→ "}
                   {targetW}×{targetH}
                </b>
                {isComfy ? "（按工作流实际参数为准）" : ""}
              </span>
            </div>

            {/* 走 ComfyUI 时这一栏没有意义：用哪块卡是它自己决定的，我们连它的进程都碰不到 */}
            {!isComfy && (
              <>
                <div className="section-label">用哪块显卡</div>
                <div className="subrm-methods">
              {opt.deviceAuto && (
                <label
                  className={`subrm-method${gpu === -1 ? " on" : ""}`}
                  title={opt.deviceNote || "让后端自己挑一块能用的"}
                >
                  <input
                    type="radio"
                    name="upscale-gpu"
                    checked={gpu === -1}
                    onChange={() => setGpu(-1)}
                  />
                  <span className="subrm-method-label">自动</span>
                  <span className="subrm-method-short">{opt.deviceNote || "自己挑一块能用的"}</span>
                </label>
              )}
              {opt.devices.map((d) => (
                <label
                  key={d.id}
                  className={`subrm-method${d.id === gpu ? " on" : ""}${d.usable ? "" : " off"}`}
                  title={d.note}
                >
                  <input
                    type="radio"
                    name="upscale-gpu"
                    checked={d.id === gpu}
                    disabled={!d.usable}
                    onChange={() => setGpu(d.id)}
                  />
                  <span className="subrm-method-label">
                    {d.name || `设备 ${d.id}`}
                    {d.recommended && <span className="eng-chip tiny upscale-mark">推荐</span>}
                  </span>
                  <span className="subrm-method-short">
                    {d.usable ? d.note : d.note || "这块卡现在用不了"}
                  </span>
                </label>
              ))}
                </div>
              </>
            )}

            {blockReason && <div className="director-merge-warn">{blockReason}</div>}
            {problem && <div className="director-merge-warn">{problem}</div>}

            {opt.notes.length > 0 && (
              <div className="upscale-notes">
                {/* 后端给的是纯文本：这里逐条原样显示，不做任何 markdown 渲染 */}
                {opt.notes.map((n, i) => (
                  <div key={i} className="subrm-note">
                    {n}
                  </div>
                ))}
              </div>
            )}

            <div className="subrm-foot">
              <span className="muted">
                {opt.kind === "video"
                  ? "逐帧过一遍，这一段要跑一会儿；派发后去任务中心看进度。"
                  : isComfy
                    ? "交给你的 ComfyUI 跑，要排队与加载模型；派发后去任务中心看结果。"
                    : "图片是秒级出结果，产物直接进资产库。"}
              </span>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busy || Boolean(blockReason)}
                onClick={() => void submit()}
              >
                {busy ? <Spinner /> : <Maximize2 size={14} />}
                {opt.kind === "video" || isComfy ? "开始放大（后台跑）" : "开始放大"}
              </button>
            </div>
          </>
        )}

        {problem && !opt && <div className="director-merge-warn">{problem}</div>}
      </div>
    </Dialog>
  );
}
