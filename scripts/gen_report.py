"""Generate a professional OSINT report for a Daisy Search project.

Usage:
    uv run python scripts/gen_report.py <project_name_or_id>

Pulls all facts + attributes from Postgres, generates a styled Quarto
report, and renders to self-contained HTML.
"""

import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DAISY_ROOT = Path(__file__).resolve().parent.parent


def resolve_project(project_input: str):
    """Resolve project name or ID to (project_id, name, workspace_path, subjects)."""

    async def _resolve():
        from sqlalchemy import select

        from daisy.mcp_server import _session_factory
        from daisy.models.fact import Fact
        from daisy.models.project import Project
        from daisy.models.subject import Subject

        async with _session_factory() as session:
            result = await session.execute(
                select(Project).where(
                    Project.name.icontains(project_input)
                )
            )
            proj = result.scalar_one_or_none()
            if not proj:
                result = await session.execute(
                    select(Project).where(
                        Project.id.cast(str).startswith(project_input)
                    )
                )
                proj = result.scalar_one_or_none()
            if not proj:
                print(f"Project not found: {project_input}")
                sys.exit(1)

            subjects = (
                await session.execute(
                    select(Subject).where(
                        Subject.project_id == proj.id
                    )
                )
            ).scalars().all()

            facts = (
                await session.execute(
                    select(Fact)
                    .where(Fact.project_id == proj.id)
                    .order_by(Fact.local_confidence.desc())
                )
            ).scalars().all()

            return {
                "id": str(proj.id),
                "name": proj.name,
                "status": proj.status,
                "workspace_path": proj.workspace_path or ".",
                "subjects": [
                    {"id": str(s.id), "name": s.name}
                    for s in subjects
                ],
                "facts": [
                    {
                        "id": str(f.id),
                        "subject_id": str(f.subject_id) if f.subject_id else None,
                        "claim_text": f.claim_text,
                        "claim_type": f.claim_type,
                        "source_url": f.source_url,
                        "source_type": f.source_type,
                        "event_date": str(f.event_date) if f.event_date else None,
                        "confidence": f.local_confidence,
                        "state": f.lifecycle_state,
                        "attributes": f.attributes or {},
                    }
                    for f in facts
                ],
            }

    return asyncio.run(_resolve())


def confidence_badge(conf: float) -> str:
    if conf >= 0.9:
        return '<span class="badge bg-success">Confirmed</span>'
    elif conf >= 0.7:
        return '<span class="badge bg-warning text-dark">Probable</span>'
    else:
        return '<span class="badge bg-secondary">Unverified</span>'


def state_badge(state: str) -> str:
    colors = {
        "confirmed": "bg-success",
        "likely": "bg-info",
        "pending": "bg-warning text-dark",
        "unconfirmed": "bg-warning text-dark",
        "discarded": "bg-danger",
        "needs_revalidation": "bg-secondary",
    }
    cls = colors.get(state, "bg-secondary")
    return f'<span class="badge {cls}">{state}</span>'


def escape_md(text: str) -> str:
    """Escape pipe chars for markdown tables."""
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ")


def group_facts(facts: list[dict]) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for f in facts:
        grouped[f["claim_type"]].append(f)
    return dict(grouped)


def build_subject_profile(facts: list[dict]) -> str:
    """Build the subject profile card grid from facts."""
    profile = {}
    for f in facts:
        attrs = f.get("attributes", {})
        claim = f["claim_text"]

        if "full name" in claim.lower() or "name" in attrs:
            profile.setdefault("Full Name", (attrs.get("name") or attrs.get("full_name") or claim, f["confidence"]))
        if attrs.get("dob"):
            profile.setdefault("DOB", (attrs["dob"], f["confidence"]))
        if attrs.get("ssn"):
            profile.setdefault("SSN", (attrs["ssn"], f["confidence"]))
        if attrs.get("phone"):
            profile.setdefault("Phone", (attrs["phone"], f["confidence"]))
        if attrs.get("address"):
            addr = attrs.get("address", "")
            city = attrs.get("city", "")
            state = attrs.get("state", "")
            zp = attrs.get("zip", "")
            full = f"{addr}, {city} {state} {zp}".strip(", ")
            profile.setdefault("Address", (full, f["confidence"]))

    if not profile:
        return ""

    cards = []
    for label, (value, conf) in profile.items():
        cards.append(
            f'::: {{.g-col-6 .g-col-md-4}}\n'
            f'<div class="metric-card">\n'
            f'<div class="metric-label">{label}</div>\n'
            f'<div class="metric-value">{escape_md(str(value))}</div>\n'
            f'<div class="metric-conf">{confidence_badge(conf)}</div>\n'
            f'</div>\n:::\n'
        )

    return '::: {.grid}\n' + '\n'.join(cards) + ':::\n'


