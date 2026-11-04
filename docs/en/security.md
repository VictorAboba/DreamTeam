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
* Secrets (`*_API_KEY`, `*_KEY`, `*_TOKEN`, `*_PAT`, `*SECRET*`, `*PASSWORD*`, …) are not passed to the agents'
  console and their MCP servers, unless you explicitly allow them in `shell.pass_env`. A password or token in a URL
  (`PIP_INDEX_URL=https://user:password@…`, `DATABASE_URL=postgres://user:password@…`, a proxy's address, also one
  without a scheme such as `HTTPS_PROXY=user:password@host:3128`) is removed from the value there: the variable still
  points at the same index or database, only without the credentials. The installers of your own skills'
  dependencies and the MCP servers of `[mcp.external]` keep the credentials in URLs, but they do not get the
  variables with secret names either: give those to an `[mcp.external]` server in its `env` table or through
  `shell.pass_env`, and to your own skills' installers through `[skills] install_env`.
* With `[mcp] require_approval = true` you approve a snapshot of an agent's MCP server: the code that passed its
  automatic check, with its requirements, kept outside the workspace in `.dreamteam/mcp/<name>/`. The MCP card and
  the terminal dialog show its SHA-256, and the snapshot is what runs. If `mcp/<name>/server.py` in the workspace
  changes afterwards, the server is checked again at its next start and waits for your approval again, and an
  approval is refused while that file differs from the snapshot, or when the agent created the server again since
  the card or the dialog showed it (they send the SHA-256 they show; `/approve <name> <sha256>` in the console and
  the terminal UI and `mcp_approve` take the one that `mcp_list` or the card shows, given in full or by its first 8
  or more characters, and `/approve <name>` alone in the terminal UI sends the one of the dialog you put off with
  Esc). That SHA-256 covers the start command and the requirements as well as `server.py`: it is not the file's own.
  The snapshot runs in the folder `mcp/<name>/`: a server opens the files it keeps there by relative
  paths, not through `__file__`. A server you approved (or created yourself) goes to the shared library with its
  approval, so another session that imports exactly that code runs it without asking; other library code still
  waits for your approval. An agent cannot replace a library server that another session saved, even under the
  same owner's name. Without `require_approval` a changed `server.py` is checked again at the next start and used
  without a question. The snapshot is out of reach of the agents' file tools, not of their console commands, and
  the approval marks in the library are only as safe as the library folder.
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
  reverse proxy with authentication, or without a token: with `DREAMTEAM_HUB_TOKEN` set, every request needs it, from
  the hub's own computer too (the header `Authorization: Bearer <token>`, or the cookie that a board address with
  `?token=` sets); without it the answer is 401. The hub compares the SHA-256 of what a request sends
  with the token's, in constant time (`DREAMTEAM_HUB_TOKEN_SHA256` alone is enough for it). A request that only has
  the cookie must come from the board's own origin: browsers send a cookie to every port of `127.0.0.1`. A hub on an
  address other than loopback does not start without a token, unless `DREAMTEAM_HUB_NO_TOKEN=1` is set.
* A hub with a token asks every peer for it, the hub's own loopback included: in the Docker sandbox the agents'
  commands reach the hub from the container's loopback, and without the token they cannot use its API. DreamTeam's
  commands on the host send it themselves, and the console of `dreamteam serve` and `run` and the TUI get into the
  hub they serve with a token of their own, kept in that process's memory. Anything on the same computer that
  forwards connections to the hub (a reverse proxy, `ssh -R`, socat, a tunnel agent) lets nobody in without the
  token either. The hub never takes `X-Forwarded-For` or `Forwarded` into account (whatever `FORWARDED_ALLOW_IPS`
  says). A hub without a token (by default `dreamteam serve` on `127.0.0.1`) asks nothing of anyone: every program on
  its computer, the agents' commands outside Docker among them, can use its API.
* In the Docker sandbox the hub listens on `0.0.0.0` inside the container (a published port does not reach the
  container's loopback), so other containers on the same Docker network reach its port. `dreamteam sandbox` therefore
  gives every container its own token, keeps it on the host outside every folder it mounts (see
  [Docker sandbox](sandbox.md#access-token)) and gives the container only its SHA-256.
* Protection against DNS rebinding and cross-site requests works with any bind address (including `0.0.0.0` in
  Docker): access through `localhost` always works, and by IP when it is the address the connection came to (any IP
  with the token); list other host names in `DREAMTEAM_ALLOWED_HOSTS=board.example.com`. `DREAMTEAM_PUBLIC_URL` sets
  the board address that users see (`dreamteam sandbox` sets it). A browser request (the board's WebSocket too) must
  come from the board's own address, the one in `DREAMTEAM_PUBLIC_URL` or an allowed name: a page on another port of
  `localhost` belongs to another program and is refused. A proxy on the same computer that changes the `Host` header
  needs `DREAMTEAM_PUBLIC_URL` set to the address users open. On an address without a port, the board's address also
  covers the other default port of that name (80 or 443), except for `localhost`, `127.0.0.1` and `::1`: the hub does
  not see the scheme in front of a proxy that ends TLS. A hub with a token still needs `DREAMTEAM_PUBLIC_URL` there.
* `docker compose up --build` sends only the sources to Docker (`.dockerignore`): neither `.venv`, nor `.git`, nor
  the swarm folders with their databases, nor `dreamteam.toml` with keys get into the image.
* In the Docker image git trusts no repository that belongs to another user (its `safe.directory` check is on): the
  agents' commands, their background processes and the MCP servers of a session trust only that session's folder,
  the folders inside it and, for a folder without a repository of its own, the repository it lies in; DreamTeam's
  own git names the one repository it works in. git refuses only repositories of another user: the folders the
  host's user made (a bind mount on Linux) or, for the commands of a session without network, the folders root
  made. With network on, the commands run as root, who also owns every session folder DreamTeam creates, so this
  check does not separate the sessions from one another.
