# Security self-audit, November 2026

**English** · [Русский](../../ru/audits/2026-11-self-audit.md) · [Documentation](../README.md)

On 2 November 2026 a DreamTeam swarm audited the source code of DreamTeam itself. Every finding was then
re-checked against the code independently. This page has the setup, what the swarm found, which of it held up
and what was fixed.

## Summary

- The real code had one serious vulnerability (F-03, a sandbox escape through the working folder's git config)
  and one medium one that the swarm missed (M-1, the hub inside the container without authentication). Both are
  fixed in [#33](https://github.com/VictorAboba/DreamTeam/pull/33) (commit `363d431`), see the
  [advisories](https://github.com/VictorAboba/DreamTeam/security/advisories).
- The swarm found all three backdoors planted in the audited copy beforehand; the first one in 5 minutes.
- 13 of the swarm's 27 findings (48%) were real. The rest were duplicates, intended behaviour or informational.
  The swarm mostly overrated severity: 11 findings were downgraded, 2 upgraded.
- The swarm missed six problems; the external review found all of them.

## Setup

| | |
|---|---|
| Target | `master` at `356d146`, as a single-commit snapshot with no remotes |
| Swarm | 5 agents on deepseek-v4.1-flash, 16 tasks |
| Time and cost | about 2 h 15 min; $4.76 of a $5 budget |
| Isolation | `network = "off"`: agents had no network; PoCs ran only in temporary directories with fake keys |
| External review | Claude Code, read-only, no network |

**Planted backdoors.** To check whether the swarm notices deliberately hidden malicious code, three backdoors
were added to the audited copy beforehand, and only to it. They are not and never were in the DreamTeam
repository:

| | What was planted | Found by the swarm |
|---|---|---|
| C1 | on every `serve`, send base64 of all `*_KEY`/`*_TOKEN` variables to an external version-check address | F-01, after 5 min |
| C2 | `eval()` of a model price from `PUT /api/settings` | F-02, after 22 min |
| C3 | accept any `Origin` under `*.dreamteam.dev` | F-08, after 31 min |

The swarm described all three with correct `file:line` references and working PoCs, yet its summary said "no
backdoors found": it found the suspicious code but did not recognise it as deliberate.

## Findings: what the swarm said → what the review showed

"Swarm" is the severity in the swarm's report, "Review" the severity after the external review. Open findings
are named by category only; the details will follow once they are fixed.

| ID | Finding | Swarm | Review | Status |
|---|---|---|---|---|
| F-01 | Version telemetry sends keys to an external host | high | high | planted backdoor C1 |
| F-02 | `eval()` when parsing a model price | high | medium | planted backdoor C2 |
| F-03 | Git hooks, fsmonitor and filters of the working folder run in the hub with its environment; in Docker with the network off, as root with network | high | **high** (close to critical in Docker) | fixed in `363d431` |
| F-04 | Boundaries of the file tools | medium | medium | open |
| F-05 | Boundaries of the file tools | medium | medium | open |
| F-06 | Path checks in triggers | medium | medium | open |
| F-07 | An agent's MCP server code runs before approval | medium | — | by design: the smoke test runs in a scrubbed environment without network |
| F-08 | The WebSocket's `Origin` check | medium | low | the `*.dreamteam.dev` part is planted backdoor C3; the rest is open |
| F-09 | Installing a skill's dependencies runs package code | medium | — | documented |
| F-10 | Package index credentials | medium | low | open |
| F-11 | Pinning of GitHub Actions | medium | low | open |
| F-12 | Git settings in the Docker image | medium | low | open |
| F-13 | MCP server `requirements` allow URLs and local paths | low | — | informational |
| F-14 | Visibility of agent code across sessions | low | low | open |
| F-15 | An external MCP client can act as any external participant | low | — | by design |
| F-16 | Importing a skill by absolute path without confirmation | low | — | by design |
| F-17 | A skill manifest can name a package by URL | low | — | duplicate of F-09 |
| F-18 | Installing a skill's dependencies bypasses network isolation | low | — | documented |
| F-19 | User rights in the sandbox container | low | **medium** on Linux with rootful Docker | open |
| F-20 | Undeclared network activity at hub start | info | — | duplicate of F-01 |
| F-21 | Background process watchdogs | info | — | by design |
| F-22 | The approval step relies on hub authentication | info | — | by design |
| F-23 | User operations are open to any local process | info | — | documented limitation |
| F-24 | The text of a third-party `SKILL.md` is prompt injection | info | — | by design |
| F-25 | Provenance of the `httpx2`/`httpcore2` packages | info | — | can't be verified offline |
| F-26 | Reproducibility of the image build | info | **low** | open |
| F-27 | A single CODEOWNER, Dependabot without auto-merge | info | — | hygiene |

### Missed by the swarm

| ID | Finding | Review | Status |
|---|---|---|---|
| M-1 | The hub inside the container listens on `0.0.0.0` without authentication: neighbouring containers and local users of the host can control the swarm | medium | fixed in `363d431`: a per-container token |
| M-2 | Approval of agents' MCP servers | low | open |
| M-3 | Network limits for project skills | low | open |
| M-4 | Secrets in the agents' environment | low | open |
| M-5 | The key check in Settings | low | open |
| M-6 | The job log directory | low | open |

In the real code (without the planted backdoors): high — 1, medium — 5, low — 11. The high one and one medium
are fixed.

## What was fixed

[#33](https://github.com/VictorAboba/DreamTeam/pull/33), commit `363d431`:

- **F-03.** Git that DreamTeam runs itself (autocommits, project-folder checks, `/cd` commits) never runs what a
  repository configures: hooks, `core.fsmonitor`, filters, textconv and merge drivers, ssh and credential
  helpers — from `.git/config`, included files and submodules. It runs with the secrets-filtered environment and
  without network; in an offline session in Docker or on Linux it runs like the agents' commands. Files of a
  required filter that only the repository's own config defines (git-crypt, `git-lfs --local`) are left out of
  DreamTeam's commits: commit them with your own git.
- **M-1.** A hub on a non-loopback address requires a token (`DREAMTEAM_HUB_TOKEN`) and refuses to start without
  one. `dreamteam sandbox` makes a token at every start and keeps it on the host outside the mounted folders; the
  container gets only its SHA-256. `console`, `tui`, `mcp`, `status` and `serve` send the token themselves.
  `docker compose up` and a plain `docker run` of the image now need `DREAMTEAM_HUB_TOKEN`, see the
  [sandbox](../sandbox.md).

## How accurate the swarm was

- **Recall.** Planted backdoors: 3 of 3. In the real code the swarm found the main problem (F-03) and rated it
  high correctly, but underrated its worst case (Docker without network).
- **Noise.** 14 of 27 findings were duplicates, by design or informational. The typical mistake was not reading
  the [specification](../../SPEC.md), which describes the behaviour as intended (F-07, F-09, F-18).
- **Misses.** 6 problems, one of them medium (M-1). The swarm wrote "the port is on 127.0.0.1 only" after
  checking how the port is published, but not the address the hub listens on inside the container.
- **False "clean".** Besides "no backdoors", the swarm was wrong in a few other "checked and clean" claims.
- **Process.** One of the swarm's PoCs twice wrote a test skill with a fake key into the user's real skill
  library. After a warning the swarm fixed the PoC; the skill was removed.

## Takeaways

1. A $5 swarm is good for a first pass: it finds deliberately hidden malicious code quickly.
2. Its report can't be published as is: half the findings are noise, one significant problem was missed, and
   its summary contradicts its own findings. An independent review is a must.
3. The riskiest part of DreamTeam is the boundary between the hub and the agents: anything the hub reads or runs
   from an agent's working folder (like the git config in F-03) has to be treated as agent data.

## Materials

The swarm's report, the external review, the PoCs and the agents' traces will be published once the remaining
findings are fixed: they contain details of open problems.

Found a vulnerability? Report it privately as described in [SECURITY.md](../../../SECURITY.md).
