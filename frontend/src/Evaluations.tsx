import {
  Button, Checkbox, Chip, Input, Modal, ModalBody, ModalContent, ModalFooter, ModalHeader, Switch,
} from "@heroui/react";
import { ArrowLeft, ArrowRight, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  toggled, isRunning, blindPending, messageOf, api, modeName, stamp, submit, type BlindSample,
  type Dataset, type EvalResult, type Experiment, type Report, type Space, type TraceReadiness,
} from "./api";
import { Empty, Notice, PageHead, Status, useApp, useNavSection, useRemote } from "./App";
import { AtomicReport } from "./AtomicReport";
import { GoldenReport } from "./GoldenReport";
import { BlindReviewPage } from "./report/BlindReview";
import { RUNS, SECTION_NAME, SectionHead, sectionOf, type Section } from "./Sections";
import { loadSpaceModels, lockReason, modelIndex, modelLabel } from "./SpaceModels";
import { blindRule } from "./atomic";
import {
  CandidatePicker, candidateBody, candidateReady, initialChoice, loadCandidateSources, rememberChoice,
  type CandidateChoice, type ManualCandidate,
} from "./CandidatePicker";

// 重新判分会生成新记录：沿来源链找到最初那次评测，把同一条链收拢成一组，默认只看最新版本
function versionGroups(runs: Experiment[]) {
  const byId = new Map(runs.map((run) => [run.id, run]));
  const root = (run: Experiment) => {
    let current = run;
    const seen = new Set<string>();
    while (current.source_experiment_id && byId.has(current.source_experiment_id) && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.source_experiment_id)!;
    }
    return current.id;
  };
  const groups = new Map<string, Experiment[]>();
  for (const run of runs) groups.set(root(run), [...(groups.get(root(run)) || []), run]);
  return [...groups.values()].map((items) => items.sort((a, b) => b.created_at.localeCompare(a.created_at)))
    .sort((a, b) => b[0].created_at.localeCompare(a[0].created_at));
}

