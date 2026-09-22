import { useEffect, useRef, useState } from "react";
import { AudioLines, Download, FileText, Images, Library, Maximize2, Play, Trash2, Upload, Video as VideoIcon } from "lucide-react";
import { api } from "../api";
import type { Asset } from "../types";
import { Empty, formatSize, formatTime, Spinner } from "../components/common";
import { useToast } from "../components/Toast";
import Lightbox from "../components/Lightbox";
import { UpscaleDialog } from "../components/UpscaleDialog";
import { downloadUrl } from "../components/TaskCard";

const FILTERS = [
  { label: "全部", value: "" },
  { label: "图片", value: "image" },
  { label: "视频", value: "video" },
  { label: "音频", value: "audio" },
  { label: "文档", value: "document" },
];
const PAGE = 60;

export default function AssetsPage({
  onNavigate,
}: {
  /** 弹窗里「去本机引擎页下载」那个跳转要用它；不传就不显示那个按钮 */
  onNavigate?: (route: string) => void;
}) {
  const toast = useToast();
  const [filter, setFilter] = useState("");
  const [items, setItems] = useState<Asset[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<{ url: string; kind: string; dl?: string } | null>(null);
  // 正在放大的那个素材（弹窗里挑路线/模型/倍数）
  const [upscaling, setUpscaling] = useState<Asset | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = async (kind: string, append = false) => {
    setLoading(true);
    try {
      const offset = append ? items.length : 0;
      const res = await api.listAssets({ kind: kind || undefined, limit: PAGE, offset });
      setItems((prev) => (append ? [...prev, ...res.items] : res.items));
      setTotal(res.total);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(filter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  const upload = async (files: FileList | File[]) => {
    const list = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (list.length === 0) {
      toast.error("目前仅支持上传图片素材");
      return;
    }
    setUploading(true);
    let ok = 0;
    for (const f of list) {
      try {
        await api.uploadAsset(f);
        ok++;
      } catch (e) {
        toast.error(e instanceof Error ? e.message : `${f.name} 上传失败`);
      }
    }
    setUploading(false);
    if (ok) {
      toast.success(`已上传 ${ok} 个文件`);
      load(filter === "video" ? "" : filter);
      if (filter === "video") setFilter("");
    }
  };

  const remove = async (a: Asset) => {
    if (!confirm(`确定删除「${a.original_name}」？文件与记录都会被移除。`)) return;
    await api.deleteAsset(a.id);
    setItems((prev) => prev.filter((x) => x.id !== a.id));
    setTotal((t) => t - 1);
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="page-title">资产库</div>
          <div className="page-desc">生成的图片、视频与上传的素材都保存在本地，共 {total} 个文件</div>
        </div>
        <div className="row">
          <div className="segmented">
            {FILTERS.map((f) => (
              <button
                key={f.value}
                className={filter === f.value ? "active" : ""}
                onClick={() => setFilter(f.value)}
              >
                {f.label}
              </button>
            ))}
          </div>
          <button className="btn btn-primary" onClick={() => fileInput.current?.click()} disabled={uploading}>
            {uploading ? <Spinner light /> : <Upload size={15} />}
            上传图片
          </button>
          <input
            ref={fileInput}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) upload(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      {loading && items.length === 0 ? (
        <div className="loading-page">
          <span className="spinner lg" />
        </div>
      ) : items.length === 0 ? (
        <div className="card">
          <Empty
            icon={<Library />}
            title="资产库还是空的"
            desc="去生成第一张图片或第一段视频；也可以直接上传本地图片，作为图生图 / 图生视频的素材。"
          />
        </div>
      ) : (
        <>
          <div className="asset-grid">
            {items.map((a) => (
              <div key={a.id} className="asset-card">
                <div className="asset-thumb" onClick={() => setPreview({ url: a.url, kind: a.kind, dl: downloadUrl(a.id) })}>
                  {a.kind === "video" ? (
                    <>
                      <video src={a.url} muted preload="metadata" />
                      <span className="play-ic">
                        <Play />
                      </span>
                    </>
                  ) : a.kind === "document" ? (
                    <span className="asset-doc-thumb">
                      <FileText />
                      <em>Markdown 文稿</em>
                    </span>
                  ) : a.kind === "audio" ? (
                    // 音频用「波形条 + 时长」当缩略图：它没有画面可显示，
                    // 但直接塞一个 <audio controls> 会把卡片撑变形
                    <span className="asset-audio-thumb">
                      <AudioLines />
                      <em>{a.duration ? `${a.duration} 秒` : "音频"}</em>
                    </span>
                  ) : (
                    <img src={a.url} alt={a.original_name} loading="lazy" />
                  )}
                </div>
                <div className="asset-meta">
                  <div className="asset-name" title={a.prompt || a.original_name}>
                    {a.prompt || a.original_name}
                  </div>
                  <div className="asset-time">
                    {formatTime(a.created_at)} · {formatSize(a.size)}
                  </div>
                </div>
                <div className="asset-actions">
                  {/* 只有图片与视频能放大（音频 / 文稿没有「像素」这个概念） */}
                  {(a.kind === "image" || a.kind === "video") && (
                    <button
                      className="btn btn-ghost btn-sm"
                      title="放大（超分）：把这张图 / 这段片子放大成新资产，原件不动"
                      onClick={() => setUpscaling(a)}
                    >
                      <Maximize2 size={13} />
                      放大
                    </button>
                  )}
                  <a className="btn btn-ghost btn-sm" href={downloadUrl(a.id)} title="下载">
                    <Download size={13} />
                    下载
                  </a>
                  <button className="btn btn-danger-ghost btn-sm" onClick={() => remove(a)} title="删除">
                    <Trash2 size={13} />
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
          {items.length < total && (
            <div className="mt24" style={{ textAlign: "center" }}>
              <button className="btn btn-ghost" onClick={() => load(filter, true)} disabled={loading}>
                {loading ? "加载中…" : "加载更多"}
              </button>
            </div>
          )}
        </>
      )}

      {preview && (
        <Lightbox url={preview.url} kind={preview.kind} downloadUrl={preview.dl} onClose={() => setPreview(null)} />
      )}

      {upscaling && (
        <UpscaleDialog
          asset={upscaling}
          onClose={() => setUpscaling(null)}
          onGoEngines={onNavigate ? () => onNavigate("engines") : undefined}
          onDone={(made) => {
            // 图片是同步出的：产物直接插到列表最前面（列表按新→旧排），不用整页重拉。
            // 视频是后台任务，这里还拿不到东西（made 为 null），跑完在资产库里自己会出现。
            if (made) {
              setItems((prev) => [made, ...prev]);
              setTotal((t) => t + 1);
            }
            setUpscaling(null);
          }}
        />
      )}
    </div>
  );
}
