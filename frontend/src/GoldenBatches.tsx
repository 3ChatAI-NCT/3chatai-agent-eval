import { Button, Checkbox, Input, Switch } from "@heroui/react";
import { ArrowLeft, Play, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Empty, Notice, PageHead, Status, useApp, useRemote } from "./App";
import { api, isRunning, localInput, messageOf, stamp, submit, toggled,
  type Batch, type BatchItem, type Space } from "./api";
import { SectionHead } from "./Sections";
import { loadSpaceModels, modelIndex, modelLabel } from "./SpaceModels";

export function GoldenBatchesPage() {
  const [archived, setArchived] = useState(false);
  const resource = useRemote(() => api<Batch[]>(`/batches?include_archived=${archived}`), [archived]);
  const data = resource.data?.filter((batch) => batch.spec.mode === "golden_regression");
  const [error, setError] = useState("");
  async function archive(batch: Batch) {
    try {
      const result = await submit<{ changed: number }>("/batches/archive", { ids: [batch.id], archived: !batch.archived_at });
      setError(result.changed ? "" : "批次仍在运行，结束后可归档。"); resource.reload();
    } catch (cause) { setError(messageOf(cause)); }
  }
  return <><SectionHead section="golden" /><Notice error={error || resource.error} />
    <section className="panel"><div className="panel-head"><h2>采样与回放批次</h2>
      <Switch size="sm" isSelected={archived} onValueChange={setArchived}>显示已归档</Switch></div>
      {!data?.length ? <Empty>点击右上角“批量采样并回放”，为一个或多个空间采样并自动执行。</Empty> :
        <div className="table-scroll"><table><thead><tr><th>批次</th><th>每空间采样目标</th><th>回放进度</th><th>创建时间</th><th /></tr></thead><tbody>
          {data.map((batch) => <tr key={batch.id} className={batch.archived_at ? "archived-row" : ""}>
            <td><Link to={`/golden/batches/${batch.id}`}>{batch.name}</Link>{batch.archived_at && <small className="block muted">已归档</small>}</td>
            <td>{batch.spec.size} 个会话 · 至少 {batch.spec.min_turns} 轮 · 每轮 {batch.spec.repeats} 次</td>
            <td>{batch.finished} / {batch.spaces} 空间已结束{batch.failed > 0 && ` · ${batch.failed} 个空间未开跑`}</td>
            <td>{stamp(batch.created_at)}</td><td><Button size="sm" variant="light" onPress={() => void archive(batch)}>{batch.archived_at ? "恢复" : "归档"}</Button></td>
          </tr>)}</tbody></table></div>}
    </section></>;
}

export function GoldenBatchNewPage() {
  const navigate = useNavigate();
  const { capabilities } = useApp();
  const spaces = useRemote(() => api<Space[]>("/spaces"), []);
  const models = useRemote(loadSpaceModels, []);
  const index = modelIndex(models.data);
  const active = spaces.data?.filter((space) => space.status === "active");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [form, setForm] = useState(() => ({ name: `逐轮批量回放_${localInput(new Date()).slice(0, 10)}`,
    start: localInput(new Date(Date.now() - 60 * 86400000)), end: localInput(new Date()), size: "20", minTurns: "3", repeats: "3" }));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (spaces.data) setSelected(new Set(spaces.data.filter((s) => s.status === "active").map((s) => s.id))); }, [spaces.data]);
  const field = (key: keyof typeof form) => ({ value: form[key], onValueChange: (value: string) => setForm({ ...form, [key]: value }) });
  const valid = form.name.trim() && selected.size && form.start < form.end
    && Number.isInteger(+form.size) && +form.size >= 1 && +form.size <= 100
    && Number.isInteger(+form.minTurns) && +form.minTurns >= 1 && +form.minTurns <= 50
    && Number.isInteger(+form.repeats) && +form.repeats >= 1 && +form.repeats <= 5;
  async function create() {
    setBusy(true); setError("");
    try {
      const batch = await submit<{ id: string }>("/batches", { mode: "golden_regression", name: form.name.trim(),
        space_ids: [...selected], start: new Date(form.start).toISOString(), end: new Date(form.end).toISOString(),
        size: +form.size, min_turns: +form.minTurns, repeats: +form.repeats, trial: false }, { "Idempotency-Key": crypto.randomUUID() });
      navigate(`/golden/batches/${batch.id}`);
    } catch (cause) { setError(messageOf(cause)); } finally { setBusy(false); }
  }
  return <><Link className="back" to="/golden/batches"><ArrowLeft size={15} /> 逐轮批次</Link>
    <PageHead title="批量采样并回放" description="选择空间与采样条件，后台依次完成采样、冻结参考数据、重复回放和评分。单个空间失败不影响其他空间。" />
    <Notice error={error || spaces.error} />
    <section className="panel form-panel batch-form"><div className="form-stack">
      <Input label="批次名称" {...field("name")} maxLength={100} />
      <div><div className="panel-head"><strong>评测空间 · 已选 {selected.size} / {active?.length || 0}</strong>
        <Button size="sm" variant="light" onPress={() => setSelected(selected.size === active?.length ? new Set() : new Set(active?.map((s) => s.id)))}>{selected.size === active?.length ? "全不选" : "全选"}</Button></div>
        <div className="space-picks">{active?.map((space) => <Checkbox key={space.id} isSelected={selected.has(space.id)}
          onValueChange={() => setSelected((current) => toggled(current, [space.id]))}>{space.name}<small className="muted"> · {space.source_name}
            {index.has(space.id) && ` · 执行模型 ${modelLabel(index.get(space.id)!.model)}`}</small></Checkbox>)}</div></div>
      <div className="form-row"><Input type="datetime-local" label="采样开始" {...field("start")} /><Input type="datetime-local" label="采样结束" {...field("end")} /></div>
      <div className="form-row"><Input type="number" label="每空间会话数" min={1} max={100} {...field("size")} />
        <Input type="number" label="每会话至少轮数" min={1} max={50} {...field("minTurns")} />
        <Input type="number" label="每轮执行次数" min={1} max={5} {...field("repeats")} /></div>
      <div className="selection-box"><strong>执行计划</strong>
        <span>{selected.size} 个空间，各采集最多 {form.size} 个会话；只纳入至少 {form.minTurns} 个可回放轮次的会话。</span>
        <span>每轮独立执行 {form.repeats} 次；报告按会话汇总所评轮次，计算 pass^1 至 pass^{form.repeats}，衡量重复执行的稳定通过率。</span>
        <span>使用各 EVAL 空间当前配置；执行模型在每个空间开跑时冻结，可在<Link to="/spaces">评测空间</Link>页调整。来源回复仅作未经人工确认的参考；评分标准依据空间规则独立生成，不要求照抄参考话术。</span>
        <span>未达到配额会显示实际样本数与原因；不会降低最少轮数或重复凑数。参考数据可在采集后修订审核。</span>
        <span>Judge：{capabilities.judge_model || "尚未配置"} · <Link to="/settings">评分设置</Link></span>
      </div>
      <Button color="primary" isDisabled={!valid || !capabilities.judge_configured} isLoading={busy} startContent={<Play size={16} />} onPress={() => void create()}>开始采样并回放</Button>
    </div></section></>;
}

