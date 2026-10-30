from __future__ import annotations

from atomic_eval.checks import run_checks
from atomic_eval.models import EvalCase, Output, Severity, ToolCall


def make_case(**overrides) -> EvalCase:
    data = {
        "case_id": "c1",
        "task_type": "mainagent",
        "current_turn": "我想问一下周六有没有课",
        "history_tool_calls": [ToolCall(name="knowledge", arguments={"query": "周六课程安排"})],
        "profile_snapshot": {"所在位置": "高县龙湾"},
        "available_tools": ["knowledge", "tool_update_visitor_loc_c"],
        "outputs": [
            Output(side_id="baseline", messages=["周六有课"]),
            Output(side_id="candidate", messages=["周六有课"]),
        ],
    }
    data.update(overrides)
    return EvalCase.model_validate(data)


def test_repeated_profile_write_is_flagged():
    case = make_case(
        outputs=[
            Output(
                side_id="candidate",
                tool_calls=[
                    ToolCall(name="tool_update_visitor_loc_c", arguments={"所在位置": "高县龙湾"})
                ],
            )
        ]
    )
    issues = run_checks(case, "candidate")
    assert [i.dimension for i in issues] == ["duplicate-action"]
    assert issues[0].severity is Severity.MAJOR


def test_changed_profile_value_is_not_duplicate():
    case = make_case(
        outputs=[
            Output(
                side_id="candidate",
                tool_calls=[
                    ToolCall(name="tool_update_visitor_loc_c", arguments={"所在位置": "成都天府"})
                ],
            )
        ]
    )
    assert run_checks(case, "candidate") == []


def test_repeated_query_and_unknown_tool_are_flagged():
    case = make_case(
        outputs=[
            Output(
                side_id="candidate",
                tool_calls=[
                    ToolCall(name="knowledge", arguments={"query": "周六课程安排。"}),
                    ToolCall(name="不存在的工具", arguments={}),
                ],
            )
        ]
    )
    dims = sorted(i.dimension for i in run_checks(case, "candidate"))
    assert dims == ["duplicate-action", "tool-contract"]
