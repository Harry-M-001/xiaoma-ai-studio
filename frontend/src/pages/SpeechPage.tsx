import { useEffect, useRef, useState } from "react";
import { AudioLines, Download, Settings, Sparkles, Trash2 } from "lucide-react";
import { api, cfgString } from "../api";
import type { Asset, SpeechResult, SpeechVoices, ModelOption } from "../types";
import { Empty, ModelSelect, Spinner } from "../components/common";
import { useToast } from "../components/Toast";
import { prefs } from "../prefs";

/**
 * 配音页：写下几句话 → 选音色 → 点一下 → 立刻听到。
 *
 * 三条设计取舍：
 *
 * 1. **试听就是生成，产物直接进资产库。** 这一段是花过钱的，必须能重听、能复用
 *    （样片旁白、导演台配音都要用它）。做成「听一次就没了」等于每次重听都要再付费。
 * 2. **音色是「快捷选项 + 可手填」**，不是下拉白名单。各家音色名不通用
 *    （`alloy` / `zh-CN-XiaoxiaoNeural` / 自建音色 id），写死选择框就逼用户等我们发版。
 * 3. **失败时把话说完整**：没配语音模型时直接给出「去哪儿加一个音频模型」的路径，
 *    而不是一句「生成失败」。
 */
export default function SpeechPage({ onGoSettings }: { onGoSettings: () => void }) {
  const toast = useToast();
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelKey, setModelKey] = useState("");
  const [meta, setMeta] = useState<SpeechVoices | null>(null);
  const [text, setText] = useState("");
  const [voice, setVoice] = useState("");
  const [speed, setSpeed] = useState(1);
  const [busy, setBusy] = useState(false);
  // 本次会话生成的几条：方便来回对比不同音色，不用去资产库里翻
  const [results, setResults] = useState<SpeechResult[]>([]);
  const [playing, setPlaying] = useState<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    (async () => {
      const [ms, metaRes, config] = await Promise.all([
        api.listModels("audio"),
        api.speechVoices().catch(() => null),
        api.getConfig().catch(() => ({})),
      ]);
      setModels(ms);
      setMeta(metaRes);
      if (metaRes) setSpeed(metaRes.speedDefault);
      // 模型优先用配置里的「默认语音模型」，其次用上次选的，最后退到第一个
      const keys = ms.map((m) => m.key);
      const configured = cfgString(config, "defaults.speech_model", "");
      setModelKey(
        (configured && keys.includes(configured) ? configured : "") ||
          prefs.modelKey.get("audio", keys) ||
          keys[0] ||
          "",
      );
      setVoice(prefs.voice.get());
    })();
  }, []);

  useEffect(() => {
    prefs.modelKey.set("audio", modelKey);
  }, [modelKey]);

  useEffect(() => {
    prefs.voice.set(voice);
  }, [voice]);

  if (models.length === 0) {
    return (
      <div className="page">
        <Empty
          icon={<Settings />}
          title="还没有可用的语音模型"
          desc="到「模型服务」给某个服务加一个「能力 = 音频」的模型：OpenAI 兼容的服务填 tts-1 / gpt-4o-mini-tts 这类模型名，硅基流动、MiniMax 等兼容服务同理。加完回到这里就能试听。"
          action={
            <button className="btn btn-primary" onClick={onGoSettings}>
              去配置模型服务
            </button>
          }
        />
      </div>
    );
  }

  const tooLong = meta ? text.length > meta.maxChars : false;

  const generate = async () => {
    if (!text.trim()) {
      toast.error("请先写下要念的内容");
      return;
    }
    setBusy(true);
    try {
      const r = await api.speech({ text, model_key: modelKey, voice, speed });
      // 新的排最前：刚生成的那条最可能是要听的那条
      setResults((prev) => [r, ...prev].slice(0, 8));
      toast.success(`配音已生成：${r.chars} 字${r.seconds ? ` / ${r.seconds.toFixed(1)} 秒` : ""}`);
      play(r.asset);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "生成失败");
    } finally {
      setBusy(false);
    }
  };

  const play = (asset: Asset) => {
    // 用同一个 <audio> 元素换 src：连点几条时不会叠着放，也不会留一堆元素
    const el = audioRef.current;
    if (!el) return;
    el.src = asset.url;
    setPlaying(asset.id);
    void el.play().catch(() => {
      /* 浏览器拦自动播放是正常的：控件就在旁边，用户自己点一下即可 */
    });
  };

  return (
    <div className="page">
      <div className="studio-grid">
        <div className="card studio-panel">
          <div className="field">
            <label className="field-label">语音模型</label>
            <ModelSelect models={models} value={modelKey} onChange={setModelKey} placeholder="选择语音模型" />
          </div>

          <div className="field">
            <label className="field-label">要念的内容</label>
            <textarea
              className="textarea"
              rows={6}
              placeholder="写下要念的台词或旁白，例如：黄昏的站台，最后一班车还没来。"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className={`field-hint${tooLong ? " danger" : ""}`}>
              {text.length}
              {meta ? ` / ${meta.maxChars}` : ""} 字
              {tooLong ? " —— 太长了，请分段生成（每段单独存成一条音频）" : ""}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              音色
              <span className="field-hint-inline">（可留空，用服务默认音色；也可以直接手填别家的音色名）</span>
            </label>
            <input
              className="input"
              value={voice}
              placeholder="留空 = 服务默认音色"
              onChange={(e) => setVoice(e.target.value)}
            />
            {meta && meta.presets.length > 0 && (
              <div className="speech-voices">
                {meta.presets.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    className={`speech-voice${voice === p.id ? " active" : ""}`}
                    onClick={() => setVoice(voice === p.id ? "" : p.id)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="field">
            <label className="field-label">语速 {speed.toFixed(2)}×</label>
            <input
              type="range"
              min={meta?.speedRange?.[0] ?? 0.5}
              max={meta?.speedRange?.[1] ?? 2}
              step={0.05}
              value={speed}
              onChange={(e) => setSpeed(Number(e.target.value))}
            />
          </div>

          <button className="btn btn-primary btn-block" onClick={generate} disabled={busy || tooLong}>
            {busy ? <Spinner /> : <AudioLines size={15} />}
            生成并试听
          </button>
          <div className="field-hint">
            每点一次会真实调用一次语音合成（按所选服务计费），产物会自动收进资产库。
          </div>
          <audio ref={audioRef} controls className="speech-player" onEnded={() => setPlaying(null)} />
        </div>

        <div className="card">
          <div className="speech-head">
            <h3>本次生成</h3>
            <span className="field-hint">只列这次打开页面生成的；以前的都在资产库里</span>
          </div>
          {results.length === 0 ? (
            <div className="speech-empty">
              <Sparkles size={16} />
              <span>还没有生成过。写几句台词，点「生成并试听」，这里会列出来方便对比不同音色。</span>
            </div>
          ) : (
            <div className="speech-list">
              {results.map((r) => (
                <div key={r.asset.id} className={`speech-item${playing === r.asset.id ? " active" : ""}`}>
                  <div className="speech-item-main">
                    <div className="speech-item-name">
                      {r.voice || "默认音色"}
                      <span className="speech-item-meta">
                        {r.chars} 字{r.seconds ? ` · ${r.seconds.toFixed(1)} 秒` : ""} · {r.model}
                      </span>
                    </div>
                    <div className="speech-item-text">{r.asset.prompt}</div>
                  </div>
                  <div className="speech-item-actions">
                    <button className="btn btn-ghost btn-sm" onClick={() => play(r.asset)}>
                      播放
                    </button>
                    <a className="btn btn-ghost btn-sm" href={`${r.asset.url}?download=1`} download>
                      <Download size={13} />
                    </a>
                    <button
                      className="btn btn-ghost btn-sm"
                      title="从本次列表里移除（资产库里的那条还在）"
                      onClick={() => setResults((prev) => prev.filter((x) => x.asset.id !== r.asset.id))}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