def build_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "*No data collected yet.*\n"
    hdr = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    lines = [hdr, sep]
    for row in rows:
        lines.append("| " + " | ".join(escape_md(str(c)) for c in row) + " |")
    return "\n".join(lines) + "\n"


def _has_attrs(f: dict) -> bool:
    """Check if a fact has any meaningful structured attributes."""
    a = f.get("attributes", {})
    return bool(a and any(v for v in a.values() if v))


def build_credentials_section(cred_facts: list[dict]) -> str:
    # Split into structured vs unstructured
    structured = [f for f in cred_facts if _has_attrs(f)]
    unstructured = [f for f in cred_facts if not _has_attrs(f)]

    parts = []
    if structured:
        rows = []
        for f in structured:
            a = f["attributes"]
            rows.append([
                a.get("email", ""),
                a.get("breach", a.get("source", "")),
                a.get("breach_date", f.get("event_date") or ""),
                ", ".join(a.get("fields", [])) if isinstance(a.get("fields"), list) else a.get("fields", ""),
                a.get("password", ""),
                a.get("username", ""),
                state_badge(f["state"]),
            ])
        parts.append(build_table(
            ["Email", "Breach", "Date", "Fields Exposed", "Password", "Username", "Status"],
            rows,
        ))

    if unstructured:
        rows = []
        for f in unstructured:
            rows.append([
                escape_md(f["claim_text"]),
                f.get("source_url", "")[:40],
                state_badge(f["state"]),
            ])
        parts.append(build_table(["Finding", "Source", "Status"], rows))

    return "\n".join(parts)


def _fact_card(f: dict, show_source: bool = True) -> str:
    """Render a single fact as a styled card."""
    state = f["state"]
    claim = f["claim_text"]
    src = f.get("source_url", "")
    attrs = f.get("attributes", {})

    # Build attribute pills
    attr_html = ""
    if attrs:
        pills = []
        for k, v in attrs.items():
            if k in ("fields", "source") or not v:
                continue
            val = str(v) if not isinstance(v, list) else ", ".join(str(x) for x in v)
            pills.append(f'<span class="attr-pill"><strong>{k}:</strong> {escape_md(val)}</span>')
        if pills:
            attr_html = '<div class="attr-pills">' + " ".join(pills) + "</div>"

    source_html = ""
    if show_source and src:
        short = src[:60] + ("..." if len(src) > 60 else "")
        source_html = f'<div class="card-source">{escape_md(short)}</div>'

    return f"""<div class="fact-card">
<div class="fact-header">{state_badge(state)}</div>
<div class="fact-body">{escape_md(claim)}</div>
{attr_html}{source_html}
</div>
"""


def _fact_cards(facts: list[dict], show_source: bool = True) -> str:
    if not facts:
        return "*No data collected yet.*\n"
    return "\n".join(_fact_card(f, show_source) for f in facts)


def build_employment_section(emp_facts: list[dict]) -> str:
    # Employment works well as a table when structured, cards when not
    structured = [f for f in emp_facts if _has_attrs(f)]
    unstructured = [f for f in emp_facts if not _has_attrs(f)]

    parts = []
    if structured:
        rows = []
        for f in structured:
            a = f["attributes"]
            rows.append([
                f.get("event_date") or a.get("period", ""),
                a.get("role", ""),
                a.get("employer", a.get("company", "")),
                a.get("location", ""),
                state_badge(f["state"]),
            ])
        parts.append(build_table(
            ["Period", "Role", "Employer", "Location", "Status"],
            rows,
        ))

    if unstructured:
        parts.append(_fact_cards(unstructured))

    return "\n".join(parts)


def build_location_section(loc_facts: list[dict]) -> str:
    return _fact_cards(loc_facts, show_source=True)


def build_account_section(acct_facts: list[dict]) -> str:
    return _fact_cards(acct_facts)


def build_relationship_section(rel_facts: list[dict]) -> str:
    return _fact_cards(rel_facts, show_source=True)


