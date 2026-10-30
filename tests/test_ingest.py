from __future__ import annotations

import hashlib
import json

import pytest

from atomic_eval.checks import run_checks
from atomic_eval.gateway import ParseError, parse_record, parse_records
from atomic_eval.models import EvalCase, ModelCall, Output, ToolCall
from atomic_eval.responses import (
    available_tools,
    current_turn,
    has_image,
    profile_snapshot,
    session_facts,
    to_output,
)
from atomic_eval.sampling import sample, stratum_of, to_case

CONTEXT_CARD = """【运行时补充上下文卡片】
【用户短期信息】
{"name": "L"}
【用户长期信息】
兴趣课程（记录用户感兴趣的课程类型）：健身
所在位置/商圈（记录用户所在区域）：高县龙湾
学员年级：无
【用户长期字段定义】
- 活动截图来源：记录客户提供的截图平台
"""

REQUEST = {
    "model": "agent-model",
    "input": [
        {"role": "system", "content": "【角色定义】你是顾问"},
        {"role": "user", "content": CONTEXT_CARD},
        {"role": "user", "content": "周六有课吗"},
    ],
    "tools": [
        {"name": "knowledge_knowledgeAll_knowledgeAll"},
        {"name": "tool_update_visitor_interest_c"},
    ],
}

RESPONSE = {
    "status": "completed",
    "output": [
        {"type": "message", "content": [{"text": "周六有课的"}]},
        {
            "type": "function_call",
            "name": "tool_update_visitor_interest_c",
            "arguments": '{"interest_c": "健身"}',
        },
    ],
}


def raw_record(**overrides) -> dict:
    message = (
        "[LLM请求结束] path=/v1/responses status=200 requestId=req-1 attempt=1 "
        f"upstreamModel=agent-model requestBody={json.dumps(REQUEST, ensure_ascii=False)} "
        f"responseBody={json.dumps(RESPONSE, ensure_ascii=False)}"
    )
    record = {
        "msg": message,
        "tenantId": "ws-1",
        "consumeTime": "1489",
        "@timestamp": "2026-09-09T17:51:39.922+0800",
    }
    record.update(overrides)
    return record


# --- 网关日志解析 -------------------------------------------------------------


def test_parse_record_extracts_call():
    call = parse_record(raw_record())
    assert call.call_id == "req-1:attempt:1"
    assert call.workspace_id == "ws-1"
    assert call.request["model"] == "agent-model"
    assert call.elapsed_ms == 1489.0


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ({"msg": "[其他日志] requestBody={}"}, "not_complete_call_record"),
        (
            {"msg": "[LLM请求结束] path=/v1/embeddings status=200 requestId=r requestBody={}"},
            "not_responses_call",
        ),
        (
            {"msg": "[LLM请求结束] path=/v1/responses status=200 requestId=r requestBody={"},
            "request_body_invalid_json",
        ),
        (
            {"msg": '[LLM请求结束] path=/v1/responses status=200 requestId=r requestBody={"a":1}'},
            "response_body_missing",
        ),
    ],
)
def test_parse_errors_carry_stable_reason_codes(record, reason):
    with pytest.raises(ParseError) as excinfo:
        parse_record(record)
    assert excinfo.value.reason == reason


def test_http_error_is_excluded_not_silently_kept():
    record = raw_record()
    record["msg"] = record["msg"].replace("status=200", "status=503")
    with pytest.raises(ParseError) as excinfo:
        parse_record(record)
    assert excinfo.value.reason == "http_error"


def test_parse_records_reports_both_sides():
    calls, excluded = parse_records([raw_record(), {"msg": "无关日志"}])
    assert len(calls) == 1
    assert excluded[0]["reason"] == "not_complete_call_record"


# --- Responses 协议解析 -------------------------------------------------------


def test_profile_snapshot_reads_the_context_card():
    snapshot = profile_snapshot(REQUEST)
    assert snapshot["兴趣课程"] == "健身"
    assert snapshot["所在位置/商圈"] == "高县龙湾"
    assert "学员年级" not in snapshot  # 值为"无"不算已有事实
    assert "活动截图来源" not in snapshot  # 字段定义不是画像值


