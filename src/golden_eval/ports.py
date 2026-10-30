from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from .domain.corpus import (
    CollectionPolicy,
    CorpusSnapshot,
    SessionCandidate,
    SessionHistory,
    SessionOutcome,
    SnapshotSummary,
    ToolDefinition,
    WorkspaceConfiguration,
)
from .domain.models import (
    ConnectionConfig,
    ConnectionSummary,
    ConversationMessage,
    ConversationReply,
    ConversationRequest,
    ConversationResponse,
    CreateGoldenDraftRequest,
    DatasetSummary,
    DraftModelRun,
    DraftValidationResult,
    GoldenDraft,
    GoldenDraftSummary,
    GoldenSession,
    GoldenShared,
    ProfileEnrichmentRecord,
    RubricDraftRecord,
    RunEvent,
    RunIdentity,
    RunStatus,
    RunSummary,
    TaskRecord,
    TurnObservation,
    TurnTask,
)


class ConnectionRegistry(Protocol):
    def list(self) -> list[ConnectionSummary]: ...

    def get(self, connection_id: str) -> ConnectionConfig: ...

    def verify(self, connection_id: str) -> ConnectionSummary: ...


class CorpusStore(Protocol):
    def get_shared(self, dataset_id: str) -> GoldenShared: ...

    def get_session(self, dataset_id: str, session_id: str) -> GoldenSession: ...

    def iter_sessions(self, dataset_id: str) -> Iterator[GoldenSession]: ...


class CorpusSourcePort(Protocol):
    """Read-only view of the online source Workspace.

    Implementations must never create, update or delete anything on the source side.
    """

    def list_workspace_ids(self) -> list[str]: ...

    def iter_candidates(
        self, workspace_id: str, policy: CollectionPolicy
    ) -> Iterator[SessionCandidate]:
        """Yield candidates newest-first, lazily: the caller decides when enough were seen."""
        ...

    def hydrate(self, candidate: SessionCandidate) -> SessionHistory: ...

    def current_configuration(self, workspace_id: str) -> WorkspaceConfiguration: ...

    def available_tools(self, workspace_id: str) -> list[ToolDefinition]: ...


class SnapshotStore(Protocol):
    def delete(self, snapshot_id: str) -> dict[str, int]: ...

    def save(self, snapshot: CorpusSnapshot) -> SnapshotSummary: ...

    def rename(self, snapshot_id: str, name: str) -> SnapshotSummary: ...

    def get(self, snapshot_id: str) -> CorpusSnapshot: ...

    def get_summary(self, snapshot_id: str) -> SnapshotSummary: ...

    def list(self) -> list[SnapshotSummary]: ...

    def list_sessions(self, snapshot_id: str) -> list[SessionOutcome]: ...


class GoldenDraftStore(Protocol):
    def create(self, request: CreateGoldenDraftRequest) -> GoldenDraft: ...

    def rename(
        self, draft_id: str, *, name: str, domain: str | None, expected_revision: int
    ) -> GoldenDraft: ...

    def set_evaluation_notes(
        self, draft_id: str, *, notes: str, expected_revision: int
    ) -> GoldenDraft: ...

    def delete(self, draft_id: str) -> dict[str, int]: ...

    def list_referencing_snapshot(self, snapshot_id: str) -> list[str]: ...

    def list_drafts(self) -> list[GoldenDraftSummary]: ...

    def get(self, draft_id: str) -> GoldenDraft: ...

    def update_session(
        self,
        draft_id: str,
        session: GoldenSession,
        *,
        expected_revision: int,
        enrichment: ProfileEnrichmentRecord | None = None,
        rubric_draft: RubricDraftRecord | None = None,
    ) -> GoldenDraft: ...

    def validate(self, draft_id: str) -> DraftValidationResult: ...

    def seed_provenance(
        self,
        draft_id: str,
        *,
        enrichment: list[ProfileEnrichmentRecord],
        rubric_drafts: list[RubricDraftRecord],
    ) -> GoldenDraft: ...

    def publish(self, draft_id: str, *, expected_revision: int) -> DatasetSummary: ...

    def get_dataset(self, dataset_version: str) -> DatasetSummary: ...

    def list_datasets(self) -> list[DatasetSummary]: ...

    def create_model_run(
        self, draft_id: str, kind: str, *, prompt_version: str, prompt_sha256: str
    ) -> DraftModelRun: ...

    def get_model_run(self, model_run_id: str) -> DraftModelRun: ...

    def list_model_runs(self, *, draft_id: str | None = None) -> list[DraftModelRun]: ...

    def update_model_run(
        self,
        model_run_id: str,
        *,
        status: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        result_json: str | None = None,
        error: str | None = None,
    ) -> DraftModelRun: ...


