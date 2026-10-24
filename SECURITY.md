# Security policy

DreamTeam runs commands and code written by LLM agents on your machine, so we take reports about escapes from
its limits seriously: secrets reaching agents, sessions without network that still reach it, the Docker sandbox,
the board's API being reachable from other sites or hosts, agents writing outside their workspace.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through
[GitHub's vulnerability reporting](https://github.com/VictorAboba/DreamTeam/security/advisories/new)
(the **Security** tab → **Report a vulnerability**). Include the version or commit, your OS, and steps to
reproduce. You should get an answer within a week.

## Known limits (not vulnerabilities)

These are documented and expected:

- Agents run commands with the rights of the user who started the swarm. Use `dreamteam sandbox` (Docker)
  for untrusted goals.
- A session without network (`network = "off"`) is cut off strictly only in Docker and on Linux. On Windows
  and macOS it is a best-effort setup (proxy variables and offline modes of package managers), not isolation.
- The server listens on `127.0.0.1` and has no authentication. Exposing it to a network without a reverse proxy
  with authentication gives everyone control over the swarm.
