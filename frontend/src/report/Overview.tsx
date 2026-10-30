import { stamp, type AtomicSummary, type BlindStats, type Report, type Side } from "../api";
import { Notice } from "../App";
import { seriousCount, HUMAN_LABEL, ISSUE_KIND, RELATIVE, RELATIVE_ORDER, SIDE, pct, range, seconds } from "../atomic";

// 揭晓后的盲评统计：一致率只用随机样本估计，风险样本单独列出
export function BlindSummary({ stats, onFocus }: { stats: BlindStats; onFocus: (caseId: string, repeatIndex: number) => void }) {
  const { random, risk } = stats;
  const wide = random.interval && random.interval[1] - random.interval[0] > 0.3;
  const disagreements = [...random.disagreements, ...risk.disagreements];
  return <section className="panel human-summary"><div className="panel-head"><h2>人工盲评</h2>
    <span className="muted">{stats.revealed_by} 于 {stamp(stats.revealed_at)} {stats.status === "skipped" ? "提前揭晓" : "揭晓"}</span></div>
    {stats.status === "skipped" && <Notice>样本未评完即揭晓（原因：{stats.skip_reason}），揭晓后补评的结论不计入盲评统计。</Notice>}
    <div className="human-summary-grid">
      <div><span>随机样本 · 已盲评</span><strong>{random.reviewed} / {random.sampled}</strong></div>
      <div><span>AI 与人工一致</span><strong>{random.agreed} / {random.compared} · {pct(random.agreed, random.compared)}</strong></div>
      <div><span>一致率 95% 置信区间</span><strong>{range(random.interval)}</strong></div>
      <div><span>Kappa</span><strong>{random.kappa ?? "—"}</strong></div></div>
    <p className="hint">一致率只用随机样本估计，只算双方都给出候选更优、持平或基线更优的样本。Kappa 扣除了偶然一致：0.6 以上较可靠，0.4 以下说明 AI 与人工的判断标准需要校准。
      {wide && " 样本较少、区间较宽，可在设置页提高抽样比例。"}</p>
    <p className="hint">风险样本（AI 判为分歧、指出重要问题或基线更优）：已盲评 {risk.reviewed} / {risk.sampled}，人工与 AI 一致 {risk.agreed} / {risk.compared}。只用来核对 AI 的判断，不计入一致率。
      {random.revised + risk.revised > 0 && ` 揭晓后修订 ${random.revised + risk.revised} 条，统计仍按揭晓时锁定的盲评结论。`}</p>
    {disagreements.length > 0 && <div className="human-evidence"><strong>盲评与 AI 结论不同 · {disagreements.length} 条</strong>
      {disagreements.map((item) => <a key={`${item.case_id}:${item.repeat_index}`} href="#case-explorer"
        onClick={() => onFocus(item.case_id, item.repeat_index)}>
        {item.case_id} · AI：{HUMAN_LABEL[item.ai]} · 人工：{HUMAN_LABEL[item.human]}</a>)}</div>}
  </section>;
}

export function HumanReviewSummary({ report, onFocus }: { report: Report; onFocus: (caseId: string, repeatIndex: number) => void }) {
  const labels: Record<string, number> = report.human_review?.labels || {};
  const issues: Record<string, number> = report.human_review?.issue_kinds || {};
  const compared = report.human_review?.compared || 0;
  const agreed = report.human_review?.agreed || 0;
  const count = report.reviewed;
  const issueName = Object.fromEntries(ISSUE_KIND);
  const evidence = (report.review_items || []).filter((item) => item.issue_kind !== "none" && item.note);
  const disagreements = report.human_review?.disagreements || [];
  return <section className="panel human-summary"><div className="panel-head"><h2>人工复核结论</h2>
    <span className="muted">已复核 {count} / {report.review_required} 条</span></div>
    {count ? <>
      <div className="human-summary-grid">{(["G", "S", "B"] as const).map((key) =>
        <div key={key}><span>{HUMAN_LABEL[key]}</span><strong>{labels[key] || 0}</strong></div>)}
        <div><span>无法判断</span><strong>{(labels.U || 0) + (labels.unavailable || 0)}</strong></div></div>
      <p className="hint">AI 与人工结论一致：{agreed} / {compared} 条可比较样本。评审分歧、证据不足及无法判断不计入一致率分母。</p>
      {disagreements.length > 0 && <div className="human-evidence"><strong>AI 与人工结论不同 · {disagreements.length} 条</strong>
        {disagreements.slice(0, 5).map((item) => <a key={`${item.case_id}:${item.repeat_index}`}
          href="#case-explorer" onClick={() => onFocus(item.case_id, item.repeat_index)}>
          {item.case_id} · 第 {item.repeat_index + 1} 次</a>)}</div>}
      <p className="hint">问题归因：{ISSUE_KIND.filter(([key]) => key !== "none" && issues[key]).map(([key]) =>
        `${issueName[key]} ${issues[key]} 条`).join("；") || "尚未标记问题"}。逐条依据见下方已复核案例。</p>
      {evidence.length > 0 && <div className="human-evidence"><strong>复核依据</strong>{evidence.slice(0, 5).map((item) =>
        <a key={`${item.case_id}:${item.repeat_index}`} href="#case-explorer"
          onClick={() => onFocus(item.case_id, item.repeat_index)}>
          {item.case_id} · {issueName[item.issue_kind] || item.issue_kind}：{item.note.slice(0, 160)}{item.note.length > 160 ? "…" : ""}</a>)}</div>}
      <p className="hint">人工结论只代表已复核样本；覆盖不足时不要将其作为整批质量结论。</p>
    </> : <p className="muted">尚无人工复核。提交逐条结论后，这里会呈现结论分布和问题归因。</p>}
  </section>;
}

