# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/DrBenedictPorkins/daisy-search/security/advisories/new)
rather than opening a public issue.

Include what you did, what happened, and what you expected. A first response should
arrive within a week. Please give a reasonable window for a fix before disclosing publicly.

## What counts as a security issue here

Daisy handles sensitive personal data about third parties, so the threat model is wider
than "can it be exploited remotely":

- **Data leakage** — investigation data escaping the local stack: written outside
  `PROJECTS_DIR`, sent to an unintended third-party service, or logged in plaintext.
- **Credential exposure** — API keys read from or written to files that are not
  git-ignored, or echoed into tool output, logs, or the agent's context.
- **Audit integrity** — anything that lets a fetch be recorded as `fetched` when it was
  not actually retrieved, or lets an immutable fact be silently rewritten. The fetch audit
  and the fact log are the trust boundary; corrupting them is a security bug, not a cosmetic one.
- **Scope escape** — a code path that defeats authentication, evades an access control,
  or uses recovered credentials. See below.

## Scope boundary (not negotiable)

Daisy is a **public-access** OSINT tool. It draws on public records, published reporting,
and breach-aggregator data. It does not, and will not:

- defeat authentication or evade access controls,
- use credentials recovered from breach data to access any account,
- scrape sources whose terms forbid it as a way of routing around a paywall,
- assist surveillance, stalking, harassment, or the identification of minors.

An auth wall is a **stopping point**, recorded as `auth_wall` in the fetch log. Feature
requests to bypass one will be declined. Browser integrations exist for JS-heavy public
pages and for sessions the operator is already legitimately logged into — never as a bypass.

## Operator responsibilities

- Keep `.env` and `~/.secrets/` out of version control. Daisy never commits them; verify anyway.
- `projects/` and `searches/` hold real personal data and are git-ignored by default.
  Do not remove those entries.
- You are responsible for complying with the laws and platform terms in your jurisdiction.
