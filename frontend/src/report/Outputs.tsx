import { useContext } from "react";
import { type AtomicCase, type OutputView, type RuleCheck, type ToolCallView } from "../api";
import { PROFILE_TOOL, RULE, fieldLabels, imMessages, knownValue, toolTitle } from "../atomic";
import { ProfileFields } from "./profileFields";

function Message({ raw }: { raw: string }) {
  const parsed = imMessages(raw);
  if (!parsed) return <div className="message-box">{raw || "（空文本）"}</div>;
  return <div className="im-output">
    <span className="im-tag">IM JSON · {parsed.length ? `${parsed.length} 条消息` : "空消息列表"}</span>
    {parsed.map((item, index) => <div key={index} className="message-box">{item.text || "（空文本）"}
      {!!item.attachments.length && <small className="block muted">附件 {item.attachments.length} 个：{item.attachments.map((attachment) =>
        typeof attachment === "string" ? attachment : JSON.stringify(attachment)).join("；")}</small>}</div>)}
    <details><summary>原始输出</summary><pre className="raw-output">{raw}</pre></details>
  </div>;
}

export function OutputBlock({ output, context }: { output?: OutputView; context?: AtomicCase }) {
  if (!output) return <p className="muted">未取得完整响应。</p>;
  const invalid = output.format_check?.status === "invalid";
  return <>
    {invalid && <div className="format-problem" role="alert"><strong>响应格式问题 · 重要</strong>
      <p>未遵守本次输入中的 IM JSON Schema，可能导致下游消息解析或发送失败。</p>
      <ul>{output.format_check!.issues.map((issue, index) => <li key={index}>{issue.summary} <code>{issue.path}</code></li>)}</ul>
    </div>}
    {invalid ? <details open><summary>原始输出（格式不符合要求）</summary><pre className="raw-output">{output.messages.join("\n") || "（空文本）"}</pre></details>
      : output.messages.length ? output.messages.map((message, index) => <Message key={index} raw={message} />)
      : !output.tool_calls.length && <p className="muted">未生成对客消息或工具调用。</p>}
    {output.tool_calls.map((call, index) => <ToolCallBlock key={index} call={call} context={context} />)}
  </>;
}

export function ToolCallBlock({ call, context }: { call: ToolCallView; context?: AtomicCase }) {
  const fields = useContext(ProfileFields);
  const labels = fieldLabels(fields);
  const profile = call.name.startsWith(PROFILE_TOOL);
  const key = profile ? call.name.slice(PROFILE_TOOL.length + 1) : "";
  const description = fields[key]?.description;
  const rules = context?.tool_rules?.[call.name] || [];
  return <div className="tool-call"><strong title={call.name}>{profile ? "画像写入" : "工具调用"} · {toolTitle(call.name, context?.tool_labels, labels)}</strong>
    <dl className="kv">{Object.entries(call.arguments).map(([name, value]) => {
      const known = profile ? knownValue(context?.profile_snapshot, name, labels) : undefined;
      const text = typeof value === "string" ? value : JSON.stringify(value);
      return <div key={name}><dt title={name}>{labels[name] || name}</dt><dd>{known !== undefined ? <><span className="muted">已知：{known} → </span>{text}</> : text}</dd></div>;
    })}</dl>
    {profile && <details className="field-info"><summary>字段说明</summary>
      <dl className="kv">
        <div><dt>字段说明</dt><dd>{description || <span className="muted">空间未填写字段说明</span>}</dd></div>
        <div><dt>字段键</dt><dd className="mono">{key}</dd></div>
      </dl>
      <h5>提示词中的相关规则</h5>
      {rules.length ? rules.map((rule, index) => <div key={index} className="rules field-rule">{rule}</div>)
        : <p className="muted">提示词里没有直接提到这个工具，写入条件以「本轮业务规则」为准。</p>}
    </details>}</div>;
}

export function RuleChecks({ checks }: { checks: RuleCheck[] }) {
  return <div className="rule-checks">{checks.map((check, index) => <p key={index}>
    <span className={`severity-${check.severity}`}>规则检查 · {RULE[check.category] || check.category}</span>{check.summary}</p>)}</div>;
}