export function Overview({ summary }: { summary: AtomicSummary }) {
  const total = summary.judged;
  const decided = summary.relatives.G + summary.relatives.S + summary.relatives.B;
  const agreement = summary.agreement.compared ? summary.agreement.agreed / summary.agreement.compared : null;
  const worse = summary.intervals.B;
  return <>
    <section className="panel"><div className="panel-head"><h2>盲评结论</h2><span className="muted">{total} 条已形成 AI 结论</span></div>
      <div className="verdict-bar" aria-label="结论分布">{RELATIVE_ORDER.filter((key) => summary.relatives[key]).map((key) =>
        <span key={key} className={`verdict-${key}`} style={{ flexGrow: summary.relatives[key] }} title={`${RELATIVE[key]} ${summary.relatives[key]}`} />)}</div>
      <div className="verdict-grid">{RELATIVE_ORDER.map((key) => {
        const interval = summary.intervals[key];
        const share = total ? summary.relatives[key] / total : 0;
        return <div key={key} className={`verdict-cell verdict-text-${key}`}>
          <span>{RELATIVE[key]}</span><strong>{summary.relatives[key]}<small> 条 · {pct(summary.relatives[key], total)}</small></strong>
          {interval && <div className="interval" title={`95% 置信区间 ${range(interval)}`}>
            <span className={`interval-fill verdict-${key}`} style={{ left: `${interval[0] * 100}%`, width: `${Math.max(1, (interval[1] - interval[0]) * 100)}%` }} />
            <span className="interval-dot" style={{ left: `${share * 100}%` }} /></div>}
          <small>置信区间 {range(interval)}</small></div>;
      })}</div>
      {total > 0 && worse && <p className="key-reading"><strong>关键读数</strong>候选不如基线的比例为 {pct(summary.relatives.B, total)}，考虑抽样误差，真实比例可能在 {range(worse)} 之间。
        {total < 100 && ` 本批只有 ${total} 条，范围较宽，只能说明方向；做替换决策建议扩到 100–200 条并按技能分层。`}</p>}
      <details className="explain"><summary>「95% 置信区间」怎么读？</summary>
        <p>这批样本是从生产请求里随机抽的。换一批同样抽法的样本，比例会上下浮动；「95% 置信区间」的意思是：真实比例有 95% 的把握落在这个区间里。图中圆点是本批实测比例，色条是置信区间。</p>
        <p>样本越多，范围越窄。例如 20 条里有 3 条基线更优（15%），真实比例可能在 5%–36%；同样 15% 如果来自 200 条样本，范围会收窄到约 11%–21%。</p>
        <p>「持平」表示两者表现接近，不等于双方都合格，是否合格要看下方问题严重度。候选更优或持平合计 {summary.relatives.G + summary.relatives.S} 条（占已形成结论的 {pct(summary.relatives.G + summary.relatives.S, decided)}），不等于可替换率。</p>
      </details>
    </section>
    <div className="work-grid">
      <section className="panel"><div className="panel-head"><h2>AI 评审问题严重度</h2><span className="muted">按案例计；确定性格式问题另见「响应格式检查」</span></div>
        <table className="compact-table"><thead><tr><th /><th>严重</th><th>重要</th><th>轻微</th><th>规则检查命中</th></tr></thead><tbody>
          {(["candidate", "baseline"] as Side[]).map((side) => <tr key={side}><td>{SIDE[side]}</td>
            <td className={summary.serious[side].critical ? "danger-text" : undefined}>{summary.serious[side].critical}</td>
            <td className={summary.serious[side].major ? "warn" : undefined}>{summary.serious[side].major}</td>
            <td>{summary.serious[side].minor}</td><td>{summary.rule_checks[side]}</td></tr>)}</tbody></table>
        <p className="hint">引用无法在原文中找到而被作废的问题共 {summary.rejected_issues} 条，未计入上表。{summary.agreement.identical ? `另有 ${summary.agreement.identical} 条双方输出完全一致，只做一次合规检查。` : ""}</p>
      </section>
      <section className="panel"><div className="panel-head"><h2>性能对照</h2><span className="muted">环境不同，仅供参考</span></div>
        <table className="compact-table"><thead><tr><th /><th>样本</th><th>P50</th><th>P95</th><th>超过 30s</th><th>平均输入 / 输出 token</th></tr></thead><tbody>
          {(["candidate", "baseline"] as Side[]).map((side) => { const perf = summary.performance[side]; return <tr key={side}><td>{SIDE[side]}</td>
            <td>{perf.samples}</td><td>{seconds(perf.p50_ms)}</td><td>{seconds(perf.p95_ms)}</td><td>{perf.slow}</td>
            <td>{perf.avg_input_tokens ?? "—"} / {perf.avg_output_tokens ?? "—"}</td></tr>; })}</tbody></table>
        <p className="hint">基线耗时取自生产网关日志，候选耗时在评测 Worker 侧测得，网络与负载不同，不能直接归因于模型。</p>
      </section>
    </div>
    <section className="panel"><div className="panel-head"><h2>按技能</h2><span className="muted">技能名取自请求中的子任务卡片</span></div>
      <div className="table-scroll"><table><thead><tr><th>技能</th><th>样本</th><th>候选更优 / 持平 / 基线更优</th><th>分歧 / 证据不足</th><th>重要及以上问题：候选 / 基线</th></tr></thead><tbody>
        {Object.entries(summary.by_task).map(([task, counts]) => <tr key={task}><td>{task}</td><td>{counts.total}</td>
          <td>{counts.G || 0} / {counts.S || 0} / {counts.B || 0}</td><td>{counts.D || 0} / {counts.U || 0}</td>
          <td>{counts.candidate_serious || 0} / {counts.baseline_serious || 0}</td></tr>)}</tbody></table></div>
    </section>
    <section className="panel"><div className="panel-head"><h2>能否替换：当前证据与缺口</h2></div>
      <ul className="evidence-list">
        <li><strong>业务质量</strong><span>重要及以上问题：候选 {seriousCount(summary, "candidate")} 条、基线 {seriousCount(summary, "baseline")} 条；严重问题：候选 {summary.serious.candidate.critical} 条、基线 {summary.serious.baseline.critical} 条。优先人工核对候选的严重与重要问题。</span></li>
        <li><strong>评审可靠性</strong><span>AI 评审稳定性（交换 A/B 后两次结论一致）{agreement == null ? "—" : `${Math.round(agreement * 100)}%`}，不一致的列为评审分歧；人工已标注 {summary.human.labelled} 条，其中与 AI 一致 {summary.human.agreed} 条。一致率偏低时应先校准再下结论。</span></li>
        <li><strong>工具与接口兼容</strong><span>规则检查命中：候选 {summary.rule_checks.candidate} 条、基线 {summary.rule_checks.baseline} 条。工具参数合法不代表业务动作正确，仍需覆盖流式、拒答、截断与错误返回。</span></li>
        <li><strong>速度</strong><span>P95：候选 {seconds(summary.performance.candidate.p95_ms)}、基线 {seconds(summary.performance.baseline.p95_ms)}。需在同一环境交替调用、采集首字等待，并在接近生产的并发下测吞吐与限流。</span></li>
        <li><strong>成本</strong><span>已记录 token 用量；还需补齐双方计费单价、缓存折扣与重试费用，比较每千次请求与每次合格任务的成本。</span></li>
        <li><strong>完整业务流程</strong><span>本批是单次模型调用评估。工具执行后的下一轮、画像持久化与多轮任务完成，需要在隔离业务环境用逐轮回放验证。</span></li>
        <li><strong>替换验收</strong><span>先约定关键任务错误上限、允许的质量下降幅度、P95 与成本目标，再做非劣效比较；不把持平率或平均分当作切换结论。</span></li>
      </ul>
    </section>
  </>;
}
