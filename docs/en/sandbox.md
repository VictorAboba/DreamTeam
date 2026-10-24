# Docker sandbox and sessions without internet

**English** · [Русский](../ru/sandbox.md) · [All docs](README.md)

## Docker sandbox

```bash
dreamteam sandbox ~/DreamTeam --goal "..."
```

The command builds the `dreamteam:latest` image and runs DreamTeam in a container. The home folder is mounted at
`/swarm`, and the home page is available at `127.0.0.1:8765`. An alternative: `SWARM_DIR=~/DreamTeam docker compose up --build`.
Local models (Ollama, vLLM) are reachable from the container at `http://host.docker.internal:<port>`.
Only the home folder is mounted into the container, so sessions that `/cd` moved to a folder outside it don't start
in the sandbox: `dreamteam sandbox` lists them at startup. Before the start, return such sessions to the home folder
(`/cd --default` or `/cd` to a folder inside it: such folders are found in the container too).

## Session without internet

For benchmarks and untrusted tasks, you can turn off a session's internet: the "No internet for the agents" checkbox
in the new session dialog, the "Internet for the agents' commands" field in the session's ⚙, `[swarm] network = "off"`
in the settings, `--network off` for `dreamteam`, `serve --goal`, `run` and `sandbox`, `network: "off"` for
`session_create`. The network is lost by the agents' commands (`shell`), their background processes and the MCP
servers that DreamTeam starts; DreamTeam itself (LLM calls, the board's port) keeps the network, and localhost works.
The agents learn about it from a notice, not by trial and error. How strict this is depends on the machine, and the
interface says so plainly:

* **Docker** (`dreamteam sandbox`): commands run as a separate user whose packets, except loopback, the container's
  firewall drops; the board's port is closed to this user too (otherwise an agent could turn the network back on
  through its API). If the mounted folder belongs to a regular user (Linux), commands run with that user's uid and the
  agents' files stay yours; otherwise (Docker Desktop, a Docker volume) they run as `dreamteam-agent` from the image.
  DreamTeam does not change the permissions of your files: only what root created in the container gets the group and
  write permissions. The container gets the `NET_ADMIN` capability only when at least one session needs to be without
  network (the `--network off` flag, the settings or an existing session), and DreamTeam drops it right after it sets
  up the firewall. An image built from an old Dockerfile is rebuilt automatically in this case. A session whose network
  was turned off later, in a container without the firewall, gets only the "best effort" option (see below) until the
  sandbox is restarted: the interface says so.
* **Linux**: commands run in a separate network namespace that has only its own localhost, as your own user, as
  before. There is one namespace per DreamTeam process: a server that one command started in the background is
  reachable from the next commands, tests and MCP servers, but the machine's own localhost (the board, your local
  database) is not. If the kernel does not allow user namespaces, only the "best effort" option remains.
* **Windows and macOS: best effort, this is not isolation**: the proxy variables point at a closed port, pip, uv,
  npm, cargo, go and git are set offline, and MCP servers with a remote address in their command are not connected.
  A program that ignores this (a raw socket, Windows PowerShell 5.1's web cmdlets) gets out to the network, and the
  board's API on localhost is reachable for the agent.

While the network is off, the dependencies of skills that agents proposed are not installed (otherwise the proposal
would be a way around the restriction); environments that are already built work. Your own skills are installed as
usual: DreamTeam installs them, not an agent. Git hooks and the `core.fsmonitor` command don't run in DreamTeam's own
commits in such a session. A change in ⚙ applies to new commands; background processes that are already running keep
the previous mode.
