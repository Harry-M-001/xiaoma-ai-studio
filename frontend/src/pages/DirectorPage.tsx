import { useEffect, useRef, useState } from "react";
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
import type { Asset } from "../types";
import { downloadUrl } from "../components/TaskCard";
import { Empty, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

function fmt(t: number): string {
  if (!Number.isFinite(t)) return "--:--";
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const ms = Math.floor((t % 1) * 10);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${ms}`;
}

export default function DirectorPage({ onNavigate }: { onNavigate: (route: string) => void }) {
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

  useEffect(() => {
    (async () => {
      const [status, vids] = await Promise.all([api.ffmpegStatus(), api.listDirectorVideos()]);
      setFfmpeg(status);
      setVideos(vids);
    })();
  }, []);

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
    if (clips.length < 2) return;
    setMerging(true);
    try {
      const out = await api.mergeVideos(clips.map((c) => c.id));
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

  return (
    <div className="page">
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
              <button className="btn btn-primary btn-block" disabled={merging} onClick={doMerge}>
                {merging ? <Spinner light /> : <Clapperboard size={15} />}
                {merging ? "合并中…" : `合并导出（${clips.length} 段）`}
              </button>
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
    </div>
  );
}