def build_legal_section(legal_facts: list[dict]) -> str:
    # Legal is best as a table when structured
    structured = [f for f in legal_facts if _has_attrs(f)]
    unstructured = [f for f in legal_facts if not _has_attrs(f)]

    parts = []
    if structured:
        rows = []
        for f in structured:
            a = f["attributes"]
            rows.append([
                a.get("case_type", ""),
                a.get("case_number", ""),
                a.get("filed", f.get("event_date") or ""),
                a.get("status", ""),
                a.get("source", ""),
                escape_md(f["claim_text"][:80]),
            ])
        parts.append(build_table(
            ["Type", "Case #", "Filed", "Status", "Source", "Notes"],
            rows,
        ))

    if unstructured:
        parts.append(_fact_cards(unstructured))

    return "\n".join(parts)


def build_generic_section(facts: list[dict]) -> str:
    return _fact_cards(facts, show_source=True)


def build_stats_cards(facts: list[dict]) -> str:
    total = len(facts)
    by_state = defaultdict(int)
    by_type = defaultdict(int)
    for f in facts:
        by_state[f["state"]] += 1
        by_type[f["claim_type"]] += 1

    confirmed = by_state.get("confirmed", 0)
    likely = by_state.get("likely", 0)
    creds = by_type.get("credential", 0)
    breaches = len({
        f["attributes"].get("breach", "")
        for f in facts
        if f["claim_type"] == "credential" and f.get("attributes", {}).get("breach")
    })

    return f"""::: {{.grid}}
::: {{.g-col-3}}
<div class="stat-card">
<div class="stat-number">{total}</div>
<div class="stat-label">Total Facts</div>
</div>
:::
::: {{.g-col-3}}
<div class="stat-card stat-confirmed">
<div class="stat-number">{confirmed + likely}</div>
<div class="stat-label">Confirmed / Likely</div>
</div>
:::
::: {{.g-col-3}}
<div class="stat-card stat-breach">
<div class="stat-number">{breaches}</div>
<div class="stat-label">Breaches Found</div>
</div>
:::
::: {{.g-col-3}}
<div class="stat-card stat-cred">
<div class="stat-number">{creds}</div>
<div class="stat-label">Credential Records</div>
</div>
:::
:::
"""


def build_sources_table(facts: list[dict]) -> str:
    sources = {}
    for f in facts:
        url = f["source_url"]
        if url not in sources:
            sources[url] = {
                "type": f["source_type"],
                "count": 0,
                "types": set(),
            }
        sources[url]["count"] += 1
        sources[url]["types"].add(f["claim_type"])

    rows = []
    for url, info in sorted(sources.items(), key=lambda x: -x[1]["count"]):
        rows.append([
            escape_md(url[:60]),
            info["type"],
            str(info["count"]),
            ", ".join(sorted(info["types"])),
        ])
    return build_table(["Source", "Type", "Facts", "Categories"], rows)


def check_graph_html(workspace_path: str, slug: str) -> str:
    """Check if vis.js graph exists and return embed code."""
    graph_path = Path(workspace_path) / "reports" / "graph.html"
    if not graph_path.exists():
        search_path = DAISY_ROOT / "searches" / slug / "reports" / "graph.html"
        if search_path.exists():
            graph_path = search_path
        else:
            return "::: {.callout-note}\nNo graph visualization generated yet. Run `daisy_build_graph` then `gen_graph_html.py`.\n:::\n"

    rel_path = "graph.html"
    return f'<div class="graph-embed">\n<a href="{rel_path}" target="_blank" class="btn btn-primary">Open Interactive Graph</a>\n<p class="text-muted">Opens the full vis.js network visualization in a new tab.</p>\n</div>\n'


