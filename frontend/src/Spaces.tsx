import { Button, Checkbox, Input, Modal, ModalBody, ModalContent, ModalFooter, ModalHeader, useDisclosure } from "@heroui/react";
import { ArrowLeft, ArrowRight, ChevronDown, ChevronRight, Pencil, Play, Plus, RefreshCw } from "lucide-react";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  defaultWindow, messageOf, api, stamp, submit, toggled, type Capture, type CaptureOutcome, type Dataset, type Draft,
  type Mode, type Snapshot, type Space, type SpaceModelChange, type SpaceProbe,
} from "./api";
import { Empty, Notice, PageHead, Status, useApp, useRemote } from "./App";
import {
  loadSpaceModels, lockReason, modelIndex, modelLabel, probeAll, probeSpace, ProbeChip, ProbeResult, SpaceModelDialog,
} from "./SpaceModels";

const STRATEGIES = {
  golden: {
    mode: "golden_regression", title: "逐轮回放",
    summary: "从来源空间采集真实会话，审核为固定参考数据集，回放到 EVAL 空间逐轮对比。",
  },
  atomic: {
    mode: "atomic_replay", title: "单步回放",
    summary: "从 SLS 采集生产模型调用，用候选模型等价回放，与基线输出对比评分。",
  },
} as const satisfies Record<string, { mode: Mode; title: string; summary: string }>;
type Strategy = keyof typeof STRATEGIES;

type ModelEdit = { spaces: Space[]; reset: boolean };
// 页面内刚跑完的测试结果：null 表示测试中，字符串表示测试请求本身失败
type LiveProbes = Record<string, SpaceProbe | string | null>;

