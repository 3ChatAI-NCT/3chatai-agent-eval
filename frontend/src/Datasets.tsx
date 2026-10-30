import { Button, Checkbox, Progress, Switch, Textarea } from "@heroui/react";
import { ArrowLeft, ArrowRight, Check, GitBranchPlus, Save, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  messageOf, api, modeName, stamp, submit, type Dataset, type Draft, type GoldenMessage, type GoldenSession, type Space,
} from "./api";
import { Empty, Notice, PageHead, Status, useApp, useRemote } from "./App";
import { SectionHead, type Section } from "./Sections";
import { Images } from "./report/Context";

type Data = { spaces: Space[]; drafts: Draft[]; datasets: Dataset[] };

export function DatasetsPage({ section }: { section: Section }) {
  const navigate = useNavigate();
  const [showArchived, setShowArchived] = useState(false);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const resource = useRemote(async (): Promise<Data> => {
    const spaces = await api<Space[]>("/spaces");
    const rows = await Promise.all(spaces.map(async (space) => {
      const [drafts, datasets] = await Promise.all([
        api<Draft[]>(`/spaces/${space.id}/drafts?include_archived=${showArchived}`), api<Dataset[]>(`/spaces/${space.id}/datasets?include_archived=${showArchived}`),
      ]);
      return { drafts, datasets };
    }));
    return { spaces, drafts: rows.flatMap((row) => row.drafts),
      datasets: rows.flatMap((row) => row.datasets) };
  }, [showArchived]);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  async function derive(dataset: Dataset) {
    setBusy(dataset.id); setError("");
    try {
      const draft = await submit<{ id: string }>(`/datasets/${dataset.id}/derive-draft`, {});
      navigate(`/drafts/${draft.id}`);
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(""); }
  }
  async function archive(archived: boolean) {
    setBusy("archive"); setError("");
    try {
      for (const kind of ["drafts", "datasets"]) {
        const ids = [...chosen].filter((key) => key.startsWith(`${kind}:`)).map((key) => key.slice(kind.length + 1));
        if (ids.length) await submit(`/data/${kind}/archive`, { ids, archived });
      }
      setChosen(new Set()); resource.reload();
    } catch (cause) { setError(messageOf(cause)); } finally { setBusy(""); }
  }
  const pick = (kind: string, row: Draft | Dataset) => <Checkbox aria-label={`选择${row.name}`} size="sm"
    isSelected={chosen.has(`${kind}:${row.id}`)} onValueChange={() => setChosen((current) => {
      const next = new Set(current); const key = `${kind}:${row.id}`;
      if (next.has(key)) next.delete(key); else next.add(key); return next;
    })} />;
  const data = resource.data;
  const mode = section === "atomic" ? "atomic_replay" : "golden_regression";
  const selected = (row: { space_id: string; mode: string }) =>
    (!filter || row.space_id === filter) && row.mode === mode;
  const spaceName = (id: string) => data?.spaces.find((space) => space.id === id)?.name || id;
  const sourceName = (id: string) => data?.spaces.find((space) => space.id === id)?.source_name || "—";
  return <>
    <SectionHead section={section} />
    <div className="filters"><select className="field-select" aria-label="按空间筛选" value={filter} onChange={(event) => setFilter(event.target.value)}>
        <option value="">全部空间</option>{data?.spaces.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}
      </select>
      <Switch size="sm" isSelected={showArchived} onValueChange={(value) => { setShowArchived(value); setChosen(new Set()); }}>显示已归档</Switch>
      <Button size="sm" variant="flat" onPress={() => setChosen(new Set([
        ...(data?.drafts.filter(selected).map((row) => `drafts:${row.id}`) || []),
        ...(data?.datasets.filter(selected).map((row) => `datasets:${row.id}`) || []),
      ].slice(0, 200)))}>选择当前列表</Button>
      {chosen.size > 0 && <><span>已选 {chosen.size} 项</span><Button size="sm" isLoading={busy === "archive"} onPress={() => void archive(true)}>归档所选</Button>
        {showArchived && <Button size="sm" onPress={() => void archive(false)}>恢复所选</Button>}
        <Button size="sm" variant="light" onPress={() => setChosen(new Set())}>取消选择</Button></>}
      </div>
    <Notice error={error || resource.error} />
    {section === "golden" && <section className="panel"><div className="panel-head"><h2>参考草稿</h2><span className="muted">任何内部成员均可审核</span></div>
      {!data?.drafts.filter(selected).length ? <Empty>暂无参考草稿。可直接批量采样并回放，或在空间中采样后生成审核草稿。</Empty> :
        <div className="table-scroll"><table><thead><tr><th>选择</th><th>名称</th><th>空间</th><th>源空间</th><th>审核进度</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>
          {data.drafts.filter(selected).map((draft) => <tr key={draft.id} className={draft.archived_at ? "archived-row" : ""}><td>{pick("drafts", draft)}</td><td>{draft.archived_at && <small className="block muted">已归档</small>}<Link to={`/drafts/${draft.id}`}>{draft.name}</Link></td>
            <td>{spaceName(draft.space_id)}</td><td>{sourceName(draft.space_id)}</td><td>{typeof draft.reviewed_cases === "number" ? draft.reviewed_cases : Object.keys(draft.reviewed_cases).length} 案例已审核</td>
            <td><Status value={draft.status} /></td><td>{stamp(draft.updated_at)}</td><td><Link to={`/drafts/${draft.id}`}><ArrowRight size={16} /></Link></td></tr>)}</tbody></table></div>}
    </section>}
    <section className="panel"><div className="panel-head"><h2>已冻结的数据集版本</h2></div>
      {!data?.datasets.filter(selected).length ? <Empty>暂无已发布数据集。</Empty> :
        <div className="table-scroll"><table><thead><tr><th>选择</th><th>名称 / 版本</th><th>空间</th><th>源空间</th><th>策略</th><th>发布时间</th><th>操作</th></tr></thead><tbody>
          {data.datasets.filter(selected).map((dataset) => <tr key={dataset.id} className={dataset.archived_at ? "archived-row" : ""}><td>{pick("datasets", dataset)}</td><td>{dataset.archived_at && <small className="block muted">已归档</small>}<strong>{dataset.name}</strong><small className="block mono muted">{dataset.id}</small></td>
            <td>{spaceName(dataset.space_id)}</td><td>{sourceName(dataset.space_id)}</td><td>{modeName(dataset.mode)}</td><td>{stamp(dataset.created_at)}</td>
            <td className="action-cell"><Link to={`/evaluations/new?mode=${dataset.mode}&dataset=${dataset.id}`}>创建评测</Link>
              {dataset.mode === "golden_regression" && <Button size="sm" variant="light" isLoading={busy === dataset.id}
                onPress={() => void derive(dataset)} startContent={<GitBranchPlus size={14} />}>修订</Button>}</td></tr>)}</tbody></table></div>}
      {section === "golden" && <p className="hint">旧报告继续引用原数据集版本；修订会产生新草稿与新版本。</p>}
    </section>
  </>;
}