def generate_report(project_input: str):
    data = resolve_project(project_input)
    facts = data["facts"]
    grouped = group_facts(facts)
    slug = re.sub(r"[^a-z0-9]+", "-", data["name"].lower()).strip("-")

    # Read SESSION_STATE.md if available
    session_state = ""
    for path in [
        Path(data["workspace_path"]) / "notes" / "SESSION_STATE.md",
        DAISY_ROOT / "searches" / slug / "notes" / "SESSION_STATE.md",
    ]:
        if path.exists():
            session_state = path.read_text()
            break

    # Extract outstanding questions from SESSION_STATE
    questions = ""
    if session_state:
        match = re.search(
            r"## Outstanding Questions\s*\n(.*?)(?=\n## |\Z)",
            session_state,
            re.DOTALL,
        )
        if match:
            questions = match.group(1).strip()

    from datetime import date

    today = date.today().isoformat()

    # --- Build the report ---
    qmd = f"""---
title: "OSINT Report: {data['name']}"
subtitle: "Daisy Search | Confidential Intelligence Brief"
date: "{today}"
format:
  html:
    theme: darkly
    toc: true
    toc-depth: 3
    toc-title: "Contents"
    self-contained: true
    number-sections: true
    smooth-scroll: true
---

```{{=html}}
<style>
:root {{
  --ds-red: #e94560;
  --ds-blue: #0f3460;
  --ds-cyan: #00b4d8;
  --ds-gold: #e9c46a;
  --ds-green: #2a9d8f;
  --ds-orange: #f4a261;
  --ds-purple: #9b59b6;
}}

body {{ font-family: "Inter", -apple-system, BlinkMacSystemFont, sans-serif; }}

.stat-card {{
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  border: 1px solid #333;
  border-radius: 12px;
  padding: 1.2rem;
  text-align: center;
  transition: transform 0.2s;
}}
.stat-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 15px rgba(0,0,0,0.3); }}
.stat-number {{ font-size: 2.2rem; font-weight: 700; color: var(--ds-cyan); }}
.stat-confirmed .stat-number {{ color: var(--ds-green); }}
.stat-breach .stat-number {{ color: var(--ds-red); }}
.stat-cred .stat-number {{ color: var(--ds-gold); }}
.stat-label {{ font-size: 0.85rem; color: #aaa; margin-top: 0.3rem; text-transform: uppercase; letter-spacing: 0.05em; }}

.metric-card {{
  background: #1a1a2e;
  border-left: 4px solid var(--ds-cyan);
  border-radius: 0 8px 8px 0;
  padding: 0.8rem 1rem;
  margin-bottom: 0.5rem;
}}
.metric-label {{ font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }}
.metric-value {{ font-size: 1.1rem; font-weight: 600; color: #e0e0e0; margin: 0.2rem 0; }}
.metric-conf {{ margin-top: 0.3rem; }}

.badge {{
  font-size: 0.7rem;
  padding: 0.25em 0.6em;
  border-radius: 4px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}}
.bg-success {{ background-color: var(--ds-green) !important; }}
.bg-warning {{ background-color: var(--ds-gold) !important; }}
.bg-info {{ background-color: var(--ds-cyan) !important; color: #000 !important; }}
.bg-danger {{ background-color: var(--ds-red) !important; }}
.bg-secondary {{ background-color: #555 !important; }}

table {{
  width: 100%;
  border-collapse: collapse;
  margin: 1rem 0;
  font-size: 0.88rem;
}}
th {{
  background: #16213e;
  color: var(--ds-cyan);
  padding: 0.6rem 0.8rem;
  text-align: left;
  font-weight: 600;
  text-transform: uppercase;
  font-size: 0.75rem;
  letter-spacing: 0.05em;
  border-bottom: 2px solid var(--ds-cyan);
}}
td {{
  padding: 0.5rem 0.8rem;
  border-bottom: 1px solid #2a2a3e;
  color: #ccc;
}}
tr:hover td {{ background: rgba(0, 180, 216, 0.05); }}

.graph-embed {{
  background: #1a1a2e;
  border: 1px solid #333;
  border-radius: 12px;
  padding: 2rem;
  text-align: center;
  margin: 1rem 0;
}}
.graph-embed .btn {{
  background: var(--ds-cyan);
  color: #000;
  font-weight: 600;
  padding: 0.6rem 1.5rem;
  border-radius: 8px;
  text-decoration: none;
  display: inline-block;
}}
.graph-embed .btn:hover {{ background: #33ccee; }}

.section-divider {{
  border: none;
  height: 1px;
  background: linear-gradient(to right, transparent, var(--ds-cyan), transparent);
  margin: 2rem 0;
}}

h2 {{ color: var(--ds-cyan); border-bottom: 1px solid #2a2a3e; padding-bottom: 0.5rem; }}
h3 {{ color: var(--ds-gold); }}

.callout {{ border-radius: 8px; }}

.fact-card {{
  background: #1a1a2e;
  border-left: 3px solid var(--ds-cyan);
  border-radius: 0 8px 8px 0;
  padding: 0.9rem 1.1rem;
  margin-bottom: 0.7rem;
  transition: border-color 0.2s;
}}
.fact-card:hover {{ border-left-color: var(--ds-gold); }}
.fact-header {{ margin-bottom: 0.4rem; }}
.fact-body {{ color: #ddd; font-size: 0.92rem; line-height: 1.5; }}
.card-source {{ color: #666; font-size: 0.75rem; margin-top: 0.4rem; font-style: italic; }}

.attr-pills {{ margin-top: 0.5rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }}
.attr-pill {{
  background: #16213e;
  border: 1px solid #333;
  border-radius: 4px;
  padding: 0.15rem 0.5rem;
  font-size: 0.78rem;
  color: #bbb;
}}
.attr-pill strong {{ color: var(--ds-cyan); font-weight: 600; }}
</style>
```

::: {{.callout-warning}}
**CONFIDENTIAL** -- This document contains sensitive personal information
collected through authorized OSINT research. Handle accordingly.
:::

## Executive Summary

{build_stats_cards(facts)}

---

## Subject Profile

{build_subject_profile(facts)}

"""

    # --- Credential / Breach Exposure ---
    cred_facts = grouped.get("credential", [])
    if cred_facts:
        qmd += f"""
## Credentials & Breach Exposure

::: {{.callout-important}}
**{len(cred_facts)} credential records** found across **{len({f.get('attributes',{{}}).get('breach','') for f in cred_facts if isinstance(f.get('attributes'), dict) and f['attributes'].get('breach')})}** distinct breaches.
:::

{build_credentials_section(cred_facts)}

"""

    # --- Employment ---
    emp_facts = grouped.get("employment", [])
    if emp_facts:
        qmd += f"""
## Career & Employment

{build_employment_section(emp_facts)}

"""

    # --- Education ---
    edu_facts = grouped.get("education", [])
    if edu_facts:
        qmd += f"""
## Education

{build_generic_section(edu_facts)}

"""

    # --- Locations / Residences ---
    loc_facts = grouped.get("location", [])
    if loc_facts:
        qmd += f"""
## Residences & Addresses

{build_location_section(loc_facts)}

"""

    # --- Relationships / Family ---
    rel_facts = grouped.get("relationship", [])
    if rel_facts:
        qmd += f"""
## Family & Associates

{build_relationship_section(rel_facts)}

"""

    # --- Digital Footprint ---
    acct_facts = grouped.get("account", [])
    if acct_facts:
        qmd += f"""
## Digital Footprint

{build_account_section(acct_facts)}

"""

    # --- Legal ---
    legal_facts = grouped.get("legal", [])
    if legal_facts:
        qmd += f"""
## Legal & Court Records

{build_legal_section(legal_facts)}

"""

    # --- Remaining types ---
    handled = {"credential", "employment", "education", "location", "relationship", "account", "legal"}
    for ctype, type_facts in sorted(grouped.items()):
        if ctype not in handled:
            qmd += f"""
## {ctype.replace("_", " ").title()}

{build_generic_section(type_facts)}

"""

    # --- Graph ---
    qmd += f"""
## Relationship Graph

{check_graph_html(data['workspace_path'], slug)}

"""

    # --- Outstanding Questions ---
    if questions:
        qmd += f"""
## Outstanding Questions

{questions}

"""

    # --- Sources ---
    qmd += f"""
## Sources & Attribution

{build_sources_table(facts)}

"""

    # --- Raw Attribute Dump (collapsible) ---
    attrs_facts = [f for f in facts if f.get("attributes")]
    if attrs_facts:
        qmd += """
## Appendix: Raw Attribute Data {.appendix}

::: {.callout-note collapse="true"}
## Click to expand full attribute dump

"""
        for f in attrs_facts:
            qmd += f"**{escape_md(f['claim_text'][:60])}** ({f['claim_type']})\n\n"
            qmd += "```json\n"
            qmd += json.dumps(f["attributes"], indent=2)
            qmd += "\n```\n\n"
        qmd += ":::\n"

    # --- Write the .qmd file ---
    output_dir = DAISY_ROOT / "searches" / slug / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    qmd_path = output_dir / f"{slug}-report.qmd"
    qmd_path.write_text(qmd)
    print(f"Report written: {qmd_path}")
    return str(qmd_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/gen_report.py <project_name_or_id>")
        sys.exit(1)

    qmd_path = generate_report(sys.argv[1])
    print(f"\nTo render: quarto render {qmd_path}")
