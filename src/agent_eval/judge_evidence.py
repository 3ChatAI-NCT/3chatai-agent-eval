"""仅将同一轮已冻结链路中可关联的新工具返回交给文本 Judge。"""

import json
from collections import Counter

from .models import digest

MAX_TOOL_RESULTS = 20
MAX_CONTENT_CHARS = 8000


def prepare(job, store, objects):
    if "judge_evidence" in job["input"]:
        return job["input"]["judge_evidence"]
    return store.freeze_judge_evidence(job, assemble(job, store, objects))


def assemble(job, store, objects):
    evidence = {"version": "turn-evidence-v1", "status": "unavailable", "tool_results": [],
                "diagnostics": [], "diagnostic_counts": {}, "trace": {},
                "image_pixels_available": False,
                "truncated": False}
    executions = store.query(
        """SELECT id FROM eval_jobs WHERE experiment_id=%s AND case_id=%s
           AND repeat_index=%s AND stage='execute'""",
        (job["experiment_id"], job["case_id"], job["repeat_index"]))
    rows = store.experiment_traces(job["experiment_id"], executions[0]["id"]) if executions else []
    if not rows:
        evidence["diagnostics"].append("trace_not_available_at_judging")
        return evidence
    trace = rows[0]
    evidence["trace"] = {"trace_id": trace["id"], "content_sha256": trace["content_sha256"],
        "request_id": trace["request_id"], "workspace_id": trace["workspace_id"],
        "root_job_id": trace["job_id"], "trace_status": trace["status"],
        "call_count_status": "unknown"}
    expected_id = job["input"]["observation"].get("replay_request_id")
    workspace = job["input"]["config"]["space"]["target_workspace_id"]
    if not expected_id or trace["request_id"] != expected_id or trace["workspace_id"] != workspace:
        evidence["diagnostics"].append("trace_identity_mismatch")
        return evidence
    if (not trace.get("object_key") or not trace.get("content_sha256")
            or trace.get("object_purged_at")):
        evidence["diagnostics"].append("trace_body_not_frozen")
        return evidence
    try:
        body = objects.get(trace["object_key"], trace["content_sha256"])
    except Exception:
        # 对象校验/读取失败只降低证据可用性，不输出异常正文或改为猜测业务事实。
        evidence["diagnostics"].append("trace_object_unavailable")
        return evidence
    if body.get("request_id") != expected_id or body.get("workspace_id") != workspace:
        evidence["diagnostics"].append("trace_identity_mismatch")
        return evidence
    spans = body.get("spans", [])
    if not isinstance(spans, list) or any(
            not isinstance(span, dict) or span.get("request_id") != expected_id for span in spans):
        evidence["diagnostics"].append("span_identity_mismatch")
        return evidence
    if trace.get("expected_calls") and len(spans) >= trace["expected_calls"]:
        evidence["trace"]["call_count_status"] = "at_least_expected"
    issued, historical, seen, result_hashes, conflicting = {}, set(), set(), {}, set()
    diagnostics = Counter()
    for span in sorted(spans, key=lambda item: (item.get("seq", 0), item.get("span_id", ""))):
        if str(span.get("http_status")) != "200" or not span.get("has_body"):
            diagnostics["some_model_bodies_unavailable"] += 1
            continue
        request, response = span.get("request") or {}, span.get("response") or {}
        for result in _returns(request):
            call_id = result["call_id"]
            if not isinstance(call_id, str) or call_id not in issued or call_id in historical:
                diagnostics["unlinked_or_historical_tool_result_omitted"] += 1
                if isinstance(call_id, str):
                    historical.add(call_id)
                continue
            name, call_span = issued[call_id]
            content = result["content"]
            if content in (None, "", [], {}):
                continue
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            identity = digest({"call_id": call_id, "content": content})
            if call_id in result_hashes and result_hashes[call_id] != identity:
                conflicting.add(call_id)
                diagnostics["conflicting_tool_results_omitted"] += 1
                continue
            result_hashes[call_id] = identity
            if identity in seen or not content:
                continue
            seen.add(identity)
            if len(evidence["tool_results"]) >= MAX_TOOL_RESULTS:
                evidence["truncated"] = True
                continue
            truncated = len(content) > MAX_CONTENT_CHARS
            evidence["truncated"] |= truncated
            evidence["tool_results"].append({
                "evidence_ref": f"{job['case_id']}#trace:{span['span_id']}:tool:{identity[:12]}",
                "call_id": call_id, "name": name, "content": content[:MAX_CONTENT_CHARS],
                "span_id": span["span_id"], "call_span_id": call_span,
                "truncated": truncated, "association": "same_turn_response_call_id"})
        # 请求里已经存在、但未由本轮较早响应新产生的调用不属于本轮执行证据。
        historical.update(call_id for call_id, _ in _calls(request, request=True)
                          if isinstance(call_id, str) and call_id not in issued)
        if response.get("status") not in (None, "completed"):
            continue
        for call_id, name in _calls(response):
            if (isinstance(call_id, str) and call_id and isinstance(name, str) and name
                    and call_id not in historical):
                issued.setdefault(call_id, (name, span["span_id"]))
    evidence["tool_results"] = [result for result in evidence["tool_results"]
                                if result["call_id"] not in conflicting]
    evidence["diagnostics"] = sorted(diagnostics)
    evidence["diagnostic_counts"] = dict(diagnostics)
    if evidence["tool_results"]:
        complete = (trace["status"] == "complete"
                    and evidence["trace"]["call_count_status"] != "unknown"
                    and not evidence["truncated"] and not diagnostics)
        evidence["status"] = "available" if complete else "partial"
    else:
        evidence["diagnostics"].append("no_verified_current_turn_tool_results")
    return evidence


def _returns(request):
    for item in request.get("messages", []):
        if isinstance(item, dict) and item.get("role") == "tool":
            yield {"call_id": item.get("tool_call_id"), "content": item.get("content", "")}
    for item in request.get("input", []) if isinstance(request.get("input"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call_output", "mcp_call"} and not item.get("error"):
            yield {"call_id": item.get("call_id") or item.get("id"),
                   "content": item.get("output", "")}


def _calls(body, *, request=False):
    messages = body.get("messages", []) if request else [
        choice.get("message", {}) for choice in body.get("choices", []) if isinstance(choice, dict)]
    for message in messages:
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if isinstance(call, dict):
                yield call.get("id"), (call.get("function") or {}).get("name")
    items = body.get("input" if request else "output", [])
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("type") in {"function_call", "mcp_call"}:
            yield item.get("call_id") or item.get("id"), item.get("name")
