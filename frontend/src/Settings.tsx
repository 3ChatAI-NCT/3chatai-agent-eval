import { Button, Input, Switch } from "@heroui/react";
import { Copy, PlugZap } from "lucide-react";
import { useEffect, useState } from "react";
import {
  messageOf, api, modeName, stamp, submit, type CandidatePreset, type GatewayModel, type JudgeConfig,
  type JudgeSettings, type ProbeResult, type ReviewConfig,
} from "./api";
import { Empty, Notice, PageHead, useApp, useRemote } from "./App";

// 评审方法本身的约束：改动会让前后评测不可比，因此只展示不开放配置
const FIXED_POLICIES = [
  ["温度", "0，结果尽量可复现"],
  ["输出", "强制 JSON，按协议逐字段校验"],
  ["思考模式", "千问模型关闭：思考模式不支持 JSON 输出，实测会截断"],
  ["盲评", "隐藏模型名，交换 A/B 顺序独立评两次"],
  ["引用", "每个问题须引用原文，找不到的作废"],
  ["网络失败", "超时、429、5xx 自动重试，最多 3 次"],
];

export function SettingsPage() {
  const { refresh } = useApp();
  const resource = useRemote(() => api<JudgeSettings>("/settings/judge"), []);
  const models = useRemote(() => api<GatewayModel[]>("/settings/judge/models"), []);
  const data = resource.data;
  const [form, setForm] = useState<JudgeConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<{ ok: boolean; latency_ms: number; detail: string } | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => { if (data) setForm(data.config); }, [data?.revision]);
  if (!data || !form) return <><PageHead title="设置" /><Notice error={resource.error} />{!resource.error && <Empty>正在读取配置…</Empty>}</>;
  const changed = JSON.stringify(form) !== JSON.stringify(data.config);
  const valid = !!form.model.trim() && form.timeout_seconds >= 30 && form.timeout_seconds <= 600
    && form.format_attempts >= 1 && form.format_attempts <= 5;
  const provider = models.data?.find((item) => item.id === form.model)?.provider;
  const latest = data.history[0];

  async function save() {
    setBusy(true); setError(""); setMessage("");
    try {
      const saved = await api<{ revision: number }>("/settings/judge", {
        method: "PUT", body: JSON.stringify({ config: form, revision: data!.revision }) });
      setMessage(`已保存为第 ${saved.revision} 版，之后创建的评测使用新配置；已创建的评测不受影响。`);
      resource.reload(); refresh();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  async function test() {
    setProbing(true); setProbe(null); setError("");
    try { setProbe(await submit("/settings/judge/probe", form)); }
    catch (cause) { setError(messageOf(cause)); }
    finally { setProbing(false); }
  }

  return <>
    <PageHead title="设置" description="全局 Judge 配置与评审提示词。每次创建评测都会冻结当时的配置和提示词，修改只影响之后的新评测。" />
    <Notice error={error}>{message}</Notice>
    <div className="settings-grid">
      <section className="panel"><div className="panel-head"><h2>Judge 配置</h2>
        <span className="muted">{latest ? `第 ${data.revision} 版 · ${latest.subject} 于 ${stamp(latest.created_at)} 修改` : "尚未修改，沿用部署默认值"}</span></div>
        <div className="form-stack">
          <label>Judge 模型
            <input className="field-select" list="gateway-models" value={form.model} spellCheck={false}
              onChange={(event) => setForm({ ...form, model: event.target.value.trim() })} />
            <datalist id="gateway-models">{models.data?.map((item) => <option key={item.id} value={item.id}>{item.provider}</option>)}</datalist>
            <small className="block muted">{models.error ? "暂时读取不到网关模型列表，可直接输入模型名。"
              : `经 3Chat 模型网关调用${provider ? `，供应商 ${provider}` : ""}。建议与候选、基线模型来自不同厂商，减少同源模型互相偏好。`}</small>
          </label>
          <div className="form-row">
            <Input type="number" label="单次调用超时（秒）" min={30} max={600} value={String(form.timeout_seconds)}
              description="30–600 秒；单条盲评请求较长，建议不低于 120 秒"
              onValueChange={(value) => setForm({ ...form, timeout_seconds: Number(value) })} />
            <Input type="number" label="格式重试次数" min={1} max={5} value={String(form.format_attempts)}
              description="输出不符合 JSON 协议时重新请求，1–5 次"
              onValueChange={(value) => setForm({ ...form, format_attempts: Number(value) })} />
          </div>
          <div className="button-row">
            <Button color="primary" isLoading={busy} isDisabled={!changed || !valid} onPress={() => void save()}>保存配置</Button>
            <Button variant="flat" isLoading={probing} isDisabled={!valid} startContent={<PlugZap size={15} />} onPress={() => void test()}>测试连通</Button>
            {changed && <Button variant="light" onPress={() => setForm(data.config)}>放弃修改</Button>}
          </div>
          {probe && <Notice error={probe.ok ? undefined : `连通失败：${probe.detail}（${probe.latency_ms} ms）`}>
            {probe.ok && `连通正常：${form.model} 按 JSON 协议返回，用时 ${probe.latency_ms} ms。`}</Notice>}
        </div>
      </section>
      <section className="panel"><div className="panel-head"><h2>固定的评审方法</h2><span className="muted">不开放配置</span></div>
        <dl className="kv policy-list">{FIXED_POLICIES.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
        <p className="hint">这些约束决定结论能否跨批次比较。需要调整时应走代码评审，并用人工标注重新校准。</p>
      </section>
    </div>
    {!!data.history.length && <section className="panel"><div className="panel-head"><h2>修改记录</h2></div>
      <div className="table-scroll"><table><thead><tr><th>版本</th><th>Judge 模型</th><th>超时</th><th>格式重试</th><th>修改人</th><th>时间</th></tr></thead><tbody>
        {data.history.map((row) => <tr key={row.revision}><td>v{row.revision}</td><td>{row.config.model}</td>
          <td>{row.config.timeout_seconds}s</td><td>{row.config.format_attempts} 次</td>
          <td className="mono">{row.subject}</td><td>{stamp(row.created_at)}</td></tr>)}</tbody></table></div>
    </section>}
    <ReportSettings />
    <CandidatePresets />
    <Prompts data={data} />
  </>;
}

type PresetForm = { id?: string; name: string; base_url: string; model: string; api_key: string; note: string; revision: number; key_hint?: string };
const EMPTY_PRESET: PresetForm = { name: "", base_url: "https://", model: "", api_key: "", note: "", revision: 0 };

// 外部接口的候选模型保存一次，创建评测时直接选；key 只写不读
function CandidatePresets() {
  const presets = useRemote(() => api<CandidatePreset[]>("/settings/candidates"), []);
  const [form, setForm] = useState<PresetForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [probe, setProbe] = useState<{ id: string; result: ProbeResult } | null>(null);
  const [confirming, setConfirming] = useState("");
  const [error, setError] = useState("");
  const fail = (cause: unknown) => setError(messageOf(cause));

  async function save() {
    if (!form) return;
    setBusy(true); setError("");
    const body = { name: form.name, base_url: form.base_url, model: form.model, note: form.note,
      revision: form.revision, api_key: form.api_key || null };
    try {
      if (form.id) await api(`/settings/candidates/${form.id}`, { method: "PUT", body: JSON.stringify(body) });
      else await submit("/settings/candidates", body);
      setForm(null); presets.reload();
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }
  async function test(target: PresetForm) {
    setError(""); setProbe(null);
    try {
      const result = await submit<ProbeResult>("/settings/candidates/probe", {
        preset_id: target.id || null, base_url: target.base_url, model: target.model, api_key: target.api_key || null });
      setProbe({ id: target.id || "new", result });
    } catch (cause) { fail(cause); }
  }
  async function remove(id: string) {
    if (confirming !== id) { setConfirming(id); return; }
    setConfirming(""); setError("");
    try { await api(`/settings/candidates/${id}`, { method: "DELETE" }); presets.reload(); } catch (cause) { fail(cause); }
  }
  const probeNotice = (id: string) => probe?.id === id && <Notice error={probe.result.ok ? undefined : `连通失败：${probe.result.detail}（${probe.result.latency_ms} ms）`}>
    {probe.result.ok && `连通正常，用时 ${probe.result.latency_ms} ms。`}</Notice>;
  const valid = form && form.name.trim() && form.base_url.startsWith("https://") && form.model.trim() && (form.id || form.api_key.trim());

  return <section className="panel"><div className="panel-head"><h2>候选模型</h2>
    <Button size="sm" color="primary" variant="flat" onPress={() => { setForm({ ...EMPTY_PRESET }); setProbe(null); }}>新增外部接口</Button></div>
    <p className="muted">已接入 3Chat 网关的模型无需配置，创建评测时可直接选择，与生产请求走同一链路。没接入网关的模型在这里保存接口地址、模型与 API Key，之后创建评测直接选用。
      Key 加密保存、页面永不回显；每次评测会复制一份，评测结束自动清除，修改或删除这里不影响已创建的评测。</p>
    <Notice error={error || presets.error} />
    {presets.data?.length ? <div className="table-scroll"><table><thead><tr><th>名称</th><th>模型</th><th>接口地址</th><th>Key</th><th>备注</th><th>最近修改</th><th /></tr></thead><tbody>
      {presets.data.map((item) => <tr key={item.id}><td><strong>{item.name}</strong></td><td>{item.model}</td>
        <td className="mono">{item.base_url}</td><td className="mono">{item.key_hint}</td><td>{item.note || "—"}</td>
        <td><span className="mono">{item.updated_by}</span><small className="block muted">{stamp(item.updated_at)}</small></td>
        <td><div className="action-cell">
          <Button size="sm" variant="light" onPress={() => test({ ...item, api_key: "", revision: item.revision })}>测试连通</Button>
          <Button size="sm" variant="light" onPress={() => { setForm({ ...item, api_key: "" }); setProbe(null); }}>编辑</Button>
          <Button size="sm" variant="light" color="danger" onPress={() => void remove(item.id)}>{confirming === item.id ? "确认删除" : "删除"}</Button>
        </div>{probeNotice(item.id)}</td></tr>)}</tbody></table></div>
      : !presets.loading && <p className="muted">还没有保存的外部接口。</p>}
    {form && <div className="selection-box candidate-box preset-form"><strong>{form.id ? `编辑「${form.name}」` : "新增外部接口"}</strong>
      <div className="form-row">
        <Input label="名称" placeholder="例如 测试接口 · candidate-model" value={form.name} onValueChange={(name) => setForm({ ...form, name })} />
        <Input label="模型名" placeholder="例如 candidate-model" value={form.model} onValueChange={(model) => setForm({ ...form, model })} />
      </div>
      <Input label="接口地址" description="兼容 OpenAI Responses 接口，平台会调用 {接口地址}/responses；只接受公网 HTTPS 域名"
        value={form.base_url} onValueChange={(base_url) => setForm({ ...form, base_url })} />
      <Input type="password" label="API Key" autoComplete="new-password" value={form.api_key}
        placeholder={form.id ? `留空则沿用原 key（${form.key_hint}）` : "建议使用评测专用、带额度上限的 key"}
        onValueChange={(api_key) => setForm({ ...form, api_key })} />
      <Input label="备注" placeholder="可选，例如 key 的用途与额度" value={form.note} onValueChange={(note) => setForm({ ...form, note })} />
      <div className="button-row">
        <Button color="primary" isLoading={busy} isDisabled={!valid} onPress={() => void save()}>保存</Button>
        <Button variant="flat" isDisabled={!valid} startContent={<PlugZap size={15} />} onPress={() => void test(form)}>测试连通</Button>
        <Button variant="light" onPress={() => setForm(null)}>取消</Button>
      </div>
      {probeNotice(form.id || "new")}
    </div>}
  </section>;
}

function Prompts({ data }: { data: JudgeSettings }) {
  const [mode, setMode] = useState(data.prompts[0]?.mode);
  const [viewing, setViewing] = useState<string | null>(null);
  const prompt = data.prompts.find((item) => item.mode === mode);
  const usage = data.prompt_usage.filter((item) => item.mode === mode);
  const historic = usage.find((item) => item.sha256 === viewing);
  return <section className="panel"><div className="panel-head"><h2>评审提示词</h2>
    <div className="segmented" role="tablist">{data.prompts.map((item) => <button key={item.mode} type="button" role="tab"
      aria-selected={item.mode === mode} className={item.mode === mode ? "selected" : ""}
      onClick={() => { setMode(item.mode); setViewing(null); }}>{modeName(item.mode)}</button>)}</div></div>
    {prompt && <>
      <p className="muted">当前版本 <strong>{prompt.version}</strong> · SHA256 <span className="mono">{prompt.sha256.slice(0, 16)}…</span> · 共 {prompt.text.length} 字。
        提示词随代码评审发布，页面只读；创建评测时正文冻结进评测记录。</p>
      <PromptText text={prompt.text} />
    </>}
    <h3>版本使用情况</h3>
    {usage.length ? <div className="table-scroll"><table><thead><tr><th>版本</th><th>SHA256</th><th>评测数</th><th>首次使用</th><th>最近使用</th><th /></tr></thead><tbody>
      {usage.map((row) => <tr key={row.sha256}><td>{row.version}{row.sha256 === prompt?.sha256 ? " · 当前" : ""}</td>
        <td className="mono">{row.sha256.slice(0, 12)}…</td><td>{row.runs}</td><td>{stamp(row.first_used)}</td><td>{stamp(row.last_used)}</td>
        <td>{row.text && row.sha256 !== prompt?.sha256 && <Button size="sm" variant="light"
          onPress={() => setViewing(viewing === row.sha256 ? null : row.sha256)}>{viewing === row.sha256 ? "收起" : "查看原文"}</Button>}</td></tr>)}</tbody></table></div>
      : <p className="muted">还没有评测使用过该类提示词。</p>}
    {historic?.text && <><h3>{historic.version} 原文</h3><PromptText text={historic.text} /></>}
  </section>;
}

function PromptText({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return <div className="prompt-text">
    <Button size="sm" variant="flat" className="copy-button" startContent={<Copy size={13} />}
      onPress={() => void navigator.clipboard.writeText(text).then(() => setCopied(true))}>{copied ? "已复制" : "复制"}</Button>
    <pre>{text}</pre>
  </div>;
}

type ReportSettingsData = { config: ReviewConfig; revision: number;
  history: { revision: number; config: Partial<ReviewConfig>; subject: string; created_at: string }[] };

const SAMPLE_FIELDS: [keyof Omit<ReviewConfig, "blind_review">, string, string, number][] = [
  ["random_percent", "随机样本比例 %", "按每条评测的可评样本量计算，向上取整；用来估计 AI 与人工的一致率", 100],
  ["min_random", "随机样本下限（条）", "样本量小时至少抽这么多条，否则一致率的区间过宽", 300],
  ["risk_percent", "风险样本比例 %", "从 AI 判为分歧、指出重要问题或基线更优的样本里另抽，只核对 AI 的判断，不计入一致率", 100],
];

// 人工盲评的抽样规则：创建评测时冻结，修改只影响之后创建的评测
function ReportSettings() {
  const { refresh } = useApp();
  const resource = useRemote(() => api<ReportSettingsData>("/settings/report"), []);
  const [form, setForm] = useState<ReviewConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const data = resource.data;
  const latest = data?.history[0];
  useEffect(() => { if (data) setForm(data.config); }, [data]);
  const changed = !!form && !!data && JSON.stringify(form) !== JSON.stringify(data.config);
  const valid = !!form && SAMPLE_FIELDS.every(([key, , , max]) => Number.isInteger(form[key])
    && form[key] >= (key === "risk_percent" ? 0 : 1) && form[key] <= max);
  async function save() {
    if (!data || !form) return;
    setBusy(true); setError("");
    try {
      await api("/settings/report", { method: "PUT", body: JSON.stringify({ config: form, revision: data.revision }) });
      resource.reload(); refresh();
    } catch (cause) { setError(messageOf(cause)); }
    finally { setBusy(false); }
  }
  return <section className="panel"><div className="panel-head"><h2>人工盲评</h2>
    <span className="muted">{latest ? `${latest.subject} 于 ${stamp(latest.created_at)} 修改` : "尚未修改，使用默认值"}</span></div>
    <Notice error={error || resource.error} />
    {form && <>
      <div className="setting-row">
        <div><strong>单步回放评测结束后先抽样盲评</strong>
          <p className="muted">揭晓前所有人都看不到 AI 评审结论、候选模型与 Judge 信息，报告页只呈现抽中的样本，双方显示为「输出 A / 输出 B」。
            样本全部评完并提交后揭晓，报告给出 AI 与人工的一致率；急用时可写明原因提前揭晓。只影响之后创建的评测。</p></div>
        <Switch isSelected={form.blind_review} isDisabled={busy} onValueChange={(blind_review) => setForm({ ...form, blind_review })}
          aria-label="评测结束后先抽样盲评" />
      </div>
      {form.blind_review && <div className="form-row sample-fields">{SAMPLE_FIELDS.map(([key, label, description, max]) =>
        <Input key={key} type="number" label={label} description={description} min={key === "risk_percent" ? 0 : 1} max={max}
          value={String(form[key])} onValueChange={(value) => setForm({ ...form, [key]: Number(value) })} />)}</div>}
      <div className="editor-actions"><Button color="primary" isLoading={busy} isDisabled={!changed || !valid} onPress={() => void save()}>保存</Button>
        {form.blind_review && <small className="muted">例如 100 条可评样本：随机 {Math.min(100, Math.max(form.min_random, Math.ceil(form.random_percent)))} 条，
          风险最多 {Math.ceil(form.risk_percent)} 条。</small>}</div>
    </>}
  </section>;
}
