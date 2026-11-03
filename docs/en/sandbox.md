# Docker sandbox and sessions without internet

**English** · [Русский](../ru/sandbox.md) · [All docs](README.md)

## Docker sandbox

```bash
dreamteam sandbox ~/DreamTeam --goal "..."
```

The command builds the `dreamteam:latest` image and runs DreamTeam in a container. The home folder is mounted at
`/swarm`, and the home page is available at `127.0.0.1:8765`. An alternative: `SWARM_DIR=~/DreamTeam docker compose up --build`
with a token you set yourself (see [Access token](#access-token)).
Local models (Ollama, vLLM) are reachable from the container at `http://host.docker.internal:<port>`.
Only the home folder is mounted into the container, so sessions that `/cd` moved to a folder outside it don't start
in the sandbox: `dreamteam sandbox` lists them at startup. Before the start, return such sessions to the home folder
(`/cd --default` or `/cd` to a folder inside it: such folders are found in the container too).

### Access token

Inside the container the hub has to listen on `0.0.0.0`: a port that Docker publishes reaches the container's
network interface, not its loopback. So other containers on the same Docker network could reach it too. That is why
`dreamteam sandbox` makes a new random token every time it starts a container, and the hub refuses every request that
does not come from the container's own loopback unless it carries that token (HTTP 401; a WebSocket is closed). At
the start the command prints:

* the board address with `?token=...`: the hub turns it into a cookie (HttpOnly, SameSite=Strict) and opens the
  address without the token;
* the command for Claude Code with the header:
  `claude mcp add --transport http dreamteam http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"`.
  The token changes at every start; `claude mcp add dreamteam -- dreamteam mcp` keeps working after a restart,
  because it finds the token itself.

While the container runs, the token is kept on the host in `~/.dreamteam/hub-tokens/<port>.json` (outside the mounted
folder; `DREAMTEAM_TOKENS_DIR` changes the place), and `dreamteam console`, `tui`, `mcp`, `status` and `serve` on this
computer send it to the hub at `127.0.0.1:<port>` themselves. They look it up for every request, so a console, a TUI
or a `dreamteam mcp` that Claude Code keeps running carry on when the sandbox restarts with a new token. If that
folder is inside the mounted home, the token is not stored: set `DREAMTEAM_HUB_TOKEN=<token>` for these commands. The
same holds for the host's skills library, which is mounted too: the token is never stored inside any folder the
container sees. When both exist, the commands try the stored token first and `DREAMTEAM_HUB_TOKEN` right after it (a
token file that outlived its sandbox, because the command was killed, does not get in the way).

DreamTeam's commands send a token only to a hub on a loopback address, and `DREAMTEAM_HUB_TOKEN` also to a hub address
you give on the command line: `dreamteam console --url http://192.168.1.5:8765` with `DREAMTEAM_HUB_TOKEN` set reaches
a hub on another computer. An address taken from `hub.json` (it lies in the mounted folder, where the agents can write)
gets a token only when it is a loopback one, so give `--url` only the address of a hub you started yourself. The
console of `dreamteam serve` and `run` and the TUI send `DREAMTEAM_HUB_TOKEN` to the hub they serve themselves, on
whatever address. A hub on `127.0.0.1`, `localhost` or `[::1]` is reached directly, never through a proxy from
`HTTP_PROXY`, which would see the token.

`dreamteam sandbox` never puts the token itself into the container's environment, only its SHA-256
(`DREAMTEAM_HUB_TOKEN_SHA256`). The hub compares the SHA-256 of what a request sends with it, so the digest lets nobody
in. DreamTeam also removes both variables from its environment at start: they reach neither the agents' commands, nor
MCP servers, nor DreamTeam's own git, whatever `shell.pass_env` says. Opening the board from another port of `127.0.0.1`
does not help a page there: a request that only has the cookie must come from the board's own origin (scheme, host and
port).

A hub on an address other than loopback does not start without a token: the image's default command
(`serve --host 0.0.0.0`) with a plain `docker run`, or `docker compose` without a token, stops with an error that says
what to set (with `docker compose up -d`, `docker compose logs` shows it), before it creates or starts anything. The
same goes for `dreamteam serve`, `run` and the TUI with `--host 0.0.0.0` (or such a `server.host`) on any computer.
With `docker compose`, set `DREAMTEAM_HUB_TOKEN` on the host yourself (`docker compose ps`, `logs` and `down` work
without it):

```bash
export DREAMTEAM_HUB_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
SWARM_DIR=~/DreamTeam docker compose up --build
# the board: http://127.0.0.1:8765/?token=<the token>
```

In PowerShell: `$env:DREAMTEAM_HUB_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"`. With a
plain `docker run`, pass `-e DREAMTEAM_HUB_TOKEN_SHA256=<the token's SHA-256>` (or `-e DREAMTEAM_HUB_TOKEN`) and set
`DREAMTEAM_HUB_TOKEN=<token>` for the host's commands. Prefer `DREAMTEAM_HUB_TOKEN_SHA256`: then the token itself is
never in the container's environment. `DREAMTEAM_HUB_NO_TOKEN=1` starts a hub without a token all the same (it logs a
warning): then every container of its network controls the swarm.

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
usual: DreamTeam installs them, not an agent. DreamTeam's own git commands do not run git hooks, `core.fsmonitor` or
the filters and helpers an agent wrote into the repository's config, with or without network; only a setting that an
agent's command writes at the very moment DreamTeam's git starts can still take effect (see
[Security](security.md)). In Docker and on Linux, every git command DreamTeam runs for such a session (the commits,
the checks of your folder, the commits of a `/cd`) also runs like the agents' commands (as the same user in Docker,
in the same network namespace on Linux), from the session's start on; when you switch the network off in ⚙,
DreamTeam's next git command waits until it can run so: anything that still slips through gets no more rights or
network than the agents have. A change in ⚙ applies to new commands; background processes
that are already running keep the previous mode.
