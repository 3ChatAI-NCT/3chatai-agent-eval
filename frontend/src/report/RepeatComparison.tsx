import { useEffect, useRef } from "react";
import { base, isRunning, type ChatMessage } from "../api";
import { Messages, StateChip, type MetricInfo, type TurnRow } from "./TurnDetail";

export function RepeatComparison({ runId, rows, current, metrics, reused, onSelect }: {
  runId: string; rows: TurnRow[]; current: TurnRow; metrics: Map<string, MetricInfo>; reused: boolean; onSelect: (key: string) => void;
}) {
  const strip = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const selected = strip.current?.querySelector<HTMLElement>(".repeat-reply.selected");
    if (selected) strip.current?.scrollTo({ left: selected.offsetLeft });
  }, [current.key]);
  if (rows.length < 2) return null;
  const metricIds = [...new Set(rows.flatMap((row) => row.verdict?.metrics.map((metric) => metric.metric_id) || []))];
  const differences = metricIds.filter((id) => new Set(rows.map((row) =>
    row.verdict?.metrics.find((metric) => metric.metric_id === id)?.status || "pending")).size > 1);
  return <section className="repeat-comparison" aria-label="同一轮多次执行对照">
    <div className="panel-head"><h2>第 {current.seq} 轮 · {rows.length} 次执行对照</h2>
      <small className="muted">{differences.length ? `${differences.length} 项评分状态有差异` : metricIds.length ? "各次已记录的评分状态相同" : "暂无可比较的评分"}</small></div>
    <p className="hint">{reused ? "此报告包含已有执行回答，复用记录保留原始证据。" : "每次使用该轮历史上下文独立回放，不是连续对话。"}
      此处包含该轮全部执行；点击某次可查看详细依据并复核。</p>
    {rows.length > 2 && <small className="block muted">左右滚动可查看其余回复，切换次数会定位到对应卡片。</small>}
    <div className="repeat-replies" ref={strip}>{rows.map((row) => {
      const result = row.execution.result as { candidate?: ChatMessage[] } | null;
      const selected = row.key === current.key;
      return <article key={row.key} className={`repeat-reply ${selected ? "selected" : ""}`}>
        <button type="button" className="repeat-select" aria-pressed={selected} onClick={() => onSelect(row.key)}>
          <strong>第 {row.repeat_index} 次执行{selected ? " · 正在查看" : ""}</strong><StateChip state={row.state} /></button>
        <div className="message-box">{result?.candidate?.length ? <Messages messages={result.candidate}
          mediaBase={`${base}/experiments/${runId}/results/${row.execution.id}/media`} side="candidate" />
          : isRunning(row.execution.status) ? "执行中" : "未收到回复"}</div>
      </article>;
    })}</div>
    {!!metricIds.length && <div className="table-scroll"><table className="repeat-metrics"><caption>同一指标，各次判定</caption>
      <thead><tr><th>指标</th>{rows.map((row) => <th key={row.key} className={row.key === current.key ? "current-attempt" : undefined}>
        <button type="button" className="link-button" onClick={() => onSelect(row.key)}>第 {row.repeat_index} 次</button></th>)}</tr></thead>
      <tbody>{metricIds.map((id) => <tr key={id} className={differences.includes(id) ? "different" : undefined}>
        <th scope="row">{metrics.get(id)?.name || id}{differences.includes(id) && <small className="block muted">判定有差异</small>}</th>
        {rows.map((row) => {
          const metric = row.verdict?.metrics.find((item) => item.metric_id === id);
          return <td key={row.key} className={row.key === current.key ? "current-attempt" : undefined}>
            <span className={metric ? `metric-${metric.status}` : "muted"}>{metric
              ? { pass: "通过", fail: "未通过", unavailable: "无法判断" }[metric.status] : "未取得判分"}</span></td>;
        })}</tr>)}</tbody></table></div>}
    <p className="hint">回复不同可能仍满足同一业务目标；评分变化也可能来自自动评分器。请结合下方依据核对，不能只凭差异认定智能体不稳定。</p>
  </section>;
}
