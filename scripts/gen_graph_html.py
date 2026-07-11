"""Export Neo4j graph to vis.js HTML for a specific project.

Usage:
    uv run python scripts/gen_graph_html.py <project_name_or_id>

Queries only nodes/edges belonging to the specified project.
Outputs to the project's search directory reports/graph.html.
"""

import json
import sys

from neo4j import GraphDatabase

BOLT_URI = "bolt://localhost:7687"
AUTH = ("neo4j", "daisy_dev")


def resolve_project(project_input: str) -> tuple[str, str, str]:
    """Resolve project name or ID to (project_id, name, workspace_path)."""
    import asyncio

    async def _resolve():
        from sqlalchemy import select

        from daisy.mcp_server import _session_factory
        from daisy.models.project import Project

        async with _session_factory() as session:
            # Try by name first
            result = await session.execute(
                select(Project).where(
                    Project.name.icontains(project_input)
                )
            )
            proj = result.scalar_one_or_none()
            if not proj:
                # Try by ID prefix
                result = await session.execute(
                    select(Project).where(
                        Project.id.cast(str).startswith(
                            project_input
                        )
                    )
                )
                proj = result.scalar_one_or_none()
            if not proj:
                print(f"Project not found: {project_input}")
                sys.exit(1)
            return (
                str(proj.id),
                proj.name,
                proj.workspace_path or ".",
            )

    return asyncio.run(_resolve())


def fetch_and_clean(project_id: str):
    """Fetch graph data scoped to a project."""
    driver = GraphDatabase.driver(BOLT_URI, auth=AUTH)
    raw_nodes = []
    raw_edges = []

    with driver.session() as session:
        # Get ALL nodes reachable from project persons
        # up to 3 hops (covers Person->FactNode->Entity)
        result = session.run(
            "MATCH (p:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}) "
            "OPTIONAL MATCH (p)-[*1..3]-(n) "
            "WHERE NOT n:Project "
            "WITH collect(DISTINCT p) + "
            "collect(DISTINCT n) AS all_nodes "
            "UNWIND all_nodes AS node "
            "WITH DISTINCT node "
            "WHERE node IS NOT NULL "
            "RETURN elementId(node) AS eid, "
            "labels(node) AS labels, "
            "properties(node) AS props",
            pid=project_id,
        )
        for rec in result:
            labels = rec["labels"]
            props = dict(rec["props"])
            node_type = labels[0] if labels else "Unknown"
            name = (
                props.get("name")
                or props.get("address")
                or props.get("id", "?")
            )
            raw_nodes.append({
                "eid": rec["eid"],
                "type": node_type,
                "name": name,
                "props": props,
            })

        # Get ALL edges between project-reachable nodes
        node_eids = {n["eid"] for n in raw_nodes}
        result = session.run(
            "MATCH (a)-[r]->(b) "
            "WHERE NOT a:Project AND NOT b:Project "
            "RETURN elementId(a) AS src, "
            "elementId(b) AS tgt, "
            "type(r) AS rel, properties(r) AS props",
        )
        for rec in result:
            if (
                rec["src"] in node_eids
                and rec["tgt"] in node_eids
            ):
                raw_edges.append({
                    "src": rec["src"],
                    "tgt": rec["tgt"],
                    "rel": rec["rel"],
                    "props": dict(rec["props"])
                    if rec["props"]
                    else {},
                })

    driver.close()

    # Filter and deduplicate
    removed_eids = set()
    for n in raw_nodes:
        if n["type"] == "Project":
            removed_eids.add(n["eid"])
        elif n["type"] == "FactNode":
            # Keep FactNodes that have a meaningful claim
            # (short summary), remove ones that are just UUIDs
            claim = n["props"].get("claim", "")
            if claim and len(claim) > 10:
                # Truncate for display
                n["name"] = claim[:60] + (
                    "..." if len(claim) > 60 else ""
                )
            else:
                removed_eids.add(n["eid"])

    # Remove garbage person names (generic, not subject-specific)
    garbage_indicators = [
        "director", "engineer", "manager", "architect",
        "counselor", "advisor", "president", "officer",
        "specialist", "lead ", "senior ", "sr ",
        "vp ", "svp ", "cto", "ceo",
        "aspects of", "development of", "in the early",
        "who found", "gallon tank", "community college",
        "apple pay",
    ]
    for n in raw_nodes:
        if n["eid"] in removed_eids:
            continue
        if n["type"] == "Person":
            name_lower = n["name"].lower()
            if len(n["name"].split()) > 4:
                removed_eids.add(n["eid"])
                continue
            for indicator in garbage_indicators:
                if indicator in name_lower:
                    removed_eids.add(n["eid"])
                    break
        elif n["type"] == "Company" and len(n["name"]) > 40:
            removed_eids.add(n["eid"])
        elif n["type"] == "Location" and len(n["name"]) > 50:
            removed_eids.add(n["eid"])

    # Normalize person names for dedup
    def normalize_name(name):
        prefixes = [
            "wife ", "husband ", "mother ", "father ",
            "likely ", "named ",
        ]
        n = name.strip()
        for p in prefixes:
            if n.lower().startswith(p):
                n = n[len(p):]
        n = n.split(",")[0].strip()  # Remove trailing context
        return n.strip()

    # Build deduplicated nodes keyed by (type, normalized_name)
    node_key_map = {}
    eid_to_key = {}

    for n in raw_nodes:
        if n["eid"] in removed_eids:
            continue
        name = n["name"]
        if n["type"] == "Person":
            name = normalize_name(name)
            n["name"] = name
        key = (n["type"], name)
        eid_to_key[n["eid"]] = key
        if key not in node_key_map:
            node_key_map[key] = {
                "type": n["type"],
                "name": name,
                "props": n["props"],
            }

    # Build edges with remapped endpoints
    edge_set = set()
    clean_edges = []

    for e in raw_edges:
        if e["src"] in removed_eids or e["tgt"] in removed_eids:
            continue
        if e["rel"] in ("HAS_FACT", "IN_PROJECT"):
            continue

        src_key = eid_to_key.get(e["src"])
        tgt_key = eid_to_key.get(e["tgt"])
        if not src_key or not tgt_key:
            continue
        if src_key == tgt_key:
            continue

        edge_id = (src_key, e["rel"], tgt_key)
        if edge_id in edge_set:
            continue
        edge_set.add(edge_id)

        label = ""
        if e["rel"] == "WORKS_AT":
            label = e["props"].get("role", "")
        elif e["rel"] == "MARRIED_TO":
            label = "married"
        elif e["rel"] == "CHILD_OF":
            label = "child of"

        clean_edges.append({
            "from": src_key,
            "to": tgt_key,
            "rel": e["rel"],
            "label": label,
        })

    print(
        f"  Final: {len(node_key_map)} nodes, "
        f"{len(clean_edges)} edges"
    )
    return clean_edges, node_key_map


