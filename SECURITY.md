# Security policy

DreamTeam runs commands and code written by LLM agents on your machine, so we take reports about escapes from
its limits seriously: secrets reaching agents, sessions without network that still reach it, the Docker sandbox,
the board's API being reachable from other sites or hosts, agents writing outside their workspace.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through
[GitHub's vulnerability reporting](https://github.com/VictorAboba/DreamTeam/security/advisories/new)
(the **Security** tab → **Report a vulnerability**). Include the version or commit, your OS, and steps to
reproduce. You should get an answer within a week.

## Audits

- [Security self-audit, November 2026](docs/en/audits/2026-11-self-audit.md): a DreamTeam swarm audited
  DreamTeam's own source code, and every finding was checked independently. Fixed issues are published as
  [security advisories](https://github.com/VictorAboba/DreamTeam/security/advisories).

## Known limits (not vulnerabilities)

These are documented and expected:

- Agents run commands with the rights of the user who started the swarm. Use `dreamteam sandbox` (Docker)
  for untrusted goals. Outside Docker they can therefore read whatever that user can read.
- In `dreamteam sandbox` the hub and the agents' commands of a session with network run as root in the container
  (those of a session that is without network when the sandbox starts run as an unprivileged user); the
  container runs with `no-new-privileges`. With a rootful Docker daemon on Linux, what root in the container creates
  in the mounted folder belongs to root on the host: for untrusted goals start the sandbox with the network off, or
  use rootless Docker, Docker Desktop or `userns-remap`.
- A session without network (`network = "off"`) is cut off strictly only in Docker and on Linux. On Windows
  and macOS it is a best-effort setup (proxy variables and offline modes of package managers), not isolation.
- The server listens on `127.0.0.1` and has no authentication by default. Exposing it to a network without a token
  (`DREAMTEAM_HUB_TOKEN`) or a reverse proxy with authentication gives everyone control over the swarm; a hub on a
  non-loopback address refuses to start without a token unless `DREAMTEAM_HUB_NO_TOKEN=1`. `dreamteam sandbox`
  always gives its hub a token (and the container only the token's SHA-256; so does `docker-compose.yml`).
- A hub with a token asks every peer for it, loopback peers included: the agents' commands in the sandbox's container
  reach the hub from its loopback and cannot use the API without the token. A hub without one (the default
  `dreamteam serve` on `127.0.0.1`) asks nothing: every program on that computer, the agents' commands outside Docker
  among them, can use its API.
- With `[mcp] require_approval = true` an approval covers one snapshot of an agent's MCP server (a SHA-256 over its
  code, requirements and start command), kept in the session's `.dreamteam` folder: out of reach of the agents' file
  tools, not of their console commands. The approval marks in the shared MCP library (`~/.dreamteam/library` by
  default) are only as trustworthy as that folder, which commands running as your user can write.
