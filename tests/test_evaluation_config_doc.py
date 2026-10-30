"""Keep the human-readable evaluation reference in sync with the code catalog."""

from __future__ import annotations

from pathlib import Path

from golden_eval.domain.evaluation import (
    AGGREGATION_RULES,
    METRIC_CATALOG,
    RUBRIC_SECTION_CATALOG,
    MetricMode,
    metric_display_index,
    output_metric_ids,
    required_metric_ids,
    rubric_metric_ids,
    rubric_required_sections,
    tool_diagnostic_metric_id,
    turn_gate_metric_ids,
)

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "EVALUATION_CONFIG_REFERENCE.md"
PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"


def test_doc_covers_every_catalog_metric() -> None:
    document = DOC_PATH.read_text(encoding="utf-8")

    for metric in METRIC_CATALOG:
        assert metric.metric_id in document, f"{metric.metric_id} missing from reference doc"
        assert metric.name in document, f"{metric.name} missing from reference doc"


def test_display_index_numbers_output_metrics_from_one() -> None:
    """界面编号是 1..N 的顺序号；历史编号（1.1/4.1/6.2…）仍然留在契约里。"""

    output = output_metric_ids()

    assert output[0] == "1"
    assert metric_display_index("1") == 1
    assert metric_display_index("2") == 2
    assert metric_display_index("5") == 5
    assert metric_display_index(tool_diagnostic_metric_id()) == 7
    assert metric_display_index("9") == len(output)
    # 本版不输出的历史维度没有顺序号，只保留原编号。
    assert metric_display_index("8.1") is None
    assert metric_display_index("2.1") is None




def test_doc_states_the_scoring_scope_and_gate() -> None:
    document = DOC_PATH.read_text(encoding="utf-8")

    for metric_id in required_metric_ids():
        assert metric_id in document
    assert " / ".join(required_metric_ids()) in document
    assert " / ".join(turn_gate_metric_ids()) in document
    assert tool_diagnostic_metric_id() in document
    assert "dense-pass-fail-v1" in document
    assert "full-evaluation-v8" in document


def test_doc_covers_every_aggregation_rule_key() -> None:
    document = DOC_PATH.read_text(encoding="utf-8")

    for rule in AGGREGATION_RULES:
        assert rule.key in document, f"aggregation rule {rule.key} missing from reference doc"


def test_doc_marks_non_output_metrics_as_such() -> None:
    document = DOC_PATH.read_text(encoding="utf-8")
    not_output = [
        metric.metric_id
        for metric in METRIC_CATALOG
        if metric.mode in {MetricMode.NOT_OUTPUT, MetricMode.DISABLED}
    ]

    assert not_output, "catalog should keep some metrics explicitly out of scope"
    for metric_id in not_output:
        assert metric_id in document


def test_doc_covers_every_rubric_section() -> None:
    document = DOC_PATH.read_text(encoding="utf-8")

    for spec in RUBRIC_SECTION_CATALOG:
        assert f"【{spec.section}】" in document, f"{spec.section} missing from reference doc"
        assert spec.purpose in document, f"{spec.section} purpose missing from reference doc"
    for section in rubric_required_sections("turn"):
        assert f"【{section}】" in document


def test_rubric_sections_only_reference_catalog_metrics() -> None:
    known = {metric.metric_id for metric in METRIC_CATALOG}

    assert set(rubric_metric_ids()) <= known
    # 5.2 / 5.3 / 7.1 / 7.2 are the scored criteria a Turn rubric feeds; 6.2 stays diagnostic.
    assert {"5", "6", "8", "9", "2", "3", "7"} <= set(rubric_metric_ids())
