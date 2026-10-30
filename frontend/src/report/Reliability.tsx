import type { GoldenQuality } from "../api";
import { StateChip } from "./TurnDetail";

const pct = (value: number | null | undefined) => value == null ? "无法判断" : `${Math.round(value * 100)}%`;

export function Reliability({ quality, repeats, selectedTurns, names, onSession }: {
  quality: GoldenQuality; repeats: number; selectedTurns: boolean; names: Map<string, string>;
  onSession: (id: string) => void;
}) {
  const valid = quality.sessions.filter((session) => session.pass_at_k["1"] != null).length;
  return <section className="panel reliability" aria-label="重复执行稳定性">
    <div className="panel-head"><h2>重复执行稳定性 <small className="muted">pass<sup>k</sup></small></h2>
      <span className="muted">{quality.sessions.length} 个会话 · 每个重复 {repeats} 次</span></div>
    <p className="hint">从同一会话的重复执行中任取 k 次，所评轮次每次都回答达标的比例估计；越高表示越稳定。</p>
    <p className="hint">回答达标要求正确性、完整性、安全性（4 / 5 / 6）均通过；画像与引导指标单独展示。</p>
    <div className="reliability-values">{Object.entries(quality.batch_pass_at_k).map(([k, value]) =>
      <div key={k}><span>{k === "1" ? "单次回答达标" : `${k} 次均回答达标`}</span><strong>{pct(value)}</strong>
        <small className="muted">pass<sup>{k}</sup></small></div>)}</div>
    <p className="hint">有效会话 {valid} / {quality.sessions.length}；含未判定执行的会话不计入本项均值。
      {repeats === 1 && " 本次只执行 1 次，尚不能衡量重复稳定性。"}
      {quality.sessions.length < 10 && " 当前样本较少，结果仅用于本批排查。"}</p>
    {selectedTurns && <p className="scope-note">本次只回放指定轮次。以下结论仅覆盖所选轮次，不能代表完整会话成功率。</p>}
    <details><summary>会话与重复明细 · {quality.sessions.length} 个会话</summary>
      <div className="table-scroll"><table className="metric-table reliability-table"><thead><tr>
        <th>会话</th><th>每次执行结论</th><th>达标次数</th><th>稳定性</th><th>首次未达标轮次</th>
      </tr></thead><tbody>{quality.sessions.map((session) => <tr key={session.session_id}>
        <td><button type="button" className="link-button session-label" title={session.session_id}
          onClick={() => onSession(session.session_id)}>{names.get(session.session_id) || session.session_id}</button></td>
        <td><div className="attempt-chips">{session.attempts.map((attempt) => <span key={attempt.repeat_index}>
          <small>第 {attempt.repeat_index} 次 </small><StateChip state={attempt.session_status} /></span>)}</div></td>
        <td>{session.passed_attempt_count} / {repeats}</td>
        <td>{Object.entries(session.pass_at_k).map(([k, value]) => <small key={k} className="block">
          pass<sup>{k}</sup> {pct(value)}</small>)}</td>
        <td>{session.attempts.filter((attempt) => attempt.first_failure_turn != null).map((attempt) =>
          <small className="block" key={attempt.repeat_index}>第 {attempt.repeat_index} 次 · 第 {attempt.first_failure_turn} 轮</small>)}
          {session.attempts.every((attempt) => attempt.first_failure_turn == null) && "—"}</td>
      </tr>)}</tbody></table></div>
    </details>
    <details className="scoring-notes"><summary>怎样理解这些分数</summary>
      <p>本页的“回答达标”只由回答正确性、回答完整性、回答安全性（4 / 5 / 6）决定。画像与引导指标单列展示，工具调用仅作诊断。
        因此，回答达标仍可能存在画像或引导问题。</p>
      <p>每轮使用冻结历史独立回放，会话结论是所评轮次的汇总，不是 Agent 连续自主对话的任务成功率。</p>
      <p>pass<sup>k</sup> = C(c, k) / C(n, k)：n 为同一会话的重复次数，c 为达标次数。先按会话计算，再对有效会话取均值。
        例如 3 次中 2 次达标，pass<sup>1</sup> 为 66.7%，pass<sup>2</sup> 为 33.3%，pass<sup>3</sup> 为 0%。</p>
      <p>pass@k 问的是“k 次中至少一次达标”，本页暂不统计。它与要求 k 次均达标的 pass<sup>k</sup> 含义不同，不能混用。</p>
    </details>
  </section>;
}
