import { Button, Progress, Textarea } from "@heroui/react";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  isFinished, isRunning, messageOf, modeName, stamp, submit, type Dataset, type EvalResult, type Experiment,
  type GoldenQuality, type GoldenTurn, type Report, type TargetModel, type TurnVerdict,
} from "./api";
import { Empty, Notice, PageHead, Status } from "./App";
import { RUNS, SECTION_NAME } from "./Sections";
import { modelLabel } from "./SpaceModels";
import { TRACE_STATUS } from "./report/CallTrace";
import { EvidenceBundle } from "./report/EvidenceBundle";
import { RepeatComparison } from "./report/RepeatComparison";
import { Reliability } from "./report/Reliability";
import { RunControls } from "./report/RunControls";
import { StateChip, TurnDetail, TURN_STATE, type MetricInfo, type TurnRow } from "./report/TurnDetail";

type GoldenAttempt = GoldenQuality["sessions"][number]["attempts"][number];
const pct = (value: number | null | undefined) => value == null ? "—" : `${Math.round(value * 100)}%`;
const REVIEW_ISSUES = [["agent_issue", "智能体表现问题"], ["judge_error", "自动评分偏差"],
  ["gold_error", "参考答案或评分依据问题"], ["environment_issue", "回放环境差异"]];

function stateOf(execution: EvalResult, judgment: EvalResult | undefined, verdict: TurnVerdict | undefined,
  fixture: boolean) {
  if (isRunning(execution.status)) return "running";
  if (execution.status === "failed") return "execution_failed";
  if (execution.status === "uncertain") return "delivery_unknown";
  if (execution.status === "cancelled") return "cancelled";
  if (fixture) return "not_scored";
  if (verdict?.status === "judged") return verdict.turn_status;
  return !judgment || isRunning(judgment.status) ? "judging" : "judge_failed";
}