def test_current_turn_and_tools():
    assert current_turn(REQUEST) == "周六有课吗"
    assert available_tools(REQUEST) == [
        "knowledge_knowledgeAll_knowledgeAll",
        "tool_update_visitor_interest_c",
    ]
    assert has_image(REQUEST) is False


def test_output_splits_messages_and_tool_calls():
    output = to_output(RESPONSE, "baseline")
    assert output.messages == ["周六有课的"]
    assert output.tool_calls[0].name == "tool_update_visitor_interest_c"
    assert output.tool_calls[0].arguments == {"interest_c": "健身"}


def test_case_from_call_detects_duplicate_write_end_to_end():
    call = parse_record(raw_record())
    case = to_case(call)
    issues = run_checks(case, "baseline")
    assert [issue.dimension for issue in issues] == ["duplicate-action"]
    assert "兴趣课程" in issues[0].summary


# --- 采样 ---------------------------------------------------------------------


def make_call(index: int, *, workspace: str = "ws-1", with_tool: bool = False) -> ModelCall:
    response = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"text": "回复"}]}],
    }
    if with_tool:
        response["output"].append({"type": "function_call", "name": "t", "arguments": "{}"})
    return ModelCall(
        call_id=f"req-{index}",
        workspace_id=workspace,
        request={"model": "agent-model", "input": [{"role": "user", "content": "问题"}]},
        response=response,
        request_id=f"req-{index}",
    )


def test_sampling_is_reproducible_and_seed_sensitive():
    calls = [make_call(i) for i in range(40)]
    first = sample(calls, size=10, seed="s1")
    again = sample(calls, size=10, seed="s1")
    other = sample(calls, size=10, seed="s2")
    ids = [c.call_id for c in first.selected]
    assert ids == [c.call_id for c in again.selected]
    assert ids != [c.call_id for c in other.selected]


def test_stratified_sampling_keeps_both_groups():
    calls = [make_call(i) for i in range(30)] + [
        make_call(100 + i, with_tool=True) for i in range(6)
    ]
    result = sample(calls, size=12, seed="s1")
    assert len(result.selected) == 12
    assert len(result.selected_strata) == 2  # 有工具 / 无工具两层都取到了
    assert any("有工具" in key for key in result.selected_strata)


def test_streaming_sampler_matches_full_sort():
    calls = [make_call(i, with_tool=i % 7 == 0) for i in range(300)]

    def rank(call):
        return hashlib.sha256(f"s1:{call.call_id}".encode()).hexdigest()

    result = sample(iter(calls), size=20, seed="s1")
    # 对照：全量排序后按层取前 N 条（流式实现只保留每层前 size 条）
    groups = {}
    for call in calls:
        groups.setdefault(stratum_of(call), []).append(call)
    expected = [call for key, items in groups.items()
                for call in sorted(items, key=rank)[: result.selected_strata.get(key, 0)]]
    assert [c.call_id for c in result.selected] == [c.call_id for c in sorted(expected, key=rank)]
    assert result.strata == {key: len(items) for key, items in groups.items()}


def test_filters_and_shortfall_are_reported():
    calls = [make_call(i) for i in range(3)] + [make_call(9, workspace="ws-2")]
    result = sample(calls, size=10, seed="s1", workspace_ids=["ws-1"])
    assert len(result.selected) == 3
    assert result.shortfall == 7
    assert result.funnel["excluded"]["workspace_mismatch"] == 1


def test_stratum_uses_only_computable_signals():
    call = make_call(1, with_tool=True)
    assert stratum_of(call) == "unknown|有工具|无图|短"


# --- 短期信息 ≠ 已持久化画像 ----------------------------------------------------

CARD_WITH_SESSION = """【用户信息补充】
短期信息：{"name": "圻安致远", "phoneNumber": null, "email": null, "extra": []}
长期信息：无
长期字段定义：- 客户所在城市：记录客户当前所在城市
"""

