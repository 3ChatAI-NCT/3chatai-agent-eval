from __future__ import annotations

import re
from pathlib import Path

import pytest

from golden_eval.adapters.errors import InvalidTransitionError
from golden_eval.adapters.sqlite_prompts import SqlitePromptStore
from golden_eval.application.prompt_registry import (
    PromptRegistry,
    extract_system_prompt,
    load_builtin_prompts,
)
from golden_eval.domain.models import PromptKind, PromptSource

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_ROOT = REPO_ROOT / "prompts"


def build_registry(tmp_path) -> tuple[PromptRegistry, SqlitePromptStore]:
    store = SqlitePromptStore(tmp_path / "prompts.sqlite")
    return PromptRegistry(store, prompts_root=PROMPTS_ROOT), store


PROMPT_SECTIONS = (
    "## 1. 目的",
    "## 2. 输入",
    "## 3. 输出",
    "## 4. 输出 Schema",
    "## 5. 规则",
    "## 6. 判断口径",
    "## 7. 校验与失败处理",
    "## 8. 版本与变更",
)


def test_every_builtin_prompt_follows_the_template() -> None:
    for kind, builtin in load_builtin_prompts(PROMPTS_ROOT).items():
        body = builtin.body
        for section in PROMPT_SECTIONS:
            assert section in body, f"{kind} body is missing {section}"
        # The file tail (change log, links) must not leak into the prompt body.
        assert "## 变更记录" not in body
        assert "## 相关文档" not in body


def test_extracts_system_prompt_from_both_contract_shapes() -> None:
    judge_document = (PROMPTS_ROOT / "judge" / "full-evaluation-v14.md").read_text(
        encoding="utf-8"
    )
    profile_document = (
        PROMPTS_ROOT / "profile" / "profile-enrichment-v6.md"
    ).read_text(encoding="utf-8")

    judge_body = extract_system_prompt(judge_document)
    profile_body = extract_system_prompt(profile_document)

    assert judge_body.startswith("## 1. 目的")
    assert "2、3、4、5、6、8、9" in judge_body
    assert "compiled_rubric.must_cover" in judge_body
    assert profile_body.startswith("## 1. 目的")
    assert "profile_snapshot" in profile_body
    # The contract tail must not leak into the extracted prompt body.
    assert "校准用例" not in judge_body
    assert "## 变更记录" not in profile_body


JUDGE_ANCHORS = (
    "2、3、4、5、6、8、9",
    "[2,3,4,5,6,8,9]",
    "compiled_rubric.must_cover",
    "compiled_rubric.forbidden",
    "compiled_rubric.guidance.should_lead",
    "followup_allowed=false",
    "turn_pass",
    "session_status",
    "denominator=pass_count+fail_count",
    "pass^j(s)=C(c_s,j)/C(K,j)",
    "teacher_forcing",
    "not_applicable",
    "evaluation_error",
    "quality_warnings",
    "tool_diagnostics",
    "evidence_refs",
)


def test_judge_body_keeps_every_scoring_anchor() -> None:
    """Restructuring the prompt must not drop a scoring rule."""

    document = (PROMPTS_ROOT / "judge" / "full-evaluation-v14.md").read_text(encoding="utf-8")
    body = re.sub(r"\s+", "", extract_system_prompt(document))

    missing = [anchor for anchor in JUDGE_ANCHORS if anchor not in body]
    assert missing == []


def test_builtin_versions_carry_body_and_file_hashes() -> None:
    builtins = load_builtin_prompts(PROMPTS_ROOT)

    assert set(builtins) == {
        PromptKind.JUDGE,
        PromptKind.PROFILE_ENRICHMENT,
        PromptKind.RUBRIC_DRAFT,
    }
    judge = builtins[PromptKind.JUDGE]
    assert judge.version_label == "full-evaluation-v14"
    assert judge.body_sha256 != judge.file_sha256
    assert len(judge.body_sha256) == 64
    # Extraction is deterministic: a second load yields the same body hash.
    assert load_builtin_prompts(PROMPTS_ROOT)[PromptKind.JUDGE].body_sha256 == judge.body_sha256


def test_saving_creates_a_new_immutable_version(tmp_path) -> None:
    registry, store = build_registry(tmp_path)

    saved = registry.save(
        PromptKind.JUDGE,
        content="  你是新的评分引擎。  ",
        version_label=None,
        notes="第一次调整",
        activate=True,
    )

    assert saved.version_label == "edit-1"
    assert saved.source is PromptSource.EDITED
    assert saved.content == "你是新的评分引擎。"
    assert saved.is_active is True
    assert saved.notes == "第一次调整"

    versions = {item.version_label for item in registry.list_versions(PromptKind.JUDGE)}
    assert versions == {"full-evaluation-v14", "edit-1"}

    with pytest.raises(InvalidTransitionError):
        registry.save(
            PromptKind.JUDGE,
            content="另一种写法",
            version_label="edit-1",
            notes=None,
            activate=False,
        )

    # The built-in baseline is untouched and still readable.
    builtin = registry.version(PromptKind.JUDGE, "full-evaluation-v14")
    assert builtin.source is PromptSource.BUILTIN
    assert builtin.content.startswith("## 1. 目的")
    store.close()


def test_active_version_can_switch_between_edited_and_builtin(tmp_path) -> None:
    registry, store = build_registry(tmp_path)
    registry.save(
        PromptKind.PROFILE_ENRICHMENT,
        content="你是新的画像标注器。",
        version_label=None,
        notes=None,
        activate=True,
    )
    assert registry.active(PromptKind.PROFILE_ENRICHMENT).version_label == "edit-1"

    reverted = registry.activate(PromptKind.PROFILE_ENRICHMENT, "profile-enrichment-v6")

    assert reverted.version_label == "profile-enrichment-v6"
    assert reverted.is_active is True
    assert registry.active(PromptKind.PROFILE_ENRICHMENT).source is PromptSource.BUILTIN
    store.close()


def test_edited_version_cannot_reuse_builtin_label_and_blank_is_rejected(tmp_path) -> None:
    registry, store = build_registry(tmp_path)

    with pytest.raises(ValueError):
        registry.save(
            PromptKind.JUDGE,
            content="内容",
            version_label="full-evaluation-v14",
            notes=None,
            activate=True,
        )
    with pytest.raises(ValueError):
        registry.save(
            PromptKind.JUDGE,
            content="   ",
            version_label=None,
            notes=None,
            activate=True,
        )
    store.close()


def test_versions_survive_reopen(tmp_path) -> None:
    database = tmp_path / "prompts.sqlite"
    first = SqlitePromptStore(database)
    PromptRegistry(first, prompts_root=PROMPTS_ROOT).save(
        PromptKind.JUDGE,
        content="持久化内容",
        version_label="edit-9",
        notes=None,
        activate=True,
    )
    first.close()

    reopened = SqlitePromptStore(database)
    registry = PromptRegistry(reopened, prompts_root=PROMPTS_ROOT)

    assert registry.active(PromptKind.JUDGE).version_label == "edit-9"
    assert registry.active(PromptKind.JUDGE).content == "持久化内容"
    reopened.close()
