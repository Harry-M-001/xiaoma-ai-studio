/**
 * 提示词草稿传递：提示词库页面把模板内容暂存到 localStorage，
 * 目标创作页挂载时取走并填入输入框。页面切换会卸载重挂，因此取走即删即可。
 */
const KEY = "xm_draft_prompt";

export type DraftTarget = "chat" | "image" | "video";

export function setDraftPrompt(target: DraftTarget, text: string): void {
  localStorage.setItem(KEY, JSON.stringify({ target, text }));
}

export function consumeDraftPrompt(target: DraftTarget): string | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const draft = JSON.parse(raw) as { target?: string; text?: unknown };
    if (draft.target !== target || typeof draft.text !== "string") return null;
    localStorage.removeItem(KEY);
    return draft.text;
  } catch {
    return null;
  }
}

/** 导演台 → 视频生成：把片段作为图生视频的首帧带过去 */
const FF_KEY = "xm_draft_first_frame";

export function setDraftFirstFrame(asset: { id: number; url: string }): void {
  localStorage.setItem(FF_KEY, JSON.stringify(asset));
}

export function consumeDraftFirstFrame(): { id: number; url: string } | null {
  try {
    const raw = localStorage.getItem(FF_KEY);
    if (!raw) return null;
    localStorage.removeItem(FF_KEY);
    const d = JSON.parse(raw) as { id?: number; url?: string };
    if (typeof d.id !== "number" || typeof d.url !== "string") return null;
    return { id: d.id, url: d.url };
  } catch {
    return null;
  }
}
