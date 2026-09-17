import { useEffect, useState } from "react";
import { X, Download } from "lucide-react";
import { Dialog } from "./Dialog";

export default function Lightbox({
  url,
  kind,
  downloadUrl,
  onClose,
}: {
  url: string;
  kind: string;
  downloadUrl?: string;
  onClose: () => void;
}) {
  const isDoc = kind === "document";
  const [text, setText] = useState<string | null>(null);

  // Markdown 产物按纯文本读出展示（不同系统对 .md 的 MIME 处理不一致，不能靠 iframe）
  useEffect(() => {
    if (!isDoc) return;
    let alive = true;
    setText(null);
    fetch(url)
      .then((r) => r.text())
      .then((t) => {
        if (alive) setText(t);
      })
      .catch(() => {
        if (alive) setText("（内容读取失败，请用下载按钮获取）");
      });
    return () => {
      alive = false;
    };
  }, [isDoc, url]);

  return (
    <Dialog
      onClose={onClose}
      label={isDoc ? "文稿预览" : kind === "video" ? "视频预览" : "图片预览"}
      maskClassName="lightbox"
      className="lightbox-inner"
    >
      <div className="lightbox-bar">
        {downloadUrl && (
          <a className="btn btn-ghost btn-sm" href={downloadUrl} download>
            <Download size={15} />
            下载
          </a>
        )}
        <button className="btn btn-ghost btn-sm" onClick={onClose}>
          <X size={15} />
          关闭
        </button>
      </div>
      <div className={`lightbox-media ${isDoc ? "lightbox-media-doc" : ""}`}>
        {isDoc ? (
          <pre className="lightbox-doc">{text ?? "加载中…"}</pre>
        ) : kind === "video" ? (
          <video src={url} controls autoPlay />
        ) : (
          <img src={url} alt="预览" />
        )}
      </div>
    </Dialog>
  );
}