# --- Styling ---

TYPE_STYLE = {
    "Person": {"color": "#e94560", "shape": "dot"},
    "Company": {"color": "#0f3460", "shape": "box"},
    "Location": {"color": "#00b4d8", "shape": "diamond"},
    "Account": {"color": "#e9c46a", "shape": "triangle"},
    "School": {"color": "#2a9d8f", "shape": "star"},
    "Property": {"color": "#f4a261", "shape": "square"},
    "Family": {"color": "#9b59b6", "shape": "hexagon"},
    "FactNode": {"color": "#6c757d", "shape": "dot"},
}

EDGE_STYLE = {
    "WORKS_AT": {"color": "#4a7ab5"},
    "MARRIED_TO": {"color": "#e94560", "width": 3},
    "CHILD_OF": {"color": "#e94560", "dashes": True},
    "LOCATED_AT": {"color": "#0096c7"},
    "LOCATED_IN": {"color": "#0096c7", "dashes": True},
    "OWNS": {"color": "#f4a261", "width": 2},
    "HAS_ACCOUNT": {"color": "#c9a227", "width": 1},
    "MEMBER_OF": {"color": "#9b59b6", "dashes": True},
    "NEXT_ROLE": {
        "color": "#666", "dashes": True, "arrows": True,
    },
    "BELONGS_TO": {"color": "#c9a227", "dashes": True},
    "SUBSIDIARY_OF": {"color": "#4a7ab5", "dashes": True},
    "COLLEAGUE": {"color": "#2a9d8f", "dashes": [2, 4]},
    "CO_LOCATED": {"color": "#0096c7", "dashes": [2, 4]},
    "CO_OWNER": {"color": "#f4a261", "dashes": True},
}


