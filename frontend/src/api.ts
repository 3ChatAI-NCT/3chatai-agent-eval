export type Mode = "atomic_replay" | "golden_regression";
export type Space = {
  id: string;
  name: string;
  source_name: string;
  source_workspace_id: string;
  target_workspace_id: string;
  status: "active" | "needs_check";
};
// 执行模型以网关实时查询为准；model 为 null 表示未替换，Agent 使用默认模型
// 一次性测试结果，不落库
export type SpaceProbe = { ok: boolean; latency_ms: number; model: string | null; reply: string;
  input_tokens: number | null; output_tokens: number | null; detail: string };
export type SpaceModel = { space_id: string; model: string | null; other_mappings: number; locked: boolean;
  shared: boolean; protected: boolean };
export type SpaceModels = { from_model: string; items: SpaceModel[] };
export type SpaceModelChange = { id: number; space_id: string; from_model: string; previous_model: string | null;
  model: string | null; subject: string; created_at: string };
export type SpaceModelResult = { changes: SpaceModelChange[]; mismatched: string[]; unknown_model: boolean | null };
export type TargetModel = { from_model: string; model?: string | null; error?: string };
export type Draft = { archived_at?: string | null;
  id: string;
  space_id: string;
  name: string;
  mode: Mode;
  status: string;
  revision: number;
  reviewed_cases: number | Record<string, { reviewer: string; sha256: string }>;
  content?: GoldenDataset;
  updated_at: string;
};
export type GoldenAttachment = { name: string; content_type: string; preview_url?: string };
export type GoldenMessage = { seq: number; type: string; content: string; attachments?: GoldenAttachment[] | null };
export type GoldenTurn = {
  turn_id: string;
  seq: number;
  input: { customer_messages: GoldenMessage[] };
  expected: { messages: GoldenMessage[]; profile_update: Record<string, {
    value: unknown; updated_from?: unknown; source?: "pulled" | "ai" | "manual";
  }> };
  rubric: string;
};
export type GoldenSession = { session_id: string; turns: GoldenTurn[] };
export type GoldenDataset = {
  name: string;
  mode: Mode;
  shared: Record<string, unknown>;
  items: GoldenSession[];
};
export type Dataset = {
  id: string;
  space_id: string;
  name: string;
  mode: Mode;
  created_at: string;
  content_sha256: string;
  archived_at?: string | null;
  source_snapshot_id?: string;
  content?: GoldenDataset;
};
export type CaptureOutcome = {
  count?: number;
  window?: { start: string; end: string };
  models?: Record<string, number>;
  guidance?: string[];
  funnel?: { eligible?: number; selected?: number; shortfall?: number;
    excluded?: Record<string, number>; selected_strata?: Record<string, number> };
  capture?: { rows?: number; truncated?: boolean };
  counts?: { scanned?: number; included?: number; excluded?: Record<string, number>; quota_shortfall?: number };
  profile_fields?: string[];
  warnings?: string[];
};
export type Capture = {
  id: string;
  space_id: string;
  name: string;
  mode: Mode;
  status: string;
  error_code?: string;
  error_message?: string | null;
  snapshot_id?: string;
  dataset_version_id?: string;
  created_at: string;
  finished_at?: string | null;
  request: { start?: string; end?: string; size?: number; model?: string;
    quota_per_workspace?: number; min_turns?: number; max_scanned_sessions?: number };
  outcome?: CaptureOutcome | null;
};
export type Snapshot = {
  id: string;
  space_id: string;
  mode: Mode;
  summary: { count?: number; profile_fields?: string[]; warnings?: string[] };
  created_at: string;
};
export type ExecutionProgress = {
  planned: number; executed: number; executing: number; waiting: number;
  execution_failed: number; uncertain: number; excluded: number;
  judged: number; judging: number; judge_waiting: number; judge_failed: number;
  execution_records: number;
};

