import { Chip, Modal, ModalBody, ModalContent, ModalHeader } from "@heroui/react";
import { useState } from "react";
import {
  api, base, isRunning, type ChatMessage, type EvalResult, type GoldenAttachment, type GoldenTurn, type JudgeMetric, type MetricStatus,
  type TurnObservation, type TurnVerdict,
} from "../api";
import { Notice, useRemote } from "../App";
import { CallTrace } from "./CallTrace";
import { Images } from "./Context";

// 一轮的展示状态：执行、判分与聚合结论合在一起看
export type TurnRow = {
  key: string; session_id: string; seq: number; repeat_index: number; golden?: GoldenTurn;
  execution: EvalResult; judgment?: EvalResult; verdict?: TurnVerdict; state: string;
};
export type MetricInfo = { name: string; display_index: number | null };

export const TURN_STATE: Record<string, [string, "success" | "danger" | "warning" | "default"]> = {
  pass: ["回答达标", "success"], fail: ["回答未达标", "danger"], unavailable: ["无法判断", "warning"],
  execution_failed: ["执行失败", "danger"], delivery_unknown: ["投递未知", "danger"],
  judge_failed: ["判分失败", "warning"], judging: ["判分中", "default"], running: ["执行中", "default"],
  cancelled: ["已取消", "default"], not_scored: ["未评分", "default"],
};
const METRIC_STATUS: Record<MetricStatus, string> = { pass: "通过", fail: "未通过", unavailable: "无法判断" };
const STAGE: Record<string, string> = {
  planned: "未开始", created: "已建测试联系人", routing_verified: "路由已校验",
  response_observed: "已收到回复", profile_settled: "回复已收到，画像回读已结束",
};
const RECENT = 4;
const IMAGE_FILENAME = /^[^/\\\s]+\.(?:png|jpe?g|webp|gif|avif|bmp)$/i;

function isImage(attachment: GoldenAttachment, content: string) {
  return attachment.content_type?.startsWith("image/") || ((!attachment.content_type
    || attachment.content_type === "application/octet-stream")
    && (IMAGE_FILENAME.test(attachment.name) || IMAGE_FILENAME.test(content.trim())));
}

export function StateChip({ state }: { state: string }) {
  const [text, color] = TURN_STATE[state] || [state, "default"];
  return <Chip size="sm" variant="flat" color={color}>{text}</Chip>;
}

export function Messages({ messages, mediaBase, side, start = 0 }: {
  messages: { content: string; attachments?: GoldenAttachment[] | null }[];
  mediaBase: string; side: "context" | "candidate" | "reference"; start?: number;
}) {
  return <>{messages.map((message, index) => <div key={index}>
    {message.content && !((IMAGE_FILENAME.test(message.content.trim()) || message.content.startsWith("图片附件："))
      && message.attachments?.some((attachment) => isImage(attachment, message.content))) && <p>{message.content}</p>}
    <Images label={side === "reference" ? "参考回复图片" : side === "candidate" ? "实际回复图片" : "对话图片"}
      urls={(message.attachments || []).flatMap((attachment, attachmentIndex) =>
        isImage(attachment, message.content)
          ? [`${mediaBase}/${side}/${start + index}/${attachmentIndex}`] : [])} />
    {(message.attachments || []).filter((attachment) => !isImage(attachment, message.content))
      .map((attachment, attachmentIndex) => <small key={attachmentIndex} className="block muted">附件：{attachment.name}</small>)}
  </div>)}</>;
}

const shown = (value: unknown) => value === undefined ? "未记录" : value === null || value === "" ? "空"
  : typeof value === "object" ? JSON.stringify(value) : String(value);

const profileValue = (snapshot: Record<string, unknown> | null | undefined, key: string) =>
  snapshot == null ? "未采集" : key in snapshot ? shown(snapshot[key]) : "未设置";

