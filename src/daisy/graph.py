"""Neo4j graph operations for Daisy Search.

Builds a richly connected graph from confirmed facts. The graph is NOT
a flat star from the subject — it creates inter-node connections:

- People connected through shared companies (colleagues)
- Family members connected to each other
- Properties linked to locations and co-owners
- Accounts linked to companies (email domains)
- Career timeline as a chain
- Locations connected to schools, properties, people
- Company hierarchies (subsidiaries, parent companies)

The graph is a projection of Postgres — always rebuildable from scratch.

Entity extraction uses LLM (Ollama preferred, Anthropic fallback) instead
of regex. Each fact claim is sent to the LLM once and the structured JSON
result is cached for subsequent rebuilds.
"""

from __future__ import annotations

import json
import logging
import re
import uuid as uuid_mod

import httpx
from neo4j import GraphDatabase

from daisy.config import settings
from daisy.constants import (
    DEFAULT_LLM_EFFORT,
    DEFAULT_LLM_MAX_TOKENS,
    ClaimType,
    LifecycleState,
)

logger = logging.getLogger(__name__)


def _get_driver():
    return GraphDatabase.driver(
        settings.neo4j_url,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


def _ensure_constraints(tx):
    """Create uniqueness constraints (idempotent)."""
    for label, prop in [
        ("Project", "id"),
        ("Person", "id"),
        ("Company", "name"),
        ("School", "name"),
        ("Location", "name"),
        ("Account", "id"),
        ("Property", "address"),
        ("Family", "name"),
    ]:
        tx.run(
            f"CREATE CONSTRAINT IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        )


def ensure_schema():
    driver = _get_driver()
    with driver.session() as session:
        session.execute_write(_ensure_constraints)
    driver.close()


# -------------------------------------------------------------------
# Normalization helpers (applied AFTER LLM extraction)
# -------------------------------------------------------------------

COMPANY_ALIASES = {
    "showtime networks inc": "Showtime Networks",
    "showtime networks inc.": "Showtime Networks",
    "showtime networks": "Showtime Networks",
    "showtime channels": "Showtime Networks",
    "showtime anytime": "Showtime Networks",
    "showtime": "Showtime Networks",
    "lime wire llc": "LimeWire",
    "lime wire": "LimeWire",
    "limewire": "LimeWire",
    "lime": "LimeWire",
    "lime (limewire)": "LimeWire",
    "sony music entertainment": "Sony Music",
    "sony music": "Sony Music",
    "conde nast": "Conde Nast",
    "paramount+": "Paramount",
    "paramount": "Paramount",
    "marchfirst": "marchFIRST",
    "nearverse": "NearVerse",
    "payfone": "Payfone",
}

COMPANY_HIERARCHY = {
    "Showtime Networks": "Paramount",
}

LOCATION_ALIASES = {
    "new york, new york, united states": "New York, NY",
    "new york, ny": "New York, NY",
    "nyc, ny": "New York, NY",
    "tenafly, new jersey, united states": "Tenafly, NJ",
    "tenafly, new jersey": "Tenafly, NJ",
    "tenafly, nj": "Tenafly, NJ",
    "saratoga springs, new york": "Saratoga Springs, NY",
    "saratoga springs, ny": "Saratoga Springs, NY",
    "montvale, nj": "Montvale, NJ",
    "montvale nj 07645": "Montvale, NJ",
    "weehawken, nj": "Weehawken, NJ",
    "west new york, nj": "West New York, NJ",
}


def _normalize_company(name: str) -> str:
    key = name.lower().strip().rstrip(".,;:")
    return COMPANY_ALIASES.get(key, name.strip().rstrip(".,;:"))


def _normalize_location(name: str) -> str:
    key = name.lower().strip().rstrip(".,;:")
    return LOCATION_ALIASES.get(key, name.strip().rstrip(".,;:"))


def _normalize_person_name(name: str) -> str:
    """Strip relationship prefixes and clean up person names."""
    prefixes = [
        "wife ", "husband ", "mother ", "father ",
        "sister ", "brother ", "likely ", "named ",
    ]
    cleaned = name.strip()
    for prefix in prefixes:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
    cleaned = re.sub(
        r",?\s*who\s+.*$", "", cleaned
    ).strip()
    cleaned = re.sub(
        r"'s\s+.*$", "", cleaned
    ).strip()
    return cleaned.strip().rstrip(".,;:")


def _person_id(name: str) -> str:
    """Generate a stable ID from a person name."""
    clean = _normalize_person_name(name)
    return (
        f"person-{clean.lower().replace(' ', '-')}"
    )


# -------------------------------------------------------------------
# LLM-powered entity extraction
# -------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """\
You are a structured data extractor for an OSINT knowledge graph. \
Given a factual claim about a person, extract all entities and \
relationships as JSON.

Return a JSON object with these optional fields (include only \
what's present in the text):
{
  "persons": [{"name": "...", "role_in_claim": \
"subject|spouse|parent|child|sibling|colleague|friend|other"}],
  "companies": [{"name": "...", "role": "...", \
"start_year": null, "end_year": null}],
  "schools": [{"name": "...", "degree": "...", "years": "..."}],
  "locations": [{"name": "...", "type": \
"address|city|state|country", "context": \
"lives|works|born|property"}],
  "properties": [{"address": "...", "location": "...", \
"price": "...", "year": "..."}],
  "accounts": [{"platform": "...", "identifier": "...", \
"url": "..."}],
  "relationships": [{"from": "...", "to": "...", "type": \
"married_to|child_of|sibling|colleague|friend|family"}]
}

Rules:
- Use canonical names (first and last name, no prefixes like \
"Wife" or "Mother")
- Normalize company names (e.g., "Lime Wire LLC" -> "LimeWire")
- For locations, use "City, ST" format for US addresses
- Only extract what's explicitly stated, don't infer
- Return ONLY the JSON object, no other text"""


def _parse_llm_json(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        # Remove closing fence
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON: %s", text[:200])
        return {}


async def _extract_entities_llm(
    claim_text: str, claim_type: str,
) -> dict:
    """Use LLM to extract structured entities from a fact claim.

    Priority: Ollama > Anthropic API > empty dict.
    """
    if settings.ollama_url:
        return await _extract_via_ollama(claim_text, claim_type)
    if settings.anthropic_api_key:
        return await _extract_via_anthropic(claim_text, claim_type)
    logger.debug(
        "No LLM backend available for entity extraction"
    )
    return {}


async def _extract_via_ollama(
    claim_text: str, claim_type: str,
) -> dict:
    """Call Ollama chat/completions endpoint."""
    url = f"{settings.ollama_url.rstrip('/')}/api/chat"
    payload = {
        "model": settings.ollama_extraction_model,
        "messages": [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Claim type: {claim_type}\n\n{claim_text}"
                ),
            },
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            return _parse_llm_json(content)
    except Exception:
        logger.exception("Ollama extraction failed")
        # Fall back to Anthropic if available
        if settings.anthropic_api_key:
            return await _extract_via_anthropic(
                claim_text, claim_type,
            )
        return {}


async def _extract_via_anthropic(
    claim_text: str, claim_type: str,
) -> dict:
    """Call Anthropic Messages API."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.extraction_model,
        "max_tokens": DEFAULT_LLM_MAX_TOKENS,
        "output_config": {"effort": DEFAULT_LLM_EFFORT},
        "system": _EXTRACTION_SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Claim type: {claim_type}\n\n{claim_text}"
                ),
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url, json=payload, headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("stop_reason") == "refusal":
                logger.warning(
                    "Anthropic extraction refused: %s",
                    data.get("stop_details"),
                )
                return {}
            # Responses can open with a thinking block; read by type.
            text = next(
                (b["text"] for b in data["content"]
                 if b.get("type") == "text"),
                "",
            )
            return _parse_llm_json(text)
    except Exception:
        logger.exception("Anthropic extraction failed")
        return {}


# -------------------------------------------------------------------
# Graph build — from LLM-extracted entities
# -------------------------------------------------------------------


def _add_fact_from_entities(
    session,
    project_id: str,
    subject_id: str,
    subject_name: str,
    fact_id: str,
    claim_text: str,
    claim_type: str,
    source_url: str,
    state: str,
    entities: dict,
):
    """Process one fact into graph nodes and edges using
    pre-extracted LLM entities."""

    # Ensure project and subject nodes exist
    session.run(
        "MERGE (proj:Project {id: $pid}) "
        "SET proj.name = $pid",
        pid=project_id,
    )
    session.run(
        "MERGE (p:Person {id: $sid}) "
        "SET p.name = $name "
        "WITH p "
        "MERGE (proj:Project {id: $pid}) "
        "MERGE (p)-[:IN_PROJECT]->(proj)",
        sid=subject_id,
        name=subject_name,
        pid=project_id,
    )

    # If LLM returned nothing, store as generic fact node
    if not entities:
        session.run(
            "MERGE (f:FactNode {id: $fid}) "
            "SET f.claim = $claim, "
            "f.type = $type, "
            "f.state = $state "
            "WITH f "
            "MATCH (p:Person {id: $sid}) "
            "MERGE (p)-[:HAS_FACT]->(f)",
            fid=fact_id,
            claim=claim_text[:200],
            type=claim_type,
            state=state,
            sid=subject_id,
        )
        return

    # --- Persons ---
    # Build a map of person name -> person_id for edge creation
    person_map: dict[str, str] = {}
    for person in entities.get("persons", []):
        name = _normalize_person_name(person.get("name", ""))
        if not name or len(name) < 2:
            continue
        pid = _person_id(name)
        person_map[name] = pid
        session.run(
            "MERGE (p:Person {id: $pid}) "
            "SET p.name = $name",
            pid=pid,
            name=name,
        )
        # Connect to project
        session.run(
            "MATCH (p:Person {id: $pid}) "
            "MERGE (proj:Project {id: $projid}) "
            "MERGE (p)-[:IN_PROJECT]->(proj)",
            pid=pid,
            projid=project_id,
        )

    # Determine the "target" person for edges: for relationship
    # claims about secondary people, attach employment/education/
    # location/account data to the secondary person, not the subject.
    # If there's a non-subject person, use them as target.
    target_id = subject_id
    if claim_type == ClaimType.RELATIONSHIP:
        for person in entities.get("persons", []):
            name = _normalize_person_name(person.get("name", ""))
            role = person.get("role_in_claim", "other")
            if role != "subject" and name in person_map:
                target_id = person_map[name]
                break

    # --- Companies ---
    for company in entities.get("companies", []):
        raw_name = company.get("name", "")
        if not raw_name:
            continue
        co_name = _normalize_company(raw_name)
        role = company.get("role", "")
        _merge_edge(
            session, "Person", target_id, "id",
            "WORKS_AT", "Company", co_name, "name",
            fact_id, state, claim_text,
        )
        if role:
            session.run(
                "MATCH (p:Person {id: $pid})"
                "-[r:WORKS_AT]->"
                "(c:Company {name: $company}) "
                "SET r.role = $role",
                pid=target_id,
                company=co_name,
                role=role,
            )

    # --- Schools ---
    for school in entities.get("schools", []):
        school_name = school.get("name", "")
        if not school_name:
            continue
        _merge_edge(
            session, "Person", target_id, "id",
            "ATTENDED", "School", school_name, "name",
            fact_id, state, claim_text,
        )
        degree = school.get("degree", "")
        if degree:
            session.run(
                "MATCH (p:Person {id: $pid})"
                "-[r:ATTENDED]->"
                "(s:School {name: $school}) "
                "SET r.degree = $degree",
                pid=target_id,
                school=school_name,
                degree=degree,
            )

    # --- Locations ---
    for location in entities.get("locations", []):
        loc_name = location.get("name", "")
        if not loc_name:
            continue
        loc_name = _normalize_location(loc_name)
        _merge_edge(
            session, "Person", target_id, "id",
            "LOCATED_AT", "Location", loc_name, "name",
            fact_id, state, claim_text,
        )

    # --- Properties ---
    for prop in entities.get("properties", []):
        addr = prop.get("address", "")
        if not addr:
            continue
        session.run(
            "MERGE (pr:Property {address: $addr})",
            addr=addr,
        )
        price = prop.get("price", "")
        if price:
            session.run(
                "MATCH (pr:Property {address: $addr}) "
                "SET pr.price = $price",
                addr=addr,
                price=str(price),
            )
        _merge_edge(
            session, "Person", target_id, "id",
            "OWNS", "Property", addr, "address",
            fact_id, state, claim_text,
        )
        loc = prop.get("location", "")
        if loc:
            loc = _normalize_location(loc)
            session.run(
                "MERGE (l:Location {name: $loc})",
                loc=loc,
            )
            session.run(
                "MATCH (pr:Property {address: $addr}) "
                "MATCH (l:Location {name: $loc}) "
                "MERGE (pr)-[:LOCATED_IN]->(l)",
                addr=addr,
                loc=loc,
            )

    # --- Accounts ---
    for account in entities.get("accounts", []):
        platform = account.get("platform", "")
        identifier = account.get("identifier", "")
        if not platform:
            continue
        if not identifier:
            identifier = platform.lower()
        acct_id = f"{platform}-{identifier}".lower()
        session.run(
            "MERGE (a:Account {id: $aid}) "
            "SET a.platform = $platform, "
            "a.identifier = $ident, "
            "a.name = $platform + ': ' + $ident",
            aid=acct_id,
            platform=platform,
            ident=identifier,
        )
        _merge_edge(
            session, "Person", target_id, "id",
            "HAS_ACCOUNT", "Account", acct_id, "id",
            fact_id, state, claim_text,
        )

    # --- Relationships ---
    for rel in entities.get("relationships", []):
        from_name = _normalize_person_name(
            rel.get("from", "")
        )
        to_name = _normalize_person_name(
            rel.get("to", "")
        )
        rel_type = (
            rel.get("type", "RELATED_TO")
            .upper()
            .replace(" ", "_")
        )
        if not from_name or not to_name:
            continue

        from_id = _person_id(from_name)
        to_id = _person_id(to_name)

        # Ensure both person nodes exist
        session.run(
            "MERGE (p:Person {id: $pid}) "
            "SET p.name = $name",
            pid=from_id,
            name=from_name,
        )
        session.run(
            "MERGE (p:Person {id: $pid}) "
            "SET p.name = $name",
            pid=to_id,
            name=to_name,
        )
        _merge_rel(
            session, from_id, to_id,
            rel_type, fact_id, state,
        )


# -------------------------------------------------------------------
# Edge helpers
# -------------------------------------------------------------------


def _merge_edge(session, from_label, from_val, from_prop,
                rel_type, to_label, to_val, to_prop,
                fact_id, state, claim=""):
    """Create or update an edge with fact_id tracking."""
    session.run(
        f"MERGE (n:{to_label} {{{to_prop}: $to_val}})",
        to_val=to_val,
    )
    query = (
        f"MATCH (a:{from_label} {{{from_prop}: $from_val}}) "
        f"MATCH (b:{to_label} {{{to_prop}: $to_val}}) "
        f"MERGE (a)-[r:{rel_type}]->(b) "
        "SET r.fact_ids = CASE "
        "  WHEN r.fact_ids IS NULL THEN [$fid] "
        "  WHEN NOT $fid IN r.fact_ids "
        "  THEN r.fact_ids + $fid "
        "  ELSE r.fact_ids END, "
        "r.state = $state"
    )
    params = {
        "from_val": from_val,
        "to_val": to_val,
        "fid": fact_id,
        "state": state,
    }
    if claim:
        query += ", r.claim = $claim"
        params["claim"] = claim[:200]
    session.run(query, **params)


def _merge_rel(session, from_id, to_id, rel_type,
               fact_id, state):
    """Create relationship between two Person nodes."""
    query = (
        "MATCH (a:Person {id: $fid}) "
        "MATCH (b:Person {id: $tid}) "
        f"MERGE (a)-[r:{rel_type}]->(b) "
        "SET r.fact_ids = CASE "
        "  WHEN r.fact_ids IS NULL THEN [$factid] "
        "  WHEN NOT $factid IN r.fact_ids "
        "  THEN r.fact_ids + $factid "
        "  ELSE r.fact_ids END, "
        "r.state = $state"
    )
    session.run(
        query, fid=from_id, tid=to_id,
        factid=fact_id, state=state,
    )


# -------------------------------------------------------------------
# Cross-connections — the whole point of a graph
# -------------------------------------------------------------------


def _build_cross_connections(session, project_id):
    """Create inter-node edges derived from graph structure.

    All queries are scoped to the project via IN_PROJECT.
    No hardcoded names, companies, or family members.
    Everything is inferred from the data.
    """

    # 1. Company hierarchy — only if both companies exist
    # in this project's graph
    for child, parent in COMPANY_HIERARCHY.items():
        session.run(
            "MATCH (c:Company {name: $child})"
            "<-[:WORKS_AT]-(p:Person)"
            "-[:IN_PROJECT]->(:Project {id: $pid}) "
            "MATCH (par:Company {name: $parent})"
            "<-[:WORKS_AT]-(:Person)"
            "-[:IN_PROJECT]->(:Project {id: $pid}) "
            "MERGE (c)-[:SUBSIDIARY_OF]->(par)",
            child=child,
            parent=parent,
            pid=project_id,
        )

    # 2. Colleagues — people in THIS project at same company
    session.run(
        "MATCH (a:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (b:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (a)-[:WORKS_AT]->(c:Company)"
        "<-[:WORKS_AT]-(b) "
        "WHERE a.id < b.id "
        "MERGE (a)-[:COLLEAGUE {via: c.name}]->(b)",
        pid=project_id,
    )

    # 3. Co-owners of same property (in this project)
    session.run(
        "MATCH (a:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (a)-[:OWNS]->(pr:Property)"
        "<-[:OWNS]-(b:Person) "
        "WHERE a.id < b.id "
        "MERGE (a)-[:CO_OWNER]->(b)",
        pid=project_id,
    )

    # 4. School -> Location (within project scope)
    session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (p)-[:ATTENDED]->(s:School) "
        "MATCH (p)-[:LOCATED_AT]->(l:Location) "
        "WHERE s.name CONTAINS l.name "
        "OR l.name CONTAINS "
        "split(s.name, ' ')[0] "
        "MERGE (s)-[:LOCATED_IN]->(l)",
        pid=project_id,
    )

    # 5. Co-located people (in this project)
    session.run(
        "MATCH (a:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (b:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (a)-[:LOCATED_AT]->(l:Location)"
        "<-[:LOCATED_AT]-(b) "
        "WHERE a.id < b.id "
        "MERGE (a)-[:CO_LOCATED {at: l.name}]->(b)",
        pid=project_id,
    )

    # 6. Family clustering — auto-detect shared last names
    # among persons in this project and create Family nodes
    session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "WHERE p.name CONTAINS ' ' "
        "WITH p, split(p.name, ' ')[-1] AS surname "
        "WITH surname, collect(p) AS members "
        "WHERE size(members) >= 2 "
        "MERGE (f:Family {name: surname}) "
        "WITH f, members "
        "UNWIND members AS m "
        "MERGE (m)-[:MEMBER_OF]->(f)",
        pid=project_id,
    )

    # 7. Career timeline — order companies by employment
    # date extracted from fact claims (look for year patterns)
    session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (p)-[r:WORKS_AT]->(c:Company) "
        "WHERE r.claim IS NOT NULL "
        "WITH c, r.claim AS claim "
        "WITH c, "
        "toInteger(head("
        "[x IN split(claim, ' ') "
        "WHERE x =~ '\\\\d{4}' "
        "AND toInteger(x) > 1990 "
        "AND toInteger(x) < 2030]"
        ")) AS start_year "
        "WHERE start_year IS NOT NULL "
        "WITH c, start_year ORDER BY start_year "
        "WITH collect({company: c, year: start_year}) "
        "AS career "
        "UNWIND range(0, size(career)-2) AS i "
        "WITH career[i].company AS prev, "
        "career[i+1].company AS next "
        "MERGE (prev)-[:NEXT_ROLE]->(next)",
        pid=project_id,
    )

    # 8. People who own property are located there
    session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (p)-[:OWNS]->(pr:Property)"
        "-[:LOCATED_IN]->(l:Location) "
        "MERGE (p)-[:LOCATED_AT]->(l)",
        pid=project_id,
    )