def generate_html(
    edges, node_key_map, title, output_path
):
    key_to_int = {}
    for i, key in enumerate(node_key_map.keys()):
        key_to_int[key] = i

    conn_count = {}
    for e in edges:
        conn_count[e["from"]] = (
            conn_count.get(e["from"], 0) + 1
        )
        conn_count[e["to"]] = (
            conn_count.get(e["to"], 0) + 1
        )

    # Find the primary subject (most connected person)
    max_conn = 0
    primary_key = None
    for key, node in node_key_map.items():
        if node["type"] == "Person":
            c = conn_count.get(key, 0)
            if c > max_conn:
                max_conn = c
                primary_key = key

    vis_nodes = []
    for key, node in node_key_map.items():
        nid = key_to_int[key]
        style = TYPE_STYLE.get(
            node["type"], {"color": "#666", "shape": "dot"}
        )
        size = 12 + conn_count.get(key, 0) * 2
        if key == primary_key:
            size = 45

        font_color = "#fff"
        if node["type"] in ("Account",):
            font_color = "#333"

        label = node["name"]
        if node["type"] == "Account":
            p = node["props"].get("platform", "")
            i = node["props"].get("identifier", "")
            if p and i:
                label = f"{p}\\n{i}"

        vis_nodes.append({
            "id": nid,
            "label": label,
            "shape": style["shape"],
            "size": size,
            "color": style["color"],
            "font": {"color": font_color, "size": 11},
        })

    vis_edges = []
    for e in edges:
        src = key_to_int.get(e["from"])
        tgt = key_to_int.get(e["to"])
        if src is None or tgt is None:
            continue

        style = EDGE_STYLE.get(
            e["rel"], {"color": "#555"}
        )
        edge = {"from": src, "to": tgt}
        if e["label"]:
            edge["label"] = e["label"]
        edge["color"] = {
            "color": style.get("color", "#555")
        }
        if style.get("width"):
            edge["width"] = style["width"]
        if style.get("dashes"):
            edge["dashes"] = (
                style["dashes"]
                if isinstance(style["dashes"], list)
                else True
            )
        if style.get("arrows"):
            edge["arrows"] = {"to": {"enabled": True}}

        vis_edges.append(edge)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title} - Identity Graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; }}
#header {{ padding: 16px 24px; background: #16213e; border-bottom: 2px solid #0f3460; }}
#header h1 {{ font-size: 20px; color: #e94560; }}
#header p {{ font-size: 12px; color: #888; margin-top: 2px; }}
#graph {{ width: 100%; height: calc(100vh - 70px); }}
#legend {{ position: absolute; bottom: 16px; left: 16px; background: rgba(22,33,62,0.95); padding: 12px; border-radius: 6px; border: 1px solid #0f3460; font-size: 11px; }}
#legend div {{ margin: 3px 0; }}
#legend span {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }}
</style>
</head>
<body>
<div id="header">
  <h1>{title} - Identity Graph</h1>
  <p>{len(vis_nodes)} nodes, {len(vis_edges)} edges | Built from Daisy Search confirmed facts</p>
</div>
<div id="graph"></div>
<div id="legend">
  <div><span style="background:#e94560"></span> Person</div>
  <div><span style="background:#0f3460"></span> Company</div>
  <div><span style="background:#00b4d8"></span> Location</div>
  <div><span style="background:#e9c46a"></span> Account</div>
  <div><span style="background:#2a9d8f"></span> School</div>
  <div><span style="background:#f4a261"></span> Property</div>
  <div><span style="background:#9b59b6"></span> Family</div>
</div>
<script>
const nodes = new vis.DataSet({json.dumps(vis_nodes)});
const edges = new vis.DataSet({json.dumps(vis_edges)});
const container = document.getElementById('graph');
const options = {{
  physics: {{
    forceAtlas2Based: {{
      gravitationalConstant: -80,
      centralGravity: 0.005,
      springLength: 180,
      springConstant: 0.01,
      damping: 0.4,
    }},
    solver: 'forceAtlas2Based',
    stabilization: {{ iterations: 400 }},
  }},
  edges: {{
    smooth: {{ type: 'curvedCW', roundness: 0.1 }},
    arrows: {{ to: {{ enabled: false }} }},
    font: {{ strokeWidth: 0, size: 9, color: '#aaa', background: '#1a1a2e' }},
  }},
  interaction: {{ hover: true, zoomView: true, dragView: true }},
}};
const network = new vis.Network(container, {{ nodes, edges }}, options);
network.on('stabilized', () => network.setOptions({{ physics: {{ enabled: false }} }}));
network.on('doubleClick', () => {{
  network.setOptions({{ physics: {{ enabled: true }} }});
  setTimeout(() => network.setOptions({{ physics: {{ enabled: false }} }}), 3000);
}});
</script>
</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)
    print(f"Written to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: uv run python scripts/gen_graph_html.py "
            "<project_name_or_id>"
        )
        sys.exit(1)

    project_input = " ".join(sys.argv[1:])
    print(f"Resolving project: {project_input}")
    project_id, name, workspace = resolve_project(
        project_input
    )
    print(f"  ID: {project_id}")
    print(f"  Name: {name}")
    print(f"  Workspace: {workspace}")

    import os

    output_path = os.path.join(workspace, "reports", "graph.html")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print("Fetching and cleaning graph...")
    edges, node_key_map = fetch_and_clean(project_id)
    print("Generating HTML...")
    generate_html(edges, node_key_map, name, output_path)
    print("Done.")
