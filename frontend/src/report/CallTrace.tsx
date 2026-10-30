import { Button } from "@heroui/react";
import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import { api, downloadFile, type Span, type SpanIndex, type TraceStatus, type TurnTrace } from "../api";
import { useRemote } from "../App";

export const TRACE_STATUS: Record<TraceStatus, string> = {
  pending: "等待采集", complete: "已见调用正文齐全", partial: "部分采集", summary_only: "仅摘要",
  not_found: "未找到", failed: "采集失败",
};
const TRACE_HINT: Partial<Record<TraceStatus, string>> = {
  pending: "网关日志写入 SLS 有延迟，轮次结束后数分钟内自动采集。",
  summary_only: "该 EVAL 空间未开启完整请求体日志，只有模型、耗时与 tokens，没有输入输出。",
  partial: "部分调用的输入输出正文缺失或无法解析，或调用数少于 Agent 日志记录的次数。展开对应调用可查看原因。",
  not_found: "截止时间内没有在网关日志里找到这一轮的调用。",
};

type Message = Record<string, unknown>;
const BODY_ERROR: Record<string, string> = {
  body_logging_disabled: "该调用未开启正文记录",
  request_body_invalid_json: "请求正文不是有效 JSON",
  response_body_invalid_json: "响应正文不是有效 JSON",
};
const ROLE: Record<string, string> = {
  user: "客户", assistant: "助手", system: "系统", developer: "系统", tool: "工具结果",
  function_call: "调用", function_call_output: "工具结果",
};

function contentText(message: Message): string {
  const content = message.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((part: Message) => typeof part.text === "string" ? part.text
    : String(part.type || "").includes("image") ? "[图片]" : JSON.stringify(part)).join("\n");
  if (message.type === "function_call") return `调用 ${message.name}(${message.arguments ?? ""})`;
  if (message.type === "function_call_output") return `工具结果：${message.output ?? ""}`;
  return JSON.stringify(message);
}

// 兼容 Responses（instructions + input）与 Chat Completions（messages）两种请求
function requestParts(request: Message) {
  const messages: Message[] = Array.isArray(request.messages) ? request.messages as Message[]
    : Array.isArray(request.input) ? request.input as Message[]
      : typeof request.input === "string" ? [{ role: "user", content: request.input }] : [];
  const system = (message: Message) => message.role === "system" || message.role === "developer";
  return {
    system: [typeof request.instructions === "string" ? request.instructions : "",
      ...messages.filter(system).map(contentText)].filter(Boolean).join("\n\n"),
    messages: messages.filter((message) => !system(message)),
    tools: ((request.tools as Message[] | undefined) || [])
      .map((tool) => String(tool.name || (tool.function as Message | undefined)?.name || tool.type)),
  };
}

function responseText(response: Message): string {
  const output = Array.isArray(response.output) ? response.output as Message[] : [];
  const choice = Array.isArray(response.choices) ? (response.choices[0] as Message | undefined)?.message as Message | undefined : undefined;
  const items = output.length ? output : choice ? [choice, ...((choice.tool_calls as Message[] | undefined) || [])
    .map((call) => ({ type: "function_call", ...(call.function as Message) }))] : [];
  return items.map((item) => item.type === "reasoning" ? "" : contentText(item)).filter(Boolean).join("\n\n");
}

function SpanBody({ span }: { span: Span }) {
  if (!span.request) return <p className="muted">这次调用没有可用的完整输入输出正文{span.body_error ? `（${BODY_ERROR[span.body_error] || span.body_error}）` : ""}。</p>;
  const parts = requestParts(span.request);
  return <div className="span-body">
    {parts.system && <details><summary>系统提示词 · {parts.system.length} 字</summary><pre>{parts.system}</pre></details>}
    {!!parts.tools.length && <p className="muted">可用工具：{parts.tools.join("、")}</p>}
    <h4>输入 · {parts.messages.length} 条</h4>
    <div className="turn-list">{parts.messages.map((message, index) => <div key={index} className={`turn turn-${message.role || "tool"}`}>
      <span>{ROLE[String(message.role || message.type)] || String(message.role || message.type || "")}</span><div className="turn-text"><p>{contentText(message)}</p></div></div>)}</div>
    <h4>输出</h4><div className="message-box">{span.response ? responseText(span.response) || "（无文本输出）" : "—"}</div>
    <details><summary>原始 JSON</summary><pre>{JSON.stringify({ request: span.request, response: span.response }, null, 2)}</pre></details>
  </div>;
}

