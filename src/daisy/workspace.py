"""Case-workspace scaffolding.

One implementation, shared by the `daisy_new_project` MCP tool and the
`daisy new-search` CLI command, so a project's database row and its folder on
disk can never disagree about where the workspace lives.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path

WORKSPACE_SUBDIRS = ("notes", "downloads", "screenshots", "reports", "raw")

# Rendered into the workspace from templates/, keyed by output path.
_TEMPLATE_TARGETS = {
    "CLAUDE.md.template": Path("CLAUDE.md"),
    "SESSION_STATE.md.template": Path("notes/SESSION_STATE.md"),
}


def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:50].rstrip("-")


def daisy_root() -> Path:
    """Repository root, used to resolve templates/ and the MCP server path."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists():
        return candidate
    return Path.cwd()


def resolve_workspace_dir(
    projects_root: Path,
    slug: str,
    claimed_paths: Iterable[str] = (),
) -> Path:
    """Pick the workspace directory for a new project.

    An existing directory that no other project has claimed is **adopted**, not
    sidestepped — that is the directory a user scaffolded ahead of time, and
    suffixing it would strand their notes in a folder the database never
    points at. A directory another project already claims is skipped.
    """
    claimed = {str(Path(p).resolve()) for p in claimed_paths if p}
    candidate = projects_root / slug
    if str(candidate.resolve()) not in claimed:
        return candidate
    n = 2
    while str((projects_root / f"{slug}-{n}").resolve()) in claimed:
        n += 1
    return projects_root / f"{slug}-{n}"


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def scaffold_workspace(
    project_dir: Path,
    *,
    subject_name: str,
    project_id: str,
    description: str = "investigation",
    root: Path | None = None,
) -> list[str]:
    """Create the workspace tree, render templates, and write MCP settings.

    Existing files are never overwritten, so re-running against a directory a
    user has already worked in is safe. Returns the paths that were written,
    relative to `project_dir`.
    """
    root = root or daisy_root()
    templates_dir = root / "templates"
    written: list[str] = []

    for subdir in WORKSPACE_SUBDIRS:
        (project_dir / subdir).mkdir(parents=True, exist_ok=True)

    values = {
        "SUBJECT_NAME": subject_name,
        "SUBJECT_SLUG": slugify(subject_name),
        "DESCRIPTION": description,
        "DATE": date.today().isoformat(),
        "PROJECT_ID": project_id,
        "DAISY_ROOT": str(root),
    }

    for template_name, relative_target in _TEMPLATE_TARGETS.items():
        source = templates_dir / template_name
        target = project_dir / relative_target
        if target.exists() or not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _render(source.read_text(encoding="utf-8"), values), encoding="utf-8"
        )
        written.append(str(relative_target))

    settings_path = project_dir / ".claude" / "settings.local.json"
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "daisy-search": {
                            "type": "stdio",
                            "command": "uv",
                            "args": ["--directory", str(root), "run", "daisy-mcp"],
                            "env": {},
                        }
                    },
                    "permissions": {
                        "allow": [
                            "mcp__daisy-search__*",
                            "mcp__claude-in-chrome__*",
                            "Read(*)",
                            f"Write({project_dir}/*)",
                            "Glob(*)",
                            "Grep(*)",
                        ]
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(".claude/settings.local.json")

    return written
