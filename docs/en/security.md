# Security

**English** · [Русский](../ru/security.md) · [All docs](README.md)

* Agents run commands and MCP server code with the rights of the user who started the swarm. For untrusted
  tasks, use `dreamteam sandbox` (Docker). A session without network (`network = "off"`) is strictly cut off from
  the network only in Docker and on Linux; on Windows and macOS it is only a proxy and offline settings.
* Agents' background jobs run with the same rights in the session folder. They do not outlive their session:
  stopping, deleting or moving it (`/cd`) stops them together with everything they started (on Windows through a
  Job Object, on Linux and macOS through a process group and a watchdog process; the same after a server crash).
  Their output is kept in `.scratch/<agent>/jobs/`. In the Docker sandbox, ports that agents open (dev servers)
  are not published outside.
* A session attached to your project with `/cd` edits its files and commits to the current branch. Before the
  move, switch to a separate branch if you do not want agents' commits in your main one.
* Secrets (`*_API_KEY`, `*_TOKEN`, `*SECRET*`, …) are not passed to the agents' console and their MCP servers,
  unless you explicitly allow them in `shell.pass_env`.
* Agents can change the session's `.git` from their console. So DreamTeam's own git commands (autocommits, the checks of
  your folder) never run what a repository names for git to run: hooks, `core.fsmonitor`, filters, textconv and merge
  drivers, ssh and credential helpers from `.git/config`, included files and submodules. They run without the variables
  the agents' console treats as secrets (also those `shell.pass_env` lets through there) and without network access for
  git. Your own filters from the global or system git config (git-lfs) keep working. A filter that only the repository's
  own config defines and that is marked required (git-crypt, or git-lfs installed with `--local`) cannot run there:
  DreamTeam then leaves such files out of its commits instead of storing them unfiltered, and out of the checks of your
  folder (a change to them never stops a session from starting; in a submodule that has such a filter of its own, a file
  merely touched still counts as a change of the submodule until your own `git status` has seen it), and says so once in
  its log: commit them with your own git. This works with git 2.13 and newer (with git older than 2.44, as on macOS and
  Debian 12, such files are left out by name). One limit remains: a setting that an agent's command writes at the very
  moment DreamTeam's git starts can still take effect. In a session without network in Docker and on Linux, every git
  command DreamTeam runs for it (the autocommits, the checks of your folder, the commits of a `/cd`) runs like the
  agents' commands, so such a setting runs with the agents' rights and without network. Elsewhere it runs as DreamTeam's
  user, with the same filtered environment.
* Outside Docker the agents' commands run as the same user as DreamTeam (on Linux without network too: only the
  network is separate there), so a program they run can read whatever DreamTeam's user can read. Keeping secrets
  out of the agents' environment and out of DreamTeam's git stops DreamTeam from handing them over; it is no boundary
  against such a program.
* By default the hub listens only on `127.0.0.1` and has no authentication. Do not expose it to the network without a
  reverse proxy with authentication, or without a token: with `DREAMTEAM_HUB_TOKEN` set, every request that does not
  come from the hub's own loopback needs it (the header `Authorization: Bearer <token>`, or the cookie that a board
  address with `?token=` sets); without it the answer is 401. The hub compares the SHA-256 of what a request sends
  with the token's, in constant time (`DREAMTEAM_HUB_TOKEN_SHA256` alone is enough for it). A request that only has
  the cookie must come from the board's own origin: browsers send a cookie to every port of `127.0.0.1`. A hub on an
  address other than loopback does not start without a token, unless `DREAMTEAM_HUB_NO_TOKEN=1` is set.
* The token protects a direct bind to a non-loopback address only. Every peer on the hub's own loopback goes without
  it, so anything on the same computer that forwards connections to the hub (a reverse proxy, `ssh -R`, socat, a
  tunnel agent) lets its clients in without the token: such a forwarder needs its own authentication. The hub never
  takes `X-Forwarded-For` or `Forwarded` into account (whatever `FORWARDED_ALLOW_IPS` says), so a header cannot make a
  remote peer loopback.
* In the Docker sandbox the hub listens on `0.0.0.0` inside the container (a published port does not reach the
  container's loopback), so other containers on the same Docker network reach its port. `dreamteam sandbox` therefore
  gives every container its own token, keeps it on the host outside every folder it mounts (see
  [Docker sandbox](sandbox.md#access-token)) and gives the container only its SHA-256.
* Protection against DNS rebinding and cross-site requests works with any bind address (including `0.0.0.0` in
  Docker): access through `localhost` always works, and by IP when it is the address the connection came to (any IP
  with the token); list other host names in `DREAMTEAM_ALLOWED_HOSTS=board.example.com`. `DREAMTEAM_PUBLIC_URL` sets
  the board address that users see (`dreamteam sandbox` sets it).
* `docker compose up --build` sends only the sources to Docker (`.dockerignore`): neither `.venv`, nor `.git`, nor
  the swarm folders with their databases, nor `dreamteam.toml` with keys get into the image.
