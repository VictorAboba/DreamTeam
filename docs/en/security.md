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
* By default the hub listens only on `127.0.0.1`. Do not expose it to the network without a reverse proxy with
  authentication.
* Protection against DNS rebinding and cross-site requests works with any bind address (including `0.0.0.0` in
  Docker): access by IP and `localhost` always works; list other host names in
  `DREAMTEAM_ALLOWED_HOSTS=board.example.com`. `DREAMTEAM_PUBLIC_URL` sets the board address that users see
  (`dreamteam sandbox` sets it).
* `docker compose up --build` sends only the sources to Docker (`.dockerignore`): neither `.venv`, nor `.git`, nor
  the swarm folders with their databases, nor `dreamteam.toml` with keys get into the image.
