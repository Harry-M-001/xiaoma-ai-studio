import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, ArrowRight, PlugZap, X } from "lucide-react";
import { api } from "../api";
import { PROVIDERS_CHANGED } from "../providerEvents";
import { prefs } from "../prefs";
import type { SetupStatus } from "../types";
import { useToast } from "./Toast";

/**
 * 未配置模型时的引导横幅。
 *
 * 开源项目最大的流失点在「下载之后到跑通之前」：界面能打开、按钮都在，
 * 但第一次生图会失败——因为没有任何模型服务。这条横幅就是为了让那一步之前
 * 就把话说清楚，并且**给出两条能立刻走通的路**：粘贴一个 Key，或者接入本机 Ollama。
 *
 * 关闭状态用「当前缺什么」当签名存下来：关掉「缺视频模型」的提示之后，
 * 将来万一一个服务都不剩了，新的提示仍然会露出来——不要因为一次「知道了」
 * 就把真正的阻断问题一起永久静音。
 */

// 关闭状态由 prefs 统一管理（键名与合法化都在那里）
const MODALITY_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  video: "视频",
};

function signature(status: SetupStatus): string {
  if (status.services === 0) return "none";
  if (!status.ready) return "no-models";
  return `missing:${[...status.missing].sort().join(",")}`;
}

export default function SetupBanner({
  route,
  onNavigate,
}: {
  /** 当前路由：切页时重新问一次状态（刚配完模型回来，提示就该消失） */
  route: string;
  onNavigate: (r: string) => void;
}) {
  const toast = useToast();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [dismissed, setDismissed] = useState<string>(() => prefs.setupDismissed.get());
  const [connecting, setConnecting] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.setupStatus());
    } catch {
      // 后端没起来 / 接口挂了都不该在界面上再叠一层错误，安静地不显示
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, route]);

  // 「模型服务」页改了服务列表就地刷新：不然刚接入完模型，
  // 这条横幅还挂着「还没有接入任何模型」，和下面那页自相矛盾。
  useEffect(() => {
    const onChange = () => load();
    window.addEventListener(PROVIDERS_CHANGED, onChange);
    return () => window.removeEventListener(PROVIDERS_CHANGED, onChange);
  }, [load]);

  if (!status) return null;
  // 模型齐了就没什么可提示的
  if (status.ready && status.missing.length === 0) return null;
  const sig = signature(status);
  if (dismissed === sig) return null;

  const close = () => {
    prefs.setupDismissed.set(sig);
    setDismissed(sig);
  };

  const connectOllama = async () => {
    setConnecting(true);
    try {
      const r = await api.ollamaConnect();
      toast.success(`已接入本机 Ollama（${r.models.length} 个模型）`);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "接入失败");
    } finally {
      setConnecting(false);
    }
  };

  const strong = status.services === 0;
  const missingLabels = status.missing.map((m) => MODALITY_LABEL[m] ?? m).join("、");

  return (
    <div className={`setup-banner${strong ? " setup-banner--strong" : ""}`}>
      <AlertTriangle size={16} className="setup-banner-icon" />
      <div className="setup-banner-body">
        <div className="setup-banner-title">
          {strong
            ? "还没有接入任何模型"
            : status.ready
              ? `还没有可用的${missingLabels}模型`
              : "已接入的服务里还没有填模型"}
        </div>
        <div className="setup-banner-desc">
          {strong ? (
            <>
              对话、生图、生视频都依赖模型服务。最快的方式是<b>粘贴一个 API Key</b>
              （自动认出归属并测通），或者接入本机已经装好的 Ollama——那条路不需要任何账号。
            </>
          ) : status.ready ? (
            <>对应的创作功能会选不到模型。在「模型服务」里补一个{missingLabels}模型即可。</>
          ) : (
            <>服务地址通不通是一回事，模型清单空了照样用不了。去「模型服务」补上模型名，或用「粘贴 Key 接入」自动填。</>
          )}
        </div>
      </div>
      <div className="setup-banner-actions">
        {status.ollama.running && !status.ollama.connected && (
          <button className="btn btn-primary btn-sm" onClick={connectOllama} disabled={connecting}>
            <PlugZap size={14} />
            {connecting ? "接入中…" : `接入本机 Ollama（${status.ollama.models.length}）`}
          </button>
        )}
        <button
          className={status.ollama.running && !status.ollama.connected ? "btn btn-ghost btn-sm" : "btn btn-primary btn-sm"}
          onClick={() => onNavigate("providers")}
        >
          去接入模型
          <ArrowRight size={14} />
        </button>
        <button className="btn btn-ghost btn-sm" onClick={close} title="不再提示这类问题">
          <X size={14} />
        </button>
      </div>
    </div>
  );
}