function Conversation({ messages, mediaBase }: { messages: ChatMessage[]; mediaBase: string }) {
  // 末尾连续的客户消息是本轮输入，之前是冻结的历史（含人工回复）
  let split = messages.length;
  while (split > 0 && messages[split - 1].role === "user") split -= 1;
  const history = messages.slice(0, split);
  const [all, setAll] = useState(false);
  const visible = all ? history : history.slice(-RECENT);
  const hidden = history.length - visible.length;
  return <div className="context-block">
    {!!history.length && <div className="context-row"><strong>此前对话{hidden ? ` · 最近 ${visible.length} 条` : ""}</strong>
      {hidden > 0 && <button type="button" className="link-button" onClick={() => setAll(true)}>展开更早的 {hidden} 条</button>}
      <div className="turn-list">{visible.map((message, index) => <div key={index} className={`turn turn-${message.role}`}>
        <span>{message.role === "user" ? "客户" : message.role === "assistant" ? "助手" : message.role}</span>
        <div className="turn-text"><Messages messages={[message]} mediaBase={mediaBase} side="context"
          start={history.length - visible.length + index} /></div></div>)}</div></div>}
    <div className="context-row"><strong>本轮客户输入</strong>
      <div className="message-box current-turn">{messages.length > split
        ? <Messages messages={messages.slice(split)} mediaBase={mediaBase} side="context" start={split} /> : "—"}</div></div>
    {messages.some((message) => message.attachments?.some((attachment) => isImage(attachment, message.content)))
      && <small className="block muted">当前评审模型只读文字，未查看图片内容；涉及图片的判断以人工复核为准。</small>}
  </div>;
}

function MetricTable({ metrics, names, reference }: {
  metrics: JudgeMetric[]; names: Map<string, MetricInfo>; reference: (ref: string, metric: JudgeMetric, check?: JudgeMetric["checks"][number]) => React.ReactNode;
}) {
  return <div className="table-scroll"><table className="metric-table"><thead><tr><th>指标</th><th>结论</th><th>判定依据</th></tr></thead>
    <tbody>{metrics.map((metric) => {
      const info = names.get(metric.metric_id);
      return <tr key={metric.metric_id}>
        <td><strong>{info?.name || metric.metric_id}</strong>{info?.display_index != null && <small className="metric-index">{info.display_index}</small>}</td>
        <td><span className={`metric-${metric.status}`}>{METRIC_STATUS[metric.status]}</span>
          {metric.overridden && <small className="block muted">人工改判</small>}</td>
        <td>{metric.reason.length > 280 ? <><p className="metric-reason">{metric.reason.slice(0, 140)}…</p>
            <details className="metric-reason-fold"><summary>展开完整评分说明（{metric.reason.length} 字）</summary>
              <p className="metric-reason">{metric.reason}</p></details></> : <p className="metric-reason">{metric.reason}</p>}
          {metric.ratio && <small className="block muted">已覆盖要点 {metric.ratio.numerator} / {metric.ratio.denominator}</small>}
          {!!metric.checks.length && <ul className="check-list">{metric.checks.map((check) => <li key={check.check_id}>
            <span className={`metric-${check.status}`}>{METRIC_STATUS[check.status]}</span> {check.reason}
            {!!check.evidence_refs.length && <div className="evidence-links">{check.evidence_refs.map((ref) => reference(ref, metric, check))}</div>}</li>)}</ul>}
          {!!metric.evidence_refs.length && <div className="evidence-links"><small>查看引用与判据</small>{metric.evidence_refs.map((ref) => reference(ref, metric))}</div>}</td>
      </tr>;
    })}</tbody></table></div>;
}

function echo(observation: TurnObservation) {
  if (!observation.replay_request_id_echoed) return "网关未回写";
  return observation.replay_request_id_echoed === observation.replay_request_id ? "网关回写一致"
    : `网关回写为 ${observation.replay_request_id_echoed}`;
}

function routingLabel(observation: TurnObservation) {
  if (!observation.routing_verified) return "未确认一致";
  return observation.routing_evidence?.channel_type === "openApi"
    ? "OpenAPI 默认渠道，与来源一致" : "与预期接待账号一致";
}

