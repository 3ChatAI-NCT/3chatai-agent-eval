import {
  Button, Checkbox, Modal, ModalBody, ModalContent, ModalFooter, ModalHeader, Radio, RadioGroup, Textarea,
} from "@heroui/react";
import { useState } from "react";
import {
  downloadFile, stamp, type AtomicCase, type AtomicSummary, type Experiment, type OutputView, type Report,
  type Review, type Side, type Space,
} from "./api";
import {
  escapeRegExp,  DIMENSION, HUMAN_LABEL, ISSUE_KIND, PROFILE_TOOL, RELATIVE, RELATIVE_ORDER, SEVERITY, SIDE, citationSource,
  imMessages, judgeStability, mergeIssues, pct, range, fieldLabels, reviewOf, seconds, skillOf, thumbnail, tidy, toolTitle, type Row,
} from "./atomic";

type Audience = "internal" | "external";
type Options = {
  run: Experiment; report: Report; summary: AtomicSummary; rows: Row[]; space: Space | null;
  names: Record<Side, string>; reviews: Record<string, Review>;
};

export function ExportDialog({ onClose, ...options }: Options & { onClose: () => void }) {
  const [audience, setAudience] = useState<Audience>("internal");
  const [extra, setExtra] = useState("");
  const [rules, setRules] = useState(true);
  const terms = maskTerms(options.space?.source_name || "", extra);
  function download() {
    const name = audience === "external" ? mask(options.run.name, terms, options.space?.name || "评测空间") : options.run.name;
    downloadFile(exportHtml(options, audience, terms, rules), `${name}-${audience === "external" ? "外部版" : "内部版"}.html`,
      "text/html;charset=utf-8");
    onClose();
  }
  return <Modal isOpen onClose={onClose} size="lg"><ModalContent>
    <ModalHeader>导出报告</ModalHeader>
    <ModalBody>
      <p className="muted">导出为单个 HTML 文件，离线可打开，案例可折叠展开；在浏览器中「打印」即可另存为 PDF。</p>
      <RadioGroup value={audience} onValueChange={(value) => setAudience(value as Audience)}>
        <Radio value="internal" description="保留客户名称、生产请求 ID、候选接口地址与复核人">内部版</Radio>
        <Radio value="external" description="客户名称替换为评测空间名，去掉请求 ID、接口地址与复核人；手机号、邮箱和正文链接保留原值，省略带签名的图片链接">去客户标识版</Radio>
      </RadioGroup>
      {audience === "external" && <>
        <div className="selection-box"><strong>将替换为「{options.space?.name || "评测空间"}」的词</strong>
          <span>{terms.length ? terms.join("、") : "未找到客户名称，请在下方补充"}</span></div>
        <Textarea label="补充脱敏词" description="品牌简称、门店名、老师姓名等，用逗号或换行分隔；导出前请抽查正文" minRows={2}
          value={extra} onValueChange={setExtra} />
        <Checkbox isSelected={rules} onValueChange={setRules}>包含本轮业务规则原文</Checkbox>
      </>}
    </ModalBody>
    <ModalFooter><Button variant="light" onPress={onClose}>取消</Button><Button color="primary" onPress={download}>下载 HTML</Button></ModalFooter>
  </ModalContent></Modal>;
}

// 源空间名称中的品牌主体也需要替换；示例仅使用虚构名称。
export function maskTerms(sourceName: string, extra: string) {
  const core = sourceName.replace(/[（(]托管[)）]/g, "").replace(/的空间$/, "").trim();
  const short = core.replace(/(新零售|科技|教育|信息技术|网络|文化传播)?有限(责任)?公司$/, "").replace(/^(北京|上海|广州|深圳|成都|杭州|重庆|武汉|南京|西安)/, "");
  const terms = [sourceName.trim(), core, short, ...extra.split(/[,，\n]/).map((item) => item.trim())];
  return [...new Set(terms.filter((term) => term.length >= 2))].sort((a, b) => b.length - a.length);
}

function mask<T>(value: T, terms: string[], replacement: string): T {
  if (!terms.length) return value;
  // 英文品牌名大小写常不统一，按不区分大小写替换
  if (typeof value === "string") return terms.reduce((text, term) =>
    text.replace(new RegExp(escapeRegExp(term), "gi"), () => replacement), value as string) as T;
  if (Array.isArray(value)) return value.map((item) => mask(item, terms, replacement)) as T;
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [mask(key, terms, replacement), mask(item, terms, replacement)])) as T;
  return value;
}

