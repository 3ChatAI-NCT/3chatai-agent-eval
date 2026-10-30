import json
import threading

import pytest

from atomic_eval import pairwise
from atomic_eval.models import EvalCase, Output, ToolCall
from atomic_eval.pairwise import DIMENSIONS, JudgeError, grade_pair, packet

REQUEST = {"model": "agent-model", "input": [{"role": "user", "content": "周六有课吗"}]}


def make_case(candidate: list[str], baseline: list[str], **overrides) -> EvalCase:
    return EvalCase.model_validate({
        "case_id": "c1", "current_turn": "周六有课吗",
        "outputs": [Output(side_id="baseline", messages=baseline),
                    Output(side_id="candidate", messages=candidate)], **overrides})


def review(winner: str, issues=()) -> str:
    return json.dumps({
        "task_type": "customer_reply", "expected_action": "回答周六是否有课",
        "winner": winner, "confidence": "medium", "rationale": "依据",
        "dimensions": [{"dimension": d, "A": "pass", "B": "pass", "verdict": "tie"}
                       for d in DIMENSIONS],
        "issues": list(issues), "contract_conflicts": [], "limitations": []})


@pytest.fixture(autouse=True)
def sequential_reviews(monkeypatch, request):
    # 预设结果按调用顺序返回，其余测试顺序执行两次评审以保证可复现
    if "parallel" not in request.node.name:
        monkeypatch.setattr(pairwise, "_in_parallel",
                            lambda review, orders: [review(order) for order in orders])


