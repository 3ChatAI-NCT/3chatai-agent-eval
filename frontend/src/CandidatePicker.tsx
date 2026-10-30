import { Input } from "@heroui/react";
import { Link } from "react-router-dom";
import { api, type CandidatePreset, type GatewayModel } from "./api";

// 候选模型的来源选择：单次评测三选一，批量评测不支持临时填写（key 要保存到所有空间开跑）
export type CandidateSource = "gateway" | "preset" | "manual";
export type CandidateChoice = { source: CandidateSource; value: string };
export type ManualCandidate = { base_url: string; model: string; api_key: string };
export type CandidateSources = { presets: CandidatePreset[]; models: GatewayModel[] };

const SOURCES: [CandidateSource, string][] = [["gateway", "3Chat 网关模型"], ["preset", "已保存的接口"], ["manual", "临时填写"]];
const CHOICE_KEY = "agent-eval.candidate-choice";

export async function loadCandidateSources(): Promise<CandidateSources> {
  const [presets, models] = await Promise.all([
    api<CandidatePreset[]>("/settings/candidates"),
    // 网关模型列表读不到时仍可选已保存的接口或临时填写
    api<GatewayModel[]>("/settings/judge/models").catch(() => []),
  ]);
  return { presets, models };
}

// 记住本浏览器上次的选择；读不到或已失效时依次回退到已保存的接口、网关模型、临时填写
export function initialChoice({ presets, models }: CandidateSources, allowManual = true): CandidateChoice {
  try {
    const saved = JSON.parse(localStorage.getItem(CHOICE_KEY) || "null") as CandidateChoice | null;
    if ((saved?.source === "manual" && allowManual) || (saved?.source === "preset" && presets.some((item) => item.id === saved.value))
      || (saved?.source === "gateway" && models.some((item) => item.id === saved.value))) return saved;
  } catch { /* 浏览器禁用存储时按默认选择 */ }
  if (presets.length) return { source: "preset", value: presets[0].id };
  if (models.length || !allowManual) return { source: "gateway", value: "" };
  return { source: "manual", value: "" };
}

export function rememberChoice(choice: CandidateChoice) {
  try { localStorage.setItem(CHOICE_KEY, JSON.stringify(choice)); } catch { /* 忽略 */ }
}

export function candidateBody(choice: CandidateChoice, manual?: ManualCandidate) {
  if (choice.source === "gateway") return { candidate_gateway_model: choice.value };
  if (choice.source === "preset") return { candidate_preset_id: choice.value };
  return { candidate: manual };
}

export function candidateReady(choice: CandidateChoice | null, manual?: ManualCandidate) {
  if (!choice) return false;
  if (choice.source !== "manual") return !!choice.value;
  return !!(manual && manual.base_url.trim() && manual.model.trim() && manual.api_key);
}

export function CandidatePicker({ sources, choice, onChange, manual, onManualChange }: {
  sources: CandidateSources; choice: CandidateChoice; onChange: (choice: CandidateChoice) => void;
  // 不传 manual 即不提供「临时填写」
  manual?: ManualCandidate; onManualChange?: (manual: ManualCandidate) => void;
}) {
  const allowManual = !!manual && !!onManualChange;
  const fallback = allowManual ? "请选择已保存的接口或临时填写" : "请选择已保存的接口";
  return <div className="selection-box candidate-box"><strong>候选模型</strong>
    <div className="segmented" role="tablist">{SOURCES.filter(([value]) => allowManual || value !== "manual").map(([value, text]) =>
      <button key={value} type="button" role="tab" aria-selected={choice.source === value} className={choice.source === value ? "selected" : ""}
        onClick={() => choice.source !== value && onChange({ source: value, value: value === "preset" ? sources.presets[0]?.id || "" : "" })}>{text}</button>)}</div>
    {choice.source === "gateway" && (sources.models.length ? <label>网关模型<select className="field-select" value={choice.value}
      onChange={(event) => onChange({ source: "gateway", value: event.target.value })}><option value="">请选择</option>
      {sources.models.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.provider}</option>)}</select>
      <small className="block muted">由平台用网关 key 调用，无需填写 key；与生产请求走同一链路，延迟对比更可信。</small></label>
      : <span>暂时读取不到 3Chat 网关的模型列表，{fallback}。</span>)}
    {choice.source === "preset" && (sources.presets.length ? <label>已保存的接口<select className="field-select" value={choice.value}
      onChange={(event) => onChange({ source: "preset", value: event.target.value })}><option value="">请选择</option>
      {sources.presets.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.model} · key {item.key_hint}</option>)}</select>
      <small className="block muted">Key 从设置页的预设加密复制到评测，评测结束后自动清除。</small></label>
      : <span>还没有保存的外部接口，<Link to="/settings">去设置页添加</Link>{allowManual ? "，或选择临时填写" : ""}。</span>)}
    {choice.source === "manual" && manual && onManualChange && <>
      <Input label="接口地址" placeholder="https://api.example.com/v1" description="兼容 OpenAI Responses 接口，平台会调用 {接口地址}/responses"
        value={manual.base_url} onValueChange={(base_url) => onManualChange({ ...manual, base_url })} />
      <Input label="模型名" placeholder="例如 candidate-model" value={manual.model}
        onValueChange={(model) => onManualChange({ ...manual, model })} />
      <Input type="password" label="API Key" autoComplete="off" description="加密保存，仅用于本次评测，评测结束后自动清除；常用的接口可在设置页保存"
        value={manual.api_key} onValueChange={(api_key) => onManualChange({ ...manual, api_key })} /></>}
    {!allowManual && <small className="muted">批量评测不支持临时填写 key：key 需要保存到所有空间开跑，请先在设置页保存为接口。</small>}
  </div>;
}
