import { type PairResult, type PairReview, type Side } from "../api";
import {
  CONFIDENCE, DIMENSION, GRADE, SEVERITY, SIDE, TASK, citationSource, mergeIssues, type MergedIssue,
} from "../atomic";

const winnerText = (winner: string) => winner === "tie" ? "持平" : winner === "insufficient_evidence" ? "证据不足"
  : `${SIDE[winner as Side] || winner}更优`;

// 两次评审合并展示：先给结论与依据，问题去重后按严重度排列，全文折叠备查
export function AiSummary({ pair }: { pair: PairResult }) {
  const reviews = pair.reviews || [];
  const issues = mergeIssues(reviews);
  const first = reviews[0];
  return <div className="ai-summary">
    <div className="panel-head"><h4>AI 评审</h4><span className="muted">{pair.verdict.reason}</span></div>
    {first && <p className="muted">本轮应做：{first.expected_action}</p>}
    {reviews.length === 2 && pair.verdict.agreement === false
      ? <div className="split-reviews">{reviews.map((review, index) => <p key={index}><strong>{index === 0 ? "首次" : "交换顺序后"}：{winnerText(review.winner)}</strong>{review.rationale}</p>)}</div>
      : first && <p>{first.rationale}</p>}
    {issues.length ? <div className="issue-groups">{(["candidate", "baseline"] as Side[]).map((side) => {
      const own = issues.filter((item) => item.issue.side === side);
      return own.length ? <div key={side}><h5>{SIDE[side]}的问题 · {own.length}</h5>
        {own.map((item, index) => <IssueBlock key={index} item={item} reviews={reviews.length} />)}</div> : null;
    })}</div> : <p className="muted">两次评审均未指出成立的问题。</p>}
    <details><summary>两次评审全文：维度评分、引用、证据局限</summary>
      <div className="review-pair">{reviews.map((review, index) =>
        <ReviewCard key={index} review={review} title={reviews.length === 1 ? "合规检查（双方输出一致）" : index === 0 ? "首次盲评" : "交换顺序盲评"} />)}</div></details>
  </div>;
}

function IssueBlock({ item, reviews }: { item: MergedIssue; reviews: number }) {
  const { issue, order } = item;
  return <details className="issue"><summary><span className={`severity-${issue.severity}`}>{SEVERITY[issue.severity]}</span>
    {DIMENSION[issue.dimension] || issue.dimension}：{issue.description}
    {reviews > 1 && item.found > 1 && <em className="both-found">两次评审均指出</em>}</summary>
    {issue.citations.map((citation, index) => <p key={index} className="citation"><code>{citationSource(citation.pointer, order)}</code>「{citation.quote}」</p>)}</details>;
}

function ReviewCard({ review, title }: { review: PairReview; title: string }) {
  return <div className="review-card">
    <div className="panel-head"><h4>{title}</h4><span className="muted">A 为{SIDE[review.order.A]}，B 为{SIDE[review.order.B]}</span></div>
    <p><strong>{winnerText(review.winner)}</strong> · {CONFIDENCE[review.confidence] || review.confidence} · {TASK[review.task_type] || review.task_type}</p>
    <p>{review.rationale}</p>
    <table className="compact-table"><thead><tr><th>维度</th><th>基线</th><th>候选</th><th>判断</th></tr></thead><tbody>
      {review.dimensions.map((item) => <tr key={item.dimension} title={item.note}><td>{DIMENSION[item.dimension] || item.dimension}</td>
        <td className={`grade-${item.baseline}`}>{GRADE[item.baseline || ""] || item.baseline}</td>
        <td className={`grade-${item.candidate}`}>{GRADE[item.candidate || ""] || item.candidate}</td>
        <td>{item.verdict === "tie" ? "持平" : GRADE[item.verdict] || `${SIDE[item.verdict as Side] || item.verdict}更好`}</td></tr>)}</tbody></table>
    {!!review.contract_conflicts.length && <p className="hint">规则冲突：{review.contract_conflicts.join("；")}</p>}
    {!!review.limitations.length && <p className="hint">证据局限：{review.limitations.join("；")}</p>}
    {review.rejected_issues > 0 && <p className="hint">另有 {review.rejected_issues} 条问题因引用无法在原文找到而被作废。</p>}
  </div>;
}