export type Experiment = {
  id: string;
  name: string;
  space_id: string;
  dataset_version_id: string;
  mode: Mode;
  status: string;
  display_status?: string;
  execution_failed?: number;
  progress?: ExecutionProgress;
  execution_records?: { id: string; name: string; status: string; created_at: string }[];
  review_status: string;
  report_revision: number;
  created_by: string;
  created_at: string;
  manifest?: Record<string, unknown>;
  source_experiment_id?: string | null;
  archived_at?: string | null;
  trial?: boolean;
  deletable?: boolean;
  profile_fields?: Record<string, ProfileField>;
  // 未开启盲评为 null；blind_waiting / blind_open 期间看不到 AI 结论与双方身份
  blind_status?: BlindStatus | null;
};
export type BlindStatus = "blind_waiting" | "blind_open" | "revealed" | "skipped";
export type ReviewConfig = { blind_review: boolean; random_percent: number; min_random: number; risk_percent: number };
export type BlindChoice = "A" | "B" | "S" | "U";
export type BlindItem = {
  job_id: string; case: AtomicCase; ready: boolean; outputs: { A: OutputView; B: OutputView };
  result_sha256: string | null; reviewed: boolean; revision: number;
  review: { choice: BlindChoice; issue_kind: string; note: string } | null;
};
export type BlindSample = { status: BlindStatus; items?: BlindItem[]; reviewed?: number };
export type BlindGroup = {
  sampled: number; reviewed: number; revised: number; compared: number; agreed: number;
  interval: [number, number] | null; kappa: number | null;
  disagreements: { case_id: string; repeat_index: number; ai: string; human: string }[];
};
export type BlindStats = {
  status: "revealed" | "skipped"; skip_reason: string | null; revealed_by: string; revealed_at: string;
  random: BlindGroup; risk: BlindGroup;
};
export type Review = {
  actor_type?: "human" | "ai_assisted";
  revision: number;
  label: string;
  note: string;
  issue_kind?: string;
  subject: string;
  outputs_sha256: string;
};
export type EvalResult = {
  id: string;
  case_id: string;
  repeat_index: number;
  stage: "execute" | "judge";
  status: string;
  error_code?: string;
  attempt: number;
  result_sha256: string;
  review_revision: number;
  result: Record<string, unknown> | null;
  review: Review | null;
  case?: AtomicCase;
};
export type ToolCallView = { name: string; arguments: Record<string, unknown> };
export type ProfileField = { label: string; description: string };
export type FormatCheck = { status: "valid" | "invalid" | "not_applicable" | "unavailable";
  issues: { code: string; severity: string; path: string; summary: string }[] };
export type FormatCounts = { checked: number; invalid: number; messages_string: number;
  invalid_json: number; not_applicable: number; unavailable: number };
export type OutputView = { messages: string[]; tool_calls: ToolCallView[]; format_check?: FormatCheck };
export type AtomicCase = {
  task_name: string;
  current_turn: string;
  task_input: string;
  current_images: string[];
  prior_turns: { role: string; text: string; images: string[] }[];
  business_rules: string;
  history_tool_calls: ToolCallView[];
  profile_snapshot: Record<string, string>;
  session_facts: Record<string, string>;
  tool_labels: Record<string, string>;
  has_image: boolean;
  upstream_model: string;
  timestamp: string;
  tool_rules?: Record<string, string[]>;
};
export type Side = "candidate" | "baseline";
export type PairIssue = {
  side: Side;
  dimension: string;
  severity: "critical" | "major" | "minor";
  description: string;
  citations: { pointer: string; quote: string }[];
};
export type PairReview = {
  order: { A: Side; B: Side };
  task_type: string;
  expected_action: string;
  winner: string;
  confidence: string;
  rationale: string;
  dimensions: ({ dimension: string; verdict: string; note: string } & Partial<Record<Side, string>>)[];
  issues: PairIssue[];
  rejected_issues: number;
  contract_conflicts: string[];
  limitations: string[];
};
export type RuleCheck = { category: string; severity: string; summary: string; pointer: string };
export type PairResult = {
  verdict: { relative: string; reason: string; agreement: boolean | null; identical: boolean };
  task_type: string;
  reviews?: PairReview[];
  worst?: Record<Side, string>;
  rule_checks?: Record<Side, RuleCheck[]>;
};
export type SidePerformance = {
  samples: number; p50_ms: number | null; p95_ms: number | null; slow: number;
  avg_input_tokens: number | null; avg_output_tokens: number | null;
};
export type AtomicSummary = {
  response_format?: Record<Side, FormatCounts>;
  judged: number;
  relatives: Record<string, number>;
  intervals: Record<string, [number, number] | null>;
  agreement: { agreed: number; compared: number; identical: number };
  serious: Record<Side, { critical: number; major: number; minor: number }>;
  rule_checks: Record<Side, number>;
  rejected_issues: number;
  by_task: Record<string, Record<string, number>>;
  performance: Record<Side, SidePerformance>;
  human: { labelled: number; agreed: number };
};
export type Report = {
  progress?: ExecutionProgress;
  mode: Mode;
  status: string;
  review_status: string;
  planned: number;
  executed: number;
  execution_failed: number;
  uncertain: number;
  judged: number;
  judge_failed: number;
  reviewed: number;
  review_required: number;
  human_review?: {
    labels: Record<string, number>; issue_kinds: Record<string, number>;
    compared: number; agreed: number;
    disagreements: { case_id: string; repeat_index: number }[];
  };
  assisted_review?: {
    count: number; labels: Record<string, number>; issue_kinds: Record<string, number>;
  };
  review_items?: {
    case_id: string; repeat_index: number; label: string; issue_kind: string;
    note: string; subject: string; revision: number; created_at: string;
    actor_type?: "human" | "ai_assisted";
  }[];
  case_count: number;
  repeats: number;
  fixture: boolean;
  judge_prompt_version: string;
  judge_prompt_sha256: string;
  judge_model: string;
  candidate?: { model: string; host: string; preset?: string | null } | null;
  source_experiment_id?: string | null;
  execution_warnings?: string[];
  artifact?: { status: string; revision: number; error_code?: string } | null;
  atomic?: AtomicSummary;
  // 揭晓前只有 status；揭晓后为分组统计
  blind?: BlindStats | { status: BlindStatus };
  quality?: GoldenQuality;
  scope?: "selected_turns" | "published_dataset";
  trace?: TraceOverview;
};

