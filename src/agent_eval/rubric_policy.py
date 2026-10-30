"""评分前独立起草业务标准；未确认参考和候选回答不进入起草工单。"""

from datetime import UTC, datetime

from golden_eval.application.rubric_model import RubricBuildError, generate_one
from golden_eval.domain.models import ConversationMessage, GoldenSession, PromptVersion
from golden_eval.integrations.structured_model import (
    OpenAICompatibleStructuredModel,
    StructuredModelConfig,
    StructuredModelError,
)

from . import gateway, workspace_policy
from .models import digest


def prepare(job, store, settings, manifest):
    cached = manifest.get("judge_rubrics", {}).get(job["case_id"])
    if cached:
        return cached
    shared = workspace_policy.judging_shared(job["input"]["shared"], manifest["workspace_policy"])
    session = GoldenSession.model_validate(job["input"]["item"])
    turn = next(turn for turn in session.turns if turn.turn_id == job["case_id"])
    confirmed = shared.context.reference_status == "confirmed"
    basis = {"source": "confirmed_reference" if confirmed else "workspace_rules",
        "rules_sha256": manifest["workspace_policy"]["config_sha256"],
        "generated_at": datetime.now(UTC).isoformat()}
    if confirmed:
        rubric = turn.rubric
    else:
        from .engines import ExecutionError, frozen_judge

        prompt = PromptVersion.model_validate(manifest["rubric_prompt"])
        config = frozen_judge(manifest)
        model = OpenAICompatibleStructuredModel(StructuredModelConfig(provider="gateway",
            model=config["model"], base_url=settings.model_gateway_url,
            api_key=gateway.api_key(settings), timeout_seconds=config["timeout_seconds"],
            max_retries=0, extra_body=gateway.judge_extras(config["model"])))
        # 直接使用原执行的请求历史，兼容原数据未记录 history 的旧会话。
        messages = (job["input"]["observation"].get("request_messages")
                    or job["input"]["turn"]["messages"])
        history = [ConversationMessage.model_validate(message)
                   for message in messages[:len(messages) - len(turn.input.customer_messages)]]
        reference_free = turn.model_copy(update={"history_length": len(history),
            "expected": turn.expected.model_copy(update={
                "messages": [], "profile_update": {}, "profile_snapshot": {}, "tool_calls": []})})
        before = job["input"]["observation"].get("profile_observed_before")
        if before is None:
            before = job["input"]["turn"]["profile_before"]
        memory = session.input.memory_before.model_copy(update={"profile_snapshot": before})
        isolated = session.model_copy(update={"history": history, "turns": [reference_free],
            "input": session.input.model_copy(update={"memory_before": memory})})
        for _ in range(config["format_attempts"]):
            try:
                generated = generate_one(session=isolated, shared=shared,
                    draft_id=job["experiment_id"], revision=0, prompt=prompt, model=model)
                rubric = "\n".join(
                    "【画像】依据本轮客户事实、当前空间字段说明和技能规则检查更新；未确认参考不构成必写要求。"
                    if line.startswith("【画像】") else line
                    for line in generated.turns[0].rubric.splitlines())
                break
            except (RubricBuildError, StructuredModelError):
                continue
        else:
            raise ExecutionError("judge_rubric_generation_failed")
        basis.update(prompt_version=prompt.version_label, prompt_sha256=prompt.content_sha256)
    basis.update(rubric=rubric, rubric_sha256=digest({"rubric": rubric}))
    return store.freeze_turn_rubric(job, basis)