# -------------------------------------------------------------------
# Person node merging
# -------------------------------------------------------------------


def _merge_person_nodes(session, old_id: str, keep_id: str):
    """Merge old Person node into keep. Move all edges."""
    edge_types = [
        "WORKS_AT", "LOCATED_AT", "ATTENDED",
        "HAS_ACCOUNT", "OWNS", "MEMBER_OF",
        "MARRIED_TO", "CHILD_OF", "FAMILY",
        "HAS_FACT", "RELATED_TO", "COLLEAGUE",
        "CO_OWNER", "CO_LOCATED",
    ]
    # Move outgoing edges by type
    for rt in edge_types:
        session.run(
            f"MATCH (old:Person {{id: $old_id}})"
            f"-[r:{rt}]->(n) "
            f"MATCH (keep:Person {{id: $keep_id}}) "
            f"MERGE (keep)-[:{rt}]->(n)",
            old_id=old_id,
            keep_id=keep_id,
        )
    # Move incoming edges by type
    for rt in edge_types:
        session.run(
            f"MATCH (n)-[r:{rt}]->"
            f"(old:Person {{id: $old_id}}) "
            f"MATCH (keep:Person {{id: $keep_id}}) "
            f"MERGE (n)-[:{rt}]->(keep)",
            old_id=old_id,
            keep_id=keep_id,
        )
    # Delete old node
    session.run(
        "MATCH (p:Person {id: $pid}) DETACH DELETE p",
        pid=old_id,
    )