function Waterfall({ index, spans }: { index: SpanIndex[]; spans: Span[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const start = Math.min(...index.map((span) => span.started_at_ms));
  const end = Math.max(...index.map((span) => span.started_at_ms + (span.elapsed_ms || 0)));
  const total = Math.max(end - start, 1);
  return <div className="waterfall">{index.map((span) => {
    const body = spans.find((item) => item.span_id === span.span_id);
    return <div key={span.span_id} className="span-row">
      <button type="button" className={`span-head ${open === span.span_id ? "selected" : ""}`} disabled={!body}
        onClick={() => setOpen(open === span.span_id ? null : span.span_id)}>
        <span className="span-seq">#{span.seq}</span>
        <span className="span-model">{span.model || span.upstream_model || "未知模型"}{span.upstream_model && span.upstream_model !== span.model ? ` → ${span.upstream_model}` : ""}</span>
        <span className="span-bar"><i style={{ left: `${(span.started_at_ms - start) / total * 100}%`,
          width: `${Math.max((span.elapsed_ms || 0) / total * 100, 1)}%` }} /></span>
        <span className="span-meta">{span.elapsed_ms != null ? `${(span.elapsed_ms / 1000).toFixed(1)}s` : "—"} · {span.prompt_tokens ?? "—"}/{span.completion_tokens ?? "—"} tok{span.has_body ? "" : " · 正文不完整"}</span>
      </button>
      {open === span.span_id && body && <SpanBody span={body} />}
    </div>;
  })}</div>;
}

export function CallTrace({ runId, jobId }: { runId: string; jobId: string }) {
  const trace = useRemote(async () => ({
    jobId, body: await api<TurnTrace>(`/experiments/${runId}/results/${jobId}/trace`),
  }), [runId, jobId]);
  const data = trace.data?.jobId === jobId ? trace.data.body : null;
  useEffect(() => {
    if (data?.status !== "pending") return;
    const timer = window.setInterval(trace.reload, 15000);
    return () => window.clearInterval(timer);
  }, [runId, jobId, data?.status]);
  if (!data) return <p className="muted">{trace.loading ? "正在加载模型调用链路…" : trace.error || "暂无模型调用链路"}</p>;
  const index = data.summary || [];
  const sum = (pick: (span: SpanIndex) => number | null) => index.reduce((total, span) => total + (pick(span) || 0), 0);
  return <div className="call-trace">
    <div className="trace-head"><span><strong>{TRACE_STATUS[data.status]}</strong>
      {index.length > 0 && ` · ${index.length} 次调用 · 输入 ${sum((span) => span.prompt_tokens)} / 输出 ${sum((span) => span.completion_tokens)} tokens`}
      {data.expected_calls != null && ` · Agent 日志记录 ${data.expected_calls} 次`}</span>
      {!!data.spans?.length && <Button size="sm" variant="light" startContent={<Download size={14} />}
        onPress={() => downloadFile(JSON.stringify({ request_id: data.request_id, spans: data.spans }, null, 2),
          `trace-${data.request_id}.json`, "application/json")}>导出本轮链路</Button>}</div>
    <p className="muted">回放请求 ID <span className="mono">{data.request_id}</span>
      {data.echo_verified === false && " · 网关回写值与发送值不一致"}{data.error_code ? ` · ${data.error_code}` : ""}</p>
    {data.expected_calls == null && <p className="hint trace-hint">本轮调用总数未知；已采集记录不证明所有调用均已收齐。</p>}
    {TRACE_HINT[data.status] && <p className="hint trace-hint">{TRACE_HINT[data.status]}</p>}
    {data.object_purged_at && <p className="hint trace-hint">完整请求与响应已按 60 天保留期清理，仅保留调用索引。</p>}
    {!!index.length && <Waterfall index={index} spans={data.spans || []} />}
  </div>;
}
