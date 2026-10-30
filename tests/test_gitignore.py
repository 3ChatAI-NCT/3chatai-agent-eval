import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def repository(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git is required to verify repository ignore rules")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = Path(__file__).resolve().parents[1] / ".gitignore"
    shutil.copyfile(source, tmp_path / ".gitignore")
    (tmp_path / "empty-global-ignore").write_text("")
    return tmp_path


def ignored(repository, path):
    return subprocess.run(
        ["git", "-C", str(repository), "-c",
         f"core.excludesFile={repository / 'empty-global-ignore'}",
         "check-ignore", "--no-index", "--quiet", "--", path],
        check=False,
    ).returncode == 0


@pytest.mark.parametrize("path", [
    ".env", "docs/.env.production", "docs/trace.log", "docs/local.sqlite",
    "docs/data/conversation.json", "docs/.local/objects/snapshot.json",
    "prompts/.env", "src/atomic_eval/prompts/.env", "docs/service.key",
])
def test_local_and_sensitive_artifacts_stay_ignored(repository, path):
    assert ignored(repository, path), path


@pytest.mark.parametrize("path", [
    ".env.example", "README.md", "docs/ARCHITECTURE.md", "docs/assets/workbench.jpg",
    "prompts/judge/example.md", "src/atomic_eval/prompts/example.md",
])
def test_public_documentation_and_templates_can_be_tracked(repository, path):
    assert not ignored(repository, path), path
