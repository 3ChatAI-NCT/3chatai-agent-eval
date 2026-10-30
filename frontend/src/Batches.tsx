import {
  Button, Checkbox, Chip, Input, Modal, ModalBody, ModalContent, ModalFooter, ModalHeader, Switch,
} from "@heroui/react";
import { ArrowLeft, ArrowRight, Download, Play, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  defaultWindow, localInput, toggled, downloadFile, isRunning, blindPending, messageOf, api, stamp, submit,
  type AtomicSummary, type Batch, type BatchItem, type CrossSummary, type Space, type SummaryRow,
} from "./api";
import { Empty, Notice, PageHead, Status, useApp, useRemote } from "./App";
import {
  CandidatePicker, candidateBody, candidateReady, initialChoice, loadCandidateSources, rememberChoice,
  type CandidateChoice,
} from "./CandidatePicker";
import { RELATIVE, RELATIVE_ORDER, blindRule, pct, range, seconds, seriousCount } from "./atomic";
import { SectionHead } from "./Sections";
import { ResponseFormatTable } from "./report/ResponseFormat";

const withZone = (value: string) => new Date(value).toISOString();
const today = () => localInput(new Date()).slice(0, 10).replaceAll("-", "");

// 所有空间未开跑时单独显示，避免将采集失败误认为评测失败
function batchStatus(batch: Batch) {
  if (batch.finished + batch.failed < batch.spaces) return "running";
  if (!batch.started) return "not_started";
  return batch.failed ? "partial" : "completed";
}

