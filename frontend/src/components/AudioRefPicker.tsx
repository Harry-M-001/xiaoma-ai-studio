import { useEffect, useState } from "react";
import { AudioLines } from "lucide-react";
import { api } from "../api";
import type { Asset } from "../types";

/**
 * 「对白音轨」选择器：给一次视频生成挂一条配音（数字人 / 口播那条路）。
 *
 * 四条取舍：
 *
 * 1. **只列音频**（`kind=audio`）。对白就该从配音里挑，把图片视频混进同一个下拉里，
 *    用户总会挑错一次，而这一次错要花视频钱才发现。
 * 2. **把秒数显示出来**。这一段唯一需要看的数字就是「配音有多长」——视频时长要够
 *    读完整句。但**不在前端判够不够**：拦不拦由后端说了算（那边是唯一的真相），
 *    这里只摆事实，免得两套规则慢慢长出两个答案。
 * 3. **空选项是「不用对白」，不是「没选」**。对白是可选件，摘掉它是常规操作，
 *    措辞上就不该让它像个「必填项没填」。
 * 4. **提示文案从后端拿**（`/api/audio/voices` 的 `videoRefHint`）：同一句话还要出现在
 *    画布的视频节点面板上，两边各写一份必然漂移。
 */
export default function AudioRefPicker({
  value,
  onChange,
  id,
}: {
  value: number | null;
  /** 回调带的是整条资产（不只是 id）：确认弹窗要用它的名字与秒数再摆一次 */
  onChange: (asset: Asset | null) => void;
  /** 面板上的 select 需要一个 id 时才传（画布浮框里用得上） */
  id?: string;
}) {
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [hint, setHint] = useState("");

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.listAssets({ kind: "audio", limit: 60 }).then((r) => r.items),
      api.speechVoices().then((v) => v.videoRefHint).catch(() => ""),
    ])
      .then(([items, text]) => {
        if (!alive) return;
        setAssets(items);
        setHint(text);
      })
      .catch(() => {
        if (alive) setAssets([]);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (assets === null) {
    return <div className="field-hint">正在读取配音…</div>;
  }

  if (assets.length === 0) {
    return (
      <div className="audio-ref-empty">
        <AudioLines size={14} />
        <span>还没有配音。到「音频生成」页写几句、生成一段，再回来挂到这一镜上。</span>
      </div>
    );
  }

  return (
    <>
      <select
        id={id}
        className="select"
        value={value === null ? "" : String(value)}
        onChange={(e) =>
          onChange(e.target.value ? assets.find((a) => a.id === Number(e.target.value)) ?? null : null)
        }
      >
        <option value="">不用对白（出无声片子）</option>
        {assets.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name || a.prompt || a.original_name}
            {a.duration ? `（${a.duration} 秒）` : ""}
          </option>
        ))}
      </select>
      {hint && <div className="field-hint">{hint}</div>}
    </>
  );
}