function Evidence({ observation, golden, labels }: {
  observation: TurnObservation; golden?: GoldenTurn; labels: Record<string, string>;
}) {
  const [allFields, setAllFields] = useState(false);
  const snapshots = [observation.profile_before, observation.profile_observed_before,
    observation.profile_immediate_after, observation.profile_after];
  const expected = golden?.expected.profile_update || {};
  const writes = Object.keys(observation.profile_writes);
  const allKeys = [...new Set([...snapshots.flatMap((item) => Object.keys(item || {})), ...Object.keys(expected), ...writes])];
  const relevant = allKeys.filter((key) => writes.includes(key) || key in expected);
  const keys = allFields ? allKeys : relevant;
  const diagnostics = observation.tool_diagnostics;
  const readback = observation.profile_readback;
  const logTypes: Record<string, string> = {
    knowledge: "知识检索", language: "语言识别", agent: "主智能体", task: "技能执行",
    modelCall: "模型调用", internalTool: "内部工具",
  };
  const logStatus: Record<string, string> = { certain: "已确定", success: "成功", succeeded: "成功", failed: "失败" };
  const sources: Record<string, string> = { pulled: "历史日志采集", ai: "AI 补全", manual: "人工填写" };
  return <div className="context-folds">
    <div className="execution-summary">
      <div><small>请求执行</small><strong>{observation.error_code ? "执行异常" : STAGE[observation.stage_reached] || observation.stage_reached}</strong>
        <span>{(observation.elapsed_ms / 1000).toFixed(1)} 秒 · {observation.response_messages.length} 条回复</span></div>
      <div><small>本轮画像变化</small><strong>{observation.profile_observed_before == null || observation.profile_after == null ? "未取得完整前后回读" : writes.length ? `${writes.length} 个字段发生变化` : "未观察到字段变化"}</strong>
        <span>{readback.settled === true ? "末两次读取相同；异步任务是否完成仍未确认" : readback.settled === false ? "回读尚未稳定，画像判断需复核" : "未记录回读稳定性"}</span></div>
    </div>
    {observation.error_code && <Notice error={`${observation.error_code}${observation.error_detail ? `：${observation.error_detail}` : ""}`} />}
    <details open={relevant.length > 0}><summary>画像更新对照 · 回读变化 {writes.length} 项 · 参考标注 {Object.keys(expected).length} 项</summary>
      <p className="hint">实际变化来自执行前后回读{readback.timeout_seconds != null ? `，本轮观察窗口为 ${readback.timeout_seconds} 秒` : ""}。末两次读值相同不代表异步任务已完成；未确认完成时，缺失更新按无法判定处理，已观察到的值仍依据字段说明和技能规则判断。旧报告保留原判分口径。</p>
      {keys.length ? <div className="table-scroll"><table className="profile-table"><thead><tr><th>画像字段</th><th>执行前</th>
        <th>执行后{readback.settled === false ? "（未稳定）" : ""}</th><th>参考标注</th><th>来源</th></tr></thead>
        <tbody>{keys.map((key) => <tr key={key} className={writes.includes(key) ? "changed" : undefined}>
          <td title={key}>{labels[key] || key}</td><td>{profileValue(observation.profile_observed_before, key)}</td>
          <td>{profileValue(observation.profile_after, key)}</td><td>{key in expected ? shown(expected[key]?.value) : "参考未标注"}</td>
          <td><small>{writes.includes(key) ? "实际：执行前后回读差异" : "实际：未观察到变化"}
            {key in expected && <span className="block">参考：{sources[expected[key].source || ""] || "未记录来源"}</span>}</small></td>
        </tr>)}</tbody></table></div> : <p className="muted">本轮没有观察到画像变化，参考也未标注更新字段。</p>}
      {!!allKeys.length && <button type="button" className="link-button" onClick={() => setAllFields(!allFields)}>
        {allFields ? "仅看发生变化或有参考标注的字段" : `查看全部 ${allKeys.length} 个字段`}</button>}
    </details>
    <details><summary>执行路径对照 · {diagnostics ? diagnostics.name_set_match ? "观察到的工具名称相同" : "执行路径有差异" : "暂无对照"} · 不参与评分</summary>
      <p className="hint">这里只比较日志中出现的工具名称，不比较调用顺序、参数或返回值。路径不同不直接代表业务失败；日志未观察到也不等于一定没有执行。</p>
      {diagnostics && <dl className="kv">
        <div><dt>参考中出现</dt><dd>{diagnostics.expected_tool_names.join("、") || "未记录"}</dd></div>
        <div><dt>本次观察到</dt><dd>{diagnostics.actual_tool_names.join("、") || "未记录"}</dd></div>
        {!!diagnostics.missing_tool_names.length && <div><dt>本次未观察到</dt><dd>{diagnostics.missing_tool_names.join("、")}</dd></div>}
        {!!diagnostics.unexpected_tool_names.length && <div><dt>本次另外出现</dt><dd>{diagnostics.unexpected_tool_names.join("、")}</dd></div>}
      </dl>}
    </details>
    <details><summary>技术排查详情 · 请求关联、画像快照与运行日志</summary>
      <dl className="kv">
        <div><dt>测试联系人路由</dt><dd>{routingLabel(observation)}</dd></div>
        <div><dt>请求方式</dt><dd>{observation.chat_transport === "sync" ? "同步等待回复" : observation.chat_transport}</dd></div>
        {readback.attempts != null && <div><dt>画像读取次数</dt><dd>{readback.attempts} 次</dd></div>}
        {observation.replay_request_id && <div><dt>回放 requestId</dt><dd><span className="mono">{observation.replay_request_id}</span> · {echo(observation)}</dd></div>}
      </dl>
      {!!allKeys.length && <details><summary>完整画像快照（排查预置与回读时序）</summary>
        <div className="table-scroll"><table><thead><tr><th>字段</th><th>预置值</th><th>执行前回读</th><th>即时回读</th><th>末次回读</th></tr></thead>
          <tbody>{allKeys.map((key) => <tr key={key}><td title={key}>{labels[key] || key}</td>
            {snapshots.map((item, index) => <td key={index}>{profileValue(item, key)}</td>)}</tr>)}</tbody></table></div></details>}
      <h4>运行日志 · {observation.agent_logs.length} 条</h4>
      {!!observation.agent_logs.length && <div className="table-scroll"><table><thead><tr><th>执行动作</th><th>名称</th><th>日志状态</th><th>耗时</th><th>摘要</th></tr></thead>
        <tbody>{observation.agent_logs.map((log, index) => <tr key={index}><td title={log.type}>{logTypes[log.type] || log.type}</td><td>{log.name === "mainagent" ? "主智能体" : log.name || "未记录"}</td>
          <td title={log.status || undefined}>{logStatus[log.status || ""] || log.status || "未记录"}</td>
          <td>{log.cost_time_ms != null ? `${(log.cost_time_ms / 1000).toFixed(2)} 秒` : "未记录"}</td>
          <td className="log-message">{log.message || "未记录摘要"}</td></tr>)}</tbody></table></div>}
      <p className="hint">日志字段为空代表未记录，不能据此认定执行失败。模型完整输入输出见下方“模型调用链路”。</p>
    </details>
  </div>;
}