export function GoldenBatchPage() {
  const { batchId = "" } = useParams();
  const resource = useRemote(() => api<{ batch: Batch; items: BatchItem[] }>(`/batches/${batchId}`), [batchId]);
  const data = resource.data;
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function retry() {
    setBusy(true); setError("");
    try { await submit(`/batches/${batchId}/retry`, {}); resource.reload(); }
    catch (cause) { setError(messageOf(cause)); } finally { setBusy(false); }
  }
  const pending = data?.items.some((item) => isRunning(item.capture_status) || isRunning(item.experiment_status)
    || item.capture_status === "succeeded" && !item.experiment_id && !item.error_message);
  useEffect(() => { if (pending) { const timer = setInterval(resource.reload, 5000); return () => clearInterval(timer); } }, [pending, batchId]);
  return <><Link className="back" to="/golden/batches"><ArrowLeft size={15} /> 逐轮批次</Link>
    <PageHead title={data?.batch.name || "采样与回放批次"} description="采样 → 冻结参考数据 → 每轮重复执行 → 自动评分与稳定性报告"
      action={<Button variant="flat" startContent={<RefreshCw size={15} />} onPress={resource.reload}>刷新进度</Button>} />
    <Notice error={error || resource.error} />
    {data?.items.some((item) => !item.experiment_id && (item.error_message || item.capture_status === "failed")) &&
      <Button variant="flat" isLoading={busy} onPress={() => void retry()}>重试失败的采样与数据准备</Button>}
    {data && <><section className="panel"><div className="selection-box"><strong>本批次目标</strong>
      <span>{data.items.length} 个空间 · 每空间 {data.batch.spec.size} 个会话 · 每会话至少 {data.batch.spec.min_turns} 轮 · 每轮执行 {data.batch.spec.repeats} 次</span>
      <span>采样窗口：{stamp(data.batch.spec.start)} — {stamp(data.batch.spec.end)}</span>
      <span>来源回复未经人工确认；报告按空间规则判分。pass^k 按会话汇总所评轮次，表示重复执行 k 次均回答达标的稳定性，不能解释为连续多轮任务成功率。</span>
    </div></section>
    <section className="panel"><h2>各空间进度</h2><div className="table-scroll"><table><thead><tr><th>空间</th><th>采样</th><th>参考数据</th><th>回放与评分</th><th>结果 / 后续操作</th></tr></thead><tbody>
      {data.items.map((item) => <tr key={item.id}><td><Link to={`/spaces/${item.space_id}/golden`}>{item.space_name}</Link><small className="block muted">{item.source_name}</small></td>
        <td><Status value={item.capture_status} />{item.capture_outcome && <><strong className="block">已采集 {item.capture_outcome.count} 个会话</strong>
          <small className="block muted">本次回放目标 {data.batch.spec.size} 个会话</small>
          {(item.capture_outcome.counts?.quota_shortfall || 0) > 0 && <small className="block">不足 {item.capture_outcome.counts!.quota_shortfall} 个；查看空间采集记录的过滤原因</small>}</>}</td>
        <td>{item.dataset_version_id ? <Link to="/golden">已冻结 · 未经人工确认</Link> : "等待采样完成"}</td>
        <td>{item.experiment_id ? <><Status value={item.experiment_display_status || item.experiment_status} />
          <small className="block muted">执行已结束 {item.executed_turns || 0} / {item.planned_turns || 0} 次 · 已评分 {item.judged_turns || 0} 次</small>
          {!!item.execution_errors && <small className="block">执行异常 {item.execution_errors} 次</small>}
          {!!item.cancelled_turns && <small className="block">已取消且未执行 {item.cancelled_turns} 次</small>}
        </> : item.error_message || item.capture_error ? "未开跑" : "等待参考数据"}</td>
        <td>{item.experiment_id && <Link to={`/evaluations/${item.experiment_id}`}>查看进度与 pass^k 报告</Link>}
          {item.execution_stop && <p role="alert">{item.execution_stop.reason}</p>}
          {(item.error_message || item.capture_error) && <p role="alert">{item.error_message || item.capture_error}</p>}</td>
      </tr>)}</tbody></table></div></section></>}
  </>;
}
