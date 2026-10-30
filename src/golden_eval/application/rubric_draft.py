"""Draft the v0.4 fixed-heading rubric from observed facts.

Ported from the reference implementation (`codex-golden-builder-v9`): the online reply is the
reference answer, so its clauses become the 【必须覆盖】 checkpoints, and rule tables turn observed
markers into 【禁止】 / 【主导动作】 lines. Everything here is deterministic and runs at draft
generation time, so a generated Session never starts from a placeholder — a human only spot-checks
and edits.

The 【画像】 line is the one section this module cannot know at generation time: profile goldens are
produced by the enrichment step, which re-aligns that line from the validated result.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

SPLIT_PATTERN = re.compile(r"[\n。！？!?；;]+")
FOLLOWUP_MARKERS = ("？", "?", "请提供", "请补充", "发张", "发一下", "告知", "确认一下")
MAX_COVER_POINTS = 8
MAX_POINT_LENGTH = 180

TURN_SECTIONS: tuple[str, ...] = (
    "必须覆盖",
    "禁止",
    "主导动作",
    "工具",
    "画像",
    "标注说明",
)
# Marker → required lead action. Written from the observed service scenarios; an unmatched turn
# falls back to 「无」 so the draft never invents a business action.
GUIDANCE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("品牌＋门店", "品牌+门店"), "请客户补充品牌和具体门店名称"),
    (("具体门店名称", "哪家门店"), "请客户补充具体门店名称"),
    (("订单截图", "券码截图"), "请客户提供可核验的订单或券码截图"),
    (("美团、抖音", "订单是美团", "下单平台"), "请客户确认订单来源平台"),
    (("注册手机号", "电话号", "联系电话"), "请客户提供本人联系电话"),
    (("哪天、什么时间", "大概几点", "几点到店"), "请客户确认期望到店日期和时间"),
    (("还在门店现场", "是否仍在门店"), "确认客户当前是否仍在门店现场"),
    (("正文截图",), "请客户补充文章正文截图后再整理对外口径"),
    (("想整理成", "用于什么", "哪种形式", "哪方面内容"), "请客户确认内容用途和交付形式"),
    (("点击立即领取", "戳链接领取"), "引导客户打开小程序链接完成领取"),
    (("等待工作人员", "核实后会联系", "结果出来后"), "等待工作人员核实并向客户同步结果"),
)

PENDING_MARKERS = ("转交", "核实", "处理中", "等待", "暂无")
CLAIMED_DELIVERY_MARKERS = ("点击立即领取", "戳链接领取", "领取链接")
CORRECTION_MARKERS = ("刚才的说明有误", "此前", "推翻")
DISPUTE_MARKERS = ("投诉", "欺骗消费者", "诱导消费者")
VOUCHER_PROBLEM_MARKERS = ("待支付", "不一致", "不能作为", "重新发")

BASE_FORBIDDEN = "不得加入金标上文、当前客户消息、Workspace 配置或工具调用中没有依据的新业务事实"

PROFILE_PENDING = "本轮无画像更新（补全结果写入后会同步本行）。"
NOTE_DRAFT = "规则预填：依据线上回复、客户消息与金标规则生成，待人工抽检后修改。"
TOOLS_NONE = "本轮无可观察执行动作；多余调用记错。"


def set_section(rubric: str, section: str, content: str) -> str:
    """Replace one fixed heading in place, or append it when the heading is missing."""

    heading = f"【{section}】"
    lines = [line for line in rubric.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith(heading):
            lines[index] = f"{heading}{content}"
            return "\n".join(lines)
    lines.append(f"{heading}{content}")
    return "\n".join(lines)


def draft_turn_rubric(
    *,
    customer_texts: Sequence[str],
    expected_texts: Sequence[str],
    history_texts: Sequence[str] = (),
    tool_names: Sequence[str] = (),
) -> str:
    customer_text = "\n".join(customer_texts)
    expected_text = "\n".join(expected_texts)
    history_text = "\n".join(history_texts)
    guidance = guidance_for(expected_text)
    if guidance is None and has_followup(expected_texts):
        guidance = "允许继续追问。"
    lines = [
        f"【必须覆盖】{_join(_cover_points(expected_texts))}。",
        f"【禁止】{_join(forbidden_for(customer_text, expected_text, history_text, tool_names))}。",
        f"【主导动作】{guidance or '无；不得凭空追加业务动作。'}",
        f"【工具】{tool_line(tool_names)}",
        f"【画像】{PROFILE_PENDING}",
        f"【标注说明】{NOTE_DRAFT}",
    ]
    return "\n".join(lines)


def align_turn_profile_line(rubric: str, fields: Sequence[str]) -> str:
    """Keep the 【画像】 line equal to the validated profile golden."""

    content = (
        f"本轮应更新：{'、'.join(fields)}；其余画像字段保持上一轮值。"
        if fields
        else "本轮无画像更新。"
    )
    return set_section(rubric, "画像", content)


def tool_line(tool_names: Sequence[str]) -> str:
    if not tool_names:
        return TOOLS_NONE
    return f"必须观察到调用：{'、'.join(tool_names)}；只判断是否调用。"


def has_followup(texts: Sequence[str]) -> bool:
    text = "\n".join(texts)
    return any(marker in text for marker in FOLLOWUP_MARKERS)


def guidance_for(assistant_text: str) -> str | None:
    for markers, guidance in GUIDANCE_RULES:
        if any(marker in assistant_text for marker in markers):
            return guidance
    return None


def forbidden_for(
    customer_text: str,
    assistant_text: str,
    history_text: str,
    tool_names: Sequence[str],
) -> list[str]:
    forbidden = [BASE_FORBIDDEN]
    if tool_names:
        forbidden.append("不得省略本轮已观察到的工具调用及其对应关键业务动作")
    else:
        forbidden.append("不得虚构本轮存在工具或 MCP 调用")
    if any(marker in assistant_text for marker in PENDING_MARKERS):
        forbidden.append("人工核实尚未返回结论，不得宣称退款、调整、删除、预约或其他事项已经办结")
    if any(marker in assistant_text for marker in CLAIMED_DELIVERY_MARKERS):
        forbidden.append("发送领取入口不等于客户已领取或到账，不得把待客户操作写成已完成")
    if any(marker in assistant_text for marker in CORRECTION_MARKERS):
        forbidden.append("不得继续沿用已经被本轮纠正的前序结论")
    if "截图" in customer_text and any(
        marker in assistant_text for marker in VOUCHER_PROBLEM_MARKERS
    ):
        forbidden.append("凭证存在待支付、门店冲突或类型不符时，不得直接认定核验通过")
    if any(marker in customer_text + history_text for marker in DISPUTE_MARKERS):
        forbidden.append("争议尚未核实，不得擅自承诺赔偿、通用权益恢复或具体完成时效")
    return list(dict.fromkeys(forbidden))


def _cover_points(expected_texts: Sequence[str]) -> list[str]:
    points: list[str] = []
    seen: set[str] = set()
    for text in expected_texts:
        for candidate in SPLIT_PATTERN.split(text or ""):
            point = candidate.strip(" ，,。；;：:")
            if not point:
                continue
            point = point[:MAX_POINT_LENGTH]
            key = re.sub(r"\s+", "", point)
            if key in seen:
                continue
            seen.add(key)
            points.append(f"语义覆盖：{point}")
            if len(points) == MAX_COVER_POINTS:
                return points
    return points or ["本轮回复应与线上回复在业务含义上等价"]


def _join(items: Sequence[str]) -> str:
    return "；".join(items)
