const TEXT_INPUTS = new Set(["text", "search", "number", "email", "url", "password", "tel"]);
// 只有在输入文字或弹窗（导出、看大图）打开时屏蔽快捷键；开关、复选框获得焦点不算输入
export const shortcutsBlocked = (target: EventTarget | null) =>
  !!document.querySelector('[role="dialog"]') || (target instanceof HTMLElement && (
    target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable
    || (target instanceof HTMLInputElement && TEXT_INPUTS.has(target.type))));
