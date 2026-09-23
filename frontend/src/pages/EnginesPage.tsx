import { useCallback, useEffect, useRef, useState } from "react";
import {
  Cpu,
  Download,
  ExternalLink,
  Mic,
  Play,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api";
import type { EngineItem, EngineLocalTts, EnginePageData } from "../types";
import { Empty, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

/**
 * 本机引擎页（批次 9 的地基）。
 *
 * 这一页要回答的不是「有什么功能」，而是**「这台机器上该不该装哪一个」**：
 *
 * 1. **清单与体检都是后端算的**。体积、sha256、授权、下载地址来自后端的清单
 *    （那是校验用户下载的唯一依据）；「这台机器跑不跑得动」也由后端按硬件分档给结论。
 *    前端只负责画，不抄任何一份表——抄一份就会出现「界面能点、后端不认」。
 * 2. **能装不能装，点之前就说清**。跑不了的（比如没有 Vulkan 却要装 ncnn-vulkan 那一档）
 *    按钮直接灰掉并写出原因；而不是等他下完几百 MB 才报错。
 * 3. **下载是长活儿**：几百 MB，所以要能看见进度、能停、能删（磁盘是用户自己的）。
 *    停在断点上的部分会留着，下次接着下。
 */
export default function EnginesPage({ onNavigate }: { onNavigate?: (route: string) => void }) {
  const toast = useToast();
  const [data, setData] = useState<EnginePageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  // 轮询的定时器：只有真的有任务在跑时才开着（不然这一页会一直打接口）
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.listEngines());
    } catch (e) {
      toast.error(`读本机引擎状态失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  // 有任务在跑就 1.5 秒问一次；跑完自动停
  useEffect(() => {
    const active = data?.engines.some((e) => e.downloading);
    if (timer.current) window.clearTimeout(timer.current);
    if (active) {
      timer.current = window.setTimeout(() => void load(), 1500);
    }
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [data, load]);

  const act = async (key: string, fn: () => Promise<unknown>, okMsg: string) => {
    setBusyKey(key);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
      await load();
    } catch (e) {
      // 后端的拒绝理由都是给人看的（「这台机器跑不了」「要先装运行时」），原样展示
      toast.error((e as Error).message);
    } finally {
      setBusyKey("");
    }
  };

  if (loading && !data) {
    return (
      <div className="page eng-page">
        {/* 读的时候先占上「这台机器」那张卡的形状，读到了内容就不跳一下 */}
        <div className="card eng-machine">
          <Spinner />
          <div className="muted">正在读本机引擎状态…</div>
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="page eng-page">
        <div className="card eng-machine">
          <Empty icon={<Cpu />} title="读不到本机引擎状态" desc="刷新一下试试" />
        </div>
      </div>
    );
  }

  const hw = data.hardware;
  return (
    <div className="page eng-page">
      <div className="page-header">
        <div>
          <div className="page-title">本机引擎</div>
          <div className="page-desc">
            下载到本机之后，这些能力就不再走 API、也就没有按次计费；
            模型不进仓库也不进便携包，装在你自己的数据目录里（清单合计 {data.totalSizeText}）。
          </div>
        </div>
        <button
          className="btn btn-ghost"
          disabled={refreshing}
          onClick={() => {
            setRefreshing(true);
            void act("", () => api.refreshEngineHardware(), "已重新检测这台机器");
            setRefreshing(false);
          }}
        >
          <RefreshCw size={14} /> 重新检测
        </button>
      </div>

      <div className="card eng-machine">
        <div className="eng-hw">
          <span className={"eng-chip" + (hw.hasGpu ? " ok" : "")}>显卡：{hw.gpuText}</span>
          <span className={"eng-chip" + (hw.vulkan ? " ok" : " warn")}>
            Vulkan：{hw.vulkan ? hw.vulkanDevice || "可用" : "没检测到"}
          </span>
          <span className="eng-chip">CPU：{hw.cores || "?"} 线程</span>
          <span className="eng-chip">内存：{hw.ramGb ? `${hw.ramGb} GB` : "?"}</span>
          <span className="eng-chip">已装 {data.installedCount} / {data.engines.length}</span>
        </div>
        <div className="eng-headline">{data.headline}</div>
      </div>

      <div className="card eng-services">
        <div className="eng-section">你已经有的本机能力</div>
        {data.installedServices.map((s) => (
          <div className="eng-service" key={s.key}>
            <span className={"eng-dot" + (s.running ? " on" : "")} />
            <b>{s.label}</b>
            <span className="eng-service-detail">{s.detail}</span>
          </div>
        ))}
        <div className="eng-note">
          已经有 ComfyUI 的话，超分与补帧可以先走它，不必再下下面那几档——
          同一件事能用你已经装好的工具做完，就别再装第二个。
        </div>
      </div>

      <LocalTtsCard
        info={data.localTts}
        busy={busyKey === "local-tts"}
        onConnect={() =>
          void act("local-tts", () => api.connectLocalTts(), "已接入：去配音页选「本机跑」试试")
        }
        onDisconnect={() =>
          void act("local-tts", () => api.disconnectLocalTts(), "已停用本机配音模型")
        }
        onNavigate={onNavigate}
      />

      <div className="eng-list">
        {data.engines.map((e) => (
          <EngineCard
            key={e.key}
            item={e}
            phaseLabels={data.phaseLabels}
            busy={busyKey === e.key}
            onDownload={() => void act(e.key, () => api.downloadEngine(e.key), `已开始下载「${e.label}」`)}
            onCancel={() => void act(e.key, () => api.cancelEngine(e.key), `已停在断点上：${e.label}`)}
            onRemove={() =>
              void act(e.key, async () => {
                const r = await api.removeEngine(e.key);
                toast.info(`已删掉「${e.label}」，收回 ${r.freedText}`);
              }, "")
            }
            onVerify={() =>
              void act(e.key, async () => {
                const r = await api.verifyEngine(e.key);
                if (r.ok) toast.success(`${e.label}：${r.detail}`);
                else toast.error(`${e.label}：${r.detail}`);
              }, "")
            }
          />
        ))}
      </div>
    </div>
  );
}

/**
 * 本机配音那张卡：装好了要「接一下」才能用上。
 *
 * 三件事分开说，因为用户能做的动作不同：**没装齐**说还差哪个（就在下面那一列）、
 * **装齐没接**给一个按钮、**接好了**给入口去试 + 完整音色清单。
 * 「接入」这一步不能省：引擎躺在磁盘上不会让配音页多出一个选项。
 */
function LocalTtsCard({
  info,
  busy,
  onConnect,
  onDisconnect,
  onNavigate,
}: {
  info: EngineLocalTts;
  busy: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  onNavigate?: (route: string) => void;
}) {
  return (
    <div className={"card eng-card eng-tts" + (info.connected && info.ready ? " on" : "")}>
      <div className="eng-row">
        <div className="eng-row-main">
          <div className="eng-name">
            <b>
              <Mic size={15} /> 本机配音
            </b>
            <span className="eng-chip tiny">sherpa-onnx + Kokoro 82M</span>
            {info.ready ? (
              <span className="eng-chip tiny ok">引擎已就绪</span>
            ) : (
              <span className="eng-chip tiny warn">还不能用</span>
            )}
            {info.connected && <span className="eng-chip tiny ok">已接入</span>}
            {info.disabled && <span className="eng-chip tiny">已停用</span>}
          </div>
          <div className="eng-why">
            一段话在本机念出来，不联网、不花调用费；中文英文都能念，自带
            {info.voiceCount || "若干"}个音色。接上之后配音页、样片旁白、画布逐镜对白
            都能选它。
          </div>
          {!info.ready && (
            <div className="eng-note eng-note-warn">
              {info.problem || "还差引擎没装好"}——就在下面那一列里，装完再回来接。
            </div>
          )}
          {info.ready && !info.connected && (
            <div className="eng-note">
              引擎已经装好了，点右边「接成本机配音模型」，配音页就会多出一个「本机跑」的模型。
            </div>
          )}
          {info.connected && (
            <div className="eng-note eng-note-ok">
              已接入为「{info.serviceName}」，在「模型服务」页也能看到它。
              整镜一个人的台词只合成一次，一镜多人的台词按句换音色。
            </div>
          )}
          {info.ready && info.voices.length > 0 && (
            <details className="eng-voices">
              <summary>看看全部 {info.voiceCount} 个音色</summary>
              <div className="eng-voice-list">
                {info.voices.map((v) => (
                  <span className="eng-chip tiny" key={v.sid} title={`填「${v.id}」就能用`}>
                    {v.label}
                  </span>
                ))}
              </div>
              <div className="eng-note">
                {info.named
                  ? "配音页的「音色」一栏填这里的名字就能指定；分镜表的角色音色表也写名字，"
                    + "例如「小焰=zf_xiaoxiao」。留空用第 0 号。"
                  : `配音页的「音色」一栏填这里的号码就能指定；分镜表的角色音色表也写号码，`
                    + `例如「小焰=47」。留空用第 0 号。`}
              </div>
            </details>
          )}
        </div>
        <div className="eng-actions">
          {info.connected ? (
            <>
              <button className="btn btn-primary" onClick={() => onNavigate?.("speech")}>
                <Play size={14} /> 去配音页试试
              </button>
              <button className="btn btn-ghost" disabled={busy} onClick={onDisconnect}>
                停用
              </button>
            </>
          ) : (
            <button className="btn btn-primary" disabled={busy || !info.ready} onClick={onConnect}>
              <Mic size={14} /> 接成本机配音模型
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** 进度：下载中显示百分比与已下体积，其余阶段显示阶段名 */
function progressText(item: EngineItem, phaseLabels: Record<string, string>): string {
  const job = item.job;
  if (!job) return "";
  const label = phaseLabels[job.phase] ?? job.phase;
  if (job.phase === "downloading" && job.total > 0) {
    const pct = Math.min(100, Math.round((job.done / job.total) * 100));
    return `${label} ${pct}%（${mb(job.done)} / ${mb(job.total)}）`;
  }
  return label;
}

function mb(bytes: number): string {
  if (!bytes) return "0 MB";
  const v = bytes / 1048576;
  return v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${v.toFixed(1)} MB`;
}

function EngineCard({
  item,
  phaseLabels,
  busy,
  onDownload,
  onCancel,
  onRemove,
  onVerify,
}: {
  item: EngineItem;
  phaseLabels: Record<string, string>;
  busy: boolean;
  onDownload: () => void;
  onCancel: () => void;
  onRemove: () => void;
  onVerify: () => void;
}) {
  const blocked = item.level === "no" || item.missingNeeds.length > 0;
  const job = item.job;
  const pct =
    job && job.phase === "downloading" && job.total > 0
      ? Math.min(100, Math.round((job.done / job.total) * 100))
      : job
        ? 100
        : 0;
  const partial = !item.installed && item.archiveBytes > 0 && !item.archiveComplete;
  // 按钮上写清这一步会做什么：安装程序那一档只把安装包下下来，装与跑都不归我们
  const downloadLabel = partial
    ? "接着下载"
    : item.archive === "installer"
      ? item.archiveComplete
        ? "重新下载安装包"
        : "下载安装包"
      : "下载并安装";

  return (
    <div className={"card eng-card" + (item.installed ? " on" : "")}>
      <div className="eng-row">
        <div className="eng-row-main">
          <div className="eng-name">
            <b>{item.label}</b>
            <span className="eng-chip tiny">{item.kindLabel}</span>
            <span className="eng-chip tiny">{item.sizeText}</span>
            <span className={"eng-chip tiny " + (item.level === "ok" ? "ok" : item.level === "no" ? "warn" : "")}>
              {item.levelLabel}
            </span>
            {item.installed && <span className="eng-chip tiny ok">已装好</span>}
          </div>
          <div className="eng-why">{item.why}</div>
          <div className="eng-note">{item.reason}</div>
          {/* 代价与限制必须露在外面：藏起来的表现是用户装完才发现跑不动 */}
          <div className="eng-note eng-note-dim">{item.note}</div>
          {/* 安装程序那一档（就是去字幕高质量档）：我们只负责把它下下来，装与跑都在它
              自己的界面里——所以下完之后必须把「文件在哪」写出来，不然用户下完 731MB
              会找不到它（这一条是 #44 走查时发现少了的）。 */}
          {item.archive === "installer" && item.archiveComplete && (
            <div className="eng-note eng-note-ok">
              安装包已下好：<code className="eng-path">{item.archivePath}</code>
              ——双击装完，用它自己的界面处理，成品再拖回导演台接着剪。
            </div>
          )}
          <div className="eng-meta">
            <span>授权：{item.license}</span>
            <span>包：{item.archiveLabel}</span>
            <span className="eng-proof">
              {item.proof === "local-download" ? "sha256：本地下载核对" : "sha256：上游摘要"}
            </span>
            <a href={item.homepage} target="_blank" rel="noreferrer">
              项目主页 <ExternalLink size={11} />
            </a>
          </div>
          {item.missingNeeds.length > 0 && (
            <div className="eng-note eng-note-warn">要先装：{item.needsLabel}</div>
          )}
        </div>

        <div className="eng-actions">
          {item.downloading ? (
            <>
              <button className="btn btn-ghost" disabled={busy} onClick={onCancel}>
                <X size={14} /> 停止
              </button>
              <span className="eng-status">{progressText(item, phaseLabels)}</span>
            </>
          ) : item.installed ? (
            <>
              <button className="btn btn-ghost" disabled={busy} onClick={onRemove}>
                <Trash2 size={14} /> 删除
              </button>
              <span className="eng-status eng-ok">已装好：{item.installedMarker}</span>
            </>
          ) : (
            <>
              <button className="btn btn-primary" disabled={busy || blocked} onClick={onDownload}>
                <Download size={14} /> {downloadLabel}
              </button>
              {item.archiveComplete && (
                <button className="btn btn-ghost" disabled={busy} onClick={onVerify}>
                  <ShieldCheck size={14} /> 校验
                </button>
              )}
              {partial && <span className="eng-status">已下 {mb(item.archiveBytes)}，可接着下</span>}
            </>
          )}
        </div>
      </div>

      {item.downloading && (
        <div className="eng-bar">
          <div className="eng-bar-fill" style={{ width: `${pct}%` }} />
        </div>
      )}
      {item.job?.error && <div className="eng-note eng-note-warn">上次失败：{item.job.error}</div>}
      {item.job?.phase === "done" && item.job.note && (
        <div className="eng-note eng-note-ok">{item.job.note}</div>
      )}
    </div>
  );
}
