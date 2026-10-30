import { Button, Card, CardBody } from "@heroui/react";
import { ArrowRight, ClipboardCheck, Database, Layers, Plus } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type Batch, type Draft, type Experiment, type Space, blindPending, modeName, stamp } from "./api";
import { Empty, Notice, PageHead, Status, useRemote } from "./App";

type Workbench = {
  // 待盲评的原子回放与待复核的金标报告
  pending_reports: Experiment[];
  running_batches: Batch[];
  gold_drafts: (Draft & { case_count: number; reviewed_cases: number })[];
  recent: Experiment[];
};

export function WorkbenchPage() {
  const { data, error, loading, reload } = useRemote(() => api<Workbench>("/workbench"), []);
  const spaces = useRemote(() => api<Space[]>("/spaces"), []);
  const sourceName = (spaceId: string) => spaces.data?.find((space) => space.id === spaceId)?.source_name || "—";
  return <>
    <PageHead title="工作台" description="从生产样本到可复核的回归结论，按空间追踪每一步。"
      action={<div className="head-actions">
        <Button as={Link} to="/golden/batches/new" variant="flat" startContent={<Layers size={15} />}>逐轮批量采样</Button>
        <Button as={Link} to="/evaluations/new" color="primary" startContent={<Plus size={16} />}>创建评测</Button></div>} />
    <Notice error={error || spaces.error} />
    <section className="stat-grid" aria-label="待办总览">
      <Card shadow="none" className="stat-card"><CardBody><ClipboardCheck size={20} /><strong>{data?.pending_reports.length ?? "—"}</strong><span>待盲评 / 待复核报告</span></CardBody></Card>
      <Card shadow="none" className="stat-card"><CardBody><Database size={20} /><strong>{data?.gold_drafts.length ?? "—"}</strong><span>待完成参考草稿</span></CardBody></Card>
      <Card shadow="none" className="stat-card"><CardBody><Layers size={20} /><strong>{data?.running_batches.length ?? "—"}</strong><span>进行中的批次</span></CardBody></Card>
    </section>
    <div className="work-grid">
      <section className="panel"><div className="panel-head"><h2>报告复核</h2><div className="action-cell">
        <Link to="/atomic/runs">单步回放</Link><Link to="/golden/runs">逐轮回放 <ArrowRight size={14} /></Link></div></div>
        {loading ? <p className="muted">加载中…</p> : !data?.pending_reports.length ? <Empty>暂无待盲评或待复核的报告。</Empty> :
          <div className="row-list">{data.pending_reports.map((run) => <Link className="work-row" key={run.id} to={`/evaluations/${run.id}`}>
            <div><strong>{run.name}</strong><small>{sourceName(run.space_id)} · {stamp(run.created_at)} · {modeName(run.mode)}</small></div>
            <Status value={blindPending(run.blind_status) ? run.blind_status : run.review_status} /><ArrowRight size={16} /></Link>)}</div>}
      </section>
      <section className="panel"><div className="panel-head"><h2>参考草稿审核</h2><Link to="/golden">数据集 <ArrowRight size={14} /></Link></div>
        {loading ? <p className="muted">加载中…</p> : !data?.gold_drafts.length ? <Empty>暂无待审核草稿。</Empty> :
          <div className="row-list">{data.gold_drafts.slice(0, 8).map((draft) => <Link className="work-row" key={draft.id} to={`/drafts/${draft.id}`}>
            <div><strong>{draft.name}</strong><small>{sourceName(draft.space_id)} · {draft.reviewed_cases} / {draft.case_count} 案例已审核</small></div><ArrowRight size={16} /></Link>)}</div>}
        <p className="hint">参考草稿审核与报告复核面向内部成员开放，系统记录每次复核的操作人。</p>
      </section>
    </div>
    {!!data?.running_batches.length && <section className="panel"><div className="panel-head"><h2>进行中的批次</h2>
      <Link to="/atomic">全部批次 <ArrowRight size={14} /></Link></div>
      <div className="row-list">{data.running_batches.map((batch) => <Link className="work-row" key={batch.id} to={batch.spec.mode === "golden_regression" ? `/golden/batches/${batch.id}` : `/batches/${batch.id}`}>
        <div><strong>{batch.name}</strong><small>{stamp(batch.created_at)} · {batch.finished} / {batch.spaces} 个空间已结束{batch.failed ? ` · ${batch.failed} 个未开跑` : ""}</small></div>
        <Status value="running" /><ArrowRight size={16} /></Link>)}</div>
    </section>}
    <section className="panel"><div className="panel-head"><h2>最近评测</h2><Button size="sm" variant="light" onPress={reload}>刷新</Button></div>
      {!data?.recent.length ? <Empty>创建评测后，进度与报告会出现在这里。</Empty> :
        <div className="table-scroll"><table><thead><tr><th>评测</th><th>源空间</th><th>策略</th><th>状态</th><th>复核</th><th>创建时间</th></tr></thead><tbody>
          {data.recent.slice(0, 10).map((run) => <tr key={run.id}><td><Link to={`/evaluations/${run.id}`}>{run.name}</Link></td>
            <td>{sourceName(run.space_id)}</td><td>{modeName(run.mode)}</td><td><Status value={run.display_status || run.status} />
              {run.display_status === "partial" && !!run.execution_failed &&
                <small className="block muted">{run.execution_failed} 条未评测</small>}</td>
            <td><Status value={run.review_status} /></td><td>{stamp(run.created_at)}</td></tr>)}</tbody></table></div>}
    </section>
  </>;
}
