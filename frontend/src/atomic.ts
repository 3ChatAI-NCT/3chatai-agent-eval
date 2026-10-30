// 原子回放报告与导出共用的文案和纯函数
import type {
  AtomicCase, AtomicSummary, EvalResult, PairIssue, PairResult, PairReview, ProfileField, Review, ReviewConfig, Side,
} from "./api";

// 创建评测时展示的盲评规则，与设置页一致
export const blindRule = (config: ReviewConfig) => config.blind_review
  ? `人工盲评：评测结束后抽取 ${config.random_percent}%（至少 ${config.min_random} 条）随机样本和 ${config.risk_percent}% 风险样本，揭晓前看不到 AI 结论`
  : "人工盲评：未开启";

export const RELATIVE: Record<string, string> = { G: "候选更优", S: "持平", B: "基线更优", D: "评审分歧", U: "证据不足" };
export const RELATIVE_ORDER = ["G", "S", "B", "D", "U"];
export const TASK: Record<string, string> = {
  customer_reply: "客户回复", task_routing: "任务分流", profile_update: "画像写入",
  knowledge_lookup: "知识检索", internal_task: "内部任务", other: "其他", unknown: "未分类",
};
export const DIMENSION: Record<string, string> = {
  instruction_following: "指令遵循", groundedness: "事实依据", task_completion: "任务完成",
  tool_use: "工具选择与参数", communication: "客户沟通",
};
export const GRADE: Record<string, string> = {
  pass: "符合", minor: "轻微问题", major: "重要问题", critical: "严重问题",
  not_applicable: "不适用", insufficient_evidence: "证据不足",
};
export const SEVERITY: Record<string, string> = { critical: "严重", major: "重要", minor: "轻微", none: "无" };
export const CONFIDENCE: Record<string, string> = { high: "证据充分", medium: "证据一般", low: "证据有限" };
export const RULE: Record<string, string> = { "tool-contract": "工具契约", "duplicate-action": "重复动作" };
export const SIDE: Record<Side, string> = { candidate: "候选", baseline: "基线" };
export const HUMAN_LABEL: Record<string, string> = {
  G: "候选更优", S: "持平", B: "基线更优", U: "无法判断", unavailable: "无法判断",
};
export const ISSUE_KIND: [string, string][] = [
  ["none", "无问题"], ["agent_issue", "模型行为问题"], ["judge_error", "AI 评审有误"], ["environment_issue", "环境 / 数据问题"],
];
export const PROFILE_TOOL = "tool_update_visitor";

// 工具标题：画像写入优先用空间配置的中文字段名，其次是提示词里给出的名字，最后退回字段键；
// 平台自动生成的「update tool for visitor.xxx」描述没有信息量，不采用
export function toolTitle(name: string, toolLabels: Record<string, string> = {}, fields: Record<string, string> = {}) {
  const described = toolLabels[name];
  if (!name.startsWith(PROFILE_TOOL)) return described || name;
  const key = name.slice(PROFILE_TOOL.length + 1);
  return fields[key] || (described && !described.startsWith("update tool for") ? described : key);
}

// 字段键 → 中文名；没有中文名的字段不映射，界面直接显示字段键
export const fieldLabels = (fields: Record<string, ProfileField> = {}) =>
  Object.fromEntries(Object.entries(fields).filter(([, field]) => field.label).map(([key, field]) => [key, field.label]));

// 画像字段的已知值：已知画像以中文字段名为键，写入参数以字段键为键
export const knownValue = (snapshot: Record<string, string> | undefined, key: string, fields: Record<string, string>) =>
  snapshot?.[fields[key] || key] ?? snapshot?.[key];

const ERRORS: Record<string, string> = {
  http_400: "接口拒绝了请求（400），请求格式或参数不被支持",
  http_401: "鉴权失败（401），请检查 API Key",
  http_403: "无权访问（403），请检查 API Key 权限",
  http_404: "接口地址或模型不存在（404）",
  http_413: "上游拒绝了请求负载（413）；图片等内容也可能计入限制，请核对网关日志",
  http_422: "接口无法处理该请求（422）",
  http_429: "被限流（429），重试后仍失败",
  judge_output_invalid: "评审模型连续多次输出不符合 JSON 协议",
  execution_failed: "执行出错（未归类），可重新执行该评测",
  external_delivery_unknown: "多次超时或网络失败，结果未知",
  worker_lease_expired: "任务多次被中断",
  request_not_replayable: "该请求不可等价回放",
};
export const errorText = (code: string) =>
  ERRORS[code] || (code.startsWith("http_5") ? `服务暂时不可用（${code.slice(5)}），重试后仍失败` : code);

export type Row = { execution: EvalResult; judgment?: EvalResult; pair?: PairResult };

// 请求里没有子任务卡片时，任务名会回退成推断的类型；这类统一视为主 Agent 对客调用
const INFERRED = new Set(["mainagent", "subagent", "profile-update", "knowledge-lookup"]);
export const skillName = (context?: AtomicCase) => {
  const name = context?.task_name || "";
  return name && !INFERRED.has(name) ? name : "主 Agent";
};
export const skillOf = (row: Row) => skillName(row.execution.case);

