import { Button, Textarea } from "@heroui/react";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  messageOf, stamp, submit, type BlindItem, type BlindSample, type Experiment, type Report, type ReviewConfig, type Space,
} from "../api";
import { Empty, Notice, PageHead, Status } from "../App";
import { ISSUE_KIND, skillName } from "../atomic";
import { ContextView } from "./Context";
import { Evidence } from "./Evidence";
import { OutputBlock } from "./Outputs";
import { ProfileFields } from "./profileFields";
import { ReviewBar, type Draft, type SavedDraft } from "./ReviewBar";
import { RunControls } from "./RunControls";
import { shortcutsBlocked } from "./shortcuts";

const CHOICES: [string, string][] = [["A", "A 更优"], ["S", "持平"], ["B", "B 更优"], ["U", "无法判断"]];
// 盲评时看不到 AI 评审，不能归因于评审错误
const ISSUE_KINDS = ISSUE_KIND.filter(([key]) => key !== "judge_error");

// 揭晓前的报告页：只呈现抽中的样本，A/B 顺序按样本随机，AI 结论与双方身份由服务端隐去
export function BlindReviewPage({ runId, run, report, sample, space, reload }: {
  runId: string; run: Experiment; report: Report; sample: BlindSample; space: Space | null; reload: () => void;
}) {
  const config = run.manifest?.blind_review as Omit<ReviewConfig, "blind_review"> | undefined;
  const items = sample.items || [];
  const [selected, setSelected] = useState("");
  const [saved, setSaved] = useState<Record<string, SavedDraft>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [notice, setNotice] = useState("");
  const reviewOf = (item: BlindItem): SavedDraft | null => saved[item.job_id]
    || (item.review && { label: item.review.choice, issue_kind: item.review.issue_kind, note: item.review.note, revision: item.revision });
  // 他人已盲评的样本也算完成，揭晓时取最新一条
  const done = (item: BlindItem) => !!reviewOf(item) || item.reviewed;
  const doneCount = items.filter(done).length;
  const current = items.find((item) => item.job_id === selected) || items[0];
  const index = current ? items.indexOf(current) : -1;

  function nextPending(from: BlindItem, finished: Record<string, SavedDraft>) {
    const at = items.indexOf(from);
    const next = [...items.slice(at + 1), ...items.slice(0, at)]
      .find((item) => !finished[item.job_id] && !item.review && !item.reviewed);
    if (next) setSelected(next.job_id);
    else setNotice("全部样本已盲评，可以提交并揭晓 AI 结论。");
  }
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (shortcutsBlocked(event.target) || event.metaKey || event.ctrlKey || event.altKey || !current) return;
      if (event.key === "j" && items[index + 1]) setSelected(items[index + 1].job_id);
      else if (event.key === "k" && items[index - 1]) setSelected(items[index - 1].job_id);
      else if (event.key === "n") nextPending(current, saved);
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return <>
    <Link className="back" to="/atomic/runs"><ArrowLeft size={15} /> 单步回放</Link>
    <PageHead title={run.name}
      description={`原子请求回放 · ${space ? `${space.name}${space.source_name ? ` · ${space.source_name}` : ""} · ` : ""}${stamp(run.created_at)} · 人工盲评`}
      action={<div className="head-actions"><Status value={run.display_status || run.status} />
        <Button size="sm" variant="light" isIconOnly aria-label="刷新" onPress={reload}><RefreshCw size={15} /></Button></div>} />
    <Notice>人工盲评中：揭晓前所有人都看不到 AI 评审结论、候选模型与 Judge 信息。
      {config && ` 评测结束后按样本量抽取 ${config.random_percent}%（至少 ${config.min_random} 条）随机样本，另加 ${config.risk_percent}% 风险样本，混在一起呈现。`}
      全部评完并揭晓后，报告展示 AI 结论与盲评一致率。</Notice>
    <div className="stat-grid report-stats atomic-stats">
      <div className="stat-card"><strong>{report.planned}</strong><span>生产请求样本</span></div>
      <div className="stat-card"><strong>{report.executed} / {report.planned}</strong><span>候选完整响应</span></div>
      <div className="stat-card"><strong>{report.judged} / {report.executed}</strong><span>AI 已评审</span></div>
      <div className="stat-card"><strong>{items.length ? `${doneCount} / ${items.length}` : "—"}</strong><span>已盲评样本</span></div>
    </div>
    {sample.status === "blind_waiting"
      ? <section className="panel"><Empty>评测结束后抽取盲评样本。可以先离开，稍后从工作台或评测记录回来。</Empty></section>
      : <ProfileFields.Provider value={run.profile_fields || {}}>
        <section className="panel case-explorer"><div className="panel-head"><h2>盲评样本</h2>
          <span className="muted">两侧输出的顺序按样本随机，与 AI 评审时的顺序无关</span></div>
          <div className="review-progress"><span>已盲评 {doneCount} / {items.length}</span>
            <div className="progress-track"><span style={{ width: `${items.length ? doneCount / items.length * 100 : 0}%` }} /></div>
            <span className="shortcut-hint"><kbd>1</kbd>–<kbd>4</kbd> 选择结论 · <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd> 保存并下一条 · <kbd>J</kbd>/<kbd>K</kbd> 上下条 · <kbd>N</kbd> 下一条未评</span></div>
          <Notice>{notice}</Notice>
          {items.length ? <div className="explorer-layout">
            <aside className="case-nav">{items.map((item, position) => <button key={item.job_id} type="button"
              className={`case-button ${current?.job_id === item.job_id ? "selected" : ""}`} onClick={() => setSelected(item.job_id)}>
              <span>样本 {position + 1} · {item.case.current_turn || item.case.task_input || (item.case.current_images.length ? "[图片]" : "")}</span>
              <small>{skillName(item.case)}
                {reviewOf(item) ? <b className="reviewed-mark"> · 已盲评</b> : item.reviewed && <b className="reviewed-mark"> · 他人已盲评</b>}
                {drafts[item.job_id] && <b className="draft-mark"> · 未提交草稿</b>}</small>
            </button>)}</aside>
            {current && <BlindCase key={current.job_id} runId={runId} item={current} position={index + 1}
              review={reviewOf(current)} draft={drafts[current.job_id]}
              onDraftChange={(draft) => setDrafts((all) => ({ ...all, [current.job_id]: draft }))}
              onSaved={(review, advance) => {
                const finished = { ...saved, [current.job_id]: review };
                setSaved(finished);
                setDrafts((all) => { const next = { ...all }; delete next[current.job_id]; return next; });
                if (advance) nextPending(current, finished);
              }} />}
          </div> : <Empty>没有可盲评的样本：候选与 AI 评审都成功的样本才会被抽取。可以直接揭晓。</Empty>}
        </section>
      </ProfileFields.Provider>}
    <div className="report-actions">
      {sample.status === "blind_open" && <Reveal runId={runId} missing={items.length - doneCount} onRevealed={reload} />}
      <RunControls runId={runId} run={run} report={report} reload={reload} />
    </div>
  </>;
}

