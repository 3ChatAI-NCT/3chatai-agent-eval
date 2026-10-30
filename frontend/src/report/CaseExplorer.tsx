import { Switch } from "@heroui/react";
import { useEffect, useRef, useState } from "react";
import { type OutputView, type Review, type Side } from "../api";
import { Empty, Notice } from "../App";
import {
  HUMAN_LABEL, RELATIVE, RELATIVE_ORDER, isSerious, priority, reviewOf, reviewTarget, skillOf, type Row,
} from "../atomic";
import { shortcutsBlocked } from "./shortcuts";
import { CaseDetail } from "./CaseDetail";
import { Draft } from "./ReviewBar";

export function CaseExplorer({ runId, rows, finished, names, reviews, focusFailure, focusSplit, focusReview, focusFormat, onSaved }: {
  runId: string; rows: Row[]; finished: boolean; names: Record<Side, string>; reviews: Record<string, Review>;
  focusFailure: number; focusSplit: number; focusReview: { id: string } | null;
  focusFormat: { side: Side } | null;
  onSaved: (id: string, review: Review) => void;
}) {
  const [filter, setFilter] = useState({ relative: "", task: "", serious: false, review: "" });
  const [sort, setSort] = useState("priority");
  const [format, setFormat] = useState("");
  const invalid = (row: Row, side = format) => {
    const outputs = row.execution.result as Partial<Record<Side, OutputView>> | null;
    return (["candidate", "baseline"] as Side[]).some((key) => (!side || side === "either" || key === side)
      && outputs?.[key]?.format_check?.status === "invalid");
  };
  const [selected, setSelected] = useState("");
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  // 正在复核的这一条即使刚被标为已复核，也留在「未复核」列表里，避免编辑中途被切走
  const shown = rows.filter((row) =>
    (!filter.relative || (filter.relative === "pending" ? !row.pair : row.pair?.verdict.relative === filter.relative))
    && (!filter.task || skillOf(row) === filter.task)
    && (!filter.serious || isSerious(row.pair) || invalid(row, "either"))
    && (!format || invalid(row))
    && (!filter.review || row.execution.id === selected || (filter.review === "done") === !!reviewOf(row, reviews)));
  if (sort === "priority") shown.sort((a, b) => Number(invalid(b, "either")) - Number(invalid(a, "either")) || priority(a) - priority(b));
  const current = shown.find((row) => row.execution.id === selected) || shown[0];
  const tasks = [...new Set(rows.map(skillOf))];
  const done = rows.filter((row) => reviewOf(row, reviews)).length;
  const index = current ? shown.indexOf(current) : -1;
  const [notice, setNotice] = useState("");
  const listRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!focusFailure) return;
    setFormat("");
    const failed = rows.find((row) => ["failed", "uncertain"].includes(row.execution.status)
      || ["failed", "uncertain"].includes(row.judgment?.status || ""));
    if (!failed) return;
    setFilter({ relative: "pending", task: "", serious: false, review: "" });
    setSelected(failed.execution.id);
    document.getElementById("case-explorer")?.scrollIntoView({ behavior: "smooth" });
  }, [focusFailure]);
  // 从「AI 评审稳定性」进入：只看两次盲评结论不一致的评审分歧
  useEffect(() => {
    if (!focusSplit) return;
    setFormat("");
    setFilter({ relative: "D", task: "", serious: false, review: "" });
    setSelected(rows.find((row) => row.pair?.verdict.relative === "D")?.execution.id || "");
    document.getElementById("case-explorer")?.scrollIntoView({ behavior: "smooth" });
  }, [focusSplit]);
  useEffect(() => {
    if (!focusReview) return;
    setFormat("");
    setFilter({ relative: "", task: "", serious: false, review: "" });
    setSelected(focusReview.id);
    document.getElementById("case-explorer")?.scrollIntoView({ behavior: "smooth" });
  }, [focusReview]);
  useEffect(() => {
    if (!focusFormat) return;
    setFilter({ relative: "", task: "", serious: false, review: "" });
    setFormat(focusFormat.side);
    setSelected(rows.find((row) => invalid(row, focusFormat.side))?.execution.id || "");
    document.getElementById("case-explorer")?.scrollIntoView({ behavior: "smooth" });
  }, [focusFormat]);

  function move(step: number) {
    const next = shown[index + step];
    if (next) setSelected(next.execution.id);
  }
  function nextPending(from = current, done: Record<string, Review> = reviews) {
    const at = from ? shown.findIndex((row) => row.execution.id === from.execution.id) : -1;
    const order = [...shown.slice(at + 1), ...shown.slice(0, at)];
    const next = order.find((row) => !reviewOf(row, done) && row.execution.id !== from?.execution.id);
    if (next) setSelected(next.execution.id);
    else setNotice("当前筛选下的案例已全部复核。");
  }
  // 异步保存完成时，只在用户仍停留于原案例且明确选择“保存并下一条”时跳转。
  const latest = useRef({ current, reviews, nextPending });
  latest.current = { current, reviews, nextPending };
  function saved(row: Row, review: Review, advance: boolean) {
    const target = reviewTarget(row).id;
    onSaved(target, review);
    setDrafts((current) => { const next = { ...current }; delete next[target]; return next; });
    const now = latest.current;
    if (advance && now.current?.execution.id === row.execution.id) {
      now.nextPending(row, { ...now.reviews, [target]: review });
    }
  }
  useEffect(() => {
    if (current && current.execution.id !== selected) setSelected(current.execution.id);
  }, [current?.execution.id]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (shortcutsBlocked(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "j") move(1);
      else if (event.key === "k") move(-1);
      else if (event.key === "n") nextPending();
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  useEffect(() => {
    setNotice("");
    listRef.current?.querySelector(".case-button.selected")?.scrollIntoView({ block: "nearest" });
  }, [current?.execution.id]);

  return <section id="case-explorer" className="panel case-explorer"><div className="panel-head"><h2>逐条核对</h2>
    <div className="filters">
      <select className="field-select" aria-label="排序" value={sort} onChange={(e) => setSort(e.target.value)}>
        <option value="priority">复核优先：分歧与重要问题在前</option><option value="default">按采样顺序</option></select>
      <select className="field-select" aria-label="按结论筛选" value={filter.relative} onChange={(e) => setFilter({ ...filter, relative: e.target.value })}>
        <option value="">全部结论</option>{RELATIVE_ORDER.map((key) => <option key={key} value={key}>{RELATIVE[key]}</option>)}<option value="pending">尚无结论</option></select>
      <select className="field-select" aria-label="按技能筛选" value={filter.task} onChange={(e) => setFilter({ ...filter, task: e.target.value })}>
        <option value="">全部技能</option>{tasks.map((task) => <option key={task} value={task}>{task}</option>)}</select>
      <select className="field-select" aria-label="按人工状态筛选" value={filter.review} onChange={(e) => setFilter({ ...filter, review: e.target.value })}>
        <option value="">全部人工状态</option><option value="done">已复核</option><option value="todo">未复核</option></select>
      <select className="field-select" aria-label="按响应格式筛选" value={format} onChange={(e) => setFormat(e.target.value)}>
        <option value="">全部响应格式</option><option value="candidate">候选响应格式问题</option>
        <option value="baseline">基线响应格式问题</option><option value="either">任一方响应格式问题</option></select>
      <Switch size="sm" isSelected={filter.serious} onValueChange={(serious) => setFilter({ ...filter, serious })}>只看重要及以上问题</Switch>
    </div></div>
    <div className="review-progress"><span>人工复核 {done} / {rows.length}</span>
      <div className="progress-track"><span style={{ width: `${rows.length ? done / rows.length * 100 : 0}%` }} /></div>
      <span className="shortcut-hint"><kbd>1</kbd>–<kbd>4</kbd> 选择结论 · <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd> 保存并下一条 · <kbd>J</kbd>/<kbd>K</kbd> 上下条 · <kbd>N</kbd> 下一条未复核</span></div>
    <Notice>{notice}</Notice>
    <div className="explorer-layout">
      <aside className="case-nav" ref={listRef}>{shown.length ? shown.map((row) => {
        const review = reviewOf(row, reviews);
        const hasDraft = !!drafts[reviewTarget(row).id];
        return <button key={row.execution.id} type="button"
          className={`case-button ${current?.execution.id === row.execution.id ? "selected" : ""}`} onClick={() => setSelected(row.execution.id)}>
          <span>{row.execution.case?.current_turn || row.execution.case?.task_input || (row.execution.case?.current_images.length ? "[图片]" : row.execution.case_id)}</span>
          <small>{skillOf(row)} · {row.pair ? RELATIVE[row.pair.verdict.relative] || row.pair.verdict.relative : row.judgment?.status || row.execution.status}
            {invalid(row, "either") && <b className="format-badge"> · 响应格式问题</b>}
            {review && <b className="reviewed-mark"> · 已复核：{HUMAN_LABEL[review.label] || review.label}</b>}
            {hasDraft && <b className="draft-mark"> · 未提交草稿</b>}</small>
        </button>;
      }) : <p className="muted">没有符合条件的案例。</p>}</aside>
      {current ? <CaseDetail key={`${current.execution.id}:${current.execution.result_sha256}:${current.judgment?.result_sha256 || ""}`}
        runId={runId} row={current} finished={finished} names={names}
        review={reviewOf(current, reviews)} draft={drafts[reviewTarget(current).id]}
        onDraftChange={(draft) => setDrafts((items) => ({ ...items, [reviewTarget(current).id]: draft }))}
        onSaved={(review, advance) => saved(current, review, advance)} />
        : <Empty>选择左侧案例查看上下文、双方输出与盲评依据。</Empty>}
    </div>
  </section>;
}
