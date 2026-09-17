import { useEffect, useRef, type ReactNode } from "react";

/**
 * 受控弹窗外壳：遮罩 + 容器 + 弹窗本来就该有的行为。
 *
 * 为什么要有它：**皮肤早就统一了，行为没有**。外观由 `.modal` 与 `.canvas-dialog`
 * 两套类名管着，看起来一致；但行为是三套——`Modal` 能按 Esc 关、画布里的自建弹窗
 * 与 `Lightbox` 不能，`role`/`aria-modal` 也只有 `Modal` 上有。
 * 结果就是键盘用户碰到的情况：同一个应用里有的弹窗按 Esc 能关、有的关不掉，
 * 读屏软件也不知道跳出来的这层是个对话框。
 *
 * 所以这里统一的是**行为与可访问性**，皮肤仍由调用方传进来的类名决定（视觉零变化）：
 * - Esc 关闭、点遮罩关闭（只有点在遮罩本身上才算）
 * - `role="dialog"` + `aria-modal` + 无障碍名称
 * - 打开时把焦点移进弹窗，关闭时还给原来拿着焦点的元素
 * - Tab 在弹窗内循环（键盘用户不会tab着tab着跑到背后的页面上）
 */

/** 可聚焦元素：与浏览器自身的 Tab 顺序口径保持一致 */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    // 隐藏的元素不该被 Tab 选中（offsetParent 为 null 覆盖了 display:none 与祖先隐藏）
    (el) => el.offsetParent !== null || el === document.activeElement
  );
}

export function Dialog({
  onClose,
  label,
  children,
  className = "",
  maskClassName = "modal-mask",
}: {
  onClose: () => void;
  /** 无障碍名称：读屏念出来的那句话，通常就是弹窗标题 */
  label: string;
  children: ReactNode;
  /** 容器类名（皮肤，例如 `canvas-dialog run-confirm`） */
  className?: string;
  /** 遮罩类名：两套皮肤的背景色与居中方式不同，不能硬统一 */
  maskClassName?: string;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  // 用 ref 拿最新的 onClose：键盘监听只在挂载时绑一次。
  // （弹窗打开期间父组件常常在轮询/重渲染，若把 onClose 放进依赖，监听会被反复拆装，
  // 恰好在这一瞬间按下的 Esc 就会丢——表现成「偶尔按 Esc 没反应」。）
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    // 打开前谁拿着焦点，关闭后还给谁。不还的话键盘用户会掉回页面顶部，
    // 得重新 Tab 一遍才能回到刚才那一行。
    const before = document.activeElement as HTMLElement | null;
    // 弹窗内部已经有 autoFocus 的元素时不要抢它的焦点
    if (!box.contains(document.activeElement)) {
      (focusables(box)[0] ?? box).focus();
    }
    return () => before?.focus?.();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const box = boxRef.current;
      if (!box) return;
      const nodes = focusables(box);
      if (nodes.length === 0) {
        e.preventDefault();
        box.focus();
        return;
      }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      // 焦点已经在弹窗外面（比如用户点了别处）就拉回来，而不是放它继续跑
      if (!box.contains(document.activeElement)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    // 说明：焦点陷阱只做在 keydown 里（preventDefault 拦住浏览器默认的 Tab 走查）。
    // 曾试过再加一层 focusin 兜底（焦点一旦跑到弹窗外就拉回来），但在这套环境里
    // focusin 根本不派发（探针监听器一次都没收到），无法验证、也就不能依赖——
    // 一个「写了但不确定有没有生效」的兜底，比没有更危险。
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className={maskClassName} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={boxRef}
        className={className ? `dlg-shell ${className}` : "dlg-shell"}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
      >
        {children}
      </div>
    </div>
  );
}
