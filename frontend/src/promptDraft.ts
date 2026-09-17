/**
 * 提示词草稿传递：提示词库页面把模板内容暂存下来，
 * 目标创作页挂载时取走并填入输入框（页面切换会卸载重挂，所以取走即删）。
 *
 * 真正的存取在 `prefs.ts` 里——前端所有 localStorage 读写都收口在那一个文件，
 * 这样「脏数据降级」只需要在一处保证。这里只留一层类型化的小包装，保持调用点可读。
 */
import { prefs } from "./prefs";

export type DraftTarget = "chat" | "image" | "video";

export function setDraftPrompt(target: DraftTarget, text: string): void {
  prefs.draftPrompt.set(target, text);
}

export function consumeDraftPrompt(target: DraftTarget): string | null {
  return prefs.draftPrompt.take(target);
}

/** 导演台 → 视频生成：把片段作为图生视频的首帧带过去 */
export function setDraftFirstFrame(asset: { id: number; url: string }): void {
  prefs.draftFirstFrame.set(asset);
}

export function consumeDraftFirstFrame(): { id: number; url: string } | null {
  return prefs.draftFirstFrame.take();
}
