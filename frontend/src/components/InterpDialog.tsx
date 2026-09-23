import { useEffect, useState } from "react";
import { ArrowUpRight, Gauge, X } from "lucide-react";
import { api } from "../api";
import type { Asset, InterpolateModel, InterpolateOptions, InterpolateRequest, Task } from "../types";
import { Dialog } from "./Dialog";
import { Spinner } from "./common";
import { useToast } from "./Toast";

/**
 * 帧率的显示写法。
 *
 * 后端给的是浮点（`23.976` 这种在视频里很常见），直接塞进模板会得到
 * 「23.976000000000003 帧」这一串；但也不能一律取整——24 帧的片子被写成 24 帧没问题，
 * 23.976 的片子被写成「24 帧」就是撒谎，而用户正是拿着这个数在算倍率。
 * 所以整数照原样、非整数保留两位。
 */
function fpsText(fps: number): string {
  return Number.isInteger(fps) ? String(fps) : fps.toFixed(2);
}

/**
 * 补帧：把 24 帧的片子补顺到 60 帧。
 *
 * 三件事是它存在的理由：
 * 1. **能补到多少帧由后端回答，而且是跟着模型走的**。只有 v4 系能自定义帧数，
 *    其余模型给 `-n` 会被上游直接拒绝——所以目标帧率只列所选模型的 `targets`，
 *    选到只做 2 倍的模型时把 `note2xOnly` 写出来，而不是让用户提交一个必然被拒的组合。
 * 2. **换算结果当场显示**（「24 帧 → 60 帧（补 2.50 倍）」）。用户脑子里想的是
 *    「想变顺一点」，而接口要的是目标帧率，这两个数之间的那步乘法得有人替他指出——
 *    倍数留在前端算，不为它再发一次请求，也避免「界面显示的和后端算的不一样」。
 * 3. **产物大小先说清**（预计多少帧）。补帧是逐帧过一遍，跑几分钟，等跑完才发现
 *    帧数爆了（超上限会被后端拒）就白等一场。
 *
 * 弹窗骨架、模型选择卡、底栏都复用去字幕/放大那套 `subrm-*` 类名：
 * 形状需求完全一样，另起一套只会让两边日后各自漂移。新写的只有素材信息行复用的
 * `upscale-*`（同一个形状）与「换算一句」这一处没有现成等价物的样式。
 */