const esc = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, (char) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
const safeUrl = (url: string) => /^https?:\/\//i.test(url) ? esc(url) : "#";

export function exportHtml(options: Options, audience: Audience, terms: string[], includeRules: boolean) {
  const external = audience === "external";
  const spaceName = options.space?.name || "评测空间";
  const replace = <T,>(value: T) => external ? mask(value, terms, spaceName) : value;
  const { run, report, summary } = options;
  const rows = replace(options.rows);
  const names = { ...options.names, candidate: external ? report.candidate?.model || "候选模型" : options.names.candidate };
  const total = summary.judged;
  const title = replace(run.name);
  const scope = external ? spaceName : `${spaceName}${options.space?.source_name ? ` · ${options.space.source_name}` : ""}`;
  const verdicts = RELATIVE_ORDER.map((key) => {
    const interval = summary.intervals[key];
    const share = total ? summary.relatives[key] / total : 0;
    return `<tr><td>${RELATIVE[key]}</td><td>${summary.relatives[key]}</td><td>${pct(summary.relatives[key], total)}</td>
      <td><div class="interval">${interval ? `<span class="fill v-${key}" style="left:${interval[0] * 100}%;width:${Math.max(1, (interval[1] - interval[0]) * 100)}%"></span>` : ""}<span class="dot" style="left:${share * 100}%"></span></div></td>
      <td>${range(interval)}</td></tr>`;
  }).join("");
  const severity = (["candidate", "baseline"] as Side[]).map((side) => `<tr><td>${SIDE[side]}</td><td>${summary.serious[side].critical}</td>
    <td>${summary.serious[side].major}</td><td>${summary.serious[side].minor}</td><td>${summary.rule_checks[side]}</td></tr>`).join("");
  const performance = (["candidate", "baseline"] as Side[]).map((side) => { const perf = summary.performance[side]; return `<tr><td>${SIDE[side]}</td>
    <td>${perf.samples}</td><td>${seconds(perf.p50_ms)}</td><td>${seconds(perf.p95_ms)}</td><td>${perf.avg_input_tokens ?? "—"} / ${perf.avg_output_tokens ?? "—"}</td></tr>`; }).join("");
  const skills = Object.entries(replace(summary.by_task)).map(([task, counts]) => `<tr><td>${esc(task)}</td><td>${counts.total}</td>
    <td>${counts.G || 0} / ${counts.S || 0} / ${counts.B || 0}</td><td>${counts.D || 0} / ${counts.U || 0}</td>
    <td>${counts.candidate_serious || 0} / ${counts.baseline_serious || 0}</td></tr>`).join("");
  const cases = rows.map((row, index) => caseHtml(row, index, names, replace(reviewOf(options.rows[index], options.reviews)),
    external, includeRules || !external, replace, fieldLabels(options.run.profile_fields))).join("");
  const stability = judgeStability(summary);
  const reviewed = options.rows.filter((row) => reviewOf(row, options.reviews)).length;
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><style>${CSS}</style></head><body><main>
<header><p class="eyebrow">原子请求回放评测报告${external ? "" : " · 内部版"}</p><h1>${esc(title)}</h1>
<p class="muted">${esc(scope)} · 创建于 ${esc(stamp(run.created_at))} · 导出于 ${esc(stamp(new Date().toISOString()))}</p>
<p class="versus"><b>${esc(names.candidate)}</b>（候选） 对比 <b>${esc(names.baseline)}</b>（基线）</p></header>
<section class="stats">
<div><b>${report.planned}</b><span>生产请求样本</span></div><div><b>${report.executed} / ${report.planned}</b><span>候选完整响应</span></div>
<div><b>${report.judged} / ${report.executed}</b><span>AI 已盲评</span></div><div title="${esc(stability.help)}"><b>${stability.rate}</b><span>AI 评审稳定性 · 交换 A/B 后一致 ${stability.agreed}/${stability.compared}${stability.split ? ` · ${stability.split} 条分歧` : ""}</span></div>
<div><b>${reviewed} / ${options.rows.length}</b><span>人工已复核</span></div></section>
<section><h2>盲评结论</h2><table><thead><tr><th>结论</th><th>条数</th><th>比例</th><th>置信区间（圆点为实测）</th><th>95% 置信区间</th></tr></thead><tbody>${verdicts}</tbody></table>
${summary.intervals.B ? `<p class="key"><b>关键读数</b>候选不如基线的比例为 ${pct(summary.relatives.B, total)}，考虑抽样误差，真实比例可能在 ${range(summary.intervals.B)} 之间。${total < 100 ? `本批只有 ${total} 条，只能说明方向；替换决策建议扩到 100–200 条并按技能分层。` : ""}</p>` : ""}
<p class="muted">「95% 置信区间」的意思是：按同样方式重新抽样，真实比例有 95% 的把握落在区间内，样本越多区间越窄。「持平」表示两者接近，不等于双方都合格，是否合格看问题严重度。</p></section>
<section class="grid"><div><h2>问题严重度</h2><table><thead><tr><th></th><th>严重</th><th>重要</th><th>轻微</th><th>规则检查</th></tr></thead><tbody>${severity}</tbody></table>
<p class="muted">按案例计，任一次盲评指出即列入。引用无法在原文找到的问题 ${summary.rejected_issues} 条已作废。</p></div>
<div><h2>性能对照</h2><table><thead><tr><th></th><th>样本</th><th>P50</th><th>P95</th><th>平均输入 / 输出 token</th></tr></thead><tbody>${performance}</tbody></table>
<p class="muted">基线耗时取自生产网关日志，候选耗时在评测侧测得，环境不同，仅供参考。</p></div></section>
<section><h2>按技能</h2><table><thead><tr><th>技能</th><th>样本</th><th>候选更优 / 持平 / 基线更优</th><th>分歧 / 证据不足</th><th>重要及以上问题：候选 / 基线</th></tr></thead><tbody>${skills}</tbody></table></section>
<section><h2>评审方法</h2><p class="muted">Judge 模型 ${esc(report.judge_model)} · 提示词 ${esc(report.judge_prompt_version)}。每条样本隐藏模型名后交换 A/B 顺序独立盲评两次，两次一致才形成结论，不一致列为「评审分歧」交人工复核；每个问题都须引用原文，引用不成立的问题作废。</p></section>
<section><h2>逐条案例 · ${rows.length} 条</h2><p class="muted">点击案例展开上下文、双方输出、AI 评审与人工复核。</p>${cases}</section>
</main></body></html>`;
}

// clean 用于解析后才出现的文本：IM JSON 里的 \uXXXX 转义在解析前匹配不到脱敏词
function caseHtml(row: Row, index: number, names: Record<Side, string>, review: Review | null | undefined, external: boolean,
  includeRules: boolean, clean: (text: string) => string, fields: Record<string, string>) {
  const context = row.execution.case;
  const outputs = row.execution.result as { baseline?: OutputView; candidate?: OutputView } | null;
  const pair = row.pair;
  const verdict = pair ? RELATIVE[pair.verdict.relative] || pair.verdict.relative : "尚无结论";
  const human = review ? `人工：${HUMAN_LABEL[review.label] || review.label}` : "未人工复核";
  const issues = mergeIssues(pair?.reviews || []);
  const first = pair?.reviews?.[0];
  return `<details class="case"><summary><span class="num">${index + 1}</span><b>${esc(skillOf(row))}</b>
<span class="chip v-text-${esc(pair?.verdict.relative || "")}">${esc(verdict)}</span><span class="muted">${esc(human)}</span>
<span class="preview">${esc((context?.current_turn || context?.task_input || "").slice(0, 60))}</span></summary><div class="case-body">
${external ? "" : `<p class="muted mono">生产请求 ID ${esc(row.execution.case_id)}${context?.timestamp ? ` · ${esc(stamp(context.timestamp))}` : ""}</p>`}
${context ? contextHtml(context, includeRules, external) : ""}
<div class="grid">${(["baseline", "candidate"] as Side[]).map((side) => `<div class="output"><h4>${SIDE[side]} · ${esc(names[side])}</h4>${outputHtml(outputs?.[side], clean, fields, context)}</div>`).join("")}</div>
${pair ? `<div class="ai"><h4>AI 评审 · ${esc(verdict)}</h4><p class="muted">${esc(pair.verdict.reason)}</p>
${first ? `<p class="muted">本轮应做：${esc(first.expected_action)}</p><p>${esc(first.rationale)}</p>` : ""}
${issues.map(({ issue, order, found }) => `<div class="issue"><b class="sev-${issue.severity}">${SEVERITY[issue.severity]}</b> ${SIDE[issue.side]} · ${esc(DIMENSION[issue.dimension] || issue.dimension)}：${esc(issue.description)}${found > 1 ? ' <em>两次评审均指出</em>' : ""}
${issue.citations.map((citation) => `<p class="cite"><code>${esc(citationSource(citation.pointer, order))}</code>「${esc(citation.quote)}」</p>`).join("")}</div>`).join("") || '<p class="muted">未指出成立的问题。</p>'}</div>` : ""}
${review ? `<div class="human"><h4>人工复核 · ${esc(HUMAN_LABEL[review.label] || review.label)}</h4>
<p>${esc(ISSUE_KIND.find(([value]) => value === review.issue_kind)?.[1] || "")}${review.note ? `：${esc(review.note)}` : ""}</p>${external ? "" : `<p class="muted">${esc(review.subject)} · 第 ${review.revision} 版</p>`}</div>` : ""}
</div></details>`;
}

// 带签名参数的是临时授权链接，发给外部等于转交访问凭证，外部版不保留
const SIGNED = /[?&](Signature|OSSAccessKeyId|security-token|Expires|X-Amz-|x-oss-signature|x-oss-credential)/i;

function imagesHtml(all: string[], external: boolean) {
  const urls = external ? all.filter((url) => !SIGNED.test(url)) : all;
  const omitted = all.length - urls.length;
  return (omitted ? `<p class="muted">${omitted} 张图片为临时签名链接，外部版已省略。</p>` : "") + (urls.length ? `<div class="images">${urls.map((url) => `<a href="${safeUrl(url)}" target="_blank" rel="noreferrer noopener"><img src="${safeUrl(thumbnail(url))}" alt="客户发送的图片" loading="lazy" referrerpolicy="no-referrer"></a>`).join("")}</div>` : "");
}

function contextHtml(context: AtomicCase, includeRules: boolean, external: boolean) {
  const turns = context.prior_turns.slice(-6).map((turn) => `<div class="turn turn-${esc(turn.role)}"><span>${turn.role === "user" ? "客户" : "助手"}</span><div>${turn.text ? `<p>${esc(turn.text)}</p>` : ""}${imagesHtml(turn.images, external)}</div></div>`).join("");
  const instruction = context.task_input && context.task_input !== "无" ? context.task_input : "";
  const rules = tidy(context.business_rules);
  return `<div class="context">${turns ? `<h4>此前对话（最近 ${Math.min(6, context.prior_turns.length)} 条）</h4>${turns}` : ""}
<h4>${instruction ? "子任务指令" : "客户本轮消息"}</h4><div class="msg">${esc(instruction || context.current_turn)}${instruction ? "" : imagesHtml(context.current_images, external)}</div>
${instruction && (context.current_turn || context.current_images.length) ? `<h4>客户消息</h4><div class="msg">${esc(context.current_turn)}${imagesHtml(context.current_images, external)}</div>` : ""}
${includeRules && rules ? `<details><summary>本轮业务规则</summary><div class="rules">${esc(rules)}</div></details>` : ""}</div>`;
}

function outputHtml(output: OutputView | undefined, clean: (text: string) => string, fields: Record<string, string>, context?: AtomicCase) {
  if (!output) return '<p class="muted">未取得完整响应。</p>';
  const messages = output.messages.map((message) => {
    const parsed = imMessages(message);
    if (!parsed) return `<div class="msg">${esc(message || "（空文本）")}</div>`;
    return `<p class="muted">IM JSON · ${parsed.length ? `${parsed.length} 条消息` : "空消息列表"}</p>` + parsed.map((item) =>
      `<div class="msg">${esc(clean(item.text) || "（空文本）")}${item.attachments.length ? `<br><span class="muted">附件 ${item.attachments.length} 个</span>` : ""}</div>`).join("");
  }).join("");
  const calls = output.tool_calls.map((call) => `<div class="tool"><b>${call.name.startsWith(PROFILE_TOOL) ? "画像写入" : "工具调用"} · ${esc(toolTitle(call.name, context?.tool_labels, fields))}</b>
${Object.entries(call.arguments).map(([key, value]) => `<p><span class="muted">${esc(fields[key] || key)}</span> ${esc(typeof value === "string" ? value : JSON.stringify(value))}</p>`).join("")}</div>`).join("");
  const format = output.format_check?.status === "invalid" ? `<div class="msg" style="border-left:4px solid #dc2626;background:#fff1f2"><b>响应格式问题 · 重要</b>${output.format_check.issues.map((issue) => `<p>${esc(clean(issue.summary))} ${esc(issue.path)}</p>`).join("")}</div>` : "";
  return format + messages + calls || '<p class="muted">未生成对客消息或工具调用。</p>';
}

const CSS = `
:root{color:#20242d;font-family:Inter,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#fff}
body{margin:0}main{max-width:1100px;margin:0 auto;padding:36px 28px 60px}
h1{font-size:26px;margin:4px 0 6px;letter-spacing:-.4px}h2{font-size:16px;margin:0 0 12px}h4{font-size:13px;margin:12px 0 6px;color:#344253}
section{border:1px solid #e5e9f0;border-radius:12px;padding:18px 20px;margin:18px 0}
.eyebrow{color:#0867d7;font-size:12px;font-weight:600;margin:0}.muted{color:#6b7787;font-size:12px;line-height:1.7}.mono{font-family:ui-monospace,Menlo,monospace;font-size:11px}
.versus{font-size:14px;margin:10px 0 0}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;border:0;padding:0}.stats div{border:1px solid #e5e9f0;border-radius:10px;padding:14px}
.stats b{display:block;font-size:22px}.stats span{color:#6b7787;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:8px;border-bottom:1px solid #eef1f5}th{color:#6b7787;font-weight:600;background:#f8fafc}
.interval{position:relative;height:8px;background:#eef1f5;border-radius:4px;min-width:160px}.interval .fill{position:absolute;top:0;height:8px;border-radius:4px;opacity:.45}
.interval .dot{position:absolute;top:-2px;width:12px;height:12px;margin-left:-6px;border-radius:50%;background:#1f2937;border:2px solid #fff}
.v-G{background:#16a34a}.v-S{background:#94a3b8}.v-B{background:#dc2626}.v-D{background:#d97706}.v-U{background:#cbd5e1}
.key{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:10px 12px;font-size:13px}.key b{margin-right:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}section.grid{border:0;padding:0}section.grid>div{border:1px solid #e5e9f0;border-radius:12px;padding:18px 20px}
.case{border:1px solid #e5e9f0;border-radius:10px;margin:8px 0}.case summary{cursor:pointer;padding:11px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px}
.case .num{color:#94a3b8;font-size:12px;min-width:22px}.case .preview{color:#6b7787;font-size:12px;flex:1 1 200px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.chip{font-size:12px;padding:2px 9px;border-radius:999px;background:#f1f5f9}.v-text-G{color:#15803d}.v-text-B{color:#b91c1c}.v-text-D{color:#b45309}
.case-body{padding:0 16px 16px;border-top:1px solid #eef1f5}
.context{background:#f8fafc;border-radius:10px;padding:10px 14px;margin:12px 0}
.msg{background:#fff;border:1px solid #edf0f4;border-radius:8px;padding:10px 12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.7;margin:6px 0}
.turn{display:grid;grid-template-columns:36px 1fr;gap:8px;font-size:12px;margin:5px 0}.turn span{color:#7b8897;padding-top:6px}
.turn p{margin:0;padding:6px 10px;border-radius:8px;background:#fff;border:1px solid #edf0f4;white-space:pre-wrap;overflow-wrap:anywhere}.turn-assistant p{background:#f1f6fd}
.images{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}.images img{max-width:180px;max-height:180px;border-radius:8px;border:1px solid #e5e9f0;object-fit:cover}
.rules{white-space:pre-wrap;font-size:12px;line-height:1.7;max-height:320px;overflow:auto;background:#fff;border:1px solid #e4eaf0;border-radius:8px;padding:10px}
.output{border:1px solid #e5ebf2;border-radius:10px;padding:4px 12px 10px}.tool{border:1px dashed #cbd5e1;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:12px}.tool p{margin:3px 0}
.ai,.human{border-top:1px solid #eef1f5;margin-top:12px;font-size:13px;line-height:1.7}.issue{margin:8px 0}.issue em{color:#0867d7;font-style:normal;font-size:11px;margin-left:6px}
.cite{font-size:11px;color:#536172;margin:3px 0 3px 14px}.cite code{color:#7b8897;margin-right:6px}
.sev-critical{color:#b91c1c}.sev-major{color:#b45309}.sev-minor{color:#64748b}
details summary{cursor:pointer}
@media print{details{break-inside:avoid}details:not([open])>*:not(summary){display:none}}
@media (max-width:700px){.stats{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}main{padding:20px 14px}}
`;