export function BatchesPage() {
  const [showArchived, setShowArchived] = useState(false);
  const batches = useRemote(() => api<Batch[]>(`/batches?include_archived=${showArchived}`), [showArchived]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Batch | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [deleting, setDeleting] = useState(false);
  async function archive(batch: Batch, archived: boolean) {
    setBusy(batch.id); setError("");
    try {
      const { changed } = await submit<{ changed: number }>("/batches/archive", { ids: [batch.id], archived });
      if (!changed) setError("还有空间在采集或评测中，结束后才能归档。");
      batches.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(""); }
  }
  async function remove() {
    if (!deleteTarget || confirmation !== deleteTarget.name) return;
    setDeleting(true); setError("");
    try {
      await api(`/batches/${deleteTarget.id}`, { method: "DELETE" });
      setDeleteTarget(null); setConfirmation(""); batches.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setDeleting(false); }
  }
  const data = batches.data?.filter((batch) => batch.spec.mode !== "golden_regression");
  return <>
    <SectionHead section="atomic" />
    <Notice error={error || batches.error} />
    <section className="panel"><div className="panel-head"><h2>批量评测</h2>
      <div className="filters"><Switch size="sm" isSelected={showArchived} onValueChange={setShowArchived}>显示已归档</Switch></div></div>
      {!data ? !batches.error && <p className="muted">加载中…</p>
        : !data.length ? <Empty>还没有批量评测。点右上角「批量评测」为多个空间同时采样并开跑。</Empty> :
        <div className="table-scroll"><table><thead><tr><th>批次</th><th>候选模型</th><th>评测进度</th><th>人工盲评</th><th>创建时间</th><th /></tr></thead><tbody>
          {data.map((batch) => <tr key={batch.id} className={batch.archived_at ? "archived-row" : ""}>
            <td><Link to={`/batches/${batch.id}`}>{batch.name}</Link>
              {batch.spec.trial && <Chip size="sm" variant="flat" className="inline-chip">试跑</Chip>}
              {batch.archived_at && <Chip size="sm" variant="flat" className="inline-chip">已归档</Chip>}</td>
            <td>{batch.spec.candidate_gateway_model || batch.spec.candidate_preset_name || "已保存的接口"}</td>
            <td><Status value={batchStatus(batch)} />
              <small className="block muted">{batch.finished} / {batch.spaces} 个空间已结束{batch.failed ? ` · ${batch.failed} 个未开跑` : ""}</small></td>
            <td>{batch.blind ? <>{batch.revealed} / {batch.blind} 已揭晓</> : <span className="muted">—</span>}</td>
            <td>{stamp(batch.created_at)}</td>
            <td><div className="action-cell">
              {batchStatus(batch) !== "running" && <Button size="sm" variant="light" isLoading={busy === batch.id}
                onPress={() => void archive(batch, !batch.archived_at)}>{batch.archived_at ? "恢复" : "归档"}</Button>}
              {batch.archived_at && <Button size="sm" variant="light" color="danger"
                onPress={() => { setDeleteTarget(batch); setConfirmation(""); setError(""); }}>删除</Button>}
              <Link to={`/batches/${batch.id}`} aria-label={`查看${batch.name}`}><ArrowRight size={16} /></Link></div></td></tr>)}
        </tbody></table></div>}
      <p className="hint">归档批次会连同其中已结束的评测一起从列表隐藏，数据与审计保留，可随时恢复。
        已归档的批次可永久删除：连同其中的评测一起删除，但评测有人工复核或被重新评分引用时不能删除；各空间的采集记录与采样数据保留。</p>
    </section>
    <Modal isOpen={!!deleteTarget} onClose={() => { if (!deleting) { setDeleteTarget(null); setConfirmation(""); } }}>
      <ModalContent>
        <ModalHeader>永久删除批次</ModalHeader>
        <ModalBody>
          <p>批次和其中各空间的评测、事件、报告将无法恢复；采集记录与采样数据保留在各空间。</p>
          <Notice error={error} />
          <Input label="输入批次名称以确认" value={confirmation} onValueChange={setConfirmation}
            autoComplete="off" placeholder={deleteTarget?.name} />
        </ModalBody>
        <ModalFooter>
          <Button variant="light" isDisabled={deleting} onPress={() => { setDeleteTarget(null); setConfirmation(""); }}>取消</Button>
          <Button color="danger" isLoading={deleting} isDisabled={confirmation !== deleteTarget?.name}
            onPress={() => void remove()}>永久删除</Button>
        </ModalFooter>
      </ModalContent>
    </Modal>
  </>;
}

export function BatchNewPage() {
  const navigate = useNavigate();
  const { capabilities } = useApp();
  const resource = useRemote(async () => {
    const [spaces, sources] = await Promise.all([api<Space[]>("/spaces"), loadCandidateSources()]);
    return { spaces: spaces.filter((space) => space.status === "active"), sources };
  }, []);
  const [form, setForm] = useState({
    name: `批量评测_${today()}`, ...defaultWindow(),
    size: "100", trial: false,
  });
  const [choice, setChoice] = useState<CandidateChoice | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const data = resource.data;
  useEffect(() => {
    if (!data) return;
    setChosen(new Set(data.spaces.map((space) => space.id)));
    setChoice(initialChoice(data.sources, false));
  }, [data]);
  const size = Number(form.size);
  const valid = form.name.trim() && chosen.size > 0 && size >= 1 && size <= 300 && form.start < form.end
    && candidateReady(choice);
  async function create() {
    setBusy(true); setError("");
    try {
      const batch = await submit<{ id: string }>("/batches", {
        name: form.name.trim(), space_ids: [...chosen], start: withZone(form.start), end: withZone(form.end), size, trial: form.trial,
        ...candidateBody(choice!),
      }, { "Idempotency-Key": crypto.randomUUID() });
      rememberChoice(choice!);
      navigate(`/batches/${batch.id}`);
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  const toggle = (id: string) => setChosen((current) => toggled(current, [id]));
  return <>
    <Link className="back" to="/atomic"><ArrowLeft size={15} /> 单步回放</Link>
    <PageHead title="批量评测" description="多个空间用同一时间窗采样，采集成功后自动用同一候选模型开跑单步回放；某个空间采集失败不影响其他空间。" />
    <Notice error={error || resource.error} />
    {data && <section className="panel form-panel batch-form"><div className="form-stack">
      <Input label="批次名称" value={form.name} onValueChange={(name) => setForm({ ...form, name })}
        description="每个空间的评测命名为「批次名称 · 空间名」" />
      <div><div className="panel-head"><strong className="field-title">评测空间 · 已选 {chosen.size} / {data.spaces.length}</strong>
        <Button size="sm" variant="light" onPress={() => setChosen(chosen.size === data.spaces.length ? new Set() : new Set(data.spaces.map((space) => space.id)))}>
          {chosen.size === data.spaces.length ? "全不选" : "全选"}</Button></div>
        <div className="space-picks">{data.spaces.map((space) => <Checkbox key={space.id} size="sm" isSelected={chosen.has(space.id)}
          onValueChange={() => toggle(space.id)}>{space.name}{space.source_name && <small className="muted"> · {space.source_name}</small>}</Checkbox>)}</div></div>
      <div className="form-row">
        <Input type="datetime-local" label="采样开始" value={form.start} onValueChange={(start) => setForm({ ...form, start })} />
        <Input type="datetime-local" label="采样结束" value={form.end} onValueChange={(end) => setForm({ ...form, end })}
          description="太新的日志还没写完，结束时间会自动截到约 11 分钟前" />
      </div>
      <Input type="number" label="每个空间的样本数" min={1} max={300} value={form.size} onValueChange={(value) => setForm({ ...form, size: value })}
        description="最多 300 条；替换决策建议每个空间 100–200 条，样本越多置信区间越窄" />
      {choice && <CandidatePicker sources={data.sources} choice={choice} onChange={setChoice} />}
      <Checkbox isSelected={form.trial} onValueChange={(trial) => setForm({ ...form, trial })}>试跑：结束满 7 天自动归档</Checkbox>
      <div className="selection-box"><strong>运行依据</strong>
        <span>Judge：{capabilities.judge_configured ? capabilities.judge_model : "尚未配置"} · <Link to="/settings">在设置页调整</Link></span>
        <span>将为 {chosen.size} 个空间各采集最多 {size || 0} 条生产请求，预计共 {(size || 0) * chosen.size} 条样本。</span>
        <span>{blindRule(capabilities.blind_review)} · <Link to="/settings">在设置页调整</Link></span></div>
      <Button color="primary" isLoading={busy} isDisabled={!valid || !capabilities.judge_configured} onPress={() => void create()}
        startContent={<Play size={16} />}>开始批量评测</Button>
    </div></section>}
  </>;
}

export function BatchPage() {
  const { batchId = "" } = useParams();
  const resource = useRemote(() => api<{ batch: Batch; items: BatchItem[] }>(`/batches/${batchId}`), [batchId]);
  const items = resource.data?.items || [];
  const pending = items.some((item) => isRunning(item.capture_status) || (item.capture_status === "succeeded"
    && !item.experiment_id && !item.error_message) || isRunning(item.experiment_status));
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(resource.reload, 5000);
    return () => window.clearInterval(timer);
  }, [pending, batchId]);
  const started = items.filter((item) => item.experiment_id).map((item) => item.experiment_id!);
  const finished = items.filter((item) => item.experiment_status && !isRunning(item.experiment_status)).length;
  const batch = resource.data?.batch;
  const [replaying, setReplaying] = useState(false);
  const candidate = batch ? (batch.spec.candidate_gateway_model ? `${batch.spec.candidate_gateway_model} · 3Chat 网关`
    : `${batch.spec.candidate_preset_name || "已保存的接口"}`) : "";
  return <>
    <Link className="back" to="/atomic"><ArrowLeft size={15} /> 单步回放</Link>
    <PageHead title={batch?.name || "批量评测"}
      description={batch ? `${stamp(batch.spec.start)} → ${stamp(batch.spec.end)} · 每个空间最多 ${batch.spec.size} 条 · 候选 ${candidate}${batch.spec.trial ? " · 试跑" : ""} · 创建于 ${stamp(batch.created_at)}` : ""}
      action={<div className="button-row">
        {batch && !pending && <Button size="sm" color="primary" variant="flat" onPress={() => setReplaying(true)}>复用样本更换模型</Button>}
        <Button size="sm" variant="light" isIconOnly aria-label="刷新" onPress={resource.reload}><RefreshCw size={15} /></Button></div>} />
    <Notice error={resource.error} />
    {batch?.spec.source_batch_id && <p className="hint">复用自 <Link to={`/batches/${batch.spec.source_batch_id}`}>{batch.spec.source_batch_name}</Link>；沿用全部冻结样本与评分配置。</p>}
    {replaying && batch && <BatchReplayModal batch={batch} onClose={() => setReplaying(false)} />}
    <section className="panel"><div className="panel-head"><h2>各空间进度</h2>
      <span className="muted">{started.length} / {items.length} 个空间已开跑，{finished} 个已结束{pending ? " · 每 5 秒自动刷新" : ""}</span></div>
      <div className="table-scroll"><table><thead><tr><th>空间</th><th>源空间</th><th>采集</th><th>评测</th><th>人工盲评</th></tr></thead><tbody>
        {items.map((item) => <tr key={item.id}><td><strong>{item.space_name}</strong></td><td>{item.source_name || "—"}</td>
          <td><Status value={item.capture_status} />{item.capture_error && <small className="block danger-text">{item.capture_error}</small>}</td>
          <td>{item.experiment_id ? <><Status value={item.experiment_display_status || item.experiment_status} />
              {item.experiment_display_status === "partial" && !!item.execution_failed &&
                <small className="block muted">{item.execution_failed} 条未评测</small>}
              <Link to={`/evaluations/${item.experiment_id}`}>查看报告</Link></>
            : item.error_message ? <small className="danger-text">{item.error_message}</small>
            : item.capture_status === "failed" || item.capture_status === "cancelled"
              ? <span className="muted">采集{item.capture_status === "failed" ? "失败" : "已取消"}，未开跑</span>
            : <span className="muted">等待采集完成后自动开跑</span>}</td>
          <td>{item.blind_status ? <>
            <Status value={item.blind_status} />
            {item.blind_status === "blind_open" && <Link to={`/evaluations/${item.experiment_id}`}>去盲评</Link>}</>
            : <span className="muted">—</span>}</td></tr>)}</tbody></table></div>
    </section>
    {items.some((item) => blindPending(item.blind_status)) &&
      <p className="hint">开启人工盲评的空间，揭晓前不在汇总中展示 AI 结论、也不计入合计；在各空间报告页完成抽样盲评并揭晓后即计入。</p>}
    {started.length > 0 && <CrossSummaryPanel ids={started} refreshKey={finished} />}
  </>;
}

type ReplayPreview = { source_batch_name: string; items: {
  space_name: string; source_name: string; dataset_version_id: string; dataset_sha256: string;
  samples: number; repeats: number; judge_model: string; judge_prompt_version: string;
}[] };

function BatchReplayModal({ batch, onClose }: { batch: Batch; onClose: () => void }) {
  const navigate = useNavigate();
  const resource = useRemote(async () => {
    const [preview, sources] = await Promise.all([
      api<ReplayPreview>(`/batches/${batch.id}/replay`), loadCandidateSources(),
    ]);
    return { preview, sources };
  }, [batch.id]);
  const [name, setName] = useState("");
  const [choice, setChoice] = useState<CandidateChoice | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [key] = useState(() => crypto.randomUUID());
  const data = resource.data;
  useEffect(() => { if (data) setChoice(initialChoice(data.sources, false)); }, [data]);
  async function create() {
    if (!choice) return;
    setBusy(true); setError("");
    try {
      const created = await submit<{ id: string }>(`/batches/${batch.id}/replay`, {
        name: name.trim(), ...candidateBody(choice),
      }, { "Idempotency-Key": key });
      rememberChoice(choice);
      onClose();
      navigate(`/batches/${created.id}`);
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  return <Modal isOpen size="4xl" scrollBehavior="inside" isDismissable={!busy} onClose={() => { if (!busy) onClose(); }}>
    <ModalContent><ModalHeader>复用样本更换模型</ModalHeader><ModalBody>
      <p>来源：{batch.name}。保留各空间的全部采样数据、生产基线、执行重复次数、Judge、评分提示词和盲评配置，重新调用候选模型并评分。</p>
      <Notice error={error || resource.error} />
      {!data && !resource.error && <p>正在核对全部冻结样本与运行配置…</p>}
      {data && <>
        <Input label="新批次名称" maxLength={100} value={name} onValueChange={setName} />
        {choice && <CandidatePicker sources={data.sources} choice={choice} onChange={setChoice} />}
        <strong>共 {data.preview.items.length} 个空间 · {data.preview.items.reduce((n, item) => n + item.samples, 0)} 条样本</strong>
        <div className="table-scroll"><table><thead><tr><th>空间</th><th>样本 / 重复次数</th><th>Judge / 评分提示词</th><th>冻结数据集</th></tr></thead><tbody>
          {data.preview.items.map((item) => <tr key={item.dataset_version_id}>
            <td>{item.space_name}<small className="block muted">{item.source_name}</small></td>
            <td>{item.samples} / {item.repeats}</td><td>{item.judge_model}<small className="block muted">{item.judge_prompt_version}</small></td>
            <td className="mono">{item.dataset_version_id}<small className="block muted">SHA256 {item.dataset_sha256.slice(0, 16)}…</small></td>
          </tr>)}
        </tbody></table></div>
      </>}
    </ModalBody><ModalFooter>
      <Button variant="light" isDisabled={busy} onPress={onClose}>取消</Button>
      <Button color="primary" isLoading={busy} isDisabled={!data || !name.trim() || !candidateReady(choice)} onPress={() => void create()}>开始评测</Button>
    </ModalFooter></ModalContent>
  </Modal>;
}

export function SummaryPage() {
  const [query] = useSearchParams();
  const ids = (query.get("ids") || "").split(",").filter(Boolean);
  return <>
    <Link className="back" to="/atomic/runs"><ArrowLeft size={15} /> 单步回放</Link>
    <PageHead title="跨空间汇总" description="所选单步回放评测逐条列出，合计行把全部样本合在一起重新计算比例与置信区间。" />
    {ids.length ? <CrossSummaryPanel ids={ids} refreshKey={0} /> : <Empty>请在评测记录中勾选要汇总的评测。</Empty>}
  </>;
}

function CrossSummaryPanel({ ids, refreshKey }: { ids: string[]; refreshKey: number }) {
  const key = ids.join(",");
  const summary = useRemote(() => api<CrossSummary>(`/summary?ids=${key}`), [key, refreshKey]);
  const data = summary.data;
  return <section className="panel"><div className="panel-head"><h2>汇总</h2>
    {data && <Button size="sm" variant="flat" startContent={<Download size={14} />} onPress={() => downloadCsv(data)}>导出 CSV</Button>}</div>
    <Notice error={summary.error} />
    {data?.warnings.map((warning) => <Notice key={warning}>{warning}</Notice>)}
    {!data ? !summary.error && <p className="muted">正在汇总…</p> : <SummaryTable data={data} />}
    {data?.total.response_format && revealed(data.runs).length > 0 && <ResponseFormatTable rows={[
      ...revealed(data.runs).filter((row) => row.atomic.response_format).map((row) => ({
        name: `${row.space.name} · 候选`, counts: row.atomic.response_format!.candidate,
      })),
      { name: "合计 · 候选", counts: data.total.response_format.candidate },
      { name: "合计 · 基线", counts: data.total.response_format.baseline },
    ]} />}
    <p className="hint">「基线更优」列后为 95% 置信区间：真实比例有 95% 的把握落在该范围内。运行中的评测按已完成的样本计算，结束后刷新即为最终结果。</p>
  </section>;
}

const consistency = (summary: AtomicSummary) => pct(summary.agreement.agreed, summary.agreement.compared);

type RevealedRow = Exclude<SummaryRow, { blind: true }>;
const revealed = (rows: SummaryRow[]) => rows.filter((row): row is RevealedRow => !row.blind);

function SummaryTable({ data }: { data: CrossSummary }) {
  const all = useMemo(() => [...data.runs].sort((a, b) => a.space.name.localeCompare(b.space.name)), [data]);
  const rows = revealed(all);
  const cells = (summary: AtomicSummary) => <>
    {RELATIVE_ORDER.map((key) => <td key={key} className={`verdict-text-${key}`}>{summary.relatives[key]}</td>)}
    <td>{pct(summary.relatives.B, summary.judged)}<small className="block muted">{range(summary.intervals.B)}</small></td>
    <td>{consistency(summary)}</td>
    <td>{seriousCount(summary, "candidate")} / {seriousCount(summary, "baseline")}</td>
    <td>{seconds(summary.performance.candidate.p95_ms)} / {seconds(summary.performance.baseline.p95_ms)}</td></>;
  return <div className="table-scroll"><table className="summary-table"><thead><tr><th>空间</th><th>源空间</th><th>样本</th>
    {RELATIVE_ORDER.map((key) => <th key={key}>{RELATIVE[key]}</th>)}
    <th>基线更优占比</th><th title="Judge 盲评两次、交换 A/B 位置后结论一致的比例（位置交换一致率），衡量 AI 评审是否稳定，不是候选模型的准确率">AI 评审稳定性</th><th>重要及以上问题 候选 / 基线</th><th>P95 候选 / 基线</th></tr></thead><tbody>
    {all.map((row) => row.blind ? <tr key={row.id}><td><Link to={`/evaluations/${row.id}`}>{row.space.name}</Link></td>
      <td>{row.space.source_name || "—"}</td><td colSpan={RELATIVE_ORDER.length + 5} className="muted">盲评尚未揭晓，暂不展示 AI 结论</td></tr>
      : <tr key={row.id}><td><Link to={`/evaluations/${row.id}`}>{row.space.name}</Link><small className="block muted">{row.candidate}{isRunning(row.status) ? " · 运行中" : ""}</small></td><td>{row.space.source_name || "—"}</td>
      <td>{row.atomic.judged} / {row.planned}{row.status === "cancelled" ? <small className="block muted">已取消</small>
        : row.executed < row.planned && !isRunning(row.status) && <small className="block muted">未评测 {row.planned - row.executed} 条</small>}</td>
      {cells(row.atomic)}</tr>)}
    <tr className="total-row"><td><strong>合计</strong></td><td>—</td><td>{data.total.judged} / {rows.reduce((sum, row) => sum + row.planned, 0)}</td>{cells(data.total)}</tr>
  </tbody></table></div>;
}

function downloadCsv(data: CrossSummary) {
  const header = ["空间", "来源", "评测 ID", "已评 / 样本", ...RELATIVE_ORDER.map((key) => RELATIVE[key]),
    "基线更优占比", "基线更优 95% 置信区间", "AI 评审稳定性（位置交换一致率）", "候选重要及以上问题", "基线重要及以上问题", "候选 P95 秒", "基线 P95 秒"];
  const line = (name: string, source: string, id: string, planned: number, summary: AtomicSummary) => [
    name, source, id, `${summary.judged}/${planned}`, ...RELATIVE_ORDER.map((key) => summary.relatives[key]),
    summary.judged ? (summary.relatives.B / summary.judged).toFixed(3) : "", range(summary.intervals.B), consistency(summary),
    seriousCount(summary, "candidate"), seriousCount(summary, "baseline"),
    summary.performance.candidate.p95_ms == null ? "" : (summary.performance.candidate.p95_ms / 1000).toFixed(1),
    summary.performance.baseline.p95_ms == null ? "" : (summary.performance.baseline.p95_ms / 1000).toFixed(1)];
  const shown = revealed(data.runs);
  const rows = [header, ...shown.map((row) => line(row.space.name, row.space.source_name, row.id, row.planned, row.atomic)),
    line("合计", "", "", shown.reduce((sum, row) => sum + row.planned, 0), data.total)];
  const csv = "﻿" + rows.map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(",")).join("\n");
  downloadFile(csv, `跨空间汇总_${today()}.csv`, "text/csv;charset=utf-8");
}
