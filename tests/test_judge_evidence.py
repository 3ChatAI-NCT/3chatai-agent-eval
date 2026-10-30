from copy import deepcopy

import pytest

from agent_eval.judge_evidence import assemble, prepare


def job():
    return {"id": "judge", "experiment_id": "run", "case_id": "session#T1",
            "repeat_index": 1, "stage": "judge", "input": {
                "observation": {"observation_id": "obs", "replay_request_id": "req"},
                "config": {"space": {"target_workspace_id": "ws"}}}}


class Store:
    def __init__(self):
        self.frozen = None

    def query(self, *_):
        return [{"id": "execute"}]

    def experiment_traces(self, *_):
        return [{"id": "trace", "job_id": "root", "request_id": "req", "workspace_id": "ws",
                 "object_key": "object", "content_sha256": "sha", "status": "partial",
                 "expected_calls": None}]

    def freeze_judge_evidence(self, _, evidence):
        if self.frozen is None:
            self.frozen = deepcopy(evidence)
        return self.frozen


class Objects:
    def __init__(self, spans):
        self.data = {"request_id": "req", "workspace_id": "ws", "spans": spans}
        self.reads = 0

    def get(self, key, sha):
        assert (key, sha) == ("object", "sha")
        self.reads += 1
        return deepcopy(self.data)


def span(index, request, response):
    return {"seq": index, "span_id": f"s{index}", "request_id": "req", "http_status": "200",
            "has_body": True, "request": request, "response": response}


def test_only_returns_linked_to_calls_issued_in_this_turn_become_evidence():
    objects = Objects([
        span(1, {"messages": [{"role": "system", "content": "secret system prompt"},
             {"role": "tool", "tool_call_id": "old", "content": "old 999"}]},
             {"choices": [{"message": {"tool_calls": [
                 {"id": "fresh", "function": {"name": "quote", "arguments": "{}"}}]}}]}),
        span(2, {"messages": [{"role": "tool", "tool_call_id": "old", "content": "old 999"},
             {"role": "tool", "tool_call_id": "fresh", "content": "price 199"}]}, {}),
        span(3, {"messages": [{"role": "tool", "tool_call_id": "fresh",
                               "content": "price 199"}]}, {})])
    result = assemble(job(), Store(), objects)
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["content"] == "price 199"
    assert result["tool_results"][0]["name"] == "quote"
    assert result["tool_results"][0]["call_span_id"] == "s1"
    assert result["trace"]["call_count_status"] == "unknown"
    assert result["image_pixels_available"] is False
    assert "secret system prompt" not in str(result)
    assert "old 999" not in str(result)
    assert result["diagnostic_counts"]["unlinked_or_historical_tool_result_omitted"] == 2


def test_responses_outputs_require_earlier_response_call_id():
    objects = Objects([
        span(1, {}, {"output": [{"type": "function_call", "call_id": "c", "name": "search"}]}),
        span(2, {"input": [{"type": "function_call_output", "call_id": "c", "output": "found"},
              {"type": "mcp_call", "id": "old", "name": "old", "output": "history"}]}, {})])
    result = assemble(job(), Store(), objects)
    assert [item["content"] for item in result["tool_results"]] == ["found"]
    assert result["tool_results"][0]["name"] == "search"


def test_mismatched_trace_identity_cannot_supply_tool_evidence():
    objects = Objects([])
    objects.data["request_id"] = "another-turn"
    result = assemble(job(), Store(), objects)
    assert result["status"] == "unavailable"
    assert result["tool_results"] == []
    assert "trace_identity_mismatch" in result["diagnostics"]


def test_retry_reuses_frozen_evidence_even_when_trace_becomes_available():
    store, objects = Store(), Objects([])
    current = job()
    first = prepare(current, store, objects)
    objects.data["spans"] = [span(1, {}, {"output": [{"type": "mcp_call", "id": "c",
        "name": "fresh", "output": "newly arrived"}]})]
    current["input"]["judge_evidence"] = first
    assert prepare(current, store, objects) == first
    assert objects.reads == 1


def test_historical_call_id_reused_by_response_is_not_reclassified_as_current():
    objects = Objects([
        span(1, {"input": [{"type": "function_call", "call_id": "old", "name": "history"}]},
             {"output": [{"type": "function_call", "call_id": "old", "name": "new"}]}),
        span(2, {"input": [{"type": "function_call_output", "call_id": "old",
                             "output": "ambiguous"}]}, {})])
    assert assemble(job(), Store(), objects)["tool_results"] == []


def test_old_tool_result_without_assistant_call_cannot_be_reused_as_current():
    objects = Objects([
        span(1, {"messages": [{"role": "tool", "tool_call_id": "old", "content": "history"}]},
             {"output": [{"type": "function_call", "call_id": "old", "name": "new"}]}),
        span(2, {"input": [{"type": "function_call_output", "call_id": "old",
                             "output": "ambiguous"}]}, {})])
    assert assemble(job(), Store(), objects)["tool_results"] == []


def test_conflicting_content_for_one_call_id_is_not_available_to_judge():
    objects = Objects([
        span(1, {}, {"output": [{"type": "function_call", "call_id": "c", "name": "quote"}]}),
        span(2, {"input": [{"type": "function_call_output", "call_id": "c",
                             "output": "199"}]}, {}),
        span(3, {"input": [{"type": "function_call_output", "call_id": "c",
                             "output": "299"}]}, {})])
    result = assemble(job(), Store(), objects)
    assert result["tool_results"] == []
    assert result["status"] == "unavailable"
    assert "conflicting_tool_results_omitted" in result["diagnostics"]


def test_tool_content_is_bounded_and_truncation_is_explicit():
    objects = Objects([
        span(1, {}, {"output": [{"type": "function_call", "call_id": "c", "name": "kb"}]}),
        span(2, {"input": [{"type": "function_call_output", "call_id": "c",
                             "output": "x" * 9000}]}, {})])
    result = assemble(job(), Store(), objects)
    assert result["truncated"] is True
    assert result["tool_results"][0]["truncated"] is True
    assert len(result["tool_results"][0]["content"]) == 8000
    assert result["status"] == "partial"


def test_object_checksum_failure_yields_no_evidence():
    class CorruptObjects:
        def get(self, *_):
            raise ValueError("Object checksum mismatch")

    result = assemble(job(), Store(), CorruptObjects())
    assert result["status"] == "unavailable"
    assert result["tool_results"] == []
    assert result["diagnostics"] == ["trace_object_unavailable"]


@pytest.mark.parametrize("location", ["trace", "span"])
def test_wrong_space_or_span_request_cannot_be_used(location):
    class WrongSpace(Store):
        def experiment_traces(self, *_):
            return [{**super().experiment_traces()[0], "workspace_id": "another-space"}]

    objects = Objects([span(1, {}, {})])
    if location == "span":
        objects.data["spans"][0]["request_id"] = "another-turn"
    result = assemble(job(), WrongSpace() if location == "trace" else Store(), objects)
    assert result["tool_results"] == []
    assert result["status"] == "unavailable"