# -------------------------------------------------------------------
# LLM-powered node deduplication (all types)
# -------------------------------------------------------------------

_DEDUP_PROMPT = """\
You are a deduplication engine for an OSINT knowledge graph.
Given a list of names that may contain duplicates or variants
of the same entity, group them.

Return a JSON array of groups. Each group is an array of names
that refer to the same entity. Only include groups with 2+
members. Single names with no duplicates should be omitted.

For each group, the FIRST name should be the most complete/
canonical form.

Examples:
Input: ["100 Example Street, Apt D", "100 Example St, Apt D", \
"100 Example Street D, Anytown, ST 00000", "200 Other Ave"]
Output: [["100 Example Street, Apt D", \
"100 Example St, Apt D", \
"100 Example Street D, Anytown, ST 00000"]]

Input: ["First", "First Last1", "First Middle Last2", \
"First Middle Last1 Last2", "Other Last2", "Another Last2"]
Output: [["First Middle Last1 Last2", "First Middle Last2", \
"First Last1", "First"]]

Return ONLY the JSON array, no other text.\
"""


async def _dedup_names_llm(
    names: list[str],
) -> list[list[str]]:
    """Use LLM to find duplicate names in a list."""
    if not names or len(names) < 2:
        return []

    prompt = "\n".join(names)

    if settings.ollama_url:
        try:
            url = (
                f"{settings.ollama_url.rstrip('/')}"
                "/api/chat"
            )
            payload = {
                "model": settings.ollama_extraction_model,
                "messages": [
                    {
                        "role": "system",
                        "content": _DEDUP_PROMPT,
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0},
            }
            async with httpx.AsyncClient(
                timeout=60.0
            ) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = (
                    data.get("message", {})
                    .get("content", "")
                )
                result = _parse_llm_json(content)
                # Result should be a list of lists
                if isinstance(result, list):
                    return result
                # Sometimes LLM wraps in {"groups": [...]}
                if isinstance(result, dict):
                    for v in result.values():
                        if isinstance(v, list):
                            return v
                return []
        except Exception:
            logger.exception("LLM dedup failed")
            return []
    return []


def _merge_generic_nodes(
    session, label: str, keep_name: str, old_name: str,
):
    """Merge two nodes of the same label type.

    Move all edges from old to keep, then delete old.
    """
    # Get all relationship types touching this label
    result = session.run(
        f"MATCH (n:{label} {{name: $old}})-[r]-() "
        "RETURN DISTINCT type(r) AS rt",
        old=old_name,
    )
    rel_types = [rec["rt"] for rec in result]

    for rt in rel_types:
        # Outgoing
        session.run(
            f"MATCH (old:{label} {{name: $old}})"
            f"-[:{rt}]->(n) "
            f"MERGE (keep:{label} {{name: $keep}}) "
            f"MERGE (keep)-[:{rt}]->(n)",
            old=old_name,
            keep=keep_name,
        )
        # Incoming
        session.run(
            f"MATCH (n)-[:{rt}]->"
            f"(old:{label} {{name: $old}}) "
            f"MERGE (keep:{label} {{name: $keep}}) "
            f"MERGE (n)-[:{rt}]->(keep)",
            old=old_name,
            keep=keep_name,
        )

    # Delete old
    session.run(
        f"MATCH (n:{label} {{name: $old}}) "
        "DETACH DELETE n",
        old=old_name,
    )


async def _run_llm_dedup(
    session, project_id: str,
) -> list[str]:
    """LLM-powered deduplication for ALL node types.

    Queries all nodes per type, sends names to LLM,
    applies merge groups. Returns list of issues.
    """
    issues = []

    # Node types and how to query them in project scope
    type_queries = {
        "Person": (
            "MATCH (n:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}) "
            "RETURN n.id AS id, n.name AS name"
        ),
        "Company": (
            "MATCH (:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}), "
            "(:Person)-[:WORKS_AT]->(n:Company) "
            "RETURN DISTINCT n.name AS name"
        ),
        "Location": (
            "MATCH (:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}), "
            "(:Person)-[:LOCATED_AT]->(n:Location) "
            "RETURN DISTINCT n.name AS name "
            "UNION "
            "MATCH (:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}), "
            "(:Person)-[:OWNS]->()-[:LOCATED_IN]->"
            "(n:Location) "
            "RETURN DISTINCT n.name AS name"
        ),
        "School": (
            "MATCH (:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}), "
            "(:Person)-[:ATTENDED]->(n:School) "
            "RETURN DISTINCT n.name AS name"
        ),
        "Property": (
            "MATCH (:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}), "
            "(:Person)-[:OWNS]->(n:Property) "
            "RETURN DISTINCT n.address AS name"
        ),
        "Account": (
            "MATCH (:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}), "
            "(:Person)-[:HAS_ACCOUNT]->(n:Account) "
            "RETURN DISTINCT n.name AS name"
        ),
    }

    for label, query in type_queries.items():
        result = session.run(query, pid=project_id)
        nodes: list[tuple[str, str]] = []
        if label == "Person":
            nodes = [
                (rec["id"], rec["name"])
                for rec in result
                if rec["name"]
            ]
            names = [n for _, n in nodes]
        else:
            names = [
                rec["name"]
                for rec in result
                if rec["name"]
            ]

        if len(names) < 2:
            continue

        # Ask LLM to find duplicates
        raw_groups = await _dedup_names_llm(names)

        # Normalize: ensure groups is list of lists of strings
        # LLM might return flat list, nested list, or mixed
        groups = []
        for g in raw_groups:
            if isinstance(g, list) and all(
                isinstance(x, str) for x in g
            ):
                # Proper group: ["name1", "name2"]
                # Only keep names that exist in our names list
                valid = [x for x in g if x in names]
                if len(valid) >= 2:
                    groups.append(valid)
            elif isinstance(g, str) and g in names:
                # Flat list — the whole raw_groups is one group
                valid = [
                    x for x in raw_groups
                    if isinstance(x, str) and x in names
                ]
                if len(valid) >= 2:
                    groups = [valid]
                break

        for group in groups:
            if len(group) < 2:
                continue
            canonical = group[0]  # LLM returns best first
            for duplicate in group[1:]:
                if label == "Person":
                    # Find IDs for person merge
                    keep_id = None
                    dup_id = None
                    for pid, pname in nodes:
                        if pname == canonical:
                            keep_id = pid
                        if pname == duplicate:
                            dup_id = pid
                    if keep_id and dup_id:
                        _merge_person_nodes(
                            session, dup_id, keep_id
                        )
                else:
                    _merge_generic_nodes(
                        session, label,
                        canonical, duplicate,
                    )
                issues.append(
                    f"Dedup {label}: "
                    f"'{duplicate}' -> '{canonical}'"
                )

    return issues


# -------------------------------------------------------------------
# Validation — runs after every rebuild
# -------------------------------------------------------------------


def _validate_and_clean(
    session, project_id: str
) -> list[str]:
    """Validate graph integrity and fix issues.

    ALL operations are scoped to the given project.
    Returns list of issues found and fixed.
    """
    issues = []

    # 1. Remove garbage Person nodes (job titles, fragments)
    garbage_patterns = [
        "Director", "Engineer", "Manager", "Architect",
        "Counselor", "Advisor", "President", "Officer",
        "Specialist", "Lead ", "Senior ", "Sr ",
        "VP ", "SVP ", "CTO", "CEO",
        "aspects of", "development of", "in the early",
        "who found", "gallon tank", "turtle",
        "Community College", "Apple Pay",
    ]
    result = session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "RETURN p.id AS id, p.name AS name",
        pid=project_id,
    )
    for rec in result:
        name = rec["name"] or ""
        pid = rec["id"] or ""
        is_garbage = False

        # Check patterns
        for pat in garbage_patterns:
            if pat.lower() in name.lower():
                is_garbage = True
                break

        # Check if name looks like a sentence (>4 words)
        if len(name.split()) > 4:
            is_garbage = True

        # Check if name has no capital letters (not a name)
        if name and not any(c.isupper() for c in name):
            is_garbage = True

        # Check prefixes that should have been normalized
        prefixes = [
            "wife ", "likely ", "named ", "has a ",
        ]
        for prefix in prefixes:
            if name.lower().startswith(prefix):
                is_garbage = True
                break

        if is_garbage:
            session.run(
                "MATCH (p:Person {id: $pid}) DETACH DELETE p",
                pid=pid,
            )
            issues.append(
                f"Removed garbage Person: '{name}'"
            )

    # 2. Normalize person names — strip prefixes
    result = session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "RETURN p.id AS id, p.name AS name",
        pid=project_id,
    )
    for rec in result:
        name = rec["name"] or ""
        normalized = _normalize_person_name(name)
        if normalized != name:
            session.run(
                "MATCH (p:Person {id: $pid}) "
                "SET p.name = $name",
                pid=rec["id"],
                name=normalized,
            )
            issues.append(
                f"Normalized Person: '{name}' -> '{normalized}'"
            )

    # 3. Merge duplicate Person nodes
    # First pass: exact name match
    # Second pass: name-overlap merge (handles
    # Apply pre-computed merge groups (from LLM dedup)
    # These are passed in via the dedup_groups parameter
    # handled by _apply_dedup_groups() called separately

    # 4. Remove garbage Company nodes (only those reachable
    #    from this project's persons)
    result = session.run(
        "MATCH (:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (:Person)-[:WORKS_AT]->(c:Company) "
        "WITH DISTINCT c "
        "RETURN c.name AS name",
        pid=project_id,
    )
    for rec in result:
        name = rec["name"] or ""
        if len(name) > 40 or "aspects" in name.lower():
            session.run(
                "MATCH (c:Company {name: $name}) "
                "DETACH DELETE c",
                name=name,
            )
            issues.append(
                f"Removed garbage Company: '{name}'"
            )

    # 5. Validate company names against alias table
    #    (only companies reachable from this project)
    result = session.run(
        "MATCH (:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (:Person)-[:WORKS_AT]->(c:Company) "
        "WITH DISTINCT c "
        "RETURN c.name AS name",
        pid=project_id,
    )
    for rec in result:
        name = rec["name"] or ""
        canonical = _normalize_company(name)
        if canonical != name:
            # Check if canonical already exists
            existing = session.run(
                "MATCH (c:Company {name: $name}) "
                "RETURN count(c) AS cnt",
                name=canonical,
            ).single()["cnt"]
            if existing > 0:
                # Merge edges to canonical, delete duplicate
                session.run(
                    "MATCH (old:Company {name: $old_name})"
                    "<-[r]-(p) "
                    "MATCH (new:Company {name: $new_name}) "
                    "MERGE (p)-[:WORKS_AT]->(new)",
                    old_name=name,
                    new_name=canonical,
                )
                session.run(
                    "MATCH (c:Company {name: $name}) "
                    "DETACH DELETE c",
                    name=name,
                )
            else:
                session.run(
                    "MATCH (c:Company {name: $name}) "
                    "SET c.name = $canonical",
                    name=name,
                    canonical=canonical,
                )
            issues.append(
                f"Normalized Company: '{name}'"
                f" -> '{canonical}'"
            )

    # 6. Normalize location names (only locations reachable
    #    from this project's persons)
    result = session.run(
        "MATCH (:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (:Person)-[:LOCATED_AT]->(l:Location) "
        "WITH DISTINCT l "
        "RETURN l.name AS name",
        pid=project_id,
    )
    for rec in result:
        name = rec["name"] or ""
        canonical = _normalize_location(name)
        if canonical != name:
            existing = session.run(
                "MATCH (l:Location {name: $name}) "
                "RETURN count(l) AS cnt",
                name=canonical,
            ).single()["cnt"]
            if existing > 0:
                # Merge edges
                session.run(
                    "MATCH (old:Location {name: $old})"
                    "<-[r]-(n) "
                    "MATCH (new:Location {name: $new}) "
                    "WITH n, new, type(r) AS rt "
                    "FOREACH (_ IN CASE WHEN rt = "
                    "'LOCATED_AT' THEN [1] ELSE [] END | "
                    "  MERGE (n)-[:LOCATED_AT]->(new)) "
                    "FOREACH (_ IN CASE WHEN rt = "
                    "'LOCATED_IN' THEN [1] ELSE [] END | "
                    "  MERGE (n)-[:LOCATED_IN]->(new))",
                    old=name,
                    new=canonical,
                )
                session.run(
                    "MATCH (l:Location {name: $name}) "
                    "DETACH DELETE l",
                    name=name,
                )
            else:
                session.run(
                    "MATCH (l:Location {name: $name}) "
                    "SET l.name = $canonical",
                    name=name,
                    canonical=canonical,
                )
            issues.append(
                f"Normalized Location: '{name}'"
                f" -> '{canonical}'"
            )

    # 7. Property should have exactly one LOCATED_IN
    #    (only properties owned by persons in this project)
    result = session.run(
        "MATCH (:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (:Person)-[:OWNS]->(pr:Property)"
        "-[:LOCATED_IN]->(l:Location) "
        "WITH pr, collect(l.name) AS locations "
        "WHERE size(locations) > 1 "
        "RETURN pr.address AS addr, locations",
        pid=project_id,
    )
    for rec in result:
        addr = rec["addr"]
        locs = rec["locations"]
        issues.append(
            f"Property '{addr}' linked to multiple "
            f"locations: {locs} -- needs manual review"
        )

    # 8. Remove self-edges (on project persons only)
    result = session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (p)-[r]->(p) "
        "RETURN count(r) AS cnt",
        pid=project_id,
    )
    cnt = result.single()["cnt"]
    if cnt > 0:
        session.run(
            "MATCH (p:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}) "
            "MATCH (p)-[r]->(p) DELETE r",
            pid=project_id,
        )
        issues.append(f"Removed {cnt} self-edges")

    # 9. Remove orphan Person nodes in this project
    #    (no edges except IN_PROJECT)
    result = session.run(
        "MATCH (p:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "WHERE NOT EXISTS { (p)--() "
        "WHERE NOT ()-[:IN_PROJECT]->() } "
        "RETURN p.name AS name",
        pid=project_id,
    )
    for rec in result:
        issues.append(
            f"Removed orphan Person: '{rec['name']}'"
        )
    session.run(
        "MATCH (p:Person)-[ip:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "WHERE size([(p)-->(x) "
        "WHERE NOT x:Project | x]) = 0 "
        "AND size([(p)<--(y) | y]) = 0 "
        "DETACH DELETE p",
        pid=project_id,
    )

    # Remove orphan non-Person nodes that lost all edges
    # (global orphan cleanup is safe — no edges means
    #  they belong to no project)
    for label in [
        "Company", "Location", "School",
        "Account", "Property", "Family", "FactNode",
    ]:
        result = session.run(
            f"MATCH (n:{label}) "
            "WHERE NOT EXISTS { (n)--() } "
            f"RETURN n.name AS name, count(*) AS cnt"
        )
        for rec in result:
            issues.append(
                f"Removed orphan {label}: '{rec['name']}'"
            )
        session.run(
            f"MATCH (n:{label}) "
            "WHERE NOT EXISTS { (n)--() } "
            "DELETE n"
        )

    # 10. Verbose location names (>50 chars) — likely
    # parsed from claim text, not a real location
    # (only locations reachable from this project)
    result = session.run(
        "MATCH (:Person)-[:IN_PROJECT]->"
        "(:Project {id: $pid}) "
        "MATCH (:Person)-[:LOCATED_AT]->(l:Location) "
        "WHERE size(l.name) > 50 "
        "WITH DISTINCT l "
        "RETURN l.name AS name",
        pid=project_id,
    )
    for rec in result:
        session.run(
            "MATCH (l:Location {name: $name}) "
            "DETACH DELETE l",
            name=rec["name"],
        )
        issues.append(
            f"Removed verbose Location: "
            f"'{rec['name'][:50]}...'"
        )

    return issues


# -------------------------------------------------------------------
# Entity cache — avoid re-calling LLM on every rebuild
# -------------------------------------------------------------------

_entity_cache: dict[str, dict] = {}


def _load_entity_cache(workspace_path: str | None) -> None:
    """Load cached entities from project workspace JSON file."""
    global _entity_cache
    if not workspace_path:
        _entity_cache = {}
        return
    import pathlib
    cache_path = pathlib.Path(workspace_path) / "entity_cache.json"
    if cache_path.exists():
        try:
            _entity_cache = json.loads(cache_path.read_text())
            logger.info(
                "Loaded %d cached entities from %s",
                len(_entity_cache), cache_path,
            )
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load entity cache")
            _entity_cache = {}
    else:
        _entity_cache = {}


def _save_entity_cache(workspace_path: str | None) -> None:
    """Save entity cache to project workspace JSON file."""
    if not workspace_path or not _entity_cache:
        return
    import pathlib
    cache_path = pathlib.Path(workspace_path) / "entity_cache.json"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(_entity_cache, indent=2))
        logger.info(
            "Saved %d cached entities to %s",
            len(_entity_cache), cache_path,
        )
    except OSError:
        logger.warning("Failed to save entity cache")


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------


def add_fact_to_graph(
    project_id: str,
    subject_id: str,
    subject_name: str,
    fact_id: str,
    claim_text: str,
    claim_type: str,
    source_url: str = "",
    state: str = "confirmed",
):
    """Add a single fact to the graph (sync wrapper).

    Uses cached entities if available, otherwise stores as
    a generic FactNode (LLM extraction happens during rebuild).
    """
    entities = _entity_cache.get(fact_id, {})
    driver = _get_driver()
    with driver.session() as session:
        _add_fact_from_entities(
            session, project_id, subject_id,
            subject_name, fact_id, claim_text,
            claim_type, source_url, state, entities,
        )
    driver.close()


def remove_fact_from_graph(fact_id: str):
    """Remove a fact_id from all edges. Prune empties."""
    driver = _get_driver()
    with driver.session() as session:
        session.run(
            "MATCH ()-[r]-() "
            "WHERE r.fact_ids IS NOT NULL "
            "AND $fid IN r.fact_ids "
            "SET r.fact_ids = "
            "[x IN r.fact_ids WHERE x <> $fid]",
            fid=fact_id,
        )
        session.run(
            "MATCH ()-[r]-() "
            "WHERE r.fact_ids IS NOT NULL "
            "AND size(r.fact_ids) = 0 "
            "DELETE r"
        )
        session.run(
            "MATCH (f:FactNode {id: $fid}) "
            "DETACH DELETE f",
            fid=fact_id,
        )
        for label in [
            "Company", "Location", "School",
            "Account", "Property",
        ]:
            session.run(
                f"MATCH (n:{label}) "
                "WHERE NOT EXISTS { (n)--() } "
                "DELETE n"
            )
    driver.close()


def update_edge_state(fact_id: str, new_state: str):
    """Update state on edges containing this fact_id."""
    driver = _get_driver()
    with driver.session() as session:
        session.run(
            "MATCH ()-[r]-() "
            "WHERE r.fact_ids IS NOT NULL "
            "AND $fid IN r.fact_ids "
            "SET r.state = $state",
            fid=fact_id,
            state=new_state,
        )
        session.run(
            "MATCH (f:FactNode {id: $fid}) "
            "SET f.state = $state",
            fid=fact_id,
            state=new_state,
        )
    driver.close()


def clear_project_graph(project_id: str):
    """Wipe entire graph for a project."""
    driver = _get_driver()
    with driver.session() as session:
        # Delete everything connected to project
        session.run(
            "MATCH (p:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}) "
            "OPTIONAL MATCH (p)--(n) "
            "WHERE NOT n:Project "
            "DETACH DELETE n",
            pid=project_id,
        )
        session.run(
            "MATCH (p:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}) "
            "DETACH DELETE p",
            pid=project_id,
        )
        session.run(
            "MATCH (proj:Project {id: $pid}) "
            "DELETE proj",
            pid=project_id,
        )
        # Clean orphans
        for label in [
            "Company", "Location", "School",
            "Account", "Property", "Family",
            "FactNode",
        ]:
            session.run(
                f"MATCH (n:{label}) "
                "WHERE NOT EXISTS { (n)--() } "
                "DELETE n"
            )
    driver.close()


async def rebuild_project_graph(project_id: str):
    """Full rebuild from Postgres confirmed/likely facts.

    Three-phase approach:
    1. Load facts from Postgres (async)
    2. Extract entities via LLM for all facts (async, cached)
    3. Build graph from structured entities (sync)
    """
    import asyncio

    from sqlalchemy import select

    from daisy.mcp_server import _session_factory
    from daisy.models.fact import Fact
    from daisy.models.project import Project
    from daisy.models.subject import Subject

    clear_project_graph(project_id)
    ensure_schema()

    pid = uuid_mod.UUID(project_id)

    # --- Phase 1: Load from Postgres ---
    workspace_path = None
    async with _session_factory() as session:
        # Get project workspace path for cache
        proj = (await session.execute(
            select(Project).where(Project.id == pid)
        )).scalar_one_or_none()
        if proj and hasattr(proj, "workspace_path"):
            workspace_path = proj.workspace_path

        subjects = {}
        result = await session.execute(
            select(Subject).where(Subject.project_id == pid)
        )
        for s in result.scalars().all():
            subjects[str(s.id)] = s.name

        result = await session.execute(
            select(Fact).where(
                Fact.project_id == pid,
                Fact.lifecycle_state.in_([
                    LifecycleState.CONFIRMED,
                    LifecycleState.LIKELY,
                ]),
            )
        )
        facts = result.scalars().all()

    # --- Phase 2: LLM extraction (async, with caching) ---
    _load_entity_cache(workspace_path)

    # Identify facts that need LLM extraction
    facts_needing_extraction = [
        f for f in facts
        if str(f.id) not in _entity_cache
    ]

    if facts_needing_extraction:
        logger.info(
            "Extracting entities for %d facts "
            "(%d cached, %d total)",
            len(facts_needing_extraction),
            len(facts) - len(facts_needing_extraction),
            len(facts),
        )

        # Run LLM extraction concurrently (batched to avoid
        # overwhelming the LLM server)
        batch_size = 5
        for i in range(
            0, len(facts_needing_extraction), batch_size
        ):
            batch = facts_needing_extraction[i:i + batch_size]
            tasks = [
                _extract_entities_llm(
                    f.claim_text, f.claim_type,
                )
                for f in batch
            ]
            results = await asyncio.gather(*tasks)
            for fact, entities in zip(batch, results):
                _entity_cache[str(fact.id)] = entities

        # Save cache after extraction
        _save_entity_cache(workspace_path)

    # --- Phase 3: Build graph (sync) ---
    driver = _get_driver()
    with driver.session() as neo_session:
        for fact in facts:
            sid = (
                str(fact.subject_id)
                if fact.subject_id else None
            )
            if not sid or sid not in subjects:
                continue
            entities = _entity_cache.get(str(fact.id), {})
            _add_fact_from_entities(
                neo_session, project_id, sid,
                subjects[sid], str(fact.id),
                fact.claim_text, fact.claim_type,
                fact.source_url, fact.lifecycle_state,
                entities,
            )

        # Second pass: build cross-connections
        _build_cross_connections(neo_session, project_id)

        # Third pass: validate and clean
        issues = _validate_and_clean(neo_session, project_id)

        # Fourth pass: LLM-powered dedup (all node types)
        dedup_issues = await _run_llm_dedup(
            neo_session, project_id
        )
        issues.extend(dedup_issues)

    driver.close()

    if issues:
        print(f"Validation fixed {len(issues)} issues:")
        for issue in issues:
            print(f"  - {issue}")
    return len(facts)


# -------------------------------------------------------------------
# Query helpers
# -------------------------------------------------------------------


def get_project_graph_summary(project_id: str) -> dict:
    """Return relationship type counts scoped to a project."""
    driver = _get_driver()
    with driver.session() as session:
        # Count relationships on persons in this project
        result = session.run(
            "MATCH (p:Person)-[:IN_PROJECT]->"
            "(:Project {id: $pid}) "
            "MATCH (p)-[r]->() "
            "WHERE NOT type(r) = 'IN_PROJECT' "
            "RETURN type(r) AS rel_type, "
            "count(*) AS count "
            "ORDER BY count DESC",
            pid=project_id,
        )
        summary = {}
        for record in result:
            summary[record["rel_type"]] = record["count"]
    driver.close()
    return summary


def get_person_connections(subject_id: str) -> list[dict]:
    driver = _get_driver()
    connections = []
    with driver.session() as session:
        result = session.run(
            "MATCH (p:Person {id: $sid})-[r]->(n) "
            "WHERE NOT n:Project "
            "RETURN type(r) AS rel, "
            "labels(n)[0] AS target_type, "
            "COALESCE(n.name, n.id, n.address) "
            "AS target_name, "
            "r.state AS state, "
            "r.fact_ids AS fact_ids, "
            "r.claim AS claim, "
            "r.role AS role",
            sid=subject_id,
        )
        for record in result:
            connections.append({
                "relationship": record["rel"],
                "target_type": record["target_type"],
                "target_name": record["target_name"],
                "state": record["state"],
                "fact_count": (
                    len(record["fact_ids"])
                    if record["fact_ids"]
                    else 0
                ),
                "claim": record["claim"],
                "role": record["role"],
            })
    driver.close()
    return connections
