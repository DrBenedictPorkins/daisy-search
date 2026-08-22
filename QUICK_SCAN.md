# Quick Scan — no-DB triage mode

A lightweight lookup to **confirm or characterize a lead**, not build a case file.
No persistence, no project overhead. Results are displayed, not stored.

## When to use
- A single lead you just want to size up (a phone, an email, a name + location).
- "Is this person who they say they are?" / "what's attached to this number?"
- Anything where creating a project would be more work than the answer is worth.

Use the **full workflow** instead when the target warrants an ongoing, sourced case file.

## Hard rules
- **No database writes.** Do NOT call `daisy_new_project`, `daisy_intake`, `daisy_add_fact`,
  `daisy_confirm`, `daisy_report`, or `daisy_build_graph`.
- **No project switch.** Leave the active project untouched.
- **Display only** — output to a Markdown table or `/canvas`. Nothing is saved to Postgres.
- **Sources:** government/public records + LeakCheck only. Never BeenVerified / Spokeo /
  Radaris / TruePeopleSearch-style paywall aggregators.

## Steps
1. **Pick the strongest handle.** Signal order: phone ≈ email > username > name + location.
2. **LeakCheck it** (breach/identity DB — the fastest confirmation):
   ```
   source ~/.secrets/api_keys.sh && scripts/leakcheck.sh "<digits-only-phone-or-email>"
   ```
   Phones: digits only, no dashes or `+1`. Multiple in one call, space-separated.
3. **One web pass (optional):** a single targeted `daisy_search`; read only the top 1–2 hits
   with crawl4ai `fetch_clean`. Stop there — no query sweep.
4. **Display the result** (see format below). Do not persist.

## Output format (Markdown)

**Subject:** `<name / handle>`  ·  **Scanned:** `<what you looked up>`

| Handle | Source | Finding | Confidence |
|--------|--------|---------|-----------|
| 212-555-0142 | LeakCheck | name / email / address returned (or "no hits") | high / med / low |
| … | daisy_search + fetch | … | … |

**Verdict:** one line — corroborated / partial / nothing found / conflicting.

Keep it to what's concretely returned. No speculation, no report, no DB.
