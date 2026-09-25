import { useEffect, useRef, useState } from "react";
import { AudioLines, Download, Music, Settings, Sparkles, Trash2 } from "lucide-react";
import { api, cfgString } from "../api";
import type { Asset, SpeechResult, SpeechVoices, ModelOption } from "../types";
import { Empty, ModelSelect, Spinner } from "../components/common";
import { useToast } from "../components/Toast";
import { prefs } from "../prefs";

/**
 * 音频生成页：**两支**——「配音」（写下几句 → 选音色 → 立刻听到）与「音乐生成」（还没接）。
 *
 * `#57` 把「配音」改叫「音频生成」就是为了这两支。页签状态**不落盘**：它不是偏好，
 * 而是「我现在想做哪件事」——每次进来从「配音」开始才是对的（它是真的能用的那一支）。
 *
 * 三条设计取舍（配音这一支）：
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
  const [tab, setTab] = useState<"speech" | "music">("speech");
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
      const [ms, config] = await Promise.all([
        api.listModels("audio"),
        api.getConfig().catch(() => ({})),
      ]);
      setModels(ms);
      // 模型优先用配置里的「默认语音模型」，其次用上次选的，最后退到第一个
      const keys = ms.map((m) => m.key);
      const configured = cfgString(config, "defaults.speech_model", "");
      setModelKey(
        (configured && keys.includes(configured) ? configured : "") ||
          prefs.modelKey.get("audio", keys) ||
          keys[0] ||
          "",
      );
    })();
  }, []);

  // 音色清单**跟着选中的模型来**：云端那六个通用名字（alloy/nova…）与
  // 本机模型自带的那一百多个（zf_xiaoxiao…）完全不是一回事。换模型时重新问一次，
  // 并把「上一个模型才有的音色」清掉——留着它点生成只会报一句看不懂的错。
  useEffect(() => {
    if (!modelKey) return;
    let alive = true;
    (async () => {
      const res = await api.speechVoices(modelKey).catch(() => null);
      if (!alive || !res) return;
      setMeta(res);
      setSpeed((s) => (s === 1 || !s ? res.speedDefault : s));
      setVoice((v) => {
        // 第一次进来时把上次用过的音色捡回来；换模型时只留这个模型认识的
        const saved = (v || prefs.voice.get()).trim();
        if (!saved) return "";
        if (/^\d+$/.test(saved)) return saved;
        return res.presets.some((p) => p.id === saved) ? saved : "";
      });
    })();
    return () => {
      alive = false;
    };
  }, [modelKey]);

  useEffect(() => {
    prefs.modelKey.set("audio", modelKey);
  }, [modelKey]);

  useEffect(() => {
    prefs.voice.set(voice);
  }, [voice]);

  // 没配语音模型只影响「配音」这一支：页签与另一支照样能打开
  // （原来这里是整页 early return，那样连页签都看不到）
  const noSpeechModel = models.length === 0;

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

  // 页签本身抽成变量：三处分支都要它，而配音那一支的主体结构不必因此缩进一层
  const tabs = (
    <div className="audio-tabs segmented">
      <button
        type="button"
        className={tab === "speech" ? "active" : ""}
        onClick={() => setTab("speech")}
      >
        配音
      </button>
      <button
        type="button"
        className={tab === "music" ? "active" : ""}
        onClick={() => setTab("music")}
      >
        音乐生成
      </button>
    </div>
  );

  if (tab === "music") {
    return (
      <div className="page">
        {tabs}
        <MusicPanel />
      </div>
    );
  }

  if (noSpeechModel) {
    return (
      <div className="page">
        {tabs}
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

  return (
    <div className="page">
      {tabs}
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
              <span className="field-hint-inline">
                {meta?.local
                  ? "（本机模型的音色：填下面的名字或音色号，留空用第 0 号）"
                  : "（可留空，用服务默认音色；也可以直接手填别家的音色名）"}
              </span>
            </label>
            <input
              className="input"
              value={voice}
              placeholder={meta?.local ? "留空 = 第 0 号音色" : "留空 = 服务默认音色"}
              onChange={(e) => setVoice(e.target.value)}
            />
            {meta?.voiceHint && <div className="field-hint">{meta.voiceHint}</div>}
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

/**
 * 音乐生成（**还没接生成**）。
 *
 * 这一支现在**不生成任何东西**，也**刻意不摆一个点了没反应的表单**——那比说清「还没做」更糟。
 * 它要交代三件事：它会是什么、为什么这一版没接、接的话走哪条路。
 *
 * 动手前核过上游（2026-09-25，官方文档原文）：
 *
 * - MiniMax 的音乐接口契约很清楚（`POST /v1/music_generation`，传 `prompt` + `lyrics`，
 *   回 hex 或 url），但同一页公告写的是「自 2026 年 8 月 20 日起，付费接口（音乐生成、
 *   歌词生成）不再面向新用户提供服务……免费音乐生成接口停止服务」——**新用户拿不到**。
 *   接它等于给多数人摆一个「配了也没用」的选项，所以先不接。
 * - 那份公告里官方也把话指明了：改用**开源的 MiniMax Music 3**（HuggingFace / 魔搭）。
 *   这条能走，而且与我们「本机引擎」那一套（给地址与门槛、不代装、按硬件分档）完全同形。
 *
 * 所以原计划里「云侧各家音乐 API 走 BYOK」这句**作废**，主路径改成本机跑开源模型；
 * 结论已写进路线图 `#57`。真接的时候会再核一次上游。
 */
function MusicPanel() {
  return (
    <>
      <div className="card">
        <Empty
          icon={<Music />}
          title="音乐生成还没接"
          desc="这一支先占住位置：它会按一句话描述生成一首歌（带人声或纯音乐），产物同样收进资产库。生成能力排在路线图的 #57 后半段。"
        />
      </div>

      <div className="card">
        <div className="speech-head">
          <h3>接的话走哪条路</h3>
          <span className="field-hint">2026-09-25 核过上游文档的结论，不是推测</span>
        </div>
        <ul className="music-paths">
          <li>
            <b>本机跑开源模型（主路径）</b>
            <span>
              MiniMax 已把 Music 3 开源（HuggingFace / 魔搭）。这与「本机引擎」页里 VoxCPM
              那一档同一形态：给官方地址与显存门槛，装好之后接成一条模型服务。
            </span>
          </li>
          <li>
            <b>云侧那条先不接（原计划作废）</b>
            <span>
              MiniMax 的音乐接口契约很清楚，但官方公告写明「自 2026-08-20 起付费接口不再
              面向新用户提供服务、免费档停止服务」——新用户拿不到，接上去等于给多数人摆一个
              配了也没用的选项。
            </span>
          </li>
        </ul>
      </div>
    </>
  );
}