function BlindCase({ runId, item, position, review, draft, onDraftChange, onSaved }: {
  runId: string; item: BlindItem; position: number; review: SavedDraft | null; draft?: Draft;
  onDraftChange: (draft: Draft) => void; onSaved: (review: SavedDraft, advance: boolean) => void;
}) {
  return <div className="case-body">
    <div className="panel-head"><div><h3 className="case-title">样本 {position} · {skillName(item.case)}</h3>
      {item.case.timestamp && <small className="muted">{stamp(item.case.timestamp)}</small>}</div></div>
    <ContextView context={item.case} cited={[]} />
    <div className="work-grid">{(["A", "B"] as const).map((side) => <div key={side} className="output-card">
      <h4>输出 {side}</h4><OutputBlock output={item.outputs[side]} context={item.case} /></div>)}</div>
    <Evidence runId={runId} jobId={item.job_id} requestOnly />
    {item.ready ? <ReviewBar review={review} finished colored={false} choices={CHOICES} issueKinds={ISSUE_KINDS}
      draft={draft} onDraftChange={onDraftChange} onSaved={onSaved}
      submitDraft={async (value) => (await submit<{ revision: number }>(`/experiments/${runId}/blind-review/items/${item.job_id}`, {
        choice: value.label, issue_kind: value.issue_kind, note: value.note, outputs_sha256: item.result_sha256,
        revision: review?.revision ?? item.revision })).revision} />
      : <Notice>这条样本正在补跑，完成后刷新页面再评。</Notice>}
  </div>;
}

// 全部评完直接揭晓；提前揭晓须写明原因，报告中单独标注
function Reveal({ runId, missing, onRevealed }: { runId: string; missing: number; onRevealed: () => void }) {
  const [early, setEarly] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function reveal() {
    setBusy(true); setError("");
    try { await submit(`/experiments/${runId}/blind-review/reveal`, { reason: missing ? reason : "" }); onRevealed(); }
    catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  if (!missing) return <Button color="primary" isLoading={busy} onPress={() => void reveal()}>提交盲评并揭晓 AI 结论</Button>;
  return <>
    <Button variant="flat" isDisabled>还有 {missing} 条未盲评</Button>
    {!early ? <Button variant="light" onPress={() => setEarly(true)}>提前揭晓…</Button>
      : <div className="reveal-early"><Textarea size="sm" minRows={1} label="提前揭晓的原因" value={reason} onValueChange={setReason}
          description="未评的样本不计入盲评统计，报告会标注提前揭晓" />
        <Button color="warning" variant="flat" isLoading={busy} isDisabled={!reason.trim()} onPress={() => void reveal()}>确认揭晓</Button></div>}
    {error && <small className="danger-text">{error}</small>}
  </>;
}