SESSION_REQUEST = {
    "model": "agent-model",
    "input": [
        {"role": "user", "content": CARD_WITH_SESSION},
        {"role": "user", "content": "在吗"},
    ],
    "tools": [{"name": "tool_update_visitor_name"}],
}


def test_session_facts_are_separate_from_persisted_profile():
    assert session_facts(SESSION_REQUEST) == {"name": "圻安致远"}
    assert profile_snapshot(SESSION_REQUEST) == {}  # 长期信息为"无"


def test_persisting_a_session_fact_is_not_a_duplicate_write():
    """短期信息里的姓名写入联系人是正常持久化，不能判成重复写入。"""

    case = EvalCase(
        case_id="c1",
        current_turn="在吗",
        profile_snapshot=profile_snapshot(SESSION_REQUEST),
        session_facts=session_facts(SESSION_REQUEST),
        available_tools=["tool_update_visitor_name"],
        outputs=[
            Output(
                side_id="candidate",
                tool_calls=[
                    ToolCall(name="tool_update_visitor_name", arguments={"name": "圻安致远"})
                ],
            )
        ],
    )
    assert run_checks(case, "candidate") == []


def test_rewriting_a_persisted_value_is_still_a_duplicate():
    case = EvalCase(
        case_id="c2",
        current_turn="在吗",
        profile_snapshot={"所在位置/商圈": "美立方"},
        session_facts={"name": "柚子"},
        available_tools=["tool_update_visitor_location_c"],
        outputs=[
            Output(
                side_id="candidate",
                tool_calls=[
                    ToolCall(
                        name="tool_update_visitor_location_c", arguments={"location_c": "美立方"}
                    )
                ],
            )
        ],
    )
    issues = run_checks(case, "candidate")
    assert [issue.dimension for issue in issues] == ["duplicate-action"]


# --- 当前轮 ≠ 最后一条 user 消息 -------------------------------------------------

CARD_AFTER_TURN = {
    "model": "agent-model",
    "input": [
        {"role": "system", "content": "【角色定义】你是顾问"},
        {"role": "user", "content": "刚开始\n水电还没进场"},
        {
            "role": "user",
            "content": (
                "【子任务调用输入】\n说明：本消息由主Agent生成，用于调用当前子任务；它不是用户原话。\n\n"
                "【当前任务】\n任务名称：用户问题承接\n"
                "任务输入：用户说明厨房刚开始装修，水电还没进场。请承接用户问题并说明产品价值。\n\n"
                "【优先级规则】\n1. 当前任务输入优先级高于用户本轮原始输入。"
            ),
        },
    ],
    "tools": [],
}


def test_current_turn_skips_system_generated_cards():
    """运行时卡片以 role=user 追加在客户消息之后，不能把它当成客户原话。"""

    assert current_turn(CARD_AFTER_TURN) == "刚开始\n水电还没进场"


def test_task_input_is_extracted_separately():
    from atomic_eval.responses import task_input

    text = task_input(CARD_AFTER_TURN)
    assert text.startswith("用户说明厨房刚开始装修")
    assert "优先级规则" not in text


def test_task_name_comes_from_the_subtask_card():
    """报告里要显示"用户问题承接"，而不是统一的 subagent。"""

    from atomic_eval.responses import task_name

    assert task_name(CARD_AFTER_TURN) == "用户问题承接"
    assert task_name({"input": [{"role": "user", "content": "刚开始"}]}) == ""