export function GoldenReport({ runId, run, report, results, dataset, reload }: {
  runId: string; run: Experiment; report: Report; results: EvalResult[]; dataset: Dataset | null; reload: () => void;
}) {
  const quality = report.quality;
  const [view, setView] = useState<"overview" | "turns">("overview");
  const sessionNames = useMemo(() => new Map((dataset?.content?.items || []).map((session, index) => {
    const messages = session.turns.flatMap((turn) => turn.input.customer_messages);
    const text = messages.find((message) => message.content && !message.content.startsWith("[系统提醒")
      && !message.content.startsWith("图片附件：") && !/联系人验证|我们可以开始聊天|请求添加/.test(message.content))?.content
      || (messages.some((message) => message.attachments?.length) ? "图片或附件咨询" : "首次接待");
    return [session.session_id, `会话 ${index + 1} · ${text.replace(/\s+/g, " ").slice(0, 36) || "图片或附件咨询"}`];
  })), [dataset]);
  const metrics = useMemo(() => new Map<string, MetricInfo>((quality?.metric_summaries || [])
    .map((metric) => [metric.metric_id, metric])), [quality]);
  // 画像字段的中文名取数据集里冻结的字段字典
  const labels = useMemo(() => Object.fromEntries((((dataset?.content?.shared as { context?: {
    profile_schema?: { key: string; label?: string }[] } } | undefined)?.context?.profile_schema) || [])
    .map((field) => [field.key, field.label || field.key])), [dataset]);
  const attempts = useMemo(() => new Map<string, GoldenAttempt>((quality?.sessions || []).flatMap((session) => session.attempts
    .map((attempt) => [`${session.session_id}:${attempt.repeat_index}`, attempt] as const))), [quality]);
  const rows = useMemo(() => {
    const golden = new Map<string, { session_id: string; turn: GoldenTurn }>((dataset?.content?.items || []).flatMap((session) => session.turns
      .map((turn) => [turn.turn_id, { session_id: session.session_id, turn }] as const)));
    const verdicts = new Map<string, TurnVerdict>([...attempts.values()].flatMap((attempt) => attempt.turns
      .map((turn) => [`${turn.turn_id}:${turn.repeat_index}`, turn] as const)));
    const judgments = new Map<string, EvalResult>(results.filter((row) => row.stage === "judge")
      .map((row) => [`${row.case_id}:${row.repeat_index}`, row] as const));
    return results.filter((row) => row.stage === "execute").map((execution): TurnRow => {
      const key = `${execution.case_id}:${execution.repeat_index}`;
      const found = golden.get(execution.case_id);
      const judgment = judgments.get(key);
      const verdict = verdicts.get(key);
      return { key, session_id: found?.session_id || execution.case_id.split("#")[0], seq: found?.turn.seq || 0,
        repeat_index: execution.repeat_index, golden: found?.turn, execution, judgment, verdict,
        state: stateOf(execution, judgment, verdict, report.fixture) };
    }).sort((a, b) => a.session_id.localeCompare(b.session_id) || a.repeat_index - b.repeat_index || a.seq - b.seq);
  }, [results, dataset, attempts, report.fixture]);

  const [sessionFilter, setSessionFilter] = useState("");
  const [repeatFilter, setRepeatFilter] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [metricFilter, setMetricFilter] = useState("");
  const [metricState, setMetricState] = useState("fail");
  const filtered = rows.filter((row) => (!sessionFilter || row.session_id === sessionFilter)
    && (!repeatFilter || String(row.repeat_index) === repeatFilter) && (!stateFilter || row.state === stateFilter)
    && (!metricFilter || row.verdict?.metrics.some((metric) => metric.metric_id === metricFilter && metric.status === metricState)));
  const sessionOrder = new Map([...sessionNames.keys()].map((id, index) => [id, index]));
  const groups = [...new Set(filtered.map((row) => row.session_id))].map((sessionId) => ({
    sessionId, turns: [...new Set(filtered.filter((row) => row.session_id === sessionId).map((row) => row.execution.case_id))]
      .map((turnId) => ({ turnId, rows: filtered.filter((row) => row.session_id === sessionId && row.execution.case_id === turnId) }))
      .sort((a, b) => a.rows[0].seq - b.rows[0].seq),
  })).sort((a, b) => (sessionOrder.get(a.sessionId) ?? Infinity) - (sessionOrder.get(b.sessionId) ?? Infinity));
  const [selected, setSelected] = useState("");
  // 默认打开第一条未通过的轮次，方便直接定位问题
  const current = filtered.find((row) => row.key === selected)
    || filtered.find((row) => row.state === "fail" || row.state === "execution_failed") || filtered[0];

  const currentAttempts = rows.filter((row) => row.execution.case_id === current?.execution.case_id);
  function selectAttempt(key: string) {
    setRepeatFilter(""); setStateFilter(""); setMetricFilter(""); setSelected(key);
  }
  function focusMetric(id: string, status: string) {
    setMetricFilter(id); setMetricState(status); setSessionFilter(""); setRepeatFilter("");
    setStateFilter(""); setSelected(""); setView("turns");
  }

  const [label, setLabel] = useState("");
  const [issue, setIssue] = useState("none");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const reviewTarget = current?.judgment || current?.execution;
  const ownReview = reviewTarget?.review;
  const finished = isFinished(run.status);
  const nextUnreviewed = filtered.find((row) => row.key !== current?.key
    && ![row.judgment?.review, row.execution.review].some((review) => review && review.actor_type !== "ai_assisted"));
  const humanLabels = report.human_review?.labels || {};
  const humanIssues = report.human_review?.issue_kinds || {};
  const assisted = report.assisted_review;
  const humanReviewedKeys = new Set((report.review_items || []).filter((item) => item.actor_type !== "ai_assisted")
    .map((item) => `${item.case_id}:${item.repeat_index}`));
  const assistedPending = (report.review_items || []).filter((item) => item.actor_type === "ai_assisted"
    && !humanReviewedKeys.has(`${item.case_id}:${item.repeat_index}`)).length;
  const issueItems = (report.review_items || []).filter((item) => item.issue_kind !== "none");
  const issueNames = Object.fromEntries(REVIEW_ISSUES);
  useEffect(() => {
    if (!report.trace?.statuses.pending) return;
    const timer = window.setInterval(reload, 15000);
    return () => window.clearInterval(timer);
  }, [runId, report.trace?.statuses.pending]);
  useEffect(() => {
    setLabel(ownReview?.label || "unavailable");
    setIssue(ownReview?.issue_kind || "none");
    setNote(ownReview?.note || "");
  }, [reviewTarget?.id, ownReview?.revision]);

  async function act(action: () => Promise<unknown>, done = "") {
    setBusy(true); setError(""); setMessage("");
    try { await action(); setMessage(done); reload(); }
    catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  const saveReview = () => reviewTarget && act(() => submit(`/experiments/${runId}/results/${reviewTarget.id}/review`, {
    label, issue_kind: issue, note, outputs_sha256: reviewTarget.result_sha256, actor_type: "human",
    revision: reviewTarget.review_revision ?? ownReview?.revision ?? 0,
  }), "当前执行结果的人工意见已保存；自动判定原文仍保留。");
  const distribution = Object.entries(quality?.first_failure_turn_distribution || {})
    .sort(([a], [b]) => Number(a) - Number(b));
  const target = run.manifest?.target_model as TargetModel | undefined;
  const readback = (run.manifest?.config as { profile_readback?: {
    min_wait_seconds: number; stable_seconds?: number; timeout_seconds: number;
  } } | undefined)?.profile_readback;

  return <>
    <Link className="back" to={RUNS.golden}><ArrowLeft size={15} /> {SECTION_NAME.golden}</Link>
    <PageHead title={run.name} description={`${modeName(run.mode)} · ${dataset?.name || ""} · ${stamp(run.created_at)}${report.scope === "selected_turns" ? " · 指定轮次" : ""}`}
      action={<div className="head-actions"><Status value={run.status} />
        <EvidenceBundle run={run} />
        <Button size="sm" variant="light" isIconOnly aria-label="刷新报告" onPress={reload}><RefreshCw size={15} /></Button></div>} />
    {finished && <RunControls runId={runId} run={run} report={report} reload={reload} />}
    <Notice error={error}>{message}</Notice>
    {readback?.stable_seconds && <p className="hint">画像观察：至少 {readback.min_wait_seconds} 秒，连续稳定 {readback.stable_seconds} 秒可提前结束，最长 {readback.timeout_seconds} 秒。观测稳定不代表已确认所有异步任务完成。</p>}
    {report.source_experiment_id && <Notice>本报告包含复用的回答和执行证据；下方执行次数包含这些已有记录。
      <Link to={`/evaluations/${report.source_experiment_id}`}>查看原执行报告</Link></Notice>}
    <div className="stat-grid report-stats">
      <div className="stat-card"><strong>{report.executed} / {report.planned}</strong><span>执行完成</span></div>
      <div className="stat-card"><strong>{report.judged} / {report.executed}</strong><span>自动判分</span></div>
      <div className="stat-card"><strong>{report.reviewed} / {report.review_required}</strong><span>人工复核</span></div>
      <div className="stat-card"><strong>{report.uncertain + report.execution_failed + report.judge_failed}</strong><span>异常结果</span></div>
    </div>
    {report.progress && <p className="hint" role="status">
      正在回放 {report.progress.executing} 条 · 正在评分 {report.progress.judging} 条
      {" · "}等待执行 {report.progress.waiting} 条
      {!!report.progress.excluded && ` · 已隔离 ${report.progress.excluded} 条（未执行）`}
    </p>}
    {!!run.execution_records?.length && <details className="hint">
      <summary>执行记录（{run.execution_records.length}）</summary>
      {run.execution_records.map((record, index) => <p key={record.id}>
        <Link to={`/evaluations/${record.id}`}>执行记录 {index + 1}</Link>
        {" · "}{stamp(record.created_at)} · <Status value={record.status} />
      </p>)}
    </details>}
    <nav className="segmented report-sections" aria-label="报告分区">
      <button type="button" className={view === "overview" ? "selected" : ""} aria-pressed={view === "overview"}
        onClick={() => setView("overview")}>评分总览</button>
      <button type="button" className={view === "turns" ? "selected" : ""} aria-pressed={view === "turns"}
        onClick={() => setView("turns")}>逐轮对照与复核 · {new Set(rows.map((row) => row.execution.case_id)).size} 轮 / {rows.length} 次执行</button>
    </nav>
    {view === "overview" && <>
    <section className="panel"><div className="panel-head"><h2>质量概览</h2><span className="muted">复核状态 <Status value={run.review_status} /></span></div>
      <p className="hint">已判定通过率只计算通过与未通过；可判定覆盖率仅统计已评分记录，不含尚未取得评分结果的执行，执行与判分进度见页头。无法判断不算通过或失败。所有评分项均为越高越好。画像两项暂共用同一判断，兼容项不代表另一次独立检查，也不是统计学召回率或精确率。</p>
      {report.fixture && <Notice>开发 Fixture 只校验执行链路，不产生质量分数。</Notice>}
      {!!report.execution_warnings?.length && <Notice>{report.execution_warnings.join("；")}</Notice>}
      {quality ? <>
        <div className="metric-grid">{quality.metric_summaries.map((metric) => {
          const decided = metric.pass_count + metric.fail_count;
          const total = decided + metric.unavailable_count;
          return <div key={metric.metric_id}>
          <span>{metric.name}{metric.display_index != null && <small className="metric-index">{metric.display_index}</small>}</span>
          <div className="metric-rates"><div><strong>{pct(metric.pass_rate)}</strong><small className="block muted">已判定通过率</small></div>
            <div><strong>{pct(total ? decided / total : null)}</strong><small className="block muted">已评分中可判定 {decided} / {total}</small></div></div>
          <small className="block muted">通过 {metric.pass_count} · 未通过 {metric.fail_count} · 无法判断 {metric.unavailable_count}
            {metric.mean_ratio != null ? ` · 均值 ${pct(metric.mean_ratio)}` : ""}</small>
          <div className="metric-drilldowns">{metric.fail_count > 0 && <button className="link-button metric-drilldown" type="button"
            onClick={() => focusMetric(metric.metric_id, "fail")}>查看 {metric.fail_count} 条未通过</button>}
          {metric.unavailable_count > 0 && <button className="link-button metric-drilldown" type="button"
            onClick={() => focusMetric(metric.metric_id, "unavailable")}>查看 {metric.unavailable_count} 条无法判断</button>}</div></div>;
        })}</div>
        {!!distribution.length && <p className="muted">首次失败轮次：{distribution.map(([turn, count]) => `第 ${turn} 轮 ${count} 次`).join("，")}</p>}
        {!!quality.quality_warnings.length && <Notice>{quality.quality_warnings.join("；")}</Notice>}
      </> : !report.fixture && <p className="muted">判分完成后在这里展示逐项指标与重复执行稳定性。</p>}
      {!!report.trace?.turns && <p className="muted">模型调用链路：登记 {report.trace.turns} 轮 · 已采集 {report.trace.collected} 轮
        {report.trace.avg_calls != null && ` · 平均每轮 ${report.trace.avg_calls} 次调用 · 输入 ${report.trace.avg_prompt_tokens} / 输出 ${report.trace.avg_completion_tokens} tokens`}
        {" · "}{Object.entries(report.trace.statuses).map(([status, count]) => `${TRACE_STATUS[status as keyof typeof TRACE_STATUS]} ${count}`).join("，")}</p>}
      {!!report.trace?.statuses.summary_only && <Notice>有 {report.trace.statuses.summary_only} 轮只采到调用摘要：该 EVAL 空间未开启完整请求体日志，模型调用链路没有输入输出。请在阿里云侧开启后再回放。</Notice>}
      <Progress className="report-progress" value={report.review_required ? report.reviewed / report.review_required * 100 : 0}
        size="sm" aria-label="人工复核进度" />
      <div className="report-actions"><Button color="primary" isLoading={busy}
        isDisabled={!finished || report.reviewed !== report.review_required || run.review_status === "completed"}
        onPress={() => void act(() => submit(`/experiments/${runId}/review/complete`, {}), "报告复核已完成；质量问题仍保留在逐条意见中。")}>提交报告复核结论</Button>
        <Button variant="flat" onPress={() => setView("turns")}>查看逐轮对照与复核</Button>
        {finished && <><Button as={Link} to={`/evaluations/new?reuse=${runId}`} variant="flat">使用已有回答重新评分</Button>
          <small className="muted">仅重新评分，不重放对话</small></>}
        {!finished && <Button variant="light" color="danger" isLoading={busy}
          onPress={() => void act(() => submit(`/experiments/${runId}/cancel`, {}))}>取消评测</Button>}</div>
      <details className="report-provenance"><summary>评分版本与归档</summary>
      {target && <p className="hint">执行模型：{target.error ? `创建时未能读取（${target.error}）`
        : `${modelLabel(target.model)}（Agent 请求 ${target.from_model} 时由网关替换，创建评测时冻结）`}。实际调用见各轮模型调用链路。</p>}
      <p className="hint">自动评分提示词 {report.judge_prompt_version} · SHA256 {report.judge_prompt_sha256.slice(0, 16)}… · 模型 {report.judge_model || "未配置"}。人工结论与自动判断分别留痕。</p>
      <p className="hint">报告归档：{report.artifact ? `${report.artifact.status} · v${report.artifact.revision}` : "评测结束后生成"}
        {report.artifact?.status === "succeeded" && <> · <a href={`${import.meta.env.BASE_URL}api/agent-eval/v1/experiments/${runId}/report/artifact`} target="_blank" rel="noreferrer">查看 OSS 归档投影</a></>}</p>
      </details>
    </section>
    <section className="panel golden-review-summary"><div className="panel-head"><h2>复核结论与问题归因</h2>
      <span className="muted">人工已复核 {report.reviewed} / {report.review_required}</span></div>
      <div className="human-summary-grid"><div><span>人工通过 / 未通过 / 无法判断</span>
        <strong>{humanLabels.pass || 0} / {humanLabels.fail || 0} / {humanLabels.unavailable || 0}</strong></div>
        <div><span>待人工复核</span><strong>{Math.max(0, report.review_required - report.reviewed)}</strong></div>
        <div><span>AI 辅助意见（不计人工）</span><strong>{assisted?.count || 0}</strong>
          <small className="muted">其中 {assistedPending} 条待人工确认</small></div></div>
      <p className="hint">自动原分保持不变。人工结论仅代表已复核结果；AI 辅助意见不计入人工进度，也不代表业务负责人已确认。</p>
      <p className="muted">人工问题归因：{REVIEW_ISSUES.filter(([key]) => humanIssues[key])
        .map(([key, name]) => `${name} ${humanIssues[key]} 条`).join("；") || "尚未记录"}</p>
      {!!assisted?.count && <p className="muted">AI 辅助标记：{REVIEW_ISSUES.filter(([key]) => assisted.issue_kinds[key])
        .map(([key, name]) => `${name} ${assisted.issue_kinds[key]} 条`).join("；") || "未标记问题"}（辅助意见）</p>}
      {!!issueItems.length && <details><summary>查看 {issueItems.length} 条问题意见与依据</summary>
        <ul className="review-issue-list">{issueItems.map((item) => <li key={`${item.case_id}:${item.repeat_index}:${item.subject}:${item.actor_type || "human"}`}>
          <button type="button" className="link-button" onClick={() => {
            setSessionFilter(""); selectAttempt(`${item.case_id}:${item.repeat_index}`); setView("turns");
          }}>{sessionNames.get(rows.find((row) => row.execution.case_id === item.case_id)?.session_id || "") || "会话"}
            {` · 第 ${rows.find((row) => row.execution.case_id === item.case_id)?.seq || "—"} 轮 · 第 ${item.repeat_index} 次`}</button>
          <small className="block muted">{item.actor_type === "ai_assisted"
            ? humanReviewedKeys.has(`${item.case_id}:${item.repeat_index}`) ? "AI 辅助 · 已有人工意见" : "AI 辅助 · 待人工确认"
            : "人工意见"} · {issueNames[item.issue_kind] || item.issue_kind} · {item.subject}</small>
          <p>{item.note}</p></li>)}</ul></details>}
    </section>
    {quality && <Reliability quality={quality} repeats={report.repeats} selectedTurns={report.scope === "selected_turns"}
      names={sessionNames} onSession={(id) => {
        setSessionFilter(id); setRepeatFilter(""); setMetricFilter(""); setStateFilter(""); setSelected(""); setView("turns");
      }} />}
    </>}
    {view === "turns" && <>
    <div className="filters report-filters">
      <select className="field-select" aria-label="会话" value={sessionFilter} onChange={(event) => setSessionFilter(event.target.value)}>
        <option value="">全部会话</option>{[...new Set(rows.map((row) => row.session_id))].map((id) => <option key={id} value={id}>{sessionNames.get(id) || id}</option>)}</select>
      {report.repeats > 1 && <select className="field-select" aria-label="执行次数" value={repeatFilter} onChange={(event) => setRepeatFilter(event.target.value)}>
        <option value="">全部执行次数</option>{Array.from({ length: report.repeats }, (_, index) =>
          <option key={index} value={index + 1}>第 {index + 1} 次</option>)}</select>}
      <select className="field-select" aria-label="轮次状态" value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}>
        <option value="">全部状态</option>{Object.entries(TURN_STATE).filter(([state]) => rows.some((row) => row.state === state))
          .map(([state, [text]]) => <option key={state} value={state}>{text}</option>)}</select>
      {quality && <><select className="field-select" aria-label="评分指标" value={metricFilter} onChange={(event) => setMetricFilter(event.target.value)}>
        <option value="">全部指标</option>{quality.metric_summaries.map((metric) =>
          <option key={metric.metric_id} value={metric.metric_id}>{metric.name}</option>)}</select>
        <select className="field-select" aria-label="指标判定状态" value={metricState} disabled={!metricFilter} onChange={(event) => setMetricState(event.target.value)}>
          <option value="fail">指标未通过</option><option value="unavailable">指标无法判断</option><option value="pass">指标通过</option></select></>}
      <span className="muted">{filtered.length} / {rows.length} 次执行</span>
      {(sessionFilter || repeatFilter || stateFilter || metricFilter) && <button type="button" className="link-button" onClick={() => {
        setSessionFilter(""); setRepeatFilter(""); setStateFilter(""); setMetricFilter("");
      }}>清除筛选</button>}
    </div>
    <div className="review-layout result-layout"><aside className="case-nav"><h2>会话与轮次</h2>
      {groups.map(({ sessionId, turns }) => <div key={sessionId} className="nav-group">
        <div className="nav-group-head"><span title={sessionId}>{sessionNames.get(sessionId) || sessionId}</span>
          <small>{turns.length} 个所评轮次</small></div>
        {turns.map(({ turnId, rows: items }) => <div key={turnId} className={`nav-turn ${current?.execution.case_id === turnId ? "active" : ""}`}>
          <strong>第 {items[0].seq} 轮 <small>· {items.length} 次执行</small></strong>
          {items.map((row) => <button key={row.key} type="button" aria-pressed={current?.key === row.key}
            className={`case-button ${current?.key === row.key ? "selected" : ""}`} onClick={() => setSelected(row.key)}>
            <span>第 {row.repeat_index} 次 <i className={`attempt-dot ${row.state}`} />{TURN_STATE[row.state]?.[0] || row.state}</span>
            {(row.judgment?.review || row.execution.review) && <small>{(row.judgment?.review || row.execution.review)?.actor_type === "ai_assisted" ? "AI 辅助待确认" : "人工已复核"}</small>}</button>)}
        </div>)}
      </div>)}
      {!filtered.length && <p className="muted">{rows.length ? "没有符合筛选条件的轮次。" : "任务尚未生成。"}</p>}</aside>
      <section className="case-body">{current ? <>
        <RepeatComparison runId={runId} rows={currentAttempts} current={current} metrics={metrics} reused={!!report.source_experiment_id} onSelect={selectAttempt} />
        <TurnDetail key={current.key} runId={runId} row={current} metrics={metrics} labels={labels} fixture={report.fixture}
          sessionName={sessionNames.get(current.session_id)} /></>
        : <Empty>选择左侧轮次查看执行与评分证据。</Empty>}</section>
      <aside className="case-context"><h2>人工复核</h2>
        {!current || !reviewTarget ? <p className="muted">选择一个执行结果。</p> : <>
          <p className="muted">当前：{sessionNames.get(current.session_id) || current.session_id} · 第 {current.seq} 轮 · 第 {current.repeat_index} 次 <StateChip state={current.state} /></p>
          {nextUnreviewed && <Button size="sm" variant="flat" onPress={() => setSelected(nextUnreviewed.key)}>下一条待人工复核</Button>}
          <div className="form-stack"><label>人工结论<select className="field-select" value={label} onChange={(event) => setLabel(event.target.value)}>
            <option value="pass">通过</option><option value="fail">未通过</option>
            <option value="unavailable">证据不足 / 无法判断</option></select></label>
            <label>问题类型<select className="field-select" value={issue} onChange={(event) => setIssue(event.target.value)}>
              <option value="none">无问题</option><option value="agent_issue">智能体表现问题</option>
              <option value="judge_error">自动评分偏差</option><option value="gold_error">参考答案或评分依据问题</option>
              <option value="environment_issue">回放环境差异</option></select></label>
            <Textarea label="复核依据" description={issue !== "none" ? "发现问题时必填；说明证据与处理建议。" : "可选，记录业务判断依据。"}
              value={note} onValueChange={setNote} minRows={4} />
            <Button color="primary" isLoading={busy} isDisabled={!finished || !["succeeded", "failed", "uncertain"].includes(reviewTarget.status)
              || (issue !== "none" && !note.trim())} onPress={() => void saveReview()}>保存当前复核</Button>
            {ownReview && <p className="hint">{ownReview.actor_type === "ai_assisted" ? "AI 辅助意见，仍待人工确认。" : "人工意见。"}
              已由 {ownReview.subject} 提交第 {ownReview.revision} 版意见；修改会保留历史。</p>}
            {issue === "gold_error" && <p className="hint">提交后从数据集页面派生修订草稿。旧报告保留原依据，新版本重新执行或使用已有回答重新评分。</p>}
          </div>
        </>}</aside>
    </div>
    </>}
  </>;
}
