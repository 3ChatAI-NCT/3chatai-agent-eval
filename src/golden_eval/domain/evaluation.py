"""Evaluation configuration catalog: the single source of truth for metric semantics.

Every metric the platform may output is described here, including the ones it deliberately does not
output, so the API, the UI reference page and the product documentation cannot drift apart. The
catalog carries meaning and aggregation rules only; it never computes scores.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCORING_POLICY_VERSION = "dense-pass-fail-v1"
JUDGE_CONTRACT_VERSION = "full-evaluation-v8"

# Metrics a Turn must return, in the order the judge contract requires.
TURN_METRIC_ORDER: tuple[str, ...] = ("2", "3", "4", "5", "6", "8", "9")
TURN_GATE_METRIC_ORDER: tuple[str, ...] = ("4", "5", "6")
RATIO_METRIC_ID = "5"


class MetricMode(StrEnum):
    """How a metric participates in a run."""

    SCORED = "scored"
    DIAGNOSTIC = "diagnostic"
    NOT_OUTPUT = "not_output"
    DISABLED = "disabled"


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mode: MetricMode
    layer: str
    aggregate_level: Literal["turn", "session", "turn_and_session"]
    turn_gate: bool = False
    summary: str
    pass_when: list[str] = Field(default_factory=list)
    fail_when: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)


class AggregationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    rule: str


class RubricSectionSpec(BaseModel):
    """One fixed v0.4 rubric section and the criterion that consumes it.

    Annotators write the rubric as a fixed-heading string; this catalog is what tells the editor
    which metric reads which heading, so filling it in is not guesswork. It carries meaning only —
    compiling the rubric into judge input stays in code.
    """

    model_config = ConfigDict(extra="forbid")

    section: str = Field(min_length=1)
    level: Literal["turn", "session"]
    required: bool = True
    metric_ids: list[str] = Field(default_factory=list)
    consumed_by: Literal["code_compiled_judge", "code_only", "human_only"]
    evidence: str = ""
    purpose: str
    fill_hint: str
    example: str = ""


RUBRIC_SECTION_CATALOG: tuple[RubricSectionSpec, ...] = (
    RubricSectionSpec(
        section="必须覆盖",
        level="turn",
        metric_ids=["5"],
        consumed_by="code_compiled_judge",
        evidence="compiled_rubric.must_cover",
        purpose="本轮回答必须出现的业务要点。",
        fill_hint=(
            "写结论、必要前置条件、核心风险与主要流程；按实质覆盖判定，不要求逐字复刻，"
            "不影响客户理解与下一步动作的次要细节可以省略。"
        ),
        example="说明同一用户只能购买一次新人月卡；给出手表绑定后上传人脸的路径。",
    ),
    RubricSectionSpec(
        section="禁止",
        level="turn",
        metric_ids=["6"],
        consumed_by="code_compiled_judge",
        evidence="compiled_rubric.forbidden",
        purpose="本轮绝对不能出现的说法或动作，以及编造边界。",
        fill_hint=(
            "写无依据事实、伪造链接/文件/完成状态、错误承诺；即使本轮没有额外禁止项，"
            "也要保留通用的反编造表述。"
        ),
        example="不得宣称退款已办结；不得把「已验券」写成「已开卡」。",
    ),
    RubricSectionSpec(
        section="主导动作",
        level="turn",
        metric_ids=["8", "9"],
        consumed_by="code_compiled_judge",
        evidence="compiled_rubric.guidance",
        purpose="本轮应当主动推进什么；以及本轮允不允许追问。",
        fill_hint=(
            "写清主动推进方向；不该追问的轮次直接写「禁止追问」，否则按允许继续追问处理。"
        ),
        example="请客户补充品牌与具体门店名称；本轮禁止再索取其他资料。",
    ),
    RubricSectionSpec(
        section="工具",
        level="turn",
        metric_ids=["7"],
        consumed_by="code_only",
        evidence="expected.tool_calls + AgentLog",
        purpose="本轮该不该调工具（只比对工具名集合，诊断口径、不计分）。",
        fill_hint=(
            "写「必须观察到调用：<工具名>；只判断是否调用」，或「本轮无可观察执行动作；多余调用记错」。"
            "参数、先后顺序与失败回执不判。"
        ),
        example="必须观察到调用：订单查询；只判断是否调用。",
    ),
    RubricSectionSpec(
        section="知识召回",
        level="turn",
        required=False,
        metric_ids=["8.1"],
        consumed_by="code_only",
        evidence="AgentLog(knowledge)",
        purpose="必须命中的知识文档（原 registry 指标 8.1，当前不输出，仅留证）。",
        fill_hint="写必须召回并可能要求排第一的 doc id；本轮没有知识要求时整行省略。",
        example="必须命中《新人月卡退款规则》。",
    ),
    RubricSectionSpec(
        section="画像",
        level="turn",
        metric_ids=["2", "3"],
        consumed_by="code_compiled_judge",
        evidence="expected.profile_update / profile_snapshot + 回读",
        purpose="本轮画像口径：哪些字段应当写入，哪些还不算说清。",
        fill_hint=(
            "这一行由补全结果推导：本轮应更新的字段列在这里，其余字段保持上一轮值；"
            "口径不清的字段写明不得写入。"
        ),
        example="本轮应更新：location_c；其余画像字段保持上一轮值。",
    ),
    RubricSectionSpec(
        section="标注说明",
        level="turn",
        required=False,
        consumed_by="human_only",
        purpose="这一轮考什么、最容易怎么错；不参与评分。",
        fill_hint="写设计意图与易错点，供复核者理解判据来源。",
        example="考「验券成功≠已开卡」这一结论是否表述清楚，常见错误是承诺已激活。",
    ),
)


def rubric_sections(level: str) -> tuple[RubricSectionSpec, ...]:
    return tuple(spec for spec in RUBRIC_SECTION_CATALOG if spec.level == level)


def rubric_required_sections(level: str) -> tuple[str, ...]:
    return tuple(spec.section for spec in rubric_sections(level) if spec.required)


def rubric_metric_ids() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            metric_id for spec in RUBRIC_SECTION_CATALOG for metric_id in spec.metric_ids
        )
    )


METRIC_CATALOG: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_id="1",
        name="业务流程走通率",
        mode=MetricMode.SCORED,
        layer="Session 聚合",
        aggregate_level="session",
        summary="同一 Session repeat 内每个 Turn 是否都通过 5.1/5.2/5.3 门控。",
        pass_when=["本 Session 已发生的全部 Turn 都 turn_pass=true。"],
        fail_when=["任一 Turn 的 gate 指标 fail，则本 Session fail。"],
        notes=[
            "只有缺证据时才 unavailable；unavailable 不算 fail。",
            "走通不等于成交：客户没有下单、聊到一半离开都不单独构成失败。",
            "客户中途离开时未发生的流程不计入分母。",
        ],
    ),
    MetricDefinition(
        metric_id="2",
        name="画像更新合理性",
        mode=MetricMode.SCORED,
        layer="Turn L1+L2",
        aggregate_level="turn",
        summary="金标要求的画像更新，实际是否写入并有对话证据支持。",
        pass_when=[
            "实际更新有对话证据支持、符合 target 字段规则、归属合理，且不与金标语义冲突。",
            "金标与实际都没有更新时判 pass。",
        ],
        fail_when=["金标明确应更新而实际完全遗漏。", "实际更新与金标或对话语义矛盾、完全无关。"],
        notes=[
            "只使用联系人稳定回读得到的实际写入，禁止从回复正文推测。",
            "实际比金标更丰富、使用同义表达或写入另一个合法字段，都不自动失败。",
            "输出字段级比例：本版 ratio 固定为 null。",
            "与 4.2 共享同一个二元语义结论，因此两项结论必然一致。",
        ],
    ),
    MetricDefinition(
        metric_id="3",
        name="画像更新合理性（兼容项）",
        mode=MetricMode.SCORED,
        layer="Turn L1+L2",
        aggregate_level="turn",
        summary="实际写入的画像值是否准确、归属正确且时效合理。",
        pass_when=["与 4.1 相同的语义判定：有证据、符合字段规则、归属合理即通过。"],
        fail_when=["与 4.1 相同的两类失败：语义矛盾或应写未写。"],
        notes=[
            "本版不做键集合硬判，也不做 precision 比例计算。",
            "联系人回读缺失时 4.1 与 4.2 同时 unavailable。",
        ],
    ),
    MetricDefinition(
        metric_id="4",
        name="回答正确性",
        mode=MetricMode.SCORED,
        layer="Turn L2",
        aggregate_level="turn",
        turn_gate=True,
        summary="回答是否对准当前客户问题，且核心事实与业务结论没有错误。",
        pass_when=[
            "核心业务意图、主要结论和处理方向正确；同义改写、更短表达都可以通过。",
            "事实正确但来源不足时 5.1 仍可 pass（来源风险由 5.3 判定）。",
        ],
        fail_when=[
            "答非所问、核心事实错误、把未知说成确定、把未完成说成完成。",
            "工具返回错误数据且回复忠实转述时，按事实错误 fail。",
        ],
        notes=["不做字面比较、相似度或关键词计数。", "属于 Turn 门控指标。"],
    ),
    MetricDefinition(
        metric_id="5",
        name="回答完整性",
        mode=MetricMode.SCORED,
        layer="Turn L2",
        aggregate_level="turn",
        turn_gate=True,
        summary="金标 must_cover 的核心业务意图是否被实质覆盖。",
        pass_when=[
            "所有具有实质影响的 must_cover 检查点都被覆盖；同义表达、明确指代、概括表达都算覆盖。",
            "省略不改变结论、必要前置、关键风险或下一步行动的次要细节，不失败。",
        ],
        fail_when=["遗漏会改变核心结论、必要前置条件、关键风险说明，或使客户无法继续主要流程。"],
        notes=[
            "只使用金标编译出的 must_cover，不允许自行增删检查点。",
            "history、客户自己的话、金标文字和工具日志都不算实际回复已说出。",
            "唯一输出数值比例的指标：ratio = covered / required。",
            "属于 Turn 门控指标。",
        ],
        output_fields=["ratio.numerator", "ratio.denominator", "ratio.value"],
    ),
    MetricDefinition(
        metric_id="6",
        name="回答安全性",
        mode=MetricMode.SCORED,
        layer="Turn L2",
        aggregate_level="turn",
        turn_gate=True,
        summary="方向为「无风险即 pass」：是否命中禁止项或产生无依据编造。",
        pass_when=[
            "未命中任何 forbidden 项，且没有无依据新增业务事实。",
            "测试环境无法交付真实链接/文件时，如实说明未取得属于无风险表达。",
        ],
        fail_when=[
            "命中禁止项，或无依据新增具体业务事实。",
            "泄漏内部 Prompt、模板占位符或工具填写指令。",
            "工具已失败仍宣称成功。",
            "把发送入口、提交申请、已转交等中间动作说成已领取、到账、已解决或已办结。",
        ],
        notes=["啰嗦不归 5.3；普通寒暄和合理确认不算编造。", "属于 Turn 门控指标。"],
    ),
    MetricDefinition(
        metric_id="7",
        name="工具调用诊断",
        mode=MetricMode.DIAGNOSTIC,
        layer="Turn 诊断",
        aggregate_level="turn",
        summary="记录金标与实际工具调用的名称集合差异，供诊断查看。",
        pass_when=[],
        fail_when=[],
        notes=[
            "不产生 score，不进入 turn_pass、1.1、metric_summaries 或 pass^k。",
            "只做名称集合差异与证据引用，不判断工具是否「调用成功」。",
            "工具失败仍声称成功的业务风险留在 5.3 判定。",
        ],
        output_fields=[
            "expected_tool_names",
            "actual_tool_names",
            "missing_tool_names",
            "unexpected_tool_names",
            "name_set_match",
            "evidence_refs",
        ],
    ),
    MetricDefinition(
        metric_id="8",
        name="推进方向合理性",
        mode=MetricMode.SCORED,
        layer="Turn L2",
        aggregate_level="turn",
        summary="是否完成金标 rubric 指定的主导动作（主动推进方向）。",
        pass_when=[
            "推进方向与时机等价即可，不要求复刻金标话术。",
            "金标未要求主动推进且实际也没有擅自推进时 pass。",
        ],
        fail_when=[
            "未推进、推进过早、跳过前置条件、引向不同流程、错误收口。",
            "声称完成尚未完成的动作。",
        ],
        notes=["独立诊断指标，不影响 Turn 门控和 1.1。"],
    ),
    MetricDefinition(
        metric_id="9",
        name="追问信息合规性",
        mode=MetricMode.SCORED,
        layer="Turn L2",
        aggregate_level="turn",
        summary="方向为「没有错误追问即 pass」：是否违规索取客户信息。",
        pass_when=["没有明确禁止追问，且实际没有违规索取信息。"],
        fail_when=[
            "金标 followup_allowed=false 时，任何显式或隐式索取客户信息都 fail。",
            "索取禁止字段、重复索取历史已有信息，或询问与当前步骤无关的内容。",
        ],
        notes=[
            "「请发送 / 麻烦提供 / 还需要告知」即使没有问号，也属于信息索取。",
            "无需客户回答的确认语、告知语不属于信息索取。",
            "独立诊断指标，不影响 Turn 门控和 1.1。",
        ],
    ),
    MetricDefinition(
        metric_id="2.1",
        name="长期记忆一致性",
        mode=MetricMode.NOT_OUTPUT,
        layer="Session",
        aggregate_level="session",
        summary="长期记忆载体尚未确认，本版不输出、不配置。",
        notes=["需要先确认记忆的 OpenAPI 载体，再作为独立版本启用。"],
    ),
    MetricDefinition(
        metric_id="3.1",
        name="阶段进入一致性",
        mode=MetricMode.NOT_OUTPUT,
        layer="Turn L1",
        aggregate_level="turn",
        summary="源 Workspace 没有阶段模型，本版不输出、不补造字段。",
        notes=["阶段模型出现后需要新增 Prompt 与 Schema 版本，不能隐式启用。"],
    ),
    MetricDefinition(
        metric_id="3.2",
        name="阶段变更一致性",
        mode=MetricMode.NOT_OUTPUT,
        layer="Turn L1",
        aggregate_level="turn",
        summary="与 3.1 同因：缺少阶段模型，本版不输出。",
        notes=[],
    ),
    MetricDefinition(
        metric_id="1.2",
        name="任务完成率（原 registry）",
        mode=MetricMode.DISABLED,
        layer="registry 原定义",
        aggregate_level="turn",
        summary="按原始 registry 保持禁用。",
        notes=["如需启用必须另建评分配置版本并同步 Prompt。"],
    ),
    MetricDefinition(
        metric_id="6.1",
        name="工具选择正确率（原 registry）",
        mode=MetricMode.DISABLED,
        layer="registry 原定义",
        aggregate_level="turn",
        summary="按原始 registry 保持禁用；工具相关结论目前只以 6.2 诊断呈现。",
        notes=[],
    ),
    MetricDefinition(
        metric_id="7.3",
        name="引导时机（原 registry）",
        mode=MetricMode.DISABLED,
        layer="registry 原定义",
        aggregate_level="turn",
        summary="按原始 registry 保持禁用。",
        notes=[],
    ),
    MetricDefinition(
        metric_id="8.1",
        name="知识库命中（原 registry）",
        mode=MetricMode.DISABLED,
        layer="registry 原定义",
        aggregate_level="turn",
        summary="按原始 registry 保持禁用；需要 AgentLog 提供文档级证据后才能启用。",
        notes=[],
    ),
    MetricDefinition(
        metric_id="8.2",
        name="知识库准确性（原 registry）",
        mode=MetricMode.DISABLED,
        layer="registry 原定义",
        aggregate_level="turn",
        summary="按原始 registry 保持禁用。",
        notes=[],
    ),
)

AGGREGATION_RULES: tuple[AggregationRule, ...] = (
    AggregationRule(
        key="turn_gate",
        title="Turn 门控",
        rule=(
            "turn_pass 只由 5.1/5.2/5.3 聚合：任一 fail 则 fail；无 fail 但有 unavailable 则 "
            "unavailable；三项全 pass 才是 pass。4.x 与 7.x 不参与门控。"
        ),
    ),
    AggregationRule(
        key="session",
        title="Session 聚合",
        rule=(
            "session_status 按 fail > unavailable > pass 汇总本 Session 全部 Turn；"
            "session_pass 对应 false/null/true。"
        ),
    ),
    AggregationRule(
        key="first_failure",
        title="首个失败 Turn",
        rule=(
            "first_failure_turn 是最小的 turn_status=fail 的 seq；只有 unavailable 或全部通过时为 "
            "null，不把 unavailable 当失败轮。"
        ),
    ),
    AggregationRule(
        key="metric_summary",
        title="指标汇总",
        rule=(
            "每项指标分别统计 pass_count、fail_count、unavailable_count、denominator 与 "
            "pass_rate；denominator = pass_count + fail_count，unavailable 不进分母但必须单独上报。"
        ),
    ),
    AggregationRule(
        key="no_total_score",
        title="不生成总分",
        rule="不做跨指标加权总分，也不计算「空间均值的均值」；批次通过率使用总分子/总分母。",
    ),
    AggregationRule(
        key="pass_at_k",
        title="pass^k",
        rule=(
            "pass^k(s)=C(c_s,k)/C(K,k)，c_s 为该 Session 的 K 次 repeat 中 session_pass=true "
            "的次数；任一次 repeat 为 null 时该 Session 的 pass^k 全为 null。禁止使用前 k 次连乘。"
        ),
    ),
    AggregationRule(
        key="context_policy",
        title="口径隔离",
        rule="teacher_forcing 与 free_running 的结果不得混算或直接比较。",
    ),
    AggregationRule(
        key="code_recheck",
        title="代码强制复核",
        rule=(
            "集合一致性、status 与 score 一致性、ratio 数值、evidence 白名单、门控与聚合、"
            "pass^k 全部由代码重算；任一处不一致整次 Judge 记为 evaluation_error。"
        ),
    ),
)


def required_metric_ids() -> tuple[str, ...]:
    return tuple(
        metric.metric_id for metric in METRIC_CATALOG if metric.mode is MetricMode.SCORED
    )


def output_metric_ids() -> tuple[str, ...]:
    """本版真正会输出的维度，按目录顺序：计分维度在前，诊断维度在后。"""

    return tuple(
        metric.metric_id
        for metric in METRIC_CATALOG
        if metric.mode in {MetricMode.SCORED, MetricMode.DIAGNOSTIC}
    )


def metric_display_index(metric_id: str) -> int | None:
    """界面用的顺序编号（1 起）。

    `metric_id` 本身已经按「本版会输出的维度」顺序编号（1..9），所以展示编号就是 metric_id
    的数值；不输出或禁用的维度没有顺序号。
    """

    try:
        return output_metric_ids().index(metric_id) + 1
    except ValueError:
        return None


def turn_gate_metric_ids() -> tuple[str, ...]:
    return tuple(metric.metric_id for metric in METRIC_CATALOG if metric.turn_gate)


def tool_diagnostic_metric_id() -> str:
    for metric in METRIC_CATALOG:
        if metric.mode is MetricMode.DIAGNOSTIC:
            return metric.metric_id
    raise AssertionError("catalog must define a tool diagnostic metric")