class ConversationPort(Protocol):
    async def send(self, request: ConversationRequest) -> ConversationResponse: ...


class TargetContactPort(Protocol):
    """Target-side execution: isolated contact, routing, conversation, profile readback.

    Implementations must never read online source data. Either the whole lifecycle succeeds or the
    caller records a partial observation with the stage it reached.
    """

    target: str

    def create_contact(self, *, account_id: str, profile: dict) -> str | None: ...

    def update_routing(
        self, *, visitor_id: str | None, account_id: str, bot_user_id: str, im_bot_id: str
    ) -> None: ...

    def read_contact(self, *, account_id: str, timeout_seconds: float | None = None) -> dict: ...

    def send_messages(
        self, *, account_id: str, messages: list[ConversationMessage],
        request_id: str | None = None,
    ) -> ConversationReply: ...


class ObservationStore(Protocol):
    def save(self, observation: TurnObservation) -> TurnObservation: ...

    def delete(self, observation_id: str) -> None: ...

    def get(self, observation_id: str) -> TurnObservation: ...

    def list_for_run(self, run_id: str) -> list[TurnObservation]: ...

    def count_for_run(self, run_id: str) -> int: ...


class TurnTaskSource(Protocol):
    """Resolve the TurnTask a conversation task must execute."""

    def turn_task_for(
        self, run: RunSummary, task_key: str, *, account_prefix: str
    ) -> TurnTask | None: ...


class RunStore(Protocol):
    def create(
        self,
        request: object,
        idempotency_key: str,
        *,
        identity: RunIdentity | None = None,
        source_label: str | None = None,
    ) -> RunSummary: ...

    def list(self) -> list[RunSummary]: ...

    def get(self, run_id: str) -> RunSummary: ...

    def set_status(
        self, run_id: str, status: RunStatus, *, error_code: str | None = None
    ) -> RunSummary: ...

    def rename(self, run_id: str, name: str) -> RunSummary: ...

    def set_concurrency(self, run_id: str, max_concurrency: int) -> RunSummary: ...

    def cancel(self, run_id: str) -> RunSummary: ...

    def reopen(self, run_id: str) -> RunSummary: ...

    def delete(self, run_id: str) -> dict[str, int]: ...

    def events(self, run_id: str, *, after_seq: int = 0) -> list[RunEvent]: ...

    def append_event(self, run_id: str, event_type: str, payload: dict) -> RunEvent: ...


class TaskQueue(Protocol):
    def enqueue(self, run_id: str, task_key: str, stage: str) -> TaskRecord: ...

    def list_tasks(self, run_id: str) -> list[TaskRecord]: ...

    def lease_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        stages: set[str] | None = None,
    ) -> TaskRecord | None: ...

    def start(self, task_id: str, worker_id: str) -> TaskRecord: ...

    def renew(self, task_id: str, worker_id: str, *, lease_seconds: int = 60) -> TaskRecord: ...

    def complete(self, task_id: str, worker_id: str) -> TaskRecord: ...

    def fail(self, task_id: str, worker_id: str, error_code: str) -> TaskRecord: ...

    def retry(self, task_id: str) -> TaskRecord: ...

    def recover_expired(self, *, now: datetime | None = None) -> int: ...
