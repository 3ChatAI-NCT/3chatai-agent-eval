"""One-click profile enrichment: batch generate, skip what is done, isolate failures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_jsonl_corpus import session_payload

from golden_eval.application.profile_enrichment import ProfileEnrichmentError
from golden_eval.application.profile_model import _normalise_results, plan_sessions
from golden_eval.domain.models import (
    GoldenSession,
    ProfileEnrichmentRecord,
)


def _results(session: GoldenSession) -> dict:
    """One entry per turn: the fixture's own profile deltas, written to the contract."""

    return {
        "results": [
            {
                "turn_id": turn.turn_id,
                "profile_update": {
                    key: update.model_dump(mode="json", exclude_none=True)
                    for key, update in turn.expected.profile_update.items()
                },
                "profile_snapshot": dict(turn.expected.profile_snapshot),
                "evidence_refs": [f"{turn.turn_id}#customer:1"],
            }
            for turn in session.turns
        ]
    }




def test_plan_sessions_skips_already_enriched_unless_forced() -> None:
    session = GoldenSession.model_validate(session_payload())
    record = ProfileEnrichmentRecord(
        session_id=session.session_id,
        fingerprint="f" * 64,
        prompt_kind="profile_enrichment",
        prompt_version="profile-enrichment-v2",
        prompt_sha256="a" * 64,
        applied_at=datetime.now(UTC),
    )

    targets, skipped, deferred = plan_sessions(
        sessions=[session],
        records=[record],
        requested=None,
        force=False,
        prompt_version="profile-enrichment-v2",
    )
    assert targets == [] and skipped == [session.session_id] and deferred == []

    forced, _, _ = plan_sessions(
        sessions=[session],
        records=[record],
        requested=None,
        force=True,
        prompt_version="profile-enrichment-v2",
    )
    assert [item.session_id for item in forced] == [session.session_id]

    # A new prompt version invalidates the old bookkeeping, so the work returns by default.
    rebound, skipped_again, _ = plan_sessions(
        sessions=[session],
        records=[record],
        requested=None,
        force=False,
        prompt_version="profile-enrichment-v4",
    )
    assert [item.session_id for item in rebound] == [session.session_id]
    assert skipped_again == []


def test_plan_sessions_defers_beyond_the_batch_limit() -> None:
    sessions = [
        GoldenSession.model_validate(session_payload("GD-FIXTURE-0001")),
        GoldenSession.model_validate(session_payload("GD-FIXTURE-0002")),
    ]

    targets, skipped, deferred = plan_sessions(
        sessions=sessions,
        records=[],
        requested=None,
        force=False,
        prompt_version="profile-enrichment-v4",
        max_sessions=1,
    )
    assert [item.session_id for item in targets] == ["GD-FIXTURE-0001"]
    assert skipped == []
    assert deferred == ["GD-FIXTURE-0002"]








def test_results_are_matched_by_worklist_order_and_turn_id_is_filled_in() -> None:
    items = [{"turn_id": "S#T1"}, {"turn_id": "S#T2"}]

    assert [entry["turn_id"] for entry in _normalise_results([{}, {}], items)] == [
        "S#T1",
        "S#T2",
    ]
    assert [
        entry["turn_id"] for entry in _normalise_results({"results": [{}, {}]}, items)
    ] == ["S#T1", "S#T2"]
    # A single-turn call may come back as a bare object.
    assert [
        entry["turn_id"] for entry in _normalise_results({"profile_update": {}}, items[:1])
    ] == ["S#T1"]
    # Sometimes it comes back keyed by turn id, sometimes with short keys.
    assert [
        entry["turn_id"]
        for entry in _normalise_results({"S#T1": {"profile_update": {}}, "S#T2": {}}, items)
    ] == ["S#T1", "S#T2"]
    assert [
        entry["turn_id"]
        for entry in _normalise_results({"T1": {"profile_update": {}}, "T2": {}}, items)
    ] == ["S#T1", "S#T2"]

    with pytest.raises(ProfileEnrichmentError):
        _normalise_results([{}], items)  # never guess when the counts disagree
    with pytest.raises(ProfileEnrichmentError):
        _normalise_results({"results": [{"turn_id": "S#T9"}, {}]}, items)
    with pytest.raises(ProfileEnrichmentError):
        _normalise_results({"note": "no idea"}, items)
