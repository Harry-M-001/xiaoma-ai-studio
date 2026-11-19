import { useCallback, useEffect, useState } from "react";
import { ChevronUp, Folder, X } from "lucide-react";
import { api } from "../api";
import { Spinner } from "./common";
import { Dialog } from "./Dialog";
import type { HarnessDirs } from "../types";

/**
 * 工作区选择器：只浏览目录，不读文件内容。
 *
 * 浏览器拿不到本机绝对路径（也没有目录选择框），所以由后端提供一层只读的
 * 目录列表。能不能选来当工作区仍由保存时的校验决定，这里不重复写规则。
 */
export default function HarnessDirPicker({
  initial,
  onPick,
  onClose,
}: {
  initial: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [data, setData] = useState<HarnessDirs | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    try {
      setData(await api.harnessDirs(path));
    } catch (e) {
      setError(e instanceof Error ? e.message : "读不到这个目录");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(initial);
  }, [initial, load]);

  return (
    <Dialog
      onClose={onClose}
      label="选择工作区"
      maskClassName="canvas-dialog-mask"
      className="canvas-dialog dir-picker"
    >
      <div className="canvas-dialog-head">
        <span className="canvas-dialog-title">选择工作区</span>
        <button type="button" className="canvas-dialog-close" onClick={onClose} title="关闭">
          <X size={14} />
        </button>
      </div>

      <div className="canvas-dialog-body">
        <div className="dir-picker-path">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={!data || loading || (!data.parent && data.roots)}
            onClick={() => void load(data?.parent ?? "")}
            title="上一级"
          >
            <ChevronUp size={14} /> 上级
          </button>
          <span className="dir-picker-current" title={data?.path || ""}>
            {data?.roots ? "选择磁盘" : data?.path || "…"}
          </span>
        </div>

        {loading && (
          <div className="dir-picker-hint">
            <Spinner /> 读取中…
          </div>
        )}
        {error && <div className="dir-picker-hint err">{error}</div>}

        {!loading && !error && data && (
          <div className="dir-picker-list">
            {data.entries.length === 0 && <div className="dir-picker-hint">没有子目录</div>}
            {data.entries.map((e) => (
              <button
                key={e.path}
                type="button"
                className="dir-picker-row"
                onClick={() => void load(e.path)}
                title={e.path}
              >
                <Folder size={15} />
                <span>{e.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="canvas-dialog-foot">
        <span className="canvas-dialog-hint">
          dsh 只能写这个目录；仓库根、数据目录与磁盘根不能选。
        </span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
          取消
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={!data || data.roots || loading}
          onClick={() => data && onPick(data.path)}
        >
          用这个目录
        </button>
      </div>
    </Dialog>
  );
}
