import { useContext, useEffect, useState } from "react";
import { type AtomicCase } from "../api";
import { escapeRegExp, thumbnail, tidy } from "../atomic";
import { ToolCallBlock } from "./Outputs";
import { ProfileFields } from "./profileFields";

function Highlighted({ text, marks }: { text: string; marks: string[] }) {
  if (!marks.length) return <>{text}</>;
  const pattern = new RegExp(`(${marks.map(escapeRegExp).join("|")})`, "g");
  return <>{text.split(pattern).map((part, index) => marks.includes(part) ? <mark key={index}>{part}</mark> : part)}</>;
}

export function Images({ urls, label = "客户发送的图片" }: { urls: string[]; label?: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const [broken, setBroken] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  if (!urls.length) return null;
  return <div className="image-strip">
    {urls.map((url, index) => broken.has(url)
      ? <a key={index} className="image-broken" href={url} target="_blank" rel="noreferrer noopener">图片无法加载，在新窗口重试</a>
      : <button key={index} type="button" className="image-thumb" onClick={() => setOpen(url)} aria-label="查看原图">
        <img src={thumbnail(url)} alt={label} loading="lazy" referrerPolicy="no-referrer"
          onError={() => setBroken((current) => new Set(current).add(url))} /></button>)}
    {open && <div className="lightbox" role="dialog" aria-label="原图" onClick={() => setOpen(null)}>
      <img src={open} alt={`${label}原图`} referrerPolicy="no-referrer" />
      <a href={open} target="_blank" rel="noreferrer noopener" onClick={(event) => event.stopPropagation()}>在新窗口打开原图</a></div>}
  </div>;
}

const RECENT_TURNS = 4;

export function ContextView({ context, cited }: { context: AtomicCase; cited: string[] }) {
  const instruction = context.task_input && context.task_input !== "无" ? context.task_input : "";
  const rules = tidy(context.business_rules);
  const marked = cited.filter((quote) => rules.includes(quote));
  const [allTurns, setAllTurns] = useState(false);
  // 已知画像以中文字段名为键，按中文名找到字段说明，悬停可见
  const fields = useContext(ProfileFields);
  const describeByLabel = Object.fromEntries(Object.values(fields).filter((field) => field.label && field.description)
    .map((field) => [field.label, field.description]));
  const turns = allTurns ? context.prior_turns : context.prior_turns.slice(-RECENT_TURNS);
  const hidden = context.prior_turns.length - turns.length;
  const customer = <>{context.current_turn && <div>{context.current_turn}</div>}<Images urls={context.current_images} />
    {!context.current_turn && !context.current_images.length && <span className="muted">请求中未单独提供客户消息</span>}</>;
  return <div className="context-block">
    {!!context.prior_turns.length && <div className="context-row"><strong>此前对话{hidden ? ` · 最近 ${turns.length} 条` : ""}</strong>
      {hidden > 0 && <button type="button" className="link-button" onClick={() => setAllTurns(true)}>展开更早的 {hidden} 条</button>}
      <div className="turn-list">{turns.map((turn, index) => <div key={index} className={`turn turn-${turn.role}`}>
        <span>{turn.role === "user" ? "客户" : turn.role === "assistant" ? "助手" : turn.role}</span>
        <div className="turn-text">{turn.text && <p>{turn.text}</p>}<Images urls={turn.images} /></div></div>)}</div></div>}
    <div className="context-row"><strong>{instruction ? "子任务指令" : "客户本轮消息"}</strong>
      <div className="message-box current-turn">{instruction || customer}
        {context.current_images.length > 0 && <small className="block muted">评审模型只读文字，未查看图片内容；涉及图片的判断以人工为准。</small>}</div></div>
    {instruction && (context.current_turn || context.current_images.length > 0) && <div className="context-row"><strong>客户消息</strong><div className="message-box">{customer}</div></div>}
    <div className="context-folds">
      {!!context.history_tool_calls.length && <details><summary>已执行的工具调用 · {context.history_tool_calls.length} 个</summary>
        {context.history_tool_calls.map((call, index) => <ToolCallBlock key={index} call={call} context={context} />)}</details>}
      {!!Object.keys(context.profile_snapshot).length && <details><summary>已知客户画像 · {Object.keys(context.profile_snapshot).length} 项</summary>
        <dl className="kv">{Object.entries(context.profile_snapshot).map(([key, value]) => <div key={key}>
          <dt title={describeByLabel[key]} className={describeByLabel[key] ? "has-help" : undefined}>{key}</dt><dd>{value}</dd></div>)}</dl></details>}
      {rules && <details open={marked.length > 0}><summary>本轮业务规则{marked.length ? ` · 评审引用 ${marked.length} 处（已高亮）` : ""}</summary>
        <div className="rules"><Highlighted text={rules} marks={marked} /></div>
        {context.business_rules.length >= 6000 && <p className="hint">规则较长，仅展示前 6000 字；完整原文见下方原始证据。</p>}</details>}
    </div>
  </div>;
}