export function SpacesPage() {
  const { capabilities } = useApp();
  const spaces = useRemote(() => api<Space[]>("/spaces"), []);
  // 执行模型单独读取：网关不可用时空间列表照常展示
  const models = useRemote(loadSpaceModels, []);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<ModelEdit | null>(null);
  const [live, setLive] = useState<LiveProbes>({});
  const index = modelIndex(models.data);
  const probing = Object.values(live).some((probe) => probe === null);
  // 读不到网关映射或空间被锁定时不能勾选
  const editable = (space: Space) => !!index.get(space.id) && !lockReason(index.get(space.id));
  const selectable = spaces.data?.filter(editable) || [];
  const picked = selectable.filter((space) => chosen.has(space.id));
  async function sync() {
    setBusy(true); setError("");
    try {
      const result = await submit<{ synced: number; active: number; needs_check: number }>("/registry/sync", {});
      setMessage(`已同步 ${result.synced} 个空间：${result.active} 个可用，${result.needs_check} 个待核验。`);
      spaces.reload(); models.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  function saved(text: string) {
    setEditing(null); setChosen(new Set()); setLive({}); setMessage(text); models.reload();
  }
  async function testPicked() {
    const ids = picked.map((space) => space.id);
    setLive((current) => ({ ...current, ...Object.fromEntries(ids.map((id) => [id, null])) }));
    await probeAll(ids, (id, probe) => setLive((current) => ({ ...current, [id]: probe })));
  }
  return <>
    <PageHead title="评测空间" description="空间从飞书台账只读同步；来源空间只采集，EVAL 空间只执行测试。"
      action={<Button onPress={sync} isLoading={busy} isDisabled={!capabilities.registry_configured}
        startContent={<RefreshCw size={15} />}>同步台账</Button>} />
    <Notice error={error || spaces.error}>{message || (!capabilities.registry_configured && "尚未配置飞书应用的只读授权。")}</Notice>
    {models.error && <Notice error={`执行模型读取失败：${models.error}`} />}
    {picked.length > 0 && <div className="selection-bar"><span>已选 {picked.length} 个空间</span>
      <div className="button-row">
        <Button size="sm" variant="light" isDisabled={probing} onPress={() => void testPicked()}
          startContent={<Play size={14} />}>批量测试</Button>
        <Button size="sm" variant="light" onPress={() => setEditing({ spaces: picked, reset: true })}>恢复默认</Button>
        <Button size="sm" color="primary" variant="flat" onPress={() => setEditing({ spaces: picked, reset: false })}>批量设置执行模型</Button>
      </div></div>}
    <section className="panel">
      {!spaces.data?.length ? <Empty>尚无评测空间。配置只读台账授权后同步，或请运维检查空间映射。</Empty> :
          <div className="table-scroll"><table><thead><tr>
            <th><Checkbox size="sm" aria-label="全选可设置的空间" isDisabled={!selectable.length}
              isSelected={!!selectable.length && picked.length === selectable.length}
              isIndeterminate={picked.length > 0 && picked.length < selectable.length}
              onValueChange={(all) => setChosen(all ? new Set(selectable.map((space) => space.id)) : new Set())} /></th>
            <th>评测空间</th><th>源空间</th><th>EVAL Workspace</th><th>执行模型</th><th>测试结果</th><th>状态</th><th /></tr></thead><tbody>
          {spaces.data.map((space) => {
            const item = index.get(space.id);
            const locked = lockReason(item);
            const fresh = live[space.id];
            const probe = typeof fresh === "string" ? null : fresh;
            return <tr key={space.id}>
              <td><Checkbox size="sm" aria-label={`选择${space.name}`} isDisabled={!editable(space)}
                isSelected={chosen.has(space.id) && editable(space)}
                onValueChange={() => setChosen((current) => toggled(current, [space.id]))} /></td>
              <td><Link to={`/spaces/${space.id}`}>{space.name}</Link></td>
              <td>{space.source_name || "—"}</td><td className="mono">{space.target_workspace_id}</td>
              <td>{!item ? <span className="muted">—</span> : <span className="model-cell">
                <span className={item.model ? "mono" : "muted"}>{modelLabel(item.model)}</span>
                {locked ? <small className="warn">{locked}</small>
                  : <Button size="sm" variant="light" isIconOnly aria-label={`设置${space.name}的执行模型`}
                    onPress={() => setEditing({ spaces: [space], reset: false })}><Pencil size={14} /></Button>}</span>}</td>
              <td>{!(space.id in live) ? <span className="muted">—</span> : <span className="probe-cell"
                title={typeof fresh === "string" ? fresh : probe && !probe.ok ? probe.detail : undefined}>
                <ProbeChip running={fresh === null} probe={probe} error={typeof fresh === "string" ? fresh : undefined} />
                {probe?.ok && probe.model && <small className="muted mono">{probe.model}</small>}</span>}</td>
              <td><Status value={space.status} /></td>
              <td><Link to={`/spaces/${space.id}`} aria-label={`查看${space.name}`}><ArrowRight size={16} /></Link></td></tr>;
          })}</tbody></table></div>}
      <p className="hint">“待核验”表示台账状态不是“已验证”，或来源与 EVAL 空间的 AccessToken 未分别填写。
        执行模型以网关实时映射为准：EVAL 空间的 Agent 请求 {models.data?.from_model || "默认模型"} 时替换为该模型；逐轮回放排队或执行中的空间暂不能切换。</p>
    </section>
    {editing && models.data && <SpaceModelDialog spaces={editing.spaces} models={index} fromModel={models.data.from_model}
      reset={editing.reset} onClose={() => setEditing(null)} onSaved={saved} />}
  </>;
}

function SpaceFacts({ space }: { space: Space }) {
  return <div className="space-facts"><div><span>来源空间{space.source_name ? ` · ${space.source_name}` : ""}</span><strong>{space.source_workspace_id}</strong></div>
    <div><span>EVAL Workspace</span><strong>{space.target_workspace_id}</strong></div></div>;
}

type SpaceData = { space: Space; captures: Capture[]; drafts: Draft[]; datasets: Dataset[] };

export function SpacePage() {
  const { spaceId = "" } = useParams();
  const resource = useRemote(async (): Promise<SpaceData> => {
    const [space, captures, drafts, datasets] = await Promise.all([
      api<Space>(`/spaces/${spaceId}`), api<Capture[]>(`/spaces/${spaceId}/captures`),
      api<Draft[]>(`/spaces/${spaceId}/drafts`), api<Dataset[]>(`/spaces/${spaceId}/datasets`),
    ]);
    return { space, captures, drafts, datasets };
  }, [spaceId]);
  const data = resource.data;
  const count = (rows: { mode: Mode }[] | undefined, mode: Mode) => rows?.filter((row) => row.mode === mode).length ?? 0;
  return <>
    <Link className="back" to="/spaces"><ArrowLeft size={15} /> 评测空间</Link>
    <PageHead title={data?.space.name || "空间详情"} description="生产配置与 EVAL 配置按台账配对；采集和执行时由服务端从台账读取各自的 AccessToken。"
      action={<Status value={data?.space.status} />} />
    <Notice error={resource.error} />
    {data && <><SpaceFacts space={data.space} />
      <SpaceModelPanel space={data.space} />
      <div className="work-grid">{(Object.keys(STRATEGIES) as Strategy[]).map((key) => {
        const { mode, title, summary } = STRATEGIES[key];
        return <section className="panel" key={key}><div className="panel-head"><h2>{title}</h2></div>
          <p className="muted">{summary}</p>
          <p className="strategy-counts">采集 {count(data.captures, mode)}
            {key === "golden" && ` · 草稿 ${count(data.drafts, mode)}`} · 数据集 {count(data.datasets, mode)}</p>
          <Button as={Link} to={`/spaces/${data.space.id}/${key}`} color="primary" variant="flat"
            endContent={<ArrowRight size={15} />}>进入{title}</Button>
        </section>;
      })}</div></>}
  </>;
}


function SpaceModelPanel({ space }: { space: Space }) {
  const resource = useRemote(async () => {
    const [models, changes] = await Promise.all([
      loadSpaceModels(), api<SpaceModelChange[]>(`/spaces/${space.id}/model-changes`)]);
    return { models, changes };
  }, [space.id]);
  const [editing, setEditing] = useState<boolean | null>(null);
  const [message, setMessage] = useState("");
  const [testing, setTesting] = useState(false);
  const [probe, setProbe] = useState<SpaceProbe | null>(null);
  const [probeError, setProbeError] = useState("");
  const data = resource.data;
  const index = modelIndex(data?.models);
  const item = index.get(space.id);
  const locked = lockReason(item);
  async function test() {
    setTesting(true); setProbe(null); setProbeError("");
    try { setProbe(await probeSpace(space.id)); }
    catch (cause) { setProbeError(messageOf(cause)); }
    finally { setTesting(false); }
  }
  return <section className="panel"><div className="panel-head"><h2>执行模型</h2>
    {item && <div className="button-row">
      <Button size="sm" variant="flat" isLoading={testing} onPress={() => void test()}
        startContent={testing ? null : <Play size={14} />}>测试</Button>
      {!locked && item.model && <Button size="sm" variant="light" onPress={() => setEditing(true)}>恢复默认</Button>}
      {!locked && <Button size="sm" color="primary" variant="flat" onPress={() => setEditing(false)}>切换</Button>}</div>}</div>
    <Notice error={resource.error}>{message}</Notice>
    {data && item && <><p className="model-current"><span className={item.model ? "mono" : "muted"}>{modelLabel(item.model)}</span>
      {!testing && !probeError && <ProbeChip probe={probe} />}
      {locked && <small className="warn">{locked}，暂不能切换</small>}</p>
      <p className="muted">Agent 请求 <span className="mono">{data.models.from_model}</span> 时，网关替换为{item.model ? "该模型" : "它本身（未替换）"}。
        {item.other_mappings > 0 && ` 该租户另有 ${item.other_mappings} 条其他模型映射，不受这里的设置影响。`}</p>
      <ProbeResult probe={probe} running={testing} error={probeError} />
      {!!data.changes.length && <details className="hint"><summary>变更记录（{data.changes.length}）</summary>
        {data.changes.map((change) => <p key={change.id}>{stamp(change.created_at)} · {change.subject} ·{" "}
          {modelLabel(change.previous_model)} → {modelLabel(change.model)}</p>)}</details>}</>}
    {editing !== null && data && <SpaceModelDialog spaces={[space]} models={index} fromModel={data.models.from_model}
      reset={editing} onClose={() => setEditing(null)}
      onSaved={(text) => { setEditing(null); setProbe(null); setMessage(text); resource.reload(); }} />}
  </section>;
}

type StrategyData = { space: Space; captures: Capture[]; snapshots: Snapshot[]; drafts: Draft[]; datasets: Dataset[] };

export function StrategyPage() {
  const { spaceId = "", strategy = "" } = useParams();
  if (!(strategy in STRATEGIES)) return <Empty>页面不存在。请从评测空间重新进入。</Empty>;
  return <StrategyModule spaceId={spaceId} strategy={strategy as Strategy} />;
}

function StrategyModule({ spaceId, strategy }: { spaceId: string; strategy: Strategy }) {
  const { mode, title, summary } = STRATEGIES[strategy];
  const golden = strategy === "golden";
  const navigate = useNavigate();
  const dialog = useDisclosure();
  const resource = useRemote(async (): Promise<StrategyData> => {
    const [space, captures, snapshots, drafts, datasets] = await Promise.all([
      api<Space>(`/spaces/${spaceId}`), api<Capture[]>(`/spaces/${spaceId}/captures`),
      golden ? api<Snapshot[]>(`/spaces/${spaceId}/snapshots`) : Promise.resolve([]),
      golden ? api<Draft[]>(`/spaces/${spaceId}/drafts`) : Promise.resolve([]),
      api<Dataset[]>(`/spaces/${spaceId}/datasets`),
    ]);
    const mine = <T extends { mode: Mode }>(rows: T[]) => rows.filter((row) => row.mode === mode);
    return { space, captures: mine(captures), snapshots: mine(snapshots), drafts: mine(drafts), datasets: mine(datasets) };
  }, [spaceId, strategy]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = resource.data?.captures.some((job) => job.status === "queued" || job.status === "running");
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(resource.reload, 3000);
    return () => clearInterval(timer);
  }, [pending]);
  const [form, setForm] = useState(() => ({
    ...defaultWindow(), name: golden ? "客户对话参考草稿" : "原子请求样本",
    size: "20", model: "", quota: "20", minTurns: "3",
  }));
  const field = (key: keyof typeof form) => ({ value: form[key], onValueChange: (value: string) => setForm({ ...form, [key]: value }) });

  function open() {
    setForm({ ...form, ...defaultWindow() });
    setError("");
    dialog.onOpen();
  }
  async function begin() {
    setBusy(true); setError("");
    try {
      const range = { start: new Date(form.start).toISOString(), end: new Date(form.end).toISOString() };
      const body = golden
        ? { name: form.name, policy: { ...range, quota_per_workspace: Number(form.quota), min_turns: Number(form.minTurns) } }
        : { name: form.name, ...range, size: Number(form.size), model: form.model || null };
      await submit(`/spaces/${spaceId}/captures/${strategy}`, body, { "Idempotency-Key": crypto.randomUUID() });
      dialog.onClose();
      resource.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function createDraft(snapshotId: string) {
    setBusy(true); setError("");
    try {
      const draft = await submit<{ id: string }>(`/snapshots/${snapshotId}/drafts`, {
        name: `参考草稿 · ${resource.data?.space.name || ""}`,
      });
      navigate(`/drafts/${draft.id}`);
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }

  const data = resource.data;
  const active = data?.space.status === "active";
  return <>
    <Link className="back" to={`/spaces/${spaceId}`}><ArrowLeft size={15} /> {data?.space.name || "空间详情"}</Link>
    <PageHead title={title} description={summary}
      action={<Button color="primary" isDisabled={!active} onPress={open} startContent={<Plus size={15} />}>新建采集</Button>} />
    <Notice error={(!dialog.isOpen && error) || resource.error}>{data && !active && "空间尚未核验可用，暂不能采集。"}</Notice>
    {data && <><SpaceFacts space={data.space} />
      <section className="panel"><div className="panel-head"><h2>采集记录</h2>
        <Button size="sm" variant="light" onPress={resource.reload} aria-label="刷新"><RefreshCw size={14} /></Button></div>
        {!data.captures.length ? <Empty>尚无采集。点击“新建采集”开始。</Empty> :
          <div className="table-scroll"><table><thead><tr><th>名称</th><th>采集窗口</th><th>样本</th><th>状态</th><th>产物</th></tr></thead><tbody>
            {data.captures.map((job) => <CaptureRow key={job.id} job={job} golden={golden}
              action={job.dataset_version_id ? <Link to={`/evaluations/new?dataset=${job.dataset_version_id}`}>创建评测</Link> :
                golden && job.snapshot_id ? <Button size="sm" color="primary" variant="flat" isLoading={busy}
                  onPress={() => void createDraft(job.snapshot_id!)}>生成参考草稿</Button> : "—"} />)}</tbody></table></div>}
        <p className="hint">原始证据仅存服务端对象存储。</p>
      </section>
      {golden && <section className="panel"><div className="panel-head"><h2>参考草稿</h2><span className="muted">逐案例审核后发布为数据集</span></div>
        {!data.drafts.length ? <Empty>尚无草稿。采集成功后点击“生成参考草稿”。</Empty> :
          <div className="table-scroll"><table><thead><tr><th>名称</th><th>审核进度</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>
            {data.drafts.map((draft) => <tr key={draft.id}><td><Link to={`/drafts/${draft.id}`}>{draft.name}</Link></td>
              <td>{typeof draft.reviewed_cases === "number" ? draft.reviewed_cases : Object.keys(draft.reviewed_cases).length} 案例已审核</td>
              <td><Status value={draft.status} /></td><td>{stamp(draft.updated_at)}</td>
              <td><Link to={`/drafts/${draft.id}`} aria-label={`打开${draft.name}`}><ArrowRight size={16} /></Link></td></tr>)}</tbody></table></div>}
      </section>}
      <section className="panel"><div className="panel-head"><h2>数据集</h2><Link to={golden ? "/golden" : "/atomic/datasets"}>全部数据集 <ArrowRight size={14} /></Link></div>
        {!data.datasets.length ? <Empty>{golden ? "尚无数据集。草稿审核完成后发布。" : "尚无数据集。采集成功后自动冻结为数据集。"}</Empty> :
          <div className="table-scroll"><table><thead><tr><th>名称 / 版本</th><th>发布时间</th><th /></tr></thead><tbody>
            {data.datasets.map((dataset) => <tr key={dataset.id}><td><strong>{dataset.name}</strong><small className="block mono muted">{dataset.id}</small></td>
              <td>{stamp(dataset.created_at)}</td>
              <td className="action-cell"><Link to={`/evaluations/new?dataset=${dataset.id}`}>创建评测</Link></td></tr>)}</tbody></table></div>}
      </section></>}
    <Modal isOpen={dialog.isOpen} onOpenChange={dialog.onOpenChange} size="lg">
      <ModalContent>
        <ModalHeader>新建{title}采集</ModalHeader>
        <ModalBody>
          <p className="muted">{golden ? "从来源空间采集会话、冻结画像字段和业务配置，生成待人工审核的参考草稿。"
            : "只收集包含完整请求与生产输出、可等价回放的模型调用。"}</p>
          <Notice error={error} />
          <div className="form-stack"><Input label="采集名称" {...field("name")} />
            <Input type="datetime-local" label="开始时间" {...field("start")} />
            <Input type="datetime-local" label="结束时间" {...field("end")} />
            {golden ? <div className="form-row"><Input type="number" label="会话配额" {...field("quota")} />
              <Input type="number" label="最少轮次" {...field("minTurns")} /></div>
              : <div className="form-row"><Input type="number" label="样本数" min={1} max={300} description="最多 300 条；替换决策建议每个空间 100–200 条" {...field("size")} />
                <Input label="原模型筛选（可选）" {...field("model")} /></div>}
          </div>
        </ModalBody>
        <ModalFooter>
          <Button variant="light" onPress={dialog.onClose}>取消</Button>
          <Button color="primary" isLoading={busy} isDisabled={!form.name.trim() || !form.start || !form.end}
            onPress={() => void begin()} startContent={<Plus size={15} />}>开始采集</Button>
        </ModalFooter>
      </ModalContent>
    </Modal>
  </>;
}

const EXCLUSIONS: Record<string, string> = {
  http_error: "请求失败", body_logging_disabled: "未记录请求体", unparsable_record: "日志无法解析",
  response_body_missing: "缺少响应体", response_body_invalid_json: "响应体不是合法 JSON",
  request_body_invalid_json: "请求体不是合法 JSON", request_id_missing: "缺少请求 ID",
  request_id_conflict: "请求 ID 冲突", not_responses_call: "非 Responses 调用",
  not_complete_call_record: "调用记录不完整", response_incomplete: "响应未完成",
  workspace_mismatch: "空间不匹配", model_mismatch: "原模型不匹配", empty_output: "输出为空",
  no_current_turn: "缺少当前轮次", visitor_missing: "缺少访客", bot_user_mismatch: "机器人不匹配",
  contact_filter_mismatch: "联系人筛选不匹配", group_chat: "群聊", sandbox: "沙箱会话",
  incomplete_history: "历史不完整", empty_history: "历史为空", no_visitor_message: "没有访客消息",
  missing_first_customer_message_time: "缺少首条客户消息时间", too_few_turns: "轮次不足",
  outside_latest_trigger_quota: "超出配额",
};

const minute = (value: string) => new Date(value).toLocaleString("zh-CN",
  { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });

function hours(start: string, end: string) {
  const value = (new Date(end).getTime() - new Date(start).getTime()) / 3600000;
  return value >= 1 ? `${Math.round(value * 10) / 10} 小时` : `${Math.round(value * 60)} 分钟`;
}

const joined = (counts: Record<string, number> | undefined, label = (key: string) => key) =>
  Object.entries(counts || {}).map(([key, count]) => `${label(key)} ×${count}`).join(" · ");

// 分层键形如 "场景|有工具|无图|中"：按维度汇总，只展示有区分度的维度
function composition(strata: Record<string, number> | undefined) {
  const dimensions: Record<string, number>[] = [];
  Object.entries(strata || {}).forEach(([key, count]) => key.split("|").forEach((value, index) => {
    dimensions[index] = dimensions[index] || {};
    dimensions[index][value] = (dimensions[index][value] || 0) + count;
  }));
  return dimensions.filter((dimension) => !("unknown" in dimension && Object.keys(dimension).length === 1))
    .map((dimension) => Object.entries(dimension).map(([value, count]) => `${value} ${count}`).join(" · "))
    .join("  ｜  ");
}

function CaptureRow({ job, golden, action }: { job: Capture; golden: boolean; action: ReactNode }) {
  const [open, setOpen] = useState(false);
  const outcome: CaptureOutcome = job.outcome || {};
  const range = outcome.window || (job.request.start && job.request.end
    ? { start: job.request.start, end: job.request.end } : null);
  const target = golden ? job.request.quota_per_workspace : job.request.size;
  const unit = golden ? "个会话" : "条";
  const shortfall = golden ? outcome.counts?.quota_shortfall : outcome.funnel?.shortfall;
  const scanLimit = job.request.max_scanned_sessions ?? 1000;
  const scanLimited = golden && !!shortfall && (outcome.counts?.scanned ?? 0) >= scanLimit;
  const done = job.status === "succeeded";
  const elapsed = job.finished_at
    ? Math.max(0, Math.round((new Date(job.finished_at).getTime() - new Date(job.created_at).getTime()) / 100) / 10) : null;
  return <Fragment>
    <tr>
      <td><button type="button" className="row-toggle" onClick={() => setOpen(!open)} aria-expanded={open} disabled={!done}>
        {done && (open ? <ChevronDown size={14} /> : <ChevronRight size={14} />)}{job.name}</button>
        <small className="block muted">{stamp(job.created_at)}</small></td>
      <td>{range ? <>{minute(range.start)} → {minute(range.end)}<small className="block muted">共 {hours(range.start, range.end)}</small></> : "—"}</td>
      <td>{done ? <><span className={shortfall ? "warn" : undefined}>{outcome.count ?? "—"} / {target ?? "—"} {unit}</span>
          {shortfall ? <small className="block warn">{scanLimited
            ? `已达到扫描上限（${scanLimit} 个会话），当前数量不代表窗口内全部合格会话`
            : golden ? "窗口内符合条件的会话不足，可扩大时间窗口或调整最少轮次"
              : "窗口内可回放样本不足，可放大时间窗口"}</small> : null}
          {!golden && outcome.models && <small className="block muted">基线模型 {joined(outcome.models)}</small>}
          {golden && job.request.min_turns && <small className="block muted">最少 {job.request.min_turns} 轮</small>}</>
        : <span className="muted">目标 {target ?? "—"} {unit}</span>}</td>
      <td><Status value={job.status} />{job.status === "failed" &&
        <small className="block muted">{job.error_message || "采集失败，请联系管理员查看 Worker 日志"}</small>}</td>
      <td>{action}</td>
    </tr>
    {open && done && <tr className="detail-row"><td colSpan={5}><dl className="capture-detail">
      {golden ? <>
        <dt>采集漏斗</dt><dd>扫描 {outcome.counts?.scanned ?? "—"} 个会话 → 纳入 {outcome.counts?.included ?? outcome.count ?? "—"} 个</dd>
        <dt>排除原因</dt><dd>{joined(outcome.counts?.excluded, (key) => EXCLUSIONS[key] || key) || "无"}</dd>
        <dt>画像字段</dt><dd>{outcome.profile_fields?.length ?? 0} 个{outcome.profile_fields?.length ? `：${outcome.profile_fields.join("、")}` : ""}</dd>
        {!!outcome.warnings?.length && <><dt>提醒</dt><dd>{outcome.warnings.join("；")}</dd></>}
      </> : <>
        <dt>采集漏斗</dt><dd>扫描 {outcome.capture?.rows ?? "—"} 条 → 可回放 {outcome.funnel?.eligible ?? "—"} 条 → 按分层抽样 {outcome.funnel?.selected ?? "—"} 条</dd>
        <dt>排除原因</dt><dd>{joined(outcome.funnel?.excluded, (key) => EXCLUSIONS[key] || key) || "无"}</dd>
        <dt>样本构成</dt><dd>{composition(outcome.funnel?.selected_strata) || "—"}</dd>
        {job.request.model && <><dt>原模型筛选</dt><dd>{job.request.model}</dd></>}
        {!!outcome.guidance?.length && <><dt>采集建议</dt><dd>{outcome.guidance.join(" ")}</dd></>}
      </>}
      {elapsed !== null && <><dt>用时</dt><dd>{elapsed} 秒</dd></>}
    </dl></td></tr>}
  </Fragment>;
}