// 盲评成功时复核挂在评审结果上，否则挂在执行结果上（只能标记无法判断）
export const reviewTarget = (row: Row) => row.judgment?.status === "succeeded" ? row.judgment : row.execution;
export const reviewOf = (row: Row, local: Record<string, Review>) => {
  const target = reviewTarget(row);
  const saved = local[target.id];
  if (saved?.actor_type !== "ai_assisted" && saved?.outputs_sha256 === target.result_sha256) return saved;
  return target.review?.actor_type !== "ai_assisted" && target.review?.outputs_sha256 === target.result_sha256 ? target.review : null;
};

const RANK: Record<string, number> = { critical: 3, major: 2, minor: 1 };
export const isSerious = (pair?: PairResult) =>
  ["major", "critical"].some((level) => pair?.worst?.candidate === level || pair?.worst?.baseline === level);

// 复核优先级：分歧最需要人判断，其次是指出了重要问题的案例
export function priority(row: Row) {
  const relative = row.pair?.verdict.relative;
  if (!row.pair) return 5;
  if (relative === "D") return 0;
  if (isSerious(row.pair)) return 1;
  if (relative === "U") return 2;
  if (relative === "B") return 3;
  return 4;
}

export type MergedIssue = { issue: PairIssue; order: PairReview["order"]; found: number };

// 两次评审常指出同一问题；按「哪一侧 + 维度 + 严重度」合并，并记下几次评审都指出了
export function mergeIssues(reviews: PairReview[]): MergedIssue[] {
  const merged = new Map<string, MergedIssue>();
  for (const review of reviews) {
    const seen = new Set<string>();
    for (const issue of review.issues) {
      const key = `${issue.side}|${issue.dimension}|${issue.severity}`;
      const existing = merged.get(key);
      if (!existing) merged.set(key, { issue, order: review.order, found: 1 });
      else if (!seen.has(key)) existing.found += 1;
      seen.add(key);
    }
  }
  return [...merged.values()].sort((a, b) => RANK[b.issue.severity] - RANK[a.issue.severity]);
}

// 重要及以上（严重 + 重要）的问题案例数
export const seriousCount = (summary: AtomicSummary, side: Side) => summary.serious[side].critical + summary.serious[side].major;

// AI 评审稳定性（位置交换一致率）：Judge 是否受 A/B 展示位置影响，不是候选模型的准确率
export function judgeStability(summary: AtomicSummary) {
  const { agreed, compared, identical } = summary.agreement;
  const split = compared - agreed;
  return {
    rate: pct(agreed, compared), agreed, compared, split,
    help: `同一条样本 Judge 盲评两次，第二次交换基线与候选回答的 A/B 位置，两次都选中同一个回答才算一致（即「位置交换一致率」）。`
      + `它衡量 AI 评审是否稳定、是否受展示位置影响，不代表候选模型答对了多少。`
      + `分母 ${compared} 条是双方输出不同、可以交换对比的样本${identical ? `；另有 ${identical} 条双方输出相同，只做一次合规检查，不计入` : ""}。`
      + (split ? `两次结论不一致的 ${split} 条不计入结论分布，单列为「评审分歧」，需要人工判断。` : ""),
  };
}

export const pct = (value: number, total: number) => total ? `${Math.round(value / total * 100)}%` : "—";
export const range = (interval: [number, number] | null | undefined) =>
  interval ? `${Math.round(interval[0] * 100)}%–${Math.round(interval[1] * 100)}%` : "—";
export const seconds = (ms: number | null | undefined) => ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`;

// 阿里云 OSS 原图常有数 MB，列表里用服务端缩略图，点开再看原图
export function thumbnail(url: string) {
  try {
    const parsed = new URL(url);
    if (parsed.hostname.endsWith(".aliyuncs.com") && !parsed.search) return `${url}?x-oss-process=image/resize,w_480`;
  } catch { /* 非法链接原样返回，由 img 的 onError 处理 */ }
  return url;
}

export const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// 规则原文常带大段缩进和空行，展示时去掉行首缩进并合并多余空行
export function tidy(text: string) {
  return text.split("\n").map((line) => line.trim()).join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// 引用路径里的 A/B 只在该次评审内有效，展示时换成角色名
export function citationSource(pointer: string, order: PairReview["order"]) {
  const [, root, ...rest] = pointer.split("/");
  const name = root === "A" || root === "B" ? SIDE[order[root]] : root === "request" ? "请求" : root;
  return rest.length ? `${name} · ${rest.join("/")}` : name;
}

export type ImMessage = { text: string; attachments: unknown[] };

// 对客输出按 IM JSON 协议时正文在 messages[].message 里；不是合法协议 JSON 时返回 null，按原文展示
export function imMessages(raw: string): ImMessage[] | null {
  const text = raw.trim();
  if (!text.startsWith("{")) return null;
  try {
    const parsed = JSON.parse(text) as { messages?: unknown };
    if (!Array.isArray(parsed.messages)) return null;
    return parsed.messages.map((item) => {
      if (typeof item === "string") return { text: item, attachments: [] };
      const entry = (item ?? {}) as { message?: unknown; content?: unknown; text?: unknown; attachments?: unknown };
      const body = entry.message ?? entry.content ?? entry.text ?? "";
      return { text: typeof body === "string" ? body : JSON.stringify(body),
        attachments: Array.isArray(entry.attachments) ? entry.attachments : [] };
    });
  } catch { return null; }
}