def test_tool_labels_resolve_skill_ids_and_descriptions():
    """线上工具名是技能 ID，直接显示等于没说；名字只能从提示词与工具描述里取。"""

    from atomic_eval.responses import tool_labels

    request = {
        "input": [
            {
                "role": "system",
                "content": "可用任务：skill_id：123456789012345678, skill_name：用户问题承接",
            }
        ],
        "tools": [
            {"name": "123456789012345678", "description": "此任务可以：回复用户的任何问题。"},
            {"name": "11111111-2222", "description": "此工具可以配置唤醒闹钟\n参数说明…"},
            {"name": "knowledge_knowledgeAll_knowledgeAll", "description": "当提到以下意图…"},
        ],
    }
    labels = tool_labels(request)
    assert labels["123456789012345678"] == "用户问题承接"  # 提示词里的技能名优先于描述
    assert labels["11111111-2222"] == "此工具可以配置唤醒闹钟"
    assert labels["knowledge_knowledgeAll_knowledgeAll"] == "知识库检索 · 全部知识库"


def test_tool_labels_read_profile_field_names_from_the_prompt():
    from atomic_eval.responses import tool_labels

    tool = "tool_update_visitor_intent_tier_c"
    request = {"input": [{"role": "system",
                          "content": f"## 字段名：意向分层，记录工具：`{tool}`\n可选值…"}],
               "tools": [{"name": tool, "description": "update tool for visitor.intent_tier_c"}]}
    assert tool_labels(request)[tool] == "意向分层"


def test_current_turn_falls_back_when_everything_is_a_card():
    request = {"input": [{"role": "user", "content": "【运行时补充上下文卡片】\n说明：…"}]}
    assert current_turn(request).startswith("【运行时补充上下文卡片】")


# --- 图片消息不能在历史里消失 ----------------------------------------------------


def test_image_messages_survive_extraction():
    """客户发的图片若被丢掉，评审器会把基于图片的后续动作误判成编造事实。"""

    from atomic_eval.responses import content_text, prior_turns

    request = {
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "刚拆完"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "有尺寸图吗"}]},
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": "https://oss/pic.jpg"}],
            },
            {"role": "user", "content": "【运行时补充上下文卡片】\n说明：…"},
        ]
    }
    assert "[客户发送了图片]" in content_text(request["input"][2]["content"])
    assert "https://oss/pic.jpg" in content_text(request["input"][2]["content"])
    history = prior_turns(request)
    assert [turn["role"] for turn in history] == ["user", "assistant"]
    assert current_turn(request).startswith("[客户发送了图片]")


# --- 任务类型推断 ---------------------------------------------------------------


def test_task_type_is_inferred_from_the_request_shape():
    from atomic_eval.sampling import infer_task_type

    plain = {"input": [{"role": "user", "content": "在吗"}], "tools": [{"name": "a"}]}
    assert infer_task_type(plain) == "mainagent"

    sub = {
        "input": [
            {"role": "user", "content": "在吗"},
            {"role": "user", "content": "【子任务调用输入】\n任务输入：承接用户问题"},
        ],
        "tools": [{"name": "knowledge_knowledgeAll_knowledgeAll"}, {"name": "139438"}],
    }
    assert infer_task_type(sub) == "subagent"

    profile = {**sub, "tools": [{"name": "tool_update_visitor_name"}]}
    assert infer_task_type(profile) == "profile-update"


def test_tool_rules_take_the_field_section_or_the_lines_that_mention_the_tool():
    from atomic_eval.responses import tool_rules

    status, tier = "tool_update_visitor_customer_status_c", "tool_update_visitor_intent_tier_c"
    prompt = (f"9. 若客户没有明确拒绝：将客户状态写入`{status}` 为「公开课待听课」\n"
              "无关规则\n"
              f"## 字段名：意向分层，记录工具：`{tier}`\n**可选值及定义：**\n"
              "- {待激活:客户已有线索但当前没有有效互动}\n\n***\n## 字段名：下一节")
    # 技能提示词常以 user 消息下发
    rules = tool_rules({"input": [{"role": "user", "content": prompt}]},
                       [status, tier, "tool_update_visitor_unused_c"])
    assert rules[status] == [f"9. 若客户没有明确拒绝：将客户状态写入`{status}` 为「公开课待听课」"]
    assert "待激活" in rules[tier][0] and "下一节" not in rules[tier][0]
    assert "tool_update_visitor_unused_c" not in rules
