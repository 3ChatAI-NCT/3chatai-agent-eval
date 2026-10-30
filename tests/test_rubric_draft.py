from __future__ import annotations

from golden_eval.application.rubric_draft import (
    PROFILE_PENDING,
    TOOLS_NONE,
    align_turn_profile_line,
    draft_turn_rubric,
    set_section,
)


def test_turn_rubric_is_drafted_from_the_online_reply() -> None:
    rubric = draft_turn_rubric(
        customer_texts=["你好人脸录入有问题怎么解决"],
        expected_texts=["收到～为了匹配正确的服务，请补充一下【品牌＋门店名称】"],
    )

    assert rubric.startswith("【必须覆盖】语义覆盖：收到～为了匹配正确的服务")
    assert "【禁止】" in rubric
    # The guidance rule table turned the observed request into the lead action.
    assert "【主导动作】请客户补充品牌和具体门店名称" in rubric
    # No workspace tool evidence was captured, so the draft says so instead of guessing.
    assert f"【工具】{TOOLS_NONE}" in rubric
    assert f"【画像】{PROFILE_PENDING}" in rubric
    assert "【标注说明】规则预填" in rubric


def test_turn_rubric_falls_back_to_no_action_without_markers() -> None:
    rubric = draft_turn_rubric(
        customer_texts=["那还可以退吗"],
        expected_texts=["未开卡的会员卡购买后 7 天内可以申请无理由退款。"],
    )

    assert "【主导动作】无；不得凭空追加业务动作。" in rubric
    assert (
        "【必须覆盖】语义覆盖：未开卡的会员卡购买后 7 天内可以申请无理由退款"
        in rubric
    )


def test_forbidden_rules_react_to_observed_markers() -> None:
    rubric = draft_turn_rubric(
        customer_texts=["我要投诉，截图在这里"],
        expected_texts=["已转交工作人员核实，凭证存在待支付，不能作为核验依据"],
    )

    assert "人工核实尚未返回结论，不得宣称退款、调整、删除、预约或其他事项已经办结" in rubric
    assert "凭证存在待支付、门店冲突或类型不符时，不得直接认定核验通过" in rubric
    assert "争议尚未核实，不得擅自承诺赔偿" in rubric


def test_tool_line_uses_observed_calls_when_present() -> None:
    rubric = draft_turn_rubric(
        customer_texts=["帮我查一下订单"],
        expected_texts=["已为您查询。"],
        tool_names=["订单查询"],
    )

    assert "【工具】必须观察到调用：订单查询；只判断是否调用。" in rubric
    assert "不得虚构本轮存在工具或 MCP 调用" not in rubric


def test_profile_line_follows_the_validated_profile_golden() -> None:
    rubric = align_turn_profile_line("【画像】本轮无画像更新。", ["location_c", "phoneNumber"])

    assert "【画像】本轮应更新：location_c、phoneNumber；其余画像字段保持上一轮值。" in rubric

    cleared = align_turn_profile_line(rubric, [])
    assert "【画像】本轮无画像更新。" in cleared
    assert "本轮应更新" not in cleared


def test_set_section_replaces_in_place_and_appends_when_missing() -> None:
    original = "【必须覆盖】a。\n【禁止】b。"

    replaced = set_section(original, "禁止", "c。")
    assert replaced.splitlines() == ["【必须覆盖】a。", "【禁止】c。"]

    appended = set_section(original, "主导动作", "d。")
    assert appended.splitlines() == ["【必须覆盖】a。", "【禁止】b。", "【主导动作】d。"]
