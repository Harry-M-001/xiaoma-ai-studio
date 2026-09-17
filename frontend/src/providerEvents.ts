/**
 * 模型服务变化时的一根「广播线」。
 *
 * 为什么需要它：顶部引导横幅判断的是「有没有可用模型」，而这个状态在「模型服务」页会被改掉
 * （粘贴 Key、一键接入 Ollama、加/删/停用服务）。不广播的话，用户刚接入完模型，
 * 横幅还挂在上面说「还没有接入任何模型」——和下面那页显示的内容自相矛盾，看着像坏了。
 *
 * 用 DOM 事件而不是再引一个状态库：只有这一处跨页面的通知需求，
 * 为它上全局状态管理不划算，而且这条线是「广播」而不是「共享状态」，语义上更简单。
 */

export const PROVIDERS_CHANGED = "xm:providers-changed";

export function notifyProvidersChanged(): void {
  window.dispatchEvent(new Event(PROVIDERS_CHANGED));
}