export function InterpDialog({
  asset,
  onClose,
  onDone,
  onQueued,
  onGoEngines,
}: {
  asset: Asset;
  /** 补帧是后台任务，产物要等跑完才出现，所以恒为 null（留着与放大那边同签名） */
  onDone: (made: Asset | null) => void;
  onClose: () => void;
  /** 派发成功后拿到任务。调用方要「跑完就地替换这一段」就靠它盯进度 */
  onQueued?: (task: Task) => void;
  /** 报「去本机引擎页下载」时用它跳过去；不传就不显示跳转按钮 */
  onGoEngines?: () => void;
}) {
  const toast = useToast();
  const [opt, setOpt] = useState<InterpolateOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState("");
  const [modelKey, setModelKey] = useState("");
  const [target, setTarget] = useState(0);
  /** -1 = 自动（后端自己挑一块） */
  const [gpu, setGpu] = useState(-1);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setProblem("");
    api
      .interpolateOptions(asset.id)
      .then((data) => {
        if (!alive) return;
        setOpt(data);
        // 默认值一律取后端给的那一份（defaultModel / defaultTarget / deviceAuto）：
        // 「默认」只能有一个作者，两边各挑一次迟早会对不上。
        const m = data.models.find((x) => x.key === data.defaultModel) ?? data.models[0] ?? null;
        setModelKey(m?.key ?? "");
        setTarget(m ? pickTarget(data, m) : 0);
        const auto = data.devices.find((d) => d.recommended && d.usable) ?? data.devices.find((d) => d.usable);
        setGpu(data.deviceAuto || !auto ? -1 : auto.id);
      })
      .catch((e) => {
        if (alive) setProblem(e instanceof Error ? e.message : "读取补帧选项失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [asset.id]);

  const src = opt?.source;
  const model = opt?.models.find((m) => m.key === modelKey) ?? null;
  // 目标帧率**只列所选模型的**：后端按这个模型算出来的那一列就是全部可选项
  const targets = model?.targets ?? [];
  const outFrames = Math.round((src?.duration ?? 0) * target);
  const factor = src && src.fps > 0 && target > 0 ? target / src.fps : 0;

  /**
   * 拦在前面的那一条原因；为空才允许提交。
   *
   * 后端也会拒（它会按此刻的参数再算一遍），这里先拦一道是为了**不让用户白等**：
   * 补帧是逐帧过一遍，越界的组合提交上去要等几分钟才知道不行。判据与后端
   * `services/interpolate.target_problem` 一一对应，数字全部取自 `limits`。
   */
  const blockReason = !opt
    ? ""
    : !opt.available
      ? "这台机器上现在补不了帧：按上面写明的原因处理完再回来"
      : !model || target <= 0
        ? "先挑一款能用的模型"
        : src && target <= src.fps
          ? `目标帧率要高于原来的 ${fpsText(src.fps)} 帧——补帧是把密度提高，降帧请用别的方式`
          : !model.custom && src && target !== Math.round(src.fps * 2)
            ? `「${model.label}」只做 2 倍，选它就只能补到 ${Math.round((src.fps || 0) * 2)} 帧`
            : factor > opt.limits.maxFactor
              ? `${target} 帧是原来的 ${factor.toFixed(1)} 倍，超过一次能补的 ${opt.limits.maxFactor} 倍`
              : outFrames > opt.limits.maxOutFrames
                ? `补完是 ${outFrames} 帧，超过一次能处理的 ${opt.limits.maxOutFrames} 帧，先剪短一点或者把目标帧率降一档`
                : "";

  const pickModel = (m: InterpolateModel) => {
    setModelKey(m.key);
    setTarget(opt ? pickTarget(opt, m) : (m.targets[0] ?? 0));
  };

  const submit = async () => {
    if (!opt || !model || busy || blockReason) return;
    const body: InterpolateRequest = {
      asset_id: asset.id,
      model: model.key,
      target,
      gpu,
    };
    setBusy(true);
    setProblem("");
    try {
      const task = await api.interpolateVideo(body);
      toast.success("已派发补帧任务，可在任务中心看进度");
      onQueued?.(task);
      onDone(null);
      // 逐帧过一遍要跑几分钟，留一个不知道该看哪儿的弹窗没意义，派发完就关
      onClose();
    } catch (e) {
      // **不关窗**：409（本机已经有一条重活在跑）与「这个目标帧率后端不认」
      // 都是改一下就能重试的，关掉等于让用户把刚选的一整套重填一遍。
      // toast 一闪就没了，所以同一个原因也留在弹窗里（与放大那边一致）。
      toast.error(e instanceof Error ? e.message : "补帧失败");
      setProblem(e instanceof Error ? e.message : "补帧失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      onClose={onClose}
      label="补帧"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog subrm-dialog"
    >
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">
          <Gauge size={15} /> 补帧 · {asset.original_name}
        </span>
        <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
          <X size={14} />
        </button>
      </div>

      <div className="subrm-body">
        {loading && !opt && (
          <div className="subrm-loading">
            <Spinner /> 正在看这段视频能补到多少帧…
          </div>
        )}

        {opt && (
          <>
            <div className="upscale-src">
              <span className="upscale-src-name" title={opt.source.name}>
                {opt.source.name}
              </span>
              <span className="muted">
                原尺寸 {opt.source.width}×{opt.source.height} · 原帧率{" "}
                {fpsText(opt.source.fps)} 帧 · 原帧数 {opt.source.frames} 帧 ·{" "}
                {opt.source.duration.toFixed(1)} 秒
              </span>
            </div>

            <div className="canvas-float-hint">
              补帧只<b>提高帧的密度</b>：画幅不变、时长不变、声音也不是重新生成的。
              产物是<b>新资产</b>，原片还在，随时可以回头。
            </div>

            {/* 补不了就把原因与下一步摆出来，而不是给一个点不动的按钮 */}
            {!opt.available && (
              <div className="director-merge-warn">
                {opt.reason || "这台机器上现在补不了帧"}
                {opt.action && (
                  <span className="upscale-route-action">
                    {opt.action}
                    {/* 只有调用方给了导航能力、且后端说能跳时才出现 */}
                    {opt.actionRoute === "engines" && onGoEngines && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-xs"
                        onClick={() => {
                          onClose();
                          onGoEngines();
                        }}
                      >
                        <ArrowUpRight size={12} /> 去本机引擎
                      </button>
                    )}
                  </span>
                )}
              </div>
            )}

            <div className="section-label">用哪个模型</div>
            {opt.models.length === 0 ? (
              <div className="muted">
                这台机器上还没有能用的补帧模型——{opt.engineLabel}装好之后这里才会有选项。
              </div>
            ) : (
              <div className="subrm-methods">
                {opt.models.map((m) => (
                  <label key={m.key} className={`subrm-method${m.key === modelKey ? " on" : ""}`} title={m.note}>
                    <input
                      type="radio"
                      name="interp-model"
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
                <span>补到多少帧</span>
                {targets.length === 0 ? (
                  <span className="muted">这款模型没有可选的目标帧率</span>
                ) : (
                  <div className="segmented">
                    {targets.map((t) => (
                      <button
                        key={t}
                        type="button"
                        className={t === target ? "active" : ""}
                        onClick={() => setTarget(t)}
                      >
                        {t} 帧
                      </button>
                    ))}
                  </div>
                )}
              </label>
              {/* 帧数在前端相乘：为它再发一次请求，就多了一个「界面显示的和后端算的不一样」的机会 */}
              <span className="muted subrm-geo">
                产物大概 <b>预计 {outFrames} 帧</b>
              </span>
            </div>

            {/* 倍数由前端算：接口要的是目标帧率，而用户想的是「变顺多少」，这一步得替他翻出来 */}
            {target > 0 && src && src.fps > 0 && (
              <div className="interp-calc">
                <b>
                  {fpsText(src.fps)} 帧 → {target} 帧
                </b>
                （补 {factor.toFixed(2)} 倍）
              </div>
            )}

            {/* 只做 2 倍的模型：选项为什么只有一个，必须写出来，否则像是界面坏了 */}
            {model && !model.custom && model.note2xOnly && (
              <div className="interp-2x">{model.note2xOnly}</div>
            )}

            <div className="section-label">用哪块显卡</div>
            <div className="subrm-methods">
              {opt.deviceAuto && (
                <label
                  className={`subrm-method${gpu === -1 ? " on" : ""}`}
                  title={opt.deviceNote || "让后端自己挑一块能用的"}
                >
                  <input type="radio" name="interp-gpu" checked={gpu === -1} onChange={() => setGpu(-1)} />
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
                    name="interp-gpu"
                    checked={d.id === gpu}
                    disabled={!d.usable}
                    onChange={() => setGpu(d.id)}
                  />
                  <span className="subrm-method-label">
                    {d.name || `设备 ${d.id}`}
                    {d.recommended && <span className="eng-chip tiny upscale-mark">推荐</span>}
                  </span>
                  <span className="subrm-method-short">{d.usable ? d.note : d.note || "这块卡现在用不了"}</span>
                </label>
              ))}
            </div>

            {/* 补不了那一条上面已经写着原因与下一步了，这里再说一遍是把同一句话讲两次。
                按钮依旧按 blockReason 禁用，只是不重复这句话 */}
            {blockReason && opt.available && <div className="director-merge-warn">{blockReason}</div>}
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
              <span className="muted">逐帧过一遍，这一段要跑一会儿；派发后去任务中心看进度。</span>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busy || Boolean(blockReason)}
                onClick={() => void submit()}
              >
                {busy ? <Spinner /> : <Gauge size={14} />}
                开始补帧（后台跑）
              </button>
            </div>
          </>
        )}

        {problem && !opt && <div className="director-merge-warn">{problem}</div>}
      </div>
    </Dialog>
  );
}

/**
 * 换模型时的目标帧率。
 *
 * 换模型必须重设目标：留着一个新模型 `targets` 里没有的值，等于让用户提交一个
 * 必然被拒的组合（后端 `target_problem` 会直接挡回来）。优先沿用后端点名的默认值，
 * 不在列表里就退到该模型的第一档——与放大那边「倍数跟着模型走」是同一条。
 */
function pickTarget(opt: InterpolateOptions, m: InterpolateModel): number {
  return m.targets.includes(opt.defaultTarget) ? opt.defaultTarget : (m.targets[0] ?? 0);
}