// 模型调用链路：一个 Turn 内被测 Agent 经网关发起的全部模型调用
export type TraceStatus = "pending" | "complete" | "partial" | "summary_only" | "not_found" | "failed";
export type TraceOverview = {
  statuses: Partial<Record<TraceStatus, number>>; turns: number; collected: number;
  avg_calls: number | null; avg_prompt_tokens: number | null; avg_completion_tokens: number | null;
};
export type SpanIndex = {
  seq: number; span_id: string; started_at_ms: number; path: string; http_status: string; model: string;
  upstream_model: string; attempt: string; prompt_tokens: number | null; completion_tokens: number | null;
  elapsed_ms: number | null; has_body: boolean; body_error: string | null;
};
export type Span = SpanIndex & { request: Record<string, unknown> | null; response: Record<string, unknown> | null };
export type TurnTrace = {
  status: TraceStatus; request_id: string; request_id_echoed: string | null; echo_verified: boolean | null;
  expected_calls: number | null; call_count: number | null; summary: SpanIndex[] | null; error_code: string | null;
  collected_at: string | null; object_purged_at: string | null; spans?: Span[];
};
export type TraceReadiness = {
  status: "enabled" | "disabled" | "unknown"; calls?: number; body_calls?: number; window_hours?: number; detail?: string;
};

// 逐轮回放的判分与聚合，全部由后端代码计算，前端只展示
export type MetricStatus = "pass" | "fail" | "unavailable";
export type JudgeMetric = {
  metric_id: string; status: MetricStatus; reason: string; evidence_refs: string[];
  checks: { check_id: string; status: MetricStatus; reason: string; evidence_refs: string[] }[];
  ratio: { numerator: number; denominator: number; value: number | null } | null;
  overridden?: boolean;
};
export type ToolDiagnostics = {
  expected_tool_names: string[]; actual_tool_names: string[]; missing_tool_names: string[];
  unexpected_tool_names: string[]; name_set_match: boolean;
};
export type TurnVerdict = {
  turn_id: string; seq: number; repeat_index: number; status: "judged" | "missing_judgement";
  metrics: JudgeMetric[]; turn_status: MetricStatus; failed_metric_ids: string[];
  tool_diagnostics: ToolDiagnostics | null;
};
export type GoldenQuality = {
  metric_summaries: {
    metric_id: string; name: string; display_index: number | null; mode: string;
    pass_count: number; fail_count: number; unavailable_count: number; denominator: number;
    pass_rate: number | null; mean_ratio: number | null;
  }[];
  sessions: {
    session_id: string; passed_attempt_count: number; pass_at_k: Record<string, number | null>;
    attempts: { repeat_index: number; session_status: MetricStatus; first_failure_turn: number | null; turns: TurnVerdict[] }[];
  }[];
  first_failure_turn_distribution: Record<string, number>;
  batch_pass_at_k: Record<string, number | null>;
  judged_turn_count: number;
  missing_turn_count: number;
  quality_warnings: string[];
};
export type ChatMessage = { role: string; content: string; attachments?: GoldenAttachment[] | null };
export type TurnObservation = {
  status: string; stage_reached: string; request_messages: ChatMessage[]; response_messages: ChatMessage[];
  profile_before: Record<string, unknown>; profile_observed_before: Record<string, unknown> | null;
  profile_immediate_after: Record<string, unknown> | null; profile_after: Record<string, unknown> | null;
  profile_readback: { attempts?: number; elapsed_ms?: number; settled?: boolean;
    timeout_seconds?: number };
  profile_writes: Record<string, { value: unknown; updated_from?: unknown }>;
  routing_verified: boolean; chat_transport: string; tool_diagnostics: ToolDiagnostics | null;
  routing_evidence?: { channel_type?: string; captured_at?: string; source_workspace_id?: string };
  agent_logs: { type: string; name?: string | null; status?: string | null; message?: string | null; cost_time_ms?: number | null }[];
  elapsed_ms: number; error_code: string | null; error_detail: string | null;
  replay_request_id?: string | null; replay_request_id_echoed?: string | null;
};