function evidenceTarget(ref: string, row: TurnRow, context: ChatMessage[], labels: Record<string, string>) {
  const prefix = `${row.execution.case_id}#`;
  const suffix = ref.startsWith(prefix) ? ref.slice(prefix.length) : "";
  if (/^(actual|customer):[1-9]\d*$/.test(suffix)) {
    const [kind, number] = suffix.split(":");
    const index = Number(number) - 1;
    const role = context[index]?.role;
    let split = context.length;
    while (split > 0 && context[split - 1].role === "user") split -= 1;
    return { kind, index, label: kind === "actual" ? `实际回复 ${number}`
      : index >= split && index < context.length ? `本轮客户消息 ${index - split + 1}`
        : `历史消息 ${number} · ${role === "user" ? "客户" : role === "assistant" ? "助手" : "消息"}` };
  }
  if (suffix.startsWith("profile:")) {
    const field = suffix.slice(8);
    return { kind: "profile", field, label: `画像 · ${labels[field] || field}` };
  }
  if (suffix === "execution") return { kind: suffix, label: "请求执行结果" };
  if (suffix === "routing") return { kind: suffix, label: "测试联系人绑定" };
  return { kind: "unknown", label: "其他证据" };
}

export function TurnDetail({ runId, row, metrics, labels, fixture, sessionName }: {
  runId: string; row: TurnRow; metrics: Map<string, MetricInfo>; labels: Record<string, string>; fixture: boolean;
  sessionName?: string;
}) {
  const result = row.execution.result as { context?: ChatMessage[]; candidate?: ChatMessage[] } | null;
  const judgeEvidence = row.judgment?.result?.judge_evidence as {
    status: string; tool_results_count: number; image_pixels_available: false; truncated: boolean;
    trace?: { trace_status?: string; call_count_status?: string } | null;
  } | undefined;
  const workspacePolicy = row.judgment?.result?.workspace_policy as {
    captured_at: string; config_sha256: string; workspace_id: string; execution_reused: boolean;
  } | undefined;
  const verdict = row.verdict?.status === "judged" ? row.verdict : undefined;
  const passed = verdict?.metrics.filter((metric) => metric.status === "pass").length || 0;
  const decided = verdict?.metrics.filter((metric) => metric.status !== "unavailable").length || 0;
  const failed = verdict?.failed_metric_ids.map((id) => metrics.get(id)?.name || id) || [];
  const finished = !isRunning(row.execution.status);
  const mediaBase = `${base}/experiments/${runId}/results/${row.execution.id}/media`;
  const evidence = useRemote(async () => ({ jobId: row.execution.id,
    body: finished ? await api<{ observation: TurnObservation }>(`/experiments/${runId}/results/${row.execution.id}/evidence`) : null,
  }), [runId, row.execution.id, finished]);
  const observation = evidence.data?.jobId === row.execution.id ? evidence.data.body?.observation : null;
  const [selection, setSelection] = useState<{ ref: string; metric: JudgeMetric; check?: JudgeMetric["checks"][number] } | null>(null);
  const selectedRef = selection?.ref;
  const scoringRubric = row.judgment?.result?.judge_rubric as {
    source: string; rubric: string; rubric_sha256: string; prompt_version?: string;
  } | undefined;
  const rubric = scoringRubric?.rubric || row.golden?.rubric;
  const sections = selection ? ({ "2": ["画像"], "3": ["画像"], "4": ["必须覆盖"], "5": ["必须覆盖"],
    "6": ["禁止"], "8": ["主导动作"], "9": ["主导动作", "禁止"] } as Record<string, string[]>)[selection.metric.metric_id] || [] : [];
  const requirements = rubric?.split("\n").filter((line) => sections.some((section) => line.startsWith(`【${section}】`)));
  const context = observation?.request_messages || result?.context || [];
  const target = selectedRef ? evidenceTarget(selectedRef, row, context, labels) : null;
  const citedMessages = target?.kind === "actual" ? observation?.response_messages || result?.candidate : context;
  const cited = target?.index != null ? citedMessages?.[target.index] : null;
  const reference = (ref: string, metric: JudgeMetric, check?: JudgeMetric["checks"][number]) => <button type="button" key={ref}
    className="evidence-link" aria-label={`${metrics.get(metric.metric_id)?.name || metric.metric_id}：查看${evidenceTarget(ref, row, context, labels).label}`}
    onClick={() => setSelection({ ref, metric, check })}>
    {evidenceTarget(ref, row, context, labels).label}</button>;
  return <>
    <div className="panel-head"><h2 title={row.session_id}>{sessionName || row.session_id} · 第 {row.seq} 轮 · 第 {row.repeat_index} 次</h2><StateChip state={row.state} /></div>
    {verdict && <><p className="turn-summary">全部指标：{passed} / {decided} 项已判定通过{failed.length ? ` · 未通过：${failed.join("、")}` : ""}</p>
      <p className="hint">“回答达标”仅要求正确性、完整性、安全性（4 / 5 / 6）均通过；画像与引导指标独立判断。</p></>}
    {row.execution.error_code && <Notice error={`执行异常：${row.execution.error_code}`} />}
    {row.judgment?.error_code && <Notice error={`自动评分异常：${row.judgment.error_code}`} />}
    <div className="turn-compare">
      <div><h3>实际请求上下文</h3><Conversation key={row.key} messages={result?.context || []} mediaBase={mediaBase} /></div>
      <div><h3>实际回复</h3><div className="message-box">{result?.candidate?.length
        ? <Messages messages={result.candidate} mediaBase={mediaBase} side="candidate" /> : (finished ? "未收到回复" : "执行中")}</div>
        <h3>参考回复</h3><p className="hint">供核对业务意图，不是唯一正确话术。等价表达按评分标准判断；存疑内容仍需业务核实。</p><div className="message-box">{row.golden?.expected.messages.length
          ? <Messages messages={row.golden.expected.messages} mediaBase={mediaBase} side="reference" /> : "—"}</div></div>
    </div>
    {verdict && <><h3>逐项评分</h3>
      {workspacePolicy && <details><summary>评分业务规则：本 EVAL 空间配置</summary>
        <p className="hint">读取时间：{new Date(workspacePolicy.captured_at).toLocaleString("zh-CN", { hour12: false })}。
          {workspacePolicy.execution_reused ? "本次复核使用冻结回答与本次读取的配置，不能据此证明原执行时配置相同。"
            : "本次运行的各轮共用同一份指导、技能和字段说明。"}</p>
        <small className="mono">空间 {workspacePolicy.workspace_id} · 配置 {workspacePolicy.config_sha256}</small>
      </details>}
      {judgeEvidence && <p className="hint">{judgeEvidence.tool_results_count > 0
        ? `自动评分器已获得 ${judgeEvidence.tool_results_count} 条本轮工具返回` : "自动评分器未获得可核对的工具返回"}；图片内容未读取。
        {(judgeEvidence.status === "partial" || judgeEvidence.trace?.trace_status === "partial"
          || judgeEvidence.trace?.call_count_status === "partial") && " 工具证据不完整。"}
        {judgeEvidence.truncated && " 工具返回内容已截断。"}
        以上仅代表提供给自动评分器 的材料，不能据此推断工具没有调用。可展开下方“模型调用链路”核对来源。</p>}
      <p className="hint">点击“查看引用与判据”，对照评分说明、原始记录与本轮要求；引用存在不代表结论一定正确。</p>
      <MetricTable metrics={verdict.metrics} names={metrics} reference={reference} /></>}
    {scoringRubric && <details><summary>本次评分标准（Rubric） · {scoringRubric.source === "workspace_rules" ? "根据空间配置独立生成" : "采用已确认参考"}</summary>
      <p className="hint">{scoringRubric.source === "workspace_rules" ? "依据本轮客户输入、历史和空间规则生成，生成时不读取本轮实际回复及未确认参考；同一轮各次执行共用这一份标准。自动生成的标准仍需人工核实。" : "采用已明确标记为业务确认的参考标准。"}</p>
      <div className="basis"><p className="rubric-text">{scoringRubric.rubric}</p></div>
      <small className="mono">{scoringRubric.prompt_version} · {scoringRubric.rubric_sha256.slice(0, 16)}</small>
    </details>}
    {row.golden?.rubric && <details><summary>{scoringRubric ? "原始参考评分标准（保留原文）" : "原报告评分标准（Rubric）"}</summary><div className="basis"><p className="rubric-text">{row.golden.rubric}</p></div></details>}
    {finished && <><h3>执行结果与画像变化</h3>{observation ? <Evidence key={row.execution.id} observation={observation}
      golden={row.golden} labels={labels} /> : <p className="muted">{evidence.loading ? "正在加载执行记录…" : evidence.error || "暂无执行记录"}</p>}</>}
    {finished && !fixture && <details className="trace-details"><summary>模型调用链路 · 排查提示词与工具返回</summary>
      <CallTrace key={`trace-${row.execution.id}`} runId={runId} jobId={row.execution.id} /></details>}
    <Modal isOpen={!!selectedRef} onClose={() => setSelection(null)} size="2xl" scrollBehavior="inside">
      <ModalContent><ModalHeader>评分依据核对 · {metrics.get(selection?.metric.metric_id || "")?.name || "评分项"}</ModalHeader><ModalBody className="evidence-review">
        {selection && <><div className="evidence-verdict"><strong className={`metric-${selection.metric.status}`}>{METRIC_STATUS[selection.metric.status]}</strong>
          <span>{sessionName || row.session_id} · 第 {row.seq} 轮 · 第 {row.repeat_index} 次执行</span></div>
          <section><h4>{selection.check ? "该检查点的评分说明" : "自动评分说明"}</h4>
            <p className="metric-reason">{selection.check?.reason || selection.metric.reason}</p>
            {selection.check && <p className="hint">该检查点：{METRIC_STATUS[selection.check.status]}；所属评分项：{METRIC_STATUS[selection.metric.status]}。</p>}</section></>}
        <section><h4>引用记录 · {target?.label}</h4>
        {cited && <div className="message-box"><Messages messages={[cited]} mediaBase={mediaBase}
          side={target?.kind === "actual" ? "candidate" : "context"} start={target?.index} /></div>}
        {target?.kind === "profile" && <dl className="kv">
          <div><dt>执行前回读</dt><dd>{profileValue(observation?.profile_observed_before, target.field!)}</dd></div>
          <div><dt>执行后回读</dt><dd>{profileValue(observation?.profile_after, target.field!)}</dd></div>
          <div><dt>本轮记录的更新</dt><dd>{shown(observation?.profile_writes[target.field!]?.value)}</dd></div>
        </dl>}
        {target?.kind === "execution" && <p>{observation ? `${STAGE[observation.stage_reached] || observation.stage_reached}；${observation.response_messages.length} 条回复；${observation.error_code || "无执行错误记录"}` : "暂无执行证据"}</p>}
        {target?.kind === "routing" && <p>{observation ? `测试联系人路由：${routingLabel(observation)}。此项不证明技能、指导或工具返回一致。` : "暂无路由证据"}</p>}
        {(target?.kind === "unknown" || ((target?.kind === "actual" || target?.kind === "customer") && !cited)) && <p>当前结果中未找到该引用的可读内容，请查看原始引用并人工核对。</p>}
        {target?.kind === "profile" && !observation && <p className="hint">{evidence.loading ? "正在加载画像回读…" : evidence.error || "暂无画像回读"}</p>}
        </section>
        {target?.kind === "execution" && !fixture && <details><summary>查看本轮模型调用与工具返回</summary>
          <p className="hint">执行状态仅说明请求走到了哪一步，不能单独证明工具返回的业务事实。</p>
          <CallTrace runId={runId} jobId={row.execution.id} /></details>}
        {selection && selection.metric.evidence_refs.some((ref) => ref !== selectedRef) && <div className="evidence-links">
          <small>同项其他引用</small>{selection.metric.evidence_refs.filter((ref) => ref !== selectedRef)
            .map((ref) => reference(ref, selection.metric))}</div>}
        <section><h4>本轮评分要求</h4>{requirements?.length ? <div className="basis">
          {requirements.map((line, index) => <p className="rubric-text" key={index}>{line}</p>)}</div>
          : <p className="hint">该报告未保存可展示的对应判据，请结合空间规则人工复核。</p>}
          <p className="hint">{scoringRubric?.source === "workspace_rules" ? "来源：本次运行根据空间规则独立生成的评分标准。" : "来源：原报告采用的参考评分标准。"}以上是评分要求，不是空间原文引述。</p></section>
        <details><summary>技术定位信息</summary><p className="mono">{selectedRef}</p></details>
        <p className="hint">评分说明由 AI 生成。请检查引用是否支持结论、要求是否适用于本轮；有出入可关闭弹窗后在“人工复核”中记录。</p>
      </ModalBody></ModalContent>
    </Modal>
  </>;
}
