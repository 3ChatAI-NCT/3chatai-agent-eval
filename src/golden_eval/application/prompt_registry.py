"""Prompt registry: built-in baseline plus edited versions.

The editable unit is the **system prompt body** that is actually sent to the model, not the whole
contract document. The built-in body is extracted from the versioned markdown file so the repository
stays the source of truth, and the full-file hash is kept as provenance.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..adapters.sqlite_prompts import SqlitePromptStore
from ..domain.models import (
    PromptKind,
    PromptSource,
    PromptVersion,
    PromptVersionSummary,
)

_FENCE_RE = re.compile(r"```text\n(?P<body>.*?)\n```", re.DOTALL)
_HEADING_RE = re.compile(r"^##+\s", re.MULTILINE)


@dataclass(frozen=True)
class BuiltinPrompt:
    kind: PromptKind
    version_label: str
    path: Path
    body: str
    file_sha256: str
    body_sha256: str

    def summary(self, *, is_active: bool) -> PromptVersionSummary:
        return PromptVersionSummary(
            prompt_kind=self.kind,
            version_label=self.version_label,
            source=PromptSource.BUILTIN,
            content_sha256=self.body_sha256,
            source_file_sha256=self.file_sha256,
            notes=None,
            is_active=is_active,
            char_count=len(self.body),
            created_at=_fallback_timestamp(self.path),
        )

    def version(self, *, is_active: bool) -> PromptVersion:
        return PromptVersion(**self.summary(is_active=is_active).model_dump(), content=self.body)


def _fallback_timestamp(path: Path) -> datetime:
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return datetime(1970, 1, 1, tzinfo=UTC)


def extract_system_prompt(document: str) -> str:
    """Take the canonical system prompt body from a contract document.

    The body is the first fenced `text` block at or after the "System Prompt" heading, so the body
    itself may use Markdown headings. Without a fenced block the section runs to the next heading;
    without the heading at all, the document's first fenced `text` block wins (older judge files).
    """

    lines = document.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("#") and "System Prompt" in line:
            start = index + 1
            break
    if start is not None:
        remainder = "\n".join(lines[start:])
        fenced = _FENCE_RE.search(remainder)
        if fenced is not None:
            body = fenced.group("body").strip()
            if body:
                return body
        for index in range(start, len(lines)):
            if _HEADING_RE.match(lines[index]):
                return "\n".join(lines[start:index]).strip()
        body = "\n".join(lines[start:]).strip()
        if body:
            return body
    fenced = _FENCE_RE.search(document)
    if fenced is not None:
        body = fenced.group("body").strip()
        if body:
            return body
    raise ValueError("document does not declare a System Prompt section")


def load_builtin_prompts(root: Path) -> dict[PromptKind, BuiltinPrompt]:
    sources = {
        PromptKind.JUDGE: ("full-evaluation-v14", root / "judge" / "full-evaluation-v14.md"),
        PromptKind.PROFILE_ENRICHMENT: (
            "profile-enrichment-v6",
            root / "profile" / "profile-enrichment-v6.md",
        ),
        PromptKind.RUBRIC_DRAFT: (
            "rubric-draft-v11",
            root / "rubric" / "rubric-draft-v11.md",
        ),
    }
    prompts: dict[PromptKind, BuiltinPrompt] = {}
    for kind, (label, path) in sources.items():
        document = path.read_text(encoding="utf-8")
        body = extract_system_prompt(document)
        prompts[kind] = BuiltinPrompt(
            kind=kind,
            version_label=label,
            path=path,
            body=body,
            file_sha256=hashlib.sha256(document.encode("utf-8")).hexdigest(),
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
    return prompts


class PromptRegistry:
    """Resolve built-in and edited versions behind one small interface."""

    def __init__(self, store: SqlitePromptStore, *, prompts_root: Path) -> None:
        self._store = store
        self._builtins = load_builtin_prompts(prompts_root)

    def list_versions(self, kind: PromptKind) -> list[PromptVersionSummary]:
        active_label = self.active_version_label(kind)
        builtin = self._builtins[kind]
        edited = self._store.list_versions(kind)
        return [
            *edited,
            builtin.summary(is_active=active_label == builtin.version_label),
        ]

    def active_version_label(self, kind: PromptKind) -> str:
        edited = self._store.active_version_label(kind)
        if edited is None:
            return self._builtins[kind].version_label
        return edited

    def active(self, kind: PromptKind) -> PromptVersion:
        label = self.active_version_label(kind)
        return self.version(kind, label)

    def version(self, kind: PromptKind, version_label: str) -> PromptVersion:
        builtin = self._builtins[kind]
        if version_label == builtin.version_label:
            return builtin.version(is_active=self._store.active_version_label(kind) is None)
        return self._store.get_version(kind, version_label)

    def save(
        self,
        kind: PromptKind,
        *,
        content: str,
        version_label: str | None,
        notes: str | None,
        activate: bool,
    ) -> PromptVersion:
        label = version_label or self._store.next_version_label(kind)
        if label == self._builtins[kind].version_label:
            raise ValueError("edited versions cannot reuse the built-in version label")
        body = content.strip()
        if not body:
            raise ValueError("prompt content must not be blank")
        return self._store.save_version(
            kind,
            version_label=label,
            content=body,
            content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            notes=notes,
            activate=activate,
        )

    def activate(self, kind: PromptKind, version_label: str) -> PromptVersion:
        builtin = self._builtins[kind]
        if version_label == builtin.version_label:
            self._store.clear_active(kind)
            return builtin.version(is_active=True)
        return self._store.activate(kind, version_label)

    def provenance(self, kind: PromptKind) -> dict[str, str]:
        version = self.active(kind)
        return {
            "prompt_version": version.version_label,
            "prompt_sha256": version.content_sha256,
            "prompt_source": version.source.value,
        }
