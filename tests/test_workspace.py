"""Workspace scaffolding: the DB row and the folder on disk must never disagree."""

import json

import pytest

from daisy.workspace import (
    WORKSPACE_SUBDIRS,
    resolve_workspace_dir,
    scaffold_workspace,
    slugify,
)


@pytest.fixture
def projects_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return root


def test_unclaimed_existing_directory_is_adopted(projects_root):
    """A workspace scaffolded ahead of time must be the one the project uses.

    Regression: the old handler suffixed to '<slug>-2', stranding the user's
    notes in a folder the database never pointed at.
    """
    pre_existing = projects_root / "gina-talia-faker"
    pre_existing.mkdir()

    resolved = resolve_workspace_dir(projects_root, "gina-talia-faker", [])

    assert resolved == pre_existing


def test_directory_claimed_by_another_project_is_skipped(projects_root):
    taken = projects_root / "gina-talia-faker"
    taken.mkdir()

    resolved = resolve_workspace_dir(projects_root, "gina-talia-faker", [str(taken)])

    assert resolved.name == "gina-talia-faker-2"


def test_suffix_increments_past_every_claimed_directory(projects_root):
    claimed = [str(projects_root / "sub"), str(projects_root / "sub-2")]

    resolved = resolve_workspace_dir(projects_root, "sub", claimed)

    assert resolved.name == "sub-3"


def test_scaffold_creates_the_full_tree(projects_root):
    project_dir = projects_root / "gina-talia-faker"

    scaffold_workspace(
        project_dir, subject_name="Gina Talia-Faker", project_id="test-id-1"
    )

    for subdir in WORKSPACE_SUBDIRS:
        assert (project_dir / subdir).is_dir()
    assert (project_dir / "CLAUDE.md").exists()
    assert (project_dir / "notes" / "SESSION_STATE.md").exists()
    assert (project_dir / ".claude" / "settings.local.json").exists()


def test_templates_are_fully_rendered(projects_root):
    project_dir = projects_root / "gina-talia-faker"

    scaffold_workspace(
        project_dir, subject_name="Gina Talia-Faker", project_id="test-id-1"
    )

    for name in ("CLAUDE.md", "notes/SESSION_STATE.md"):
        text = (project_dir / name).read_text()
        assert "test-id-1" in text, f"{name} did not receive the real project id"
        assert "{{" not in text, f"{name} has an unrendered placeholder"


def test_settings_enable_the_daisy_mcp_server(projects_root):
    project_dir = projects_root / "gina-talia-faker"

    scaffold_workspace(
        project_dir, subject_name="Gina Talia-Faker", project_id="test-id-1"
    )

    settings = json.loads(
        (project_dir / ".claude" / "settings.local.json").read_text()
    )
    assert "daisy-search" in settings["mcpServers"]
    assert "mcp__daisy-search__*" in settings["permissions"]["allow"]


def test_scaffolding_never_clobbers_existing_work(projects_root):
    project_dir = projects_root / "gina-talia-faker"
    scaffold_workspace(
        project_dir, subject_name="Gina Talia-Faker", project_id="test-id-1"
    )
    (project_dir / "CLAUDE.md").write_text("operator notes")

    rewritten = scaffold_workspace(
        project_dir, subject_name="Gina Talia-Faker", project_id="test-id-1"
    )

    assert rewritten == []
    assert (project_dir / "CLAUDE.md").read_text() == "operator notes"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gina Talia-Faker", "gina-talia-faker"),
        ("  Mixed   CASE  ", "mixed-case"),
        ("punctuation!?", "punctuation"),
    ],
)
def test_slugify_matches_the_shell_scaffolder(raw, expected):
    assert slugify(raw) == expected