class Judge:
    """按调用顺序返回预设结果，并记下每次看到的 packet。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.packets = []

    def complete(self, _system_prompt, body):
        self.packets.append(body)
        answer = self.answers.pop(0)
        return answer(body) if callable(answer) else answer


def test_packet_hides_model_identity_and_carries_rule_checks():
    case = make_case(["有的"], ["周六有课"])
    body = packet(case, REQUEST, {"A": "candidate", "B": "baseline"})
    assert "model" not in body["request"]
    assert body["A"] == {"messages": ["有的"], "calls": []}
    assert body["rule_checks"] == {"A": [], "B": []}


def test_consistent_winner_across_swapped_orders_is_a_verdict():
    case = make_case(["周六上午有课"], ["有课"])
    # 无论 A/B 怎么排都选中候选：模拟没有位置偏差的评审
    def prefers_candidate(body):
        return review("A" if body["A"]["messages"] == ["周六上午有课"] else "B")

    judge = Judge(prefers_candidate, prefers_candidate)
    result = grade_pair(case, REQUEST, judge, seed="s", system_prompt="p")
    assert judge.packets[0]["A"] == judge.packets[1]["B"]
    assert result["verdict"] == {"relative": "G", "reason": "两次盲评结论一致",
                                 "agreement": True, "identical": False}
    assert [r["winner"] for r in result["reviews"]] == ["candidate", "candidate"]


def test_position_bias_shows_up_as_a_split_for_human_review():
    case = make_case(["周六上午有课"], ["有课"])
    result = grade_pair(case, REQUEST, Judge(review("A"), review("A")), seed="s", system_prompt="p")
    assert result["verdict"]["relative"] == "D"
    assert result["verdict"]["agreement"] is False


def test_identical_outputs_are_reviewed_once_and_tied():
    case = make_case(["周六有课"], ["周六有课"])
    issue = {"side": "A", "dimension": "groundedness", "severity": "major",
             "description": "未核实课表就承诺", "citations": [
                 {"pointer": "/A/messages/0", "quote": "周六有课"}]}
    judge = Judge(review("A", [issue, {**issue, "side": "B", "citations": [
        {"pointer": "/B/messages/0", "quote": "周六有课"}]}]))
    result = grade_pair(case, REQUEST, judge, seed="s", system_prompt="p")
    assert len(judge.packets) == 1
    assert result["verdict"]["relative"] == "S" and result["verdict"]["identical"] is True
    assert sorted(i["side"] for i in result["reviews"][0]["issues"]) == ["baseline", "candidate"]
    assert result["worst"] == {"candidate": "major", "baseline": "major"}


def test_issue_with_a_made_up_quote_is_dropped_and_counted():
    case = make_case(["周六上午有课"], ["有课"])
    fake = {"side": "A", "dimension": "groundedness", "severity": "critical",
            "description": "捏造优惠",
            "citations": [{"pointer": "/A/messages/0", "quote": "打八折"}]}
    result = grade_pair(case, REQUEST, Judge(review("tie", [fake]), review("tie")),
                        seed="s", system_prompt="p")
    assert result["reviews"][0]["issues"] == []
    assert result["reviews"][0]["rejected_issues"] == 1
    assert result["worst"] == {"candidate": "none", "baseline": "none"}


def test_rule_quoted_from_structured_request_content_is_accepted():
    request = {"model": "m", "input": [{"role": "system", "content": [
        {"type": "input_text", "text": "只能根据知识库回答价格，\n不得承诺未提供的优惠。"}]}]}
    case = make_case(["全场八折"], ["请稍等，我帮您查一下"])
    issue = {"side": "A", "dimension": "groundedness", "severity": "critical",
             "description": "捏造折扣", "citations": [
                 {"pointer": "/A/messages/0", "quote": "全场八折"},
                 {"pointer": "/request/input/0/content", "quote": "价格， 不得承诺未提供的优惠"}]}
    judge = Judge(lambda body: review("B", [issue] if body["A"]["messages"] == ["全场八折"]
                                     else [{**issue, "side": "B", "citations": [
                                         {"pointer": "/B/messages/0", "quote": "全场八折"}]}]),
                  lambda body: review("A" if body["A"]["messages"] != ["全场八折"] else "B"))
    result = grade_pair(case, REQUEST | request, judge, seed="s", system_prompt="p")
    assert result["reviews"][0]["rejected_issues"] == 0
    assert result["worst"]["candidate"] == "critical"


def test_right_quote_with_a_miscounted_index_is_relocated_but_other_sides_text_is_not():
    request = {"model": "m", "input": [{"role": "system", "content": "不得承诺未提供的优惠"},
                                        {"role": "user", "content": "能打折吗"}]}
    case = make_case(["全场八折"], ["请稍等"])
    miscounted = {"side": "A", "dimension": "groundedness", "severity": "critical",
                  "description": "捏造折扣", "citations": [
                      {"pointer": "/A/messages/0", "quote": "全场八折"},
                      {"pointer": "/request/input/1/content", "quote": "不得承诺未提供的优惠"}]}
    wrong_side = {**miscounted, "side": "B", "citations": [
        {"pointer": "/B/messages/0", "quote": "全场八折"}]}

    def answer(body):
        label = "A" if body["A"]["messages"] == ["全场八折"] else "B"
        issue = {**miscounted, "side": label, "citations": [
            {"pointer": f"/{label}/messages/0", "quote": "全场八折"},
            miscounted["citations"][1]]}
        other = {**wrong_side, "side": "B" if label == "A" else "A"}
        return review("B" if label == "A" else "A", [issue, other])

    result = grade_pair(case, request, Judge(answer, answer), seed="s", system_prompt="p")
    first = result["reviews"][0]
    assert first["rejected_issues"] == 1
    assert first["issues"][0]["citations"][1]["pointer"] == "/request/input/0/content"


def test_extra_fields_from_the_model_are_ignored():
    case = make_case(["周六上午有课"], ["有课"])
    noisy = json.dumps({**json.loads(review("tie")), "reasoning": "hidden"})
    assert grade_pair(case, REQUEST, Judge(noisy, noisy), seed="s",
                      system_prompt="p")["verdict"]["relative"] == "S"


def test_repeated_profile_write_reaches_the_judge_as_a_rule_check():
    case = make_case([], [], profile_snapshot={"所在位置": "高县龙湾"},
                     available_tools=["tool_update_visitor_loc_c"])
    case = case.model_copy(update={"outputs": [
        Output(side_id="baseline"),
        Output(side_id="candidate", tool_calls=[
            ToolCall(name="tool_update_visitor_loc_c", arguments={"所在位置": "高县龙湾"})])]})
    judge = Judge(review("B"), review("A"))
    result = grade_pair(case, REQUEST, judge, seed="s", system_prompt="p")
    label = next(label for label in ("A", "B") if judge.packets[0][label]["calls"])
    assert judge.packets[0]["rule_checks"][label][0]["pointer"] == f"/{label}/calls/0"
    assert result["rule_checks"]["candidate"][0]["category"] == "duplicate-action"


def test_wrapped_output_is_unwrapped_and_one_bad_output_is_retried():
    case = make_case(["周六上午有课"], ["有课"])
    judge = Judge(json.dumps({"json": json.loads(review("tie"))}), "不是 JSON", review("tie"))
    result = grade_pair(case, REQUEST, judge, seed="s", system_prompt="p")
    assert result["verdict"]["relative"] == "S"
    assert len(judge.packets) == 3


def test_output_that_stays_unusable_is_an_error():
    case = make_case(["周六上午有课"], ["有课"])
    with pytest.raises(JudgeError):
        grade_pair(case, REQUEST, Judge("不是 JSON", "仍然不是", "还是不是"), seed="s",
                   system_prompt="p")


def test_format_attempts_come_from_the_frozen_judge_config():
    case = make_case(["周六上午有课"], ["有课"])
    judge = Judge("不是 JSON", review("tie"), review("tie"))
    with pytest.raises(JudgeError):
        grade_pair(case, REQUEST, judge, seed="s", system_prompt="p", format_attempts=1)
    assert len(judge.packets) == 1


def test_swapped_reviews_run_in_parallel():
    both_waiting = threading.Barrier(2, timeout=5)

    class Blocking:
        def complete(self, _system_prompt, _body):
            both_waiting.wait()  # 顺序执行时第一次评审会在这里超时
            return review("tie")

    case = make_case(["周六上午有课"], ["有课"])
    result = grade_pair(case, REQUEST, Blocking(), seed="s", system_prompt="p")
    assert result["verdict"]["relative"] == "S"


def test_ellipsis_quotes_must_match_every_piece_in_order():
    document = {"request": {"rules": "客户状态（必填）：公开课待听课；可听课日期1：2026-09-26"}}

    def cite(quote):
        return pairwise.locate(document, pairwise.Citation(pointer="/request/x", quote=quote))

    assert cite("客户状态...：公开课待听课") == "/request/rules"
    assert cite("可听课日期1…：2026-09-26") == "/request/rules"
    assert cite("公开课待听课...客户状态") is None  # 顺序颠倒
    assert cite("客户状态...：已报名") is None  # 某一段不在原文中
