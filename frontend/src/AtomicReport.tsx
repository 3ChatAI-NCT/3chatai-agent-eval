import { Button, Tooltip } from "@heroui/react";
import { ArrowLeft, Download, Info, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, isFinished, isRunning, messageOf, stamp, submit, type EvalResult, type Experiment, type PairResult, type Report, type Review,
  type Side, type Space,
} from "./api";
import { Notice, PageHead, Status, useRemote } from "./App";
import { judgeStability, reviewOf, type Row } from "./atomic";
import { ExportDialog } from "./Export";
import { EvidenceBundle } from "./report/EvidenceBundle";
import { BlindSummary, HumanReviewSummary, Overview } from "./report/Overview";
import { CaseExplorer } from "./report/CaseExplorer";
import { ProfileFields } from "./report/profileFields";
import { RunControls, abnormalCount } from "./report/RunControls";
import { ResponseFormatTable } from "./report/ResponseFormat";

export function AtomicReport({ runId, run, report, space, reload }: {
  runId: string; run: Experiment; report: Report; space: Space | null; reload: () => void;
}) {
  const resultsResource = useRemote(async () => ({ runId,
    rows: await api<EvalResult[]>(`/experiments/${runId}/results`) }), [runId]);
  const results = resultsResource.data?.runId === runId ? resultsResource.data.rows : null;
  const reloadAll = () => { reload(); resultsResource.reload(); };
  useEffect(() => {
    if (!isRunning(run.status)) return;
    const timer = window.setInterval(resultsResource.reload, 5000);
    return () => window.clearInterval(timer);
  }, [run.status, runId]);
  const summary = report.atomic;
  const rows: Row[] = useMemo(() => (results || []).filter((item) => item.stage === "execute").map((execution) => {
    const judgment = results?.find((item) => item.stage === "judge" && item.case_id === execution.case_id
      && item.repeat_index === execution.repeat_index);
    const pair = judgment?.status === "succeeded" ? judgment.result as unknown as PairResult : undefined;
    return { execution, judgment, pair };
  }), [results]);
  // 提交后先更新本地复核，报告再从服务端重新读取聚合结果
  const [reviews, setReviews] = useState<Record<string, Review>>({});
  const finished = isFinished(run.status);
  const [finalizing, setFinalizing] = useState(false);
  const [focusFailure, setFocusFailure] = useState(0);
  const [focusSplit, setFocusSplit] = useState(0);
  const [focusReview, setFocusReview] = useState<{ id: string } | null>(null);
  const [focusFormat, setFocusFormat] = useState<{ side: Side } | null>(null);
  const [error, setError] = useState("");
  const [exporting, setExporting] = useState(false);
  async function finalizeReview() {
    setFinalizing(true); setError("");
    try { await submit(`/experiments/${runId}/review/complete`, {}); reloadAll(); }
    catch (cause) { setError(messageOf(cause)); }
    finally { setFinalizing(false); }
  }
  const names: Record<Side, string> = {
    candidate: report.candidate ? `${report.candidate.model} · ${report.candidate.preset || report.candidate.host}` : "候选模型",
    baseline: rows.find((row) => row.execution.case?.upstream_model)?.execution.case?.upstream_model || "基线模型",
  };
  const reviewed = rows.filter((row) => reviewOf(row, reviews)).length;
  const abnormal = abnormalCount(report);
  const stability = summary && judgeStability(summary);
  // 开启盲评的评测只要求抽中的样本复核完，由服务端校验
  const blindStats = report.blind && "random" in report.blind ? report.blind : null;
  const focus = (caseId: string, repeatIndex: number) => {
    const row = rows.find((item) => item.execution.case_id === caseId && item.execution.repeat_index === repeatIndex);
    if (row) setFocusReview({ id: row.execution.id });
  };
  const statusText = run.display_status === "partial"
    ? `部分完成 · ${report.execution_failed === abnormal ? `${abnormal} 条未评测` : `${abnormal} 条异常`}`
    : `评测异常 · ${abnormal} 条`;
  const status = ["failed", "uncertain"].includes(run.status) && abnormal > 0
    ? <Button size="sm" variant="flat" color="warning" onPress={() => setFocusFailure((value) => value + 1)}>
        {statusText}</Button> : <Status value={run.display_status || run.status} />;
  return <>
    <Link className="back" to="/atomic/runs"><ArrowLeft size={15} /> 单步回放</Link>
    <PageHead title={run.name}
      description={`原子请求回放 · ${space ? `${space.name}${space.source_name ? ` · ${space.source_name}` : ""} · ` : ""}${stamp(run.created_at)}`}
      action={<div className="head-actions">{status}
        <Button size="sm" variant="flat" startContent={<Download size={14} />} isDisabled={!summary || !results} onPress={() => setExporting(true)}>导出报告</Button>
        <EvidenceBundle run={run} />
        <Button size="sm" variant="light" isIconOnly aria-label="刷新报告" onPress={reloadAll}><RefreshCw size={15} /></Button></div>} />
    <Notice error={error || resultsResource.error} />
    <p className="versus"><strong>{names.candidate}</strong><span>对比</span><strong>{names.baseline}（基线）</strong></p>
    <div className="stat-grid report-stats atomic-stats">
      <div className="stat-card"><strong>{report.planned}</strong><span>生产请求样本</span></div>
      <div className="stat-card"><strong>{report.executed} / {report.planned}</strong><span>候选完整响应</span></div>
      <div className="stat-card"><strong>{report.judged} / {report.executed}</strong><span>AI 已盲评</span></div>
      {stability?.compared ? <Tooltip content={<p className="stat-help">{stability.help}</p>} placement="bottom" delay={200}>
        <button type="button" className="stat-card stat-button" disabled={!stability.split} onClick={() => setFocusSplit((value) => value + 1)}>
          <strong>{stability.rate}</strong><span>AI 评审稳定性 <Info size={12} className="inline-icon" /></span>
          <small>交换 A/B 后结论一致 {stability.agreed}/{stability.compared}{stability.split ? <> · <b>{stability.split} 条分歧待核对 →</b></> : ""}</small>
        </button></Tooltip>
        : <div className="stat-card"><strong>—</strong><span>AI 评审稳定性</span></div>}
      <div className="stat-card"><strong>{results ? `${reviewed} / ${rows.length}` : "加载中…"}</strong><span>我已复核</span></div>
    </div>
    {report.fixture && <Notice>开发 Fixture 只校验执行链路，不产生质量结论。</Notice>}
    {!!report.execution_warnings?.length && <Notice>{report.execution_warnings.join("；")}</Notice>}
    {summary?.response_format && <ResponseFormatTable rows={(["candidate", "baseline"] as Side[]).map((side) => ({
      name: side === "candidate" ? "候选" : "基线", side, counts: summary.response_format![side],
    }))} onFocus={(side) => setFocusFormat({ side })} />}
    {summary && <Overview summary={summary} />}
    {blindStats && <BlindSummary stats={blindStats} onFocus={focus} />}
    <HumanReviewSummary report={report} onFocus={focus} />
    <div className="report-actions">
      {["completed", "failed", "uncertain"].includes(run.status) && rows.length > 0 &&
        <Button variant="flat" color="primary" isLoading={finalizing}
        isDisabled={(!blindStats && reviewed !== rows.length) || report.review_status === "completed"}
        onPress={() => void finalizeReview()}>完成复核并保存报告</Button>}
      {finished && <><Button as={Link} to={`/evaluations/new?reuse=${runId}`} variant="flat">使用已有回答重新评分</Button>
        <small className="muted">只重跑 Judge，不重跑 Agent</small></>}
      <RunControls runId={runId} run={run} report={report} reload={reloadAll} />
    </div>
    <p className="hint">评审方式：{report.judge_prompt_version} · SHA256 {report.judge_prompt_sha256.slice(0, 16)}… · Judge 模型 {report.judge_model}。
      每条样本交换 A/B 顺序独立盲评两次，模型名已从请求中移除；两次一致才计入结论，不一致列为评审分歧。
      {report.artifact?.status === "succeeded" && <> 报告归档 v{report.artifact.revision} · <a href={`${import.meta.env.BASE_URL}api/agent-eval/v1/experiments/${runId}/report/artifact`} target="_blank" rel="noreferrer">查看 OSS 归档投影</a></>}
      {report.review_status === "in_progress" && " 人工复核实时统计已更新；全部复核并完成后，OSS 报告才包含最终人工结论。"}</p>
    {results ? <ProfileFields.Provider value={run.profile_fields || {}}>
      <CaseExplorer runId={runId} rows={rows} finished={finished} names={names} reviews={reviews}
        focusFailure={focusFailure} focusSplit={focusSplit} focusReview={focusReview} focusFormat={focusFormat}
        onSaved={(id, review) => { setReviews((current) => ({ ...current, [id]: review })); reloadAll(); }} />
    </ProfileFields.Provider> : <section className="panel"><h2>逐条核对</h2>
      <p className="muted">{resultsResource.error ? "逐条结果加载失败，请刷新报告重试。" : "正在加载逐条结果…"}</p>
    </section>}
    {exporting && summary && <ExportDialog run={run} report={report} summary={summary} rows={rows} space={space}
      names={names} reviews={reviews} onClose={() => setExporting(false)} />}
  </>;
}