type FullDraft = Draft & { content: { items: GoldenSession[]; shared: Record<string, unknown> } | null;
  reviewed_cases: Record<string, { reviewer: string; sha256: string }> };
type Annotation = { id: string; session_id: string; status: string; error_code?: string;
  error_detail?: { stage: string; error_type: string; message: string };
  profile_prompt_version: string; profile_prompt_sha256: string;
  rubric_prompt_version: string; rubric_prompt_sha256: string };

function MessageMedia({ message, label }: { message?: GoldenMessage; label: string }) {
  if (!message) return null;
  const images = (message.attachments || []).filter((item) => item.preview_url);
  return <>
    <Images urls={images.map((item) => item.preview_url!)} label={label} />
    {(message.attachments || []).filter((item) => !item.preview_url).map((item, index) =>
      <small className="block muted" key={index}>附件：{item.name} · 暂不支持预览</small>)}
  </>;
}

function profileValue(value: unknown): string {
  if (value == null || value === "" || Array.isArray(value) && !value.length) return "未填写";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function DatasetEditor() {
  const { capabilities } = useApp();
  const { draftId = "" } = useParams();
  const navigate = useNavigate();
  const resource = useRemote(() => api<FullDraft>(`/drafts/${draftId}`), [draftId]);
  const annotations = useRemote(() => api<Annotation[]>(`/drafts/${draftId}/annotations`), [draftId]);
  useEffect(() => {
    const timer = window.setInterval(() => { annotations.reload(); resource.reload(); }, 5000);
    return () => window.clearInterval(timer);
    // Polling is scoped to the current draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftId]);
  const [caseIndex, setCaseIndex] = useState(0);
  const [turnIndex, setTurnIndex] = useState(0);
  const [rubric, setRubric] = useState("");
  const [messages, setMessages] = useState<string[]>([]);
  const [profile, setProfile] = useState("{}");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const draft = resource.data;
  const session = draft?.content?.items[caseIndex];
  const turn = session?.turns[turnIndex];
  useEffect(() => {
    setRubric(turn?.rubric || "");
    setMessages(turn?.expected.messages.map((item) => item.content) || []);
    setProfile(JSON.stringify(turn?.expected.profile_update || {}, null, 2));
  }, [turn]);

  async function edit() {
    if (!draft || !session || !turn) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const parsed = JSON.parse(profile) as Record<string, unknown>;
      const profileChanged = profile !== JSON.stringify(turn.expected.profile_update || {}, null, 2);
      await api(`/drafts/${draftId}/cases/${encodeURIComponent(session.session_id)}`, {
        method: "PATCH", headers: { "If-Match": String(draft.revision) },
        body: JSON.stringify({ turn_index: turnIndex, rubric,
          expected_messages: messages.map((text, index) => text === turn.expected.messages[index].content ? null : text),
          ...(profileChanged ? { expected_profile_update: parsed } : {}) }),
      });
      setMessage("案例已保存；修改后的案例需要重新审核。"); resource.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function review() {
    if (!draft || !session) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await submit(`/drafts/${draftId}/cases/${encodeURIComponent(session.session_id)}/review`, {},
                   { "If-Match": String(draft.revision) });
      setMessage("案例审核已记录。"); resource.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function autoDraft() {
    if (!session) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await submit(`/drafts/${draftId}/cases/${encodeURIComponent(session.session_id)}/auto-draft`, {},
                   { "Idempotency-Key": crypto.randomUUID() });
      setMessage("已提交 AI 画像补全和评分标准起草；完成后请逐轮核对并重新审核。");
      annotations.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function autoDraftAll() {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await submit<{ jobs: Annotation[]; skipped_success: string[]; skipped_active: string[] }>(
        `/drafts/${draftId}/auto-draft`, {}, { "Idempotency-Key": crypto.randomUUID() });
      setMessage(`已提交 ${result.jobs.length} 个案例；跳过 ${result.skipped_success.length} 个已成功案例、${result.skipped_active.length} 个处理中案例。完成后请核对并审核。`);
      annotations.reload();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function publish() {
    if (!draft) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await submit(`/drafts/${draftId}/publish`, {}, { "If-Match": String(draft.revision) });
      navigate("/golden");
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  const reviewed = Object.keys(draft?.reviewed_cases || {}).length;
  const total = draft?.content?.items.length || 0;
  const currentAnnotation = annotations.data?.find((item) => item.session_id === session?.session_id);
  const latestAnnotations = new Map<string, Annotation>();
  annotations.data?.forEach((item) => {
    if (!latestAnnotations.has(item.session_id)) latestAnnotations.set(item.session_id, item);
  });
  const annotationCounts = { succeeded: 0, failed: 0, queued: 0, running: 0 };
  latestAnnotations.forEach((item) => {
    if (item.status in annotationCounts) annotationCounts[item.status as keyof typeof annotationCounts]++;
  });
  const profileSchema = (draft?.content?.shared.context as { profile_schema?: { key: string; label?: string; description?: string }[] } | undefined)?.profile_schema || [];
  const profileUpdates = Object.entries(turn?.expected.profile_update || {});
  const remainingAnnotations = total - annotationCounts.succeeded - annotationCounts.queued - annotationCounts.running;
  const annotationWorking = currentAnnotation?.status === "queued" || currentAnnotation?.status === "running";
  const changed = turn && (rubric !== turn.rubric || messages.some((item, index) => item !== turn.expected.messages[index]?.content)
    || profile !== JSON.stringify(turn.expected.profile_update || {}, null, 2));
  return <>
    <Link className="back" to="/golden"><ArrowLeft size={15} /> 参考数据集</Link>
    <PageHead title={draft?.name || "参考草稿"} description="核对客户输入、参考回复、画像变化与评分要求；审核记录只绑定当前内容版本。"
      action={<Status value={draft?.status} />} />
    <Notice error={error || resource.error}>{message}</Notice>
    {draft && !draft.content && <Empty>此草稿已发布。请在数据集列表查看冻结版本或创建修订草稿。</Empty>}
    {draft?.content && <><div className="progress-line"><span>{reviewed} / {total} 案例已审核</span><Progress value={total ? reviewed / total * 100 : 0} size="sm" aria-label="案例审核进度" />
      <Button color="primary" isDisabled={draft.status !== "draft" || reviewed !== total || !!changed || busy}
        isLoading={busy} onPress={() => void publish()}>发布数据集版本</Button></div>
      <div className="panel annotation-panel"><div className="panel-head"><div><h2>AI 画像补全与评分标准起草</h2>
        <p className="hint">初始评分标准由规则预填；AI 补全后仍需人工核对。已成功的自动跳过，失败的可重新提交。</p></div>
        <Button variant="flat" startContent={<Sparkles size={15} />} isLoading={busy}
          isDisabled={!capabilities.annotation_configured || !!changed || busy || remainingAnnotations <= 0 || draft.status !== "draft"}
          onPress={() => void autoDraftAll()}>一键补全全部待处理案例</Button></div>
        <p className="hint">成功 {annotationCounts.succeeded} / {total} · 运行中 {annotationCounts.running} · 排队 {annotationCounts.queued} · 失败 {annotationCounts.failed} · 未处理 {total - latestAnnotations.size}</p>
      </div>
      <div className="review-layout"><aside className="case-nav"><h2>案例</h2>{draft.content.items.map((item, index) =>
        <button key={item.session_id} type="button" className={`case-button ${index === caseIndex ? "selected" : ""}`}
          onClick={() => { setCaseIndex(index); setTurnIndex(0); }}>
          <span>{item.session_id}</span><small>{draft.reviewed_cases[item.session_id] ? "已审核" : "待审核"} · {item.turns.length} 轮
            {latestAnnotations.get(item.session_id) && ` · AI ${({ succeeded: "成功", failed: "失败", queued: "排队", running: "运行中" } as Record<string, string>)[latestAnnotations.get(item.session_id)!.status]}`}</small>
        </button>)}</aside>
        <section className="case-body">{session && turn && <><div className="panel-head"><h2>{session.session_id}</h2><Status value={draft.reviewed_cases[session.session_id] ? "completed" : "pending"} /></div>
          <div className="turn-tabs">{session.turns.map((item, index) => <button key={item.turn_id} type="button"
            className={index === turnIndex ? "selected" : ""} onClick={() => setTurnIndex(index)}>第 {item.seq} 轮</button>)}</div>
          <h3>客户输入</h3>{turn.input.customer_messages.map((item, index) => <div className="message-box" key={`${turn.turn_id}-${index}`}>{item.content}
            <MessageMedia message={item} label="客户发送的图片" /></div>)}
          <h3>参考回复</h3><div className="form-stack">{messages.map((item, index) => <div key={`${turn.turn_id}-${index}`}><Textarea
            label={`参考回复 ${index + 1}`} value={item} onValueChange={(value) => setMessages(messages.map((old, n) => n === index ? value : old))}
            minRows={3} isDisabled={draft.status !== "draft"} />
            <MessageMedia message={turn.expected.messages[index]} label={`参考回复 ${index + 1}的图片`} /></div>)}
            {(turn.input.customer_messages.some((item) => item.attachments?.length) || turn.expected.messages.some((item) => item.attachments?.length)) &&
              <p className="hint">图片可点击查看大图。当前 AI 补全与评审只读取文字和附件信息，图片内容需人工核对。</p>}
            <Textarea label="评分标准 Rubric" description="明确何时适用、什么算达标、哪些错误不可接受。" value={rubric}
              onValueChange={setRubric} minRows={4} isDisabled={draft.status !== "draft"} />
            <div><h3>本轮画像变化</h3>{profileUpdates.length ? <div className="table-scroll"><table>
              <thead><tr><th>画像字段</th><th>更新前</th><th>更新后</th><th>来源</th></tr></thead>
              <tbody>{profileUpdates.map(([key, update]) => {
                const definition = profileSchema.find((item) => item.key === key);
                return <tr key={key}><td title={definition?.description}>{definition?.label || key}</td>
                  <td>{profileValue(update.updated_from)}</td><td>{profileValue(update.value)}</td>
                  <td>{{ pulled: "平台日志", ai: "AI 补全", manual: "人工修订" }[update.source || "ai"]}</td></tr>;
              })}</tbody></table></div> : <p className="muted">本轮暂无已记录的画像更新。</p>}</div>
            <details><summary>画像增量与来源（高级）</summary><Textarea label="本轮画像增量 JSON" value={profile} onValueChange={setProfile}
              minRows={5} isDisabled={draft.status !== "draft"} /></details></div>
          <div className="editor-actions"><Button variant="flat" startContent={<Save size={15} />} isDisabled={!changed || draft.status !== "draft"}
            isLoading={busy} onPress={() => void edit()}>保存修改</Button>
            <Button variant="flat" startContent={<Sparkles size={15} />}
              isDisabled={!capabilities.annotation_configured || !!changed || busy || annotationWorking || draft.status !== "draft"}
              onPress={() => void autoDraft()}>{currentAnnotation?.status === "failed" ? "重试当前案例 AI 补全" : "AI 补全当前案例"}</Button>
            <Button color="primary" startContent={<Check size={15} />} isDisabled={!!changed || draft.status !== "draft" || !!draft.reviewed_cases[session.session_id]}
              isLoading={busy} onPress={() => void review()}>审核当前案例</Button></div>
          {currentAnnotation && <div className="hint">AI 起草：<Status value={currentAnnotation.status} />
            {currentAnnotation.error_code && ` · ${currentAnnotation.error_detail
              ? `${currentAnnotation.error_detail.stage}: ${currentAnnotation.error_detail.message}`
              : "生成失败，请重新发起或人工编辑"}`}<br />
            画像 {currentAnnotation.profile_prompt_version} · {currentAnnotation.profile_prompt_sha256.slice(0, 12)}；
            Rubric {currentAnnotation.rubric_prompt_version} · {currentAnnotation.rubric_prompt_sha256.slice(0, 12)}
          </div>}
          <p className="hint">任何修改都会撤销当前案例的旧审核。发布要求所有案例按最新内容完成审核。</p>
        </>}</section>
        <aside className="case-context"><h2>审核依据</h2><p>检查参考回复是否符合该客户的 SOP、知识库和服务承诺，并确认评分标准可由证据判断。</p>
          <div className="basis"><strong>当前数据集</strong><span>{draft.name}</span><strong>来源快照</strong>
            <span>{(draft.content.shared.dataset as { source_config?: { snapshot_id?: string } } | undefined)?.source_config?.snapshot_id || "人工导入"}</span></div>
          <Link to="/evaluations/new?mode=golden_regression" className="muted">发布后创建评测 <ArrowRight size={14} /></Link></aside>
      </div></>}
  </>;
}
