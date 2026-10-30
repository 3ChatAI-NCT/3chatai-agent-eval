import { stamp, submit, type OutputView, type PairResult, type Review, type Side } from "../api";
import { Notice, Status, useApp } from "../App";
import { RELATIVE, SIDE, errorText, reviewTarget, skillOf, type Row } from "../atomic";
import { ContextView } from "./Context";
import { OutputBlock, RuleChecks } from "./Outputs";
import { AiSummary } from "./AiReview";
import { Evidence } from "./Evidence";
import { ReviewBar, type Draft } from "./ReviewBar";

const CHOICES: [string, string][] = [["B", "基线更优"], ["S", "持平"], ["G", "候选更优"], ["U", "无法判断"]];

export function CaseDetail({ runId, row, finished, names, review, draft, onDraftChange, onSaved }: {
  runId: string; row: Row; finished: boolean; names: Record<Side, string>; review: Review | null;
  draft?: Draft; onDraftChange: (draft: Draft) => void;
  onSaved: (review: Review, advance: boolean) => void;
}) {
  const { execution, judgment, pair } = row;
  const context = execution.case;
  const outputs = execution.result as { baseline?: OutputView; candidate?: OutputView } | null;
  const { subject } = useApp();
  const target = reviewTarget(row);
  // 执行或评审失败、开发 Fixture 都只能标记无法判断
  const unavailableOnly = target.status !== "succeeded" || target.result?.status === "not_evaluated";
  return <div className="case-body">
    <div className="panel-head"><div><h3 className="case-title">{skillOf(row)}</h3>
      <small className="muted mono">生产请求 ID {execution.case_id}{context?.timestamp ? ` · ${stamp(context.timestamp)}` : ""}</small></div>
      {pair ? <span className={`verdict-chip verdict-text-${pair.verdict.relative}`}>{RELATIVE[pair.verdict.relative] || pair.verdict.relative}</span>
        : <Status value={judgment?.status || execution.status} />}</div>
    {execution.error_code && <Notice error={`候选第 ${execution.attempt} 次调用失败：${errorText(execution.error_code)}`} />}
    {judgment?.error_code && <Notice error={`第 ${judgment.attempt} 次盲评未完成：${errorText(judgment.error_code)}`} />}
    {context && <ContextView context={context} cited={citedRules(pair)} />}
    <div className="work-grid">{(["baseline", "candidate"] as Side[]).map((side) => <div key={side} className="output-card">
      <h4>{SIDE[side]} · {names[side]}</h4>
      <OutputBlock output={outputs?.[side]} context={context} />
      {!!pair?.rule_checks?.[side]?.length && <RuleChecks checks={pair.rule_checks[side]} />}
    </div>)}</div>
    {pair?.reviews ? <AiSummary pair={pair} />
      : pair && <Notice>该评测使用旧版评分方式，没有成对盲评记录；可点击「使用已有回答重新评分」按新方式评审。</Notice>}
    <Evidence runId={runId} jobId={execution.id} />
    <ReviewBar review={review} finished={finished} colored
      choices={unavailableOnly ? [["unavailable", "无法判断"]] : CHOICES}
      draft={draft} onDraftChange={onDraftChange}
      submitDraft={async (value) => (await submit<{ revision: number }>(`/experiments/${runId}/results/${target.id}/review`, {
        ...value, outputs_sha256: target.result_sha256, actor_type: "human",
        revision: target.review_revision ?? review?.revision ?? 0 })).revision}
      onSaved={(saved, advance) => onSaved({ ...saved, actor_type: "human", subject, outputs_sha256: target.result_sha256 }, advance)} />
  </div>;
}

// 评审在请求中引用的原文，用来在业务规则里高亮
function citedRules(pair?: PairResult) {
  return [...new Set((pair?.reviews || []).flatMap((review) => review.issues)
    .flatMap((issue) => issue.citations).filter((citation) => citation.pointer.startsWith("/request"))
    .map((citation) => citation.quote.trim()).filter((quote) => quote.length >= 4))];
}
