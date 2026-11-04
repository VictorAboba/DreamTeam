# Security self-audit, November 2026

**English** · [Русский](../../ru/audits/2026-11-self-audit.md) · [Documentation](../README.md)

On 2 November 2026 a DreamTeam swarm audited the source code of DreamTeam itself. Every finding was then
re-checked against the code independently. This page has the setup, what the swarm found, which of it held up
and what was fixed.

## Summary

- The real code had one serious vulnerability (F-03, a sandbox escape through the working folder's git config),
  five medium ones (one of them, M-1, missed by the swarm) and eleven low ones. All of them are fixed in
  [#33](https://github.com/VictorAboba/DreamTeam/pull/33) and [#36](https://github.com/VictorAboba/DreamTeam/pull/36).
  One, F-19, is fixed in part, and the rest of it is a documented limit. See the
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

"Swarm" is the severity in the swarm's report, "Review" the severity after the external review.

| ID | Finding | Swarm | Review | Status |
|---|---|---|---|---|
| F-01 | Version telemetry sends keys to an external host | high | high | planted backdoor C1 |
| F-02 | `eval()` when parsing a model price | high | medium | planted backdoor C2 |
| F-03 | Git hooks, fsmonitor and filters of the working folder run in the hub with its environment; in Docker with the network off, as root with network | high | **high** (close to critical in Docker) | fixed in `363d431` |
| F-04 | The file tools' path check does not see hard links: `write_file`, `edit_file` and reads reach a file outside the workspace or inside `.git` | medium | medium | fixed in `ad3de3c` (writes refused; reads stay allowed) |
| F-05 | A race between the path check and the write; in Docker an unprivileged agent gets a write as root | medium | medium | fixed in `ad3de3c` |
| F-06 | Trigger paths accept a UNC path (`\\host\share`), and the check runs on the hub's event loop: the hub stalls, and on Windows with network the NetNTLM hash goes to a host the agent picks | medium | medium | fixed in `ad3de3c` |
| F-07 | An agent's MCP server code runs before approval | medium | — | by design: the smoke test runs in a scrubbed environment without network |
| F-08 | The WebSocket accepts an `Origin` of any localhost port | medium | low | the `*.dreamteam.dev` part is planted backdoor C3; the rest is fixed in `3a214b4` |
| F-09 | Installing a skill's dependencies runs package code | medium | — | documented |
| F-10 | Package index credentials (`PIP_INDEX_URL`, `UV_INDEX*`, `npm_config_*`) and `DATABASE_URL`, `*_PAT` pass the secrets filter | medium | low | fixed in `3a214b4` |
| F-11 | GitHub Actions pinned by tag, not by commit | medium | low | fixed in `3a214b4` |
| F-12 | The image trusts every repository for git (`safe.directory=*`) | medium | low | fixed in `3a214b4` |
| F-13 | MCP server `requirements` allow URLs and local paths | low | — | informational |
| F-14 | Agents' MCP code in the shared library is visible across sessions, and "approve once" did not hold | low | low | fixed in `3a214b4`: approvals cover a snapshot |
| F-15 | An external MCP client can act as any external participant | low | — | by design |
| F-16 | Importing a skill by absolute path without confirmation | low | — | by design |
| F-17 | A skill manifest can name a package by URL | low | — | duplicate of F-09 |
| F-18 | Installing a skill's dependencies bypasses network isolation | low | — | documented |
| F-19 | Root in the sandbox container can leave a setuid-root program in the mounted folder | low | **medium** on Linux with rootful Docker | in part: the container runs with `no-new-privileges` (`ad3de3c`); root for sessions with network is a [documented limit](../../../SECURITY.md) |
| F-20 | Undeclared network activity at hub start | info | — | duplicate of F-01 |
| F-21 | Background process watchdogs | info | — | by design |
| F-22 | The approval step relies on hub authentication | info | — | by design; a hub with a token now asks loopback peers for it too (`3a214b4`) |
| F-23 | User operations are open to any local process | info | — | documented limitation of a hub without a token |
| F-24 | The text of a third-party `SKILL.md` is prompt injection | info | — | by design |
| F-25 | Provenance of the `httpx2`/`httpcore2` packages | info | — | can't be verified offline |
| F-26 | The image installs version ranges, not `uv.lock` | info | **low** | fixed in `3a214b4` |
| F-27 | A single CODEOWNER, Dependabot without auto-merge | info | — | hygiene |

### Missed by the swarm

| ID | Finding | Review | Status |
|---|---|---|---|
| M-1 | The hub inside the container listens on `0.0.0.0` without authentication: neighbouring containers and local users of the host can control the swarm | medium | fixed in `363d431`: a per-container token |
| M-2 | A pending agent's `server.py` stays writable and is not checked again on approval: another agent could swap it | low | fixed in `3a214b4`: approval covers a snapshot by SHA-256 |
| M-3 | `claude-project` skills (written by agents) skipped the network check and `install_proposals = false` | low | fixed in `3a214b4` |
| M-4 | The secrets filter goes by name and lets `SSH_PRIVATE_KEY`, `AWS_ACCESS_KEY_ID`, `GITLAB_PAT` and similar reach agents | low | fixed in `3a214b4` |
| M-5 | The key check in Settings sends the stored key to a `base_url` the caller chooses | low | fixed in `3a214b4` |
| M-6 | The job log folder can be swapped for a junction, and the hub writes the log outside | low | fixed in `ad3de3c` |

In the real code (without the planted backdoors): high — 1, medium — 5, low — 11. All are fixed; F-19 in part.

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

[#36](https://github.com/VictorAboba/DreamTeam/pull/36), commit `ad3de3c`:

- **F-04, F-05, M-6.** `write_file` and `edit_file` refuse a file that has another hard link, checked before the
  work and again on the opened file. Right before opening, a write resolves the file's folder again and refuses
  when it now leads outside the workspace or the file has become a link. A job's log folder is used only while it
  is inside the workspace and is not a link.
- **F-06.** UNC and Windows device paths outside the workspace are refused before anything resolves them; the
  trigger loop stats files in a thread.
- **F-19.** The sandbox container runs with `no-new-privileges`, so no program in it gains rights through a
  set-user-ID bit. Commands of a session with network still run as root in the container: with a rootful Docker
  daemon on Linux, start the sandbox with the network off for untrusted goals, or use rootless Docker, Docker
  Desktop or `userns-remap`.

[#36](https://github.com/VictorAboba/DreamTeam/pull/36), commit `3a214b4`:

- **F-22 and an agent approving its own MCP server.** A hub with a token asks loopback peers for it too, so the
  agents' commands in the sandbox cannot act as the user through the API.
- **M-2, F-14.** With `require_approval` an approval covers one snapshot of an MCP server: a SHA-256 over its start
  command, requirements and `server.py`. The server runs that snapshot, and `mcp_approve` refuses when the digest
  differs.
- **M-4, F-10.** The secrets filter covers more names; credentials in URL values are removed from what agents get
  and masked in installer logs.
- **M-5.** A settings check sends a stored key only to its own endpoint.
- **M-3.** `install_proposals = false` also covers `claude-project` skills.
- **F-08.** The board and its WebSockets take only the hub's own origin, `DREAMTEAM_PUBLIC_URL` or a name of
  `DREAMTEAM_ALLOWED_HOSTS`.
- **F-11, F-12, F-26.** GitHub Actions are pinned by commit; the image installs from `uv.lock` with hashes and no
  longer trusts every repository for git.

## How accurate the swarm was

- **Recall.** Planted backdoors: 3 of 3. In the real code the swarm found the main problem (F-03) and rated it
  high correctly, but underrated its worst case (Docker without network).
- **Noise.** 14 of 27 findings were duplicates, by design or informational. The typical mistake was not reading
  the [specification](../../SPEC.md), which describes the behaviour as intended (F-07, F-09, F-18).
- **Misses.** 6 problems, one of them medium (M-1). The swarm wrote "the port is on 127.0.0.1 only" after
  checking how the port is published, but not the address the hub listens on inside the container.
- **False "clean".** Besides "no backdoors", the swarm called several things checked and clean that were not:
  "the guard rejects a foreign `Origin`" (except `*.dreamteam.dev`), "the agents' environment is scrubbed" (M-4),
  "an agent cannot approve itself" (its console could call the API as the user), "CI strictly by the lock" (the
  image did not use it).
- **Process.** One of the swarm's PoCs twice wrote a test skill with a fake key into the user's real skill
  library. After a warning the swarm fixed the PoC; the skill was removed.

## Takeaways

1. A $5 swarm is good for a first pass: it finds deliberately hidden malicious code quickly.
2. Its report can't be published as is: half the findings are noise, one significant problem was missed, and
   its summary contradicts its own findings. An independent review is a must.
3. The riskiest part of DreamTeam is the boundary between the hub and the agents: anything the hub reads or runs
   from an agent's working folder (like the git config in F-03) has to be treated as agent data.

## Materials

The swarm's report, the external review, the PoCs and the agents' traces will be published separately, once they
are cleaned of local paths.

Found a vulnerability? Report it privately as described in [SECURITY.md](../../../SECURITY.md).
