from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain.models import GoldenSession, GoldenShared
from .errors import CorpusValidationError, NotFoundError


@dataclass(frozen=True)
class _DatasetIndex:
    shared: GoldenShared
    cases_path: Path
    session_offsets: dict[str, int]


class JsonlCorpusStore:
    """Read immutable v0.4 datasets without retaining all cases in memory."""

    def __init__(self, dataset_directories: Mapping[str, Path]) -> None:
        self._datasets = {
            dataset_id: self._index_dataset(dataset_id, Path(directory))
            for dataset_id, directory in dataset_directories.items()
        }

    @classmethod
    def from_directory(cls, dataset_id: str, directory: Path) -> JsonlCorpusStore:
        return cls({dataset_id: directory})

    def get_shared(self, dataset_id: str) -> GoldenShared:
        return self._get_dataset(dataset_id).shared

    def get_session(self, dataset_id: str, session_id: str) -> GoldenSession:
        dataset = self._get_dataset(dataset_id)
        try:
            offset = dataset.session_offsets[session_id]
        except KeyError as exc:
            raise NotFoundError(session_id) from exc
        with dataset.cases_path.open("rb") as handle:
            handle.seek(offset)
            raw = self._decode_line(dataset.cases_path, handle.readline(), session_id=session_id)
        return GoldenSession.model_validate(raw)

    def iter_sessions(self, dataset_id: str) -> Iterator[GoldenSession]:
        dataset = self._get_dataset(dataset_id)
        with dataset.cases_path.open("rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = self._decode_line(dataset.cases_path, line, line_number=line_number)
                yield GoldenSession.model_validate(raw)

    def _get_dataset(self, dataset_id: str) -> _DatasetIndex:
        try:
            return self._datasets[dataset_id]
        except KeyError as exc:
            raise NotFoundError(dataset_id) from exc

    def _index_dataset(self, dataset_id: str, directory: Path) -> _DatasetIndex:
        shared_path = directory / "golden-dialogue-shared.json"
        cases_path = directory / "golden-dialogue-cases.jsonl"
        try:
            shared_raw = json.loads(shared_path.read_bytes())
            shared = GoldenShared.model_validate(shared_raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise CorpusValidationError(f"invalid shared file for {dataset_id}: {exc}") from exc

        offsets: dict[str, int] = {}
        try:
            with cases_path.open("rb") as handle:
                line_number = 0
                while True:
                    offset = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    line_number += 1
                    if not line.strip():
                        continue
                    raw = self._decode_line(cases_path, line, line_number=line_number)
                    try:
                        session = GoldenSession.model_validate(raw)
                        self._validate_shared_contract(shared, session)
                    except (ValidationError, CorpusValidationError) as exc:
                        raise CorpusValidationError(
                            f"invalid case {cases_path}:{line_number}: {exc}"
                        ) from exc
                    if session.session_id in offsets:
                        raise CorpusValidationError(
                            f"duplicate session_id {session.session_id} at "
                            f"{cases_path}:{line_number}"
                        )
                    offsets[session.session_id] = offset
        except OSError as exc:
            raise CorpusValidationError(f"cannot read cases file for {dataset_id}: {exc}") from exc
        if not offsets:
            raise CorpusValidationError(f"cases file for {dataset_id} is empty")
        return _DatasetIndex(shared=shared, cases_path=cases_path, session_offsets=offsets)

    @staticmethod
    def _decode_line(
        path: Path,
        line: bytes,
        *,
        line_number: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        location = str(line_number) if line_number is not None else f"session {session_id}"
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorpusValidationError(f"invalid JSON at {path}:{location}: {exc}") from exc
        if not isinstance(value, dict):
            raise CorpusValidationError(f"case at {path}:{location} must be a JSON object")
        return value

    @staticmethod
    def _validate_shared_contract(shared: GoldenShared, session: GoldenSession) -> None:
        profile_keys = {field.key for field in shared.context.profile_schema}
        snapshots = [
            ("input.memory_before", session.input.memory_before.profile_snapshot),
            *[
                (f"{turn.turn_id}.expected", turn.expected.profile_snapshot)
                for turn in session.turns
            ],
            ("expected.memory_after", session.expected.memory_after.profile_snapshot),
        ]
        for label, snapshot in snapshots:
            if set(snapshot) != profile_keys:
                missing = sorted(profile_keys - set(snapshot))
                extra = sorted(set(snapshot) - profile_keys)
                raise CorpusValidationError(
                    f"{session.session_id} {label} profile keys mismatch; "
                    f"missing={missing}, extra={extra}"
                )

        tool_names = {tool.name for tool in shared.context.available_tools}
        for turn in session.turns:
            unknown_profile = set(turn.expected.profile_update) - profile_keys
            if unknown_profile:
                raise CorpusValidationError(
                    f"{turn.turn_id} profile_update has unknown fields: "
                    f"{sorted(unknown_profile)}"
                )
            unknown_tools = {call.name for call in turn.expected.tool_calls} - tool_names
            if unknown_tools:
                raise CorpusValidationError(
                    f"{turn.turn_id} tool_calls has unknown tools: {sorted(unknown_tools)}"
                )

        stages = shared.context.sop_stages
        if stages is None:
            if any(turn.expected.state is not None for turn in session.turns):
                raise CorpusValidationError(
                    f"{session.session_id} defines turn state without context.sop_stages"
                )
            if session.input.memory_before.state_snapshot:
                raise CorpusValidationError(
                    f"{session.session_id} defines memory_before state without context.sop_stages"
                )
            if session.expected.memory_after.state_snapshot:
                raise CorpusValidationError(
                    f"{session.session_id} defines memory_after state without context.sop_stages"
                )
            return

        stage_ids = {stage.id for stage in stages}
        for turn in session.turns:
            state = turn.expected.state
            if state is None:
                raise CorpusValidationError(
                    f"{turn.turn_id} must define state when context.sop_stages exists"
                )
            if state.value not in stage_ids or (
                state.prev is not None and state.prev not in stage_ids
            ):
                raise CorpusValidationError(f"{turn.turn_id} references an unknown SOP stage")