export function EvaluationsPage({ section }: { section: Section }) {
  const mode = section === "atomic" ? "atomic_replay" : "golden_regression";
  const records = useRemote(() => api<Experiment[]>("/experiments?include_archived=true"), []);
  const spaces = useRemote(() => api<Space[]>("/spaces"), []);
  const [showArchived, setShowArchived] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Experiment | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleting, setDeleting] = useState(false);
  const own = (records.data || []).filter((run) => run.mode === mode);
  useEffect(() => {
    if (records.loading || !own.some((run) => isRunning(run.status))) return;
    const timer = window.setTimeout(records.reload, 5000);
    return () => window.clearTimeout(timer);
  }, [records.data, records.loading, mode]);
  const isVisible = (run: Experiment) => showArchived || !run.archived_at;
  const visible = own.filter(isVisible);
  // 先按全部记录收拢版本，再过滤可见的，避免来源评测被归档后版本链被拆散
  const groups = versionGroups(own).map((group) => group.filter(isVisible)).filter((group) => group.length);
  const [message, setMessage] = useState("");
  const archivedCount = own.filter((run) => run.archived_at).length;
  const toggle = (ids: string[]) => setSelected((current) => toggled(current, ids));
  const chosen = visible.filter((run) => selected.has(run.id));
  const sourceName = (spaceId: string) => spaces.data?.find((space) => space.id === spaceId)?.source_name || "—";
  async function archive(archived: boolean) {
    setBusy(true); setError(""); setMessage("");
    const ids = chosen.filter((run) => !run.archived_at === archived).map((run) => run.id);
    try {
      const { changed } = await submit<{ changed: number }>("/experiments/archive", { ids, archived });
      if (changed < ids.length) setMessage(`${ids.length - changed} 条评测尚未结束，未${archived ? "归档" : "恢复"}。`);
      setSelected(new Set()); records.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function deleteArchived() {
    if (!deleteTarget || deleteConfirmation !== deleteTarget.name) return;
    setDeleting(true); setError("");
    try {
      await api(`/experiments/${deleteTarget.id}`, { method: "DELETE" });
      setSelected((current) => { const next = new Set(current); next.delete(deleteTarget.id); return next; });
      setDeleteTarget(null); setDeleteConfirmation(""); records.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setDeleting(false); }
  }
  const row = (run: Experiment, group: Experiment[], version: number | null) => {
    const collapsed = version === null && group.length > 1 && !expanded.has(group[0].id);
    const ids = collapsed ? group.map((item) => item.id) : [run.id];
    return <tr key={run.id} className={`${run.archived_at ? "archived-row" : ""} ${version ? "version-row" : ""}`}>
      <td><Checkbox size="sm" aria-label="选择评测" isSelected={ids.every((id) => selected.has(id))} onValueChange={() => toggle(ids)} /></td>
      <td><Link to={`/evaluations/${run.id}`}>{run.name}</Link>
        {run.trial && <Chip size="sm" variant="flat" className="inline-chip">试跑</Chip>}
        {run.archived_at && <Chip size="sm" variant="flat" className="inline-chip">已归档</Chip>}
        {version && <small className="block muted">第 {version} 版</small>}
        {version === null && group.length > 1 && <button type="button" className="link-button"
          onClick={() => setExpanded((current) => toggled(current, [group[0].id]))}>
          {expanded.has(group[0].id) ? "收起历史版本" : `共 ${group.length} 个版本，展开历史`}</button>}</td>
      <td>{sourceName(run.space_id)}</td><td><Status value={run.display_status || run.status} />
        {run.progress && <>
          <small className="block">{run.progress.executing ? `正在回放 ${run.progress.executing} 条` : ""}
            {run.progress.executing && run.progress.judging ? " · " : ""}
            {run.progress.judging ? `正在评分 ${run.progress.judging} 条` : ""}</small>
          <small className="block muted">已执行 {run.progress.executed + run.progress.execution_failed + run.progress.uncertain} / {run.progress.planned}
            {" · "}已评分 {run.progress.judged}
            {!!run.progress.waiting && ` · 待执行 ${run.progress.waiting}`}</small>
        </>}
        {run.display_status === "partial" && !!run.execution_failed &&
          <small className="block muted">{run.execution_failed} 条未评测</small>}</td>
        <td><Status value={blindPending(run.blind_status) ? run.blind_status : run.review_status} /></td>
      <td>{stamp(run.created_at)}</td><td><div className="action-cell">
        {run.archived_at && run.deletable && <Button size="sm" variant="light" color="danger"
          onPress={() => { setDeleteTarget(run); setDeleteConfirmation(""); setError(""); }}>删除</Button>}
        <Link to={`/evaluations/${run.id}`} aria-label={`查看${run.name}`}><ArrowRight size={16} /></Link>
      </div></td></tr>;
  };
  return <>
    <SectionHead section={section} />
    <Notice error={error || records.error || spaces.error}>{message}</Notice>
    {visible.some((run) => isRunning(run.status)) && <div className="stat-grid report-stats">
      <div className="stat-card"><strong>{visible.filter((run) => isRunning(run.status)).length}</strong><span>进行中的评测</span></div>
      <div className="stat-card"><strong>{visible.reduce((n, run) => n + (run.progress?.executing || 0), 0)}</strong><span>正在回放</span></div>
      <div className="stat-card"><strong>{visible.reduce((n, run) => n + (run.progress?.judging || 0), 0)}</strong><span>正在评分</span></div>
      <div className="stat-card"><strong>{visible.reduce((n, run) => n + (run.progress?.waiting || 0), 0)}</strong><span>等待执行</span></div>
    </div>}
    <section className="panel"><div className="panel-head"><h2>全部记录</h2>
      <div className="filters">
        {chosen.length > 0 && <>
          <span className="muted">已选 {chosen.length} 条</span>
          {chosen.some((run) => !run.archived_at) && <Button size="sm" variant="flat" isLoading={busy} onPress={() => void archive(true)}>归档</Button>}
          {chosen.some((run) => run.archived_at) && <Button size="sm" variant="flat" isLoading={busy} onPress={() => void archive(false)}>恢复</Button>}
          {section === "atomic" && <Button size="sm" variant="flat" color="primary" as={Link}
            to={`/summary?ids=${chosen.map((run) => run.id).join(",")}`}>汇总所选</Button>}
        </>}
        <Switch size="sm" isSelected={showArchived} onValueChange={setShowArchived}>显示已归档{archivedCount ? `（${archivedCount}）` : ""}</Switch>
      </div></div>
      {!groups.length ? <Empty>{section === "atomic" ? "尚无评测记录。可从批量评测或采样数据创建。" : "尚无回放记录。选择已发布的参考数据集创建逐轮回放。"}</Empty> :
        <div className="table-scroll"><table><thead><tr><th /><th>评测</th><th>源空间</th><th>运行状态</th><th>人工复核</th><th>时间</th><th /></tr></thead><tbody>
          {groups.flatMap((group) => [row(group[0], group, null),
            ...(expanded.has(group[0].id) ? group.slice(1).map((run, index) => row(run, group, group.length - 1 - index)) : [])])}</tbody></table></div>}
      <p className="hint">同一评测的后续执行自动汇入进度与报告；重新评分按版本收拢，默认只显示最新一版。
        归档只从列表隐藏，数据与审计保留，可随时恢复；试跑评测结束满 7 天自动归档。仅已归档、无人复核且未被重新评分引用的记录可永久删除。</p>
    </section>
    <Modal isOpen={!!deleteTarget} onClose={() => { if (!deleting) { setDeleteTarget(null); setDeleteConfirmation(""); } }}>
      <ModalContent>
        <ModalHeader>永久删除评测记录</ModalHeader>
        <ModalBody>
          <p>删除后，评测任务、事件和报告将无法恢复。请确认不再需要这条记录。</p>
          <Notice error={error} />
          <Input label="输入评测名称以确认" value={deleteConfirmation} onValueChange={setDeleteConfirmation}
            autoComplete="off" placeholder={deleteTarget?.name} />
        </ModalBody>
        <ModalFooter>
          <Button variant="light" isDisabled={deleting} onPress={() => { setDeleteTarget(null); setDeleteConfirmation(""); }}>取消</Button>
          <Button color="danger" isLoading={deleting} isDisabled={deleteConfirmation !== deleteTarget?.name}
            onPress={() => void deleteArchived()}>永久删除</Button>
        </ModalFooter>
      </ModalContent>
    </Modal>
  </>;
}

export function NewEvaluationPage() {
  const navigate = useNavigate();
  const [query] = useSearchParams();
  const { capabilities } = useApp();
  const [spaceId, setSpaceId] = useState("");
  const [datasetId, setDatasetId] = useState(query.get("dataset") || "");
  // 未手动修改前为 null，名称随所选数据集给出默认值
  const [name, setName] = useState<string | null>(null);
  const [candidate, setCandidate] = useState<ManualCandidate>({ base_url: "https://", model: "", api_key: "" });
  const [choice, setChoice] = useState<CandidateChoice | null>(null);
  const [repeats, setRepeats] = useState("1");
  const [trial, setTrial] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const reuseId = query.get("reuse");
  // 从栏目进入时带 mode，只列该策略的数据集
  const modeParam = query.get("mode");
  const resource = useRemote(async () => {
    const spaces = await api<Space[]>("/spaces");
    const [datasets, source, sources] = await Promise.all([
      Promise.all(spaces.map((space) => api<Dataset[]>(`/spaces/${space.id}/datasets`))),
      reuseId ? api<Experiment>(`/experiments/${reuseId}`) : Promise.resolve(null),
      loadCandidateSources(),
    ]);
    return { spaces, datasets: datasets.flat(), source, sources };
  }, [reuseId]);
  useEffect(() => {
    const data = resource.data;
    if (!data) return;
    setChoice(initialChoice(data.sources));
    if (data.source) {
      setSpaceId(data.source.space_id);
      setDatasetId(data.source.dataset_version_id);
      const config = (data.source.manifest as { config?: { repeats?: number } } | undefined)?.config;
      setRepeats(String(config?.repeats || 1));
    } else if (datasetId) {
      const found = data.datasets.find((item) => item.id === datasetId);
      if (found) setSpaceId(found.space_id);
    }
  }, [resource.data]);
  const datasets = resource.data?.datasets.filter((item) => item.space_id === spaceId) || [];
  const selected = datasets.find((item) => item.id === datasetId && (!modeParam || item.mode === modeParam)
    && (!resource.data?.source || item.mode === resource.data.source.mode));
  const source = resource.data?.source;
  const fresh = selected?.mode === "atomic_replay" && !reuseId;
  // 逐轮回放可只跑指定轮次；重新评分必须沿用来源评测的全部轮次
  const scopable = selected?.mode === "golden_regression" && !reuseId;
  const [scoped, setScoped] = useState(false);
  const [turns, setTurns] = useState<Set<string>>(new Set());
  const detail = useRemote(async () => scopable && selected ? api<Dataset>(`/datasets/${selected.id}`) : null,
    [scopable, selected?.id]);
  const sessions = detail.data && detail.data.id === selected?.id ? detail.data.content?.items || [] : [];
  useEffect(() => { setScoped(false); setTurns(new Set()); }, [selected?.id]);
  // 逐轮回放前检查 EVAL 空间是否开启了完整请求体日志；只提示，不拦截
  const readiness = useRemote(async () => scopable && spaceId
    ? { spaceId, body: await api<TraceReadiness>(`/spaces/${spaceId}/trace-readiness`) } : null, [scopable, spaceId]);
  const ready = readiness.data?.spaceId === spaceId ? readiness.data.body : null;
  // 逐轮回放在 EVAL 空间执行，创建时冻结该空间当前的执行模型
  const models = useRemote(async () => scopable ? loadSpaceModels() : null, [scopable]);
  const targetModel = modelIndex(models.data).get(spaceId);
  const runName = name ?? (source ? `${source.name} · 重新评分`
    : fresh ? `单步回放_${selected.name.split("_").pop()}` : "");
  const space = resource.data?.spaces.find((item) => item.id === spaceId);
  const section = sectionOf(modeParam || source?.mode || selected?.mode
    || resource.data?.datasets.find((item) => item.id === datasetId)?.mode);
  useNavSection(section);
  async function create() {
    if (!selected || !space) return;
    setBusy(true); setError("");
    try {
      const run = await submit<{ id: string }>(`/spaces/${space.id}/experiments`, {
        name: runName.trim(), dataset_version_id: selected.id, repeats: Number(repeats),
        execution_target: "platform", reuse_experiment_id: reuseId, trial,
        ...(scopable && scoped ? { sample_scope: [...turns] } : {}),
        ...(fresh && choice ? candidateBody(choice, candidate) : {}),
      }, { "Idempotency-Key": crypto.randomUUID() });
      if (fresh && choice) rememberChoice(choice);
      navigate(`/evaluations/${run.id}`);
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  return <>
    <Link className="back" to={RUNS[section]}><ArrowLeft size={15} /> {SECTION_NAME[section]}</Link>
    <PageHead title={reuseId ? "使用已有回答重新评分" : section === "golden" ? "创建逐轮回放" : "创建评测"}
      description={reuseId ? "沿用上次的回答与执行记录，仅按当前规则重新评分。不会重放对话或创建测试联系人；逐轮回放会读取 EVAL 空间配置。执行输入必须一致。" : "选择空间与不可变数据集版本，运行已核验的 EVAL 环境。"} />
    <Notice error={error || resource.error} />
    <section className="panel form-panel"><div className="form-stack">
      <label>评测空间<select className="field-select" value={spaceId} disabled={!!reuseId}
        onChange={(event) => { setSpaceId(event.target.value); setDatasetId(""); }}><option value="">请选择</option>
        {resource.data?.spaces.map((item) => <option key={item.id} value={item.id} disabled={item.status !== "active"}>{item.name}{item.source_name ? ` · ${item.source_name}` : ""} · {item.status === "active" ? "已验证" : "待核验"}</option>)}</select></label>
      <label>数据集版本<select className="field-select" value={datasetId} onChange={(event) => setDatasetId(event.target.value)}>
        <option value="">请选择</option>{datasets.filter((item) => (!source || item.mode === source.mode)
          && (!modeParam || item.mode === modeParam)).map((item) =>
          <option key={item.id} value={item.id}>{item.name} · {modeName(item.mode)} · {item.id.slice(-8)}</option>)}</select></label>
      {selected && <div className="selection-box"><strong>{selected.name}</strong><span>{modeName(selected.mode)} · 发布于 {stamp(selected.created_at)}</span>
        <small className="mono">SHA256 {selected.content_sha256.slice(0, 18)}…</small></div>}
      <Input label="评测名称" placeholder="例如：售后策略调整回归 2026-09" value={runName} onValueChange={setName} />
      {fresh && choice && resource.data && <CandidatePicker sources={resource.data.sources} choice={choice} onChange={setChoice}
        manual={candidate} onManualChange={setCandidate} />}
      <div className="form-row"><Input type="number" label="执行重复次数" value={repeats} min={1} max={5} isDisabled={!!reuseId} onValueChange={setRepeats} />
        </div>
      {ready?.status === "disabled" && <Notice>该 EVAL 空间近 {ready.window_hours} 小时的 {ready.calls} 次模型调用都没有完整请求体：评测可以正常执行和判分，但模型调用链路只有摘要（模型、耗时、tokens），没有输入输出。如需链路，请先在阿里云侧为该空间开启请求体日志。</Notice>}
      {ready?.status === "unknown" && <p className="hint">{ready.detail ? `无法预检请求体日志：${ready.detail}` : "该 EVAL 空间近 7 天没有模型调用记录，无法预先确认是否开启了完整请求体日志；首轮执行后会在报告中复核。"}</p>}
      {scopable && <div className="turn-scope"><Checkbox isSelected={scoped} onValueChange={setScoped}>只回放指定轮次</Checkbox>
        {scoped && (detail.loading && !sessions.length ? <p className="muted">正在加载数据集轮次…</p> : <div className="scope-list">
          {sessions.map((session) => {
            const ids = session.turns.map((turn) => turn.turn_id);
            const picked = ids.filter((id) => turns.has(id)).length;
            return <div key={session.session_id} className="scope-session">
              <Checkbox isSelected={picked === ids.length} isIndeterminate={picked > 0 && picked < ids.length}
                onValueChange={() => setTurns(toggled(turns, ids))}>{session.session_id}</Checkbox>
              <div className="scope-turns">{session.turns.map((turn) => <Checkbox key={turn.turn_id} size="sm"
                isSelected={turns.has(turn.turn_id)} onValueChange={() => setTurns(toggled(turns, [turn.turn_id]))}
                title={turn.input.customer_messages.map((message) => message.content).join(" ")}>第 {turn.seq} 轮</Checkbox>)}</div>
            </div>;
          })}
          <small className="muted">已选 {turns.size} 轮；只执行并判分这些轮次，报告标注为「指定轮次」。</small></div>)}</div>}
      <Checkbox isSelected={trial} onValueChange={setTrial}>试跑：用于调参或验证链路，结束满 7 天自动归档</Checkbox>
      <div className="selection-box"><strong>运行依据</strong>
        <span>自动评分模型：{capabilities.judge_configured ? capabilities.judge_model : "尚未配置"} · <Link to="/settings">在设置页调整</Link></span>
        <span>{reuseId ? `执行证据来自 ${reuseId}` : selected?.mode === "atomic_replay"
          ? "原样重放生产请求，只替换模型名，不写入任何空间。" : "每个待评轮次独立创建测试联系人，不写入生产空间。"}</span>
        {scopable && spaceId && <span>执行模型：{targetModel ? modelLabel(targetModel.model)
          : models.error ? `读取失败（${models.error}）` : "读取中…"}，创建时冻结进评测记录 · <Link to={`/spaces/${spaceId}`}>在空间页调整</Link>
          {targetModel && lockReason(targetModel) && <span className="warn">（{lockReason(targetModel)}）</span>}</span>}
        {selected?.mode === "atomic_replay" && <span>{blindRule(capabilities.blind_review)}</span>}</div>
      <Button color="primary" isLoading={busy} isDisabled={!space || space.status !== "active" || !selected || !runName.trim() || !capabilities.judge_configured
        || (fresh && !candidateReady(choice, candidate)) || (scopable && scoped && !turns.size)} onPress={() => void create()} startContent={<Play size={16} />}>
        {reuseId ? "检查后重新评分" : "开始评测"}</Button>
    </div></section>
  </>;
}

// 盲评揭晓前没有逐条结果，改取盲评样本
type EvaluationData = { id: string; run: Experiment; report: Report; results: EvalResult[]; sample: BlindSample | null;
  dataset: Dataset | null; space: Space | null };

export function EvaluationPage() {
  const { runId = "" } = useParams();
  const datasetCache = useRef<{ id: string; value: Dataset } | null>(null);
  async function datasetFor(id: string) {
    if (datasetCache.current?.id === id) return datasetCache.current.value;
    const value = await api<Dataset>(`/datasets/${id}`);
    datasetCache.current = { id, value };
    return value;
  }
  const resource = useRemote(async (): Promise<EvaluationData> => {
    const run = await api<Experiment>(`/experiments/${runId}`);
    const blind = blindPending(run.blind_status);
    const [report, results, sample, dataset, space] = await Promise.all([
      api<Report>(`/experiments/${runId}/report`),
      blind || run.mode === "atomic_replay" ? Promise.resolve([]) : api<EvalResult[]>(`/experiments/${runId}/results`),
      blind ? api<BlindSample>(`/experiments/${runId}/blind-review`) : Promise.resolve(null),
      run.mode === "golden_regression" ? datasetFor(run.dataset_version_id) : Promise.resolve(null),
      api<Space>(`/spaces/${run.space_id}`).catch(() => null),
    ]);
    return { id: runId, run, report, results, sample, dataset, space };
  }, [runId]);
  useEffect(() => {
    if (!resource.data || resource.data.id !== runId || !isRunning(resource.data.run.status) || resource.loading) return;
    const timer = window.setTimeout(resource.reload, 5000);
    return () => window.clearTimeout(timer);
  }, [resource.data?.run.status, resource.loading, runId]);
  const data = resource.data?.id === runId ? resource.data : null;
  useNavSection(data ? sectionOf(data.run.mode) : null);
  if (!data) return <><Link className="back" to={RUNS.golden}><ArrowLeft size={15} /> {SECTION_NAME.golden}</Link>
    <PageHead title="评测报告" description="正在加载报告" /><Notice error={resource.error} /></>;
  if (data.sample) {
    return <BlindReviewPage runId={runId} run={data.run} report={data.report} sample={data.sample}
      space={data.space} reload={resource.reload} />;
  }
  if (data.run.mode === "atomic_replay") {
    return <AtomicReport runId={runId} run={data.run} report={data.report}
      space={data.space} reload={resource.reload} />;
  }
  return <GoldenReport runId={runId} run={data.run} report={data.report} results={data.results}
    dataset={data.dataset} reload={resource.reload} />;
}