export type JudgeConfig = { model: string; timeout_seconds: number; format_attempts: number };
export type JudgeSettings = {
  config: JudgeConfig;
  revision: number;
  history: { revision: number; config: JudgeConfig; subject: string; created_at: string }[];
  prompts: { mode: Mode; version: string; sha256: string; text: string }[];
  prompt_usage: { mode: Mode; version: string; sha256: string; runs: number; first_used: string;
    last_used: string; text: string | null }[];
};
export type GatewayModel = { id: string; provider: string };
export type CandidatePreset = {
  id: string; name: string; base_url: string; model: string; key_hint: string; note: string;
  revision: number; updated_by: string; updated_at: string;
};
export type ProbeResult = { ok: boolean; latency_ms: number; detail: string };
export type BatchSpec = { mode?: Mode; min_turns?: number; repeats?: number; start: string; end: string; size: number; trial: boolean;
  source_batch_id?: string; source_batch_name?: string;
  candidate_gateway_model: string | null; candidate_preset_id: string | null; candidate_preset_name?: string };
export type Batch = { id: string; name: string; spec: BatchSpec; created_by: string; created_at: string;
  spaces: number; started: number; finished: number; failed: number; blind: number; revealed: number;
  archived_at: string | null };
export type BatchItem = {
  id: string; space_id: string; space_name: string; source_name: string; capture_id: string;
  capture_status: string; capture_error: string | null; dataset_version_id: string | null;
  capture_outcome?: { count: number; counts?: { quota_shortfall?: number } };
  experiment_id: string | null; experiment_status: string | null;
  experiment_display_status: string | null; execution_failed: number | null;
  planned_turns?: number; executed_turns?: number; judged_turns?: number;
  cancelled_turns?: number; execution_errors?: number; execution_stop?: { reason: string };
  error_message: string | null; blind_status: BlindStatus | null;
};
export type SummaryRow = {
  id: string; name: string; status: string; space: { id: string; name: string; source_name: string };
} & ({ blind: true } | {
  blind?: undefined; candidate: string; judge_prompt_version: string; planned: number; executed: number; atomic: AtomicSummary;
});
export type CrossSummary = { runs: SummaryRow[]; total: AtomicSummary; warnings: string[] };
// 盲评揭晓前只返回原始请求
export type Evidence = { request: unknown; baseline?: unknown; candidate?: unknown };

export const base = "/api/agent-eval/v1";

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const body = await response.json() as { detail?: string };
      if (typeof body.detail === "string") detail = body.detail;
    } catch { /* response was not JSON */ }
    throw new Error(detail);
  }
  const body = await response.json();
  // gateway-v2 鉴权失败返回的是 HTTP 200 + {code: 401, ret: false}，不是标准的 401 状态码
  if (body && typeof body === "object" && body.ret === false && body.code === 401) {
    throw new Error("登录已失效，请先在 3chat 登录后刷新本页");
  }
  return body as T;
}

export function submit<T>(path: string, body: unknown, headers: Record<string, string> = {}) {
  return api<T>(path, { method: "POST", body: JSON.stringify(body), headers });
}

// 捕获到的异常转成可展示的文字
export const messageOf = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);

export const isRunning = (status: string | null | undefined) => status === "queued" || status === "running";
export const blindPending = (status?: string | null) => status === "blind_waiting" || status === "blind_open";
export const isFinished = (status: string | null | undefined) =>
  ["completed", "failed", "uncertain", "cancelled"].includes(status || "");

// 浏览器内生成的文件直接下载；文件名去掉系统不允许的字符
export function downloadFile(content: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename.replace(/[\\/:*?"<>|]/g, "_");
  link.click();
  URL.revokeObjectURL(url);
}

// 勾选切换：给定的项已全部选中则全部取消，否则全部选中
export function toggled<T>(set: Set<T>, items: T[]) {
  const next = new Set(set);
  const all = items.every((item) => next.has(item));
  items.forEach((item) => (all ? next.delete(item) : next.add(item)));
  return next;
}

// datetime-local 输入框要求本地时间、精确到分钟
export function localInput(date: Date) {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function defaultWindow() {
  const end = new Date();
  end.setSeconds(0, 0);
  return { start: localInput(new Date(end.getTime() - 24 * 3600000)), end: localInput(end) };
}

export function stamp(value: string | undefined) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
}

export const modeName = (mode: Mode) => mode === "golden_regression" ? "逐轮回放" : "单步回放";
