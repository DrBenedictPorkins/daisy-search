---
description: Start a new Daisy investigation — creates the project, scaffolds the workspace, and runs intake
---

Start a new Daisy Search investigation.

## Rules

- **Do not explain the process.** Do not describe phases, print a plan, or summarise
  the methodology. Ask for what you need and get moving.
- If the user's message already contains a name, use it. Otherwise ask for the
  subject's name and any seed details in **one** short prompt — do not ask twice.
- Public-access OSINT only: public records, published reporting, breach-aggregator
  data. An auth wall is a stopping point, not an obstacle to route around.
- Never research or identify a subject's minor children.

## Steps

1. If `$ARGUMENTS` is empty, ask once:

   > Name, and anything you already have — city/state, employer, usernames, emails, phone?

   Seed details are optional; a bare name is enough to start.

2. Call `daisy_new_project` with the subject's name as the project name and a one-line
   description. This creates the database row **and** scaffolds the case workspace
   (`CLAUDE.md`, `notes/SESSION_STATE.md`, `.claude/settings.local.json`, and the
   `notes/ downloads/ screenshots/ reports/ raw/` tree). It also sets the active project.

3. Call `daisy_intake` with the name plus every seed detail the user gave —
   `locations`, `occupations`, `known_usernames`, `known_emails`, `known_facts`, `notes`.
   Intake generates the name variants, username guesses, and the initial query set.

4. If the user supplied a phone or email, run LeakCheck on it first — it is the
   fastest confirmation available:

   ```
   source ~/.secrets/api_keys.sh && scripts/leakcheck.sh "<email-or-digits-only-phone>"
   ```

5. Read the methodology resource at `daisy://methodology` for the step groups, then
   begin. Record every finding with `daisy_add_fact` — sourced, scored, and tied to
   the fetch that produced it.

6. Report back with **concrete hits only**: what was found, from which source, at what
   confidence. No preamble, no summary of what you are about to do.

## If they just want a quick look

If the user only wants a single lead sized up rather than a case file, do not create a
project — follow `QUICK_SCAN.md` instead (no database writes, display only).
