"""Draft AI assistance: frozen prompts, validated profile and rubric, human review afterward."""

from pathlib import Path

from golden_eval.adapters.jsonl_corpus import JsonlCorpusStore
from golden_eval.application.profile_enrichment import ProfileEnrichmentError
from golden_eval.application.profile_model import generate_one as enrich_profile
from golden_eval.application.prompt_registry import load_builtin_prompts
from golden_eval.application.rubric_model import generate_one as draft_rubric
from golden_eval.domain.models import GoldenSession, GoldenShared, PromptKind, PromptVersion
from golden_eval.integrations.structured_model import (
    OpenAICompatibleStructuredModel,
    StructuredModelConfig,
)

from . import gateway
from .models import digest
from .privacy import safe_view
from .store import Conflict, NotFound


def frozen_prompts():
    prompts = load_builtin_prompts(Path(__file__).resolve().parents[2] / "prompts")
    return (prompts[PromptKind.PROFILE_ENRICHMENT].version(is_active=True).model_dump(mode="json"),
            prompts[PromptKind.RUBRIC_DRAFT].version(is_active=True).model_dump(mode="json"))


class AnnotationError(ValueError):
    """只保留阶段与安全校验详情，不保存模型正文。"""

    def __init__(self, stage, cause):
        self.code = getattr(cause, "code", stage + "_validation_failed")
        self.detail = {"stage": stage, "error_type": type(cause).__name__,
                       "message": (safe_view(str(cause))[:1500]
                                   if isinstance(cause, ProfileEnrichmentError)
                                   else "模型调用或结果校验失败，请核对该阶段的输出合同")}
        super().__init__(self.detail["message"])


def generate(job, store, settings):
    draft = store.draft(job["draft_id"])
    if draft["status"] != "draft":
        raise Conflict("draft_published")
    item = next((item for item in draft["content"]["items"]
                 if item["session_id"] == job["session_id"]), None)
    if item is None:
        raise NotFound("case_removed")
    shared_data = draft["content"]["shared"]
    if digest(item) != job["case_sha256"] or digest(shared_data) != job["shared_sha256"]:
        raise Conflict("draft_changed")
    shared = GoldenShared.model_validate(shared_data)
    session = GoldenSession.model_validate(item)
    model = OpenAICompatibleStructuredModel(StructuredModelConfig(
        provider="gateway", model=job["model"], base_url=settings.model_gateway_url,
        api_key=gateway.api_key(settings), max_retries=0,
        extra_body=gateway.judge_extras(job["model"]),
    ))
    try:
        profile = enrich_profile(session=session, shared=shared, draft_id=job["draft_id"],
                                 revision=draft["revision"],
                                 prompt=PromptVersion.model_validate(job["profile_prompt"]),
                                 model=model)
    except Exception as exc:
        raise AnnotationError("profile", exc) from exc
    try:
        rubric = draft_rubric(session=profile, shared=shared, draft_id=job["draft_id"],
                              revision=draft["revision"],
                              prompt=PromptVersion.model_validate(job["rubric_prompt"]),
                              model=model)
        JsonlCorpusStore._validate_shared_contract(shared, rubric)
    except Exception as exc:
        raise AnnotationError("rubric", exc) from exc
    return rubric.model_dump(mode="json")
