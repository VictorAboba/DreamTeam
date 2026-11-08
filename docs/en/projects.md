# The session folder and an existing project

**English** · [Русский](../ru/projects.md) · [All docs](README.md)

By default, a session works in its own folder `sessions/<id>/workspace`. You can move it to any other folder or point
the swarm at an existing project. When the agents must not touch your files, give a new session a copy of them
instead: see [Working on a copy of files](#working-on-a-copy-of-files).

How to connect a session to your project:

1. Prepare the repository: commit your changes or put them away with `git stash` (with uncommitted changes and
   untracked files the connection fails, see the rules below). The agents commit to the current branch, so it is
   convenient to switch to a separate one first: `git switch -c dreamteam/billing`.
2. Start a new session right in the project folder:

   ```bash
   dreamteam --workspace ~/code/my-app "Cover the billing module with tests"
   ```

   Or move a running one: in the terminal interface, run `/cd ~/code/my-app` and answer "Yes" to the question
   "The folder is not empty: the agents will work with its files and commit to its current branch…".
3. The agents work right with the project files, and their commits (under the agents' names) show up in `git log`.
   While the session runs, don't edit the project files yourself: stop it (`/stop`), commit your changes and start it
   again.
4. To disconnect the session from the project, run `/cd --default`: the project files and history stay where they
   are, and the session goes back to its own folder.

The `/cd` command in the terminal interface:

- `/cd` with no argument shows the folder of the current session: the path, whether it is the session's own folder or
  a project folder, and whether it exists;
- `/cd DIR` moves the session (a relative path and `~` are expanded from the terminal's current folder if the server
  is local). Before the move, the interface asks for confirmation;
- `/cd --default` returns the session to its own folder.

The same can be done with `/cd` in `dreamteam console`, the MCP tool `session_cd(session, path)` and
`POST /api/sessions/<id>/workspace` (`{"path": "<absolute path>"}` or `{"default": true}`). You can create a new
session in the right folder at once: `--workspace DIR` for `dreamteam`, `dreamteam -p` and `dreamteam serve --goal`,
`session_create(..., workspace=...)` in MCP, or the `workspace` key in `POST /api/sessions`.

What happens depends on the target folder:

- **The folder is empty or does not exist**: the session **moves**. Its files, git history and MCP servers move
  there, and the old place is left empty (the session's own folder is deleted and is created again on return).
  The exception is a session connected to your project: the project files stay where they are, only the session's
  MCP servers move to the new folder, and a new repository is created there.
- **The folder already has files** (your project): the session **connects** to it. The agents work right with these
  files, and their autocommits go to the **current branch** of the repository. The session's previous folder stays as
  it was (its path is printed), and the session's MCP servers move to `<folder>/mcp/<name>/`.

Safety rules for a folder that is not the session's own:

- If the repository has uncommitted changes or untracked files (not in `.gitignore`; you can't hide them with the
  `status.showUntrackedFiles` setting), the move is refused: the agents commit with `git add -A`, and your changes
  would get mixed into their commits. Commit or `git stash` first. The files of a filter that only your repository's
  config defines and marks required (git-crypt after `git-crypt unlock`, git-lfs installed with `--local`) do not
  count: DreamTeam never commits them (see [Security](security.md)), you do. So your own uncommitted edits to them
  don't stop the move or a start either, and an agent's own `git commit` in its console can still take them where
  that filter runs for it (git-crypt installed and unlocked on this machine): commit them first. A repository in
  detached HEAD state is refused too, as is an unfinished merge, rebase, cherry-pick or revert (the agent's next
  commit would finish it under the agent's name), and a subfolder of a repository (give its root).
- These checks run again at every start of the session, also when it resumes after a server restart: if, while the
  session was stopped, you left uncommitted changes in the project or switched to a detached HEAD, the session does
  not start and says why. Commit or `git stash` and start it again. If the session connected to the project with git
  turned off, and git was turned on later, the check runs at the next start.
  While the session runs, don't edit the project files yourself: the agent's next autocommit takes your changes too.
  Stop the session (`/stop`), commit your changes and start it again.
- The settings of your repository don't change: DreamTeam does not write `user.name`, `user.email`, `core.autocrlf`
  or other keys into it. The author of each agent commit is set with flags of the git command itself, and your commits
  stay yours. Line endings in the agents' commits follow your settings (`core.autocrlf` and the like), as in your own
  commits.
- If the folder is not a git repository and git is turned on (`workspace.git`), DreamTeam creates a repository in it:
  `git init`, `.gitignore` (if there is none) and an initial commit with your files. The reply says so.
- If the folder already has `mcp/<name>/` with the same name as one of the session's MCP servers, the move is refused
  before any change.
- You can't choose the root of a drive, the user's home folder, the DreamTeam home folder or the folders of other
  sessions, the git repository database itself (a `.git` folder or a bare repository), the shared library of MCP
  servers, a file, or a folder inside the current session folder or one that contains it. All checks run before the
  first change. Two sessions can't work in one folder, even if they are moved there at the same time.
- If you agreed to a move into an empty folder and files appeared in it in the meantime (or the other way round), the
  move is refused: the session does only what you agreed to.
- A running session stops, moves and starts again from the same point. If the move fails halfway, the files go back;
  files that appeared in the target folder during the move are not touched.
- When the session leaves your project (`/cd` to another folder or `/cd --default`), the project files stay where they
  are, and only the session's MCP servers leave with it. If the agents already committed them to your project,
  DreamTeam commits their removal in a separate commit ("Remove mcp/…") so that the repository stays clean. If such a
  commit is not possible (detached HEAD, an unfinished merge, git turned off), the reply says what to commit or
  restore (`git checkout -- mcp/…`).

Deleting a session never deletes a folder outside the session: it stays where it is, and the delete dialog,
`dreamteam sessions rm` and `/delete` say so. If the folder is gone (it was deleted, a drive was disconnected), the
session does not start and tells you what to do: bring the folder back or run `/cd` to another one (`/cd --default`
to its own folder).
`dreamteam console --session ID` works for this too: the console connects to a stopped session without a folder so
that you can move it.
On the home page, each session shows its folder with the label "own folder", "project folder" or "moved", and a
warning if the folder is missing; on the session board, the path is shown on the "Files" tab. You can't change the
folder from the web interface, only with the `/cd` command, through MCP or REST, but the "New session" dialog can
fill a new session's folder with a copy of files ("More" → "Files to work on").

A session that worked in a folder outside the home on another computer arrives after an [import](transfer.md)
without the files of that folder: only the path travels. When a folder with files exists at that path here, the
session works in it as in your project, and the same checks as above apply when it starts. An empty folder there
is used as the session had it on the other computer (as your project only when it was one there). When there is
no such folder, the session does not start: put the project at that path, or run `/cd` in the session. The import
refuses the session when that path is a file here, overlaps the export folder or another session's folder, or is
a folder `/cd` would refuse (the home, your user folder, a library, the root of a drive and the like). When the session
already works in a folder of its own outside the home here (after `/cd`), a later import does not change that
folder: the session stays in it, and the files of the export go to the session's own folder.

The `main` session of an old project (`dreamteam run`) can't be moved.

## Working on a copy of files

When the agents should work with your files but must not change them, give a new session a copy. DreamTeam copies
a folder or a .zip archive into the session's folder before its first start. The agents work on the copy, the
originals are only read, and nothing is copied back.

```bash
dreamteam --copy-from ~/code/my-app "Find out why the export is slow and fix it"
```

Where to give the source:

- `--copy-from DIR|ZIP` for `dreamteam` (the first new session the terminal interface creates), `dreamteam -p` and
  `dreamteam serve --goal`. It is refused with `-c` / `-r` (an existing session), and for `serve` without `--goal`;
- `/copy-from DIR|ZIP` in the terminal interface: the next new session gets the copy, with or without a current
  session. `/copy-from` alone shows the choice, `/copy-from --none` clears it. A relative path and `~` are expanded
  from the terminal's current folder if the server is local;
- in the web interface, the "New session" dialog: "More" → "Files to work on", either a folder path or a .zip
  uploaded from the browser;
- `session_create(..., copy_from=...)` in MCP, or `copy_from` in `POST /api/sessions`. A .zip from another computer
  goes to `POST /api/uploads?filename=<name>.zip` first (the file itself as the body, `Content-Type:
  application/zip`; the session card shows that name as "upload: <name>.zip"); the `upload` token of the answer
  goes into `copy_from_upload`. An upload waits an hour for its session and is used once: by the
  create that names it, also when that create is refused (upload the .zip again for the next try).

A folder or .zip path is an absolute path on the computer where DreamTeam (the hub) runs; `dreamteam` makes a
relative one absolute itself. The reply says how many files were copied, their size and what was left out, and the
session card on the home page shows "Copy of:" with the path. In the web dialog with "Start right away", the board opens
at once unless links or special files were left out: then the sessions page shows what was left out first, and a
click on that notice opens the board. The agents are not told where the files came from.

What gets into the copy:

- Everything in the folder except `.git`, `node_modules`, `.venv`, `venv` and `__pycache__` (at any depth, in any
  letter case), links (symlinks, junctions) and special files (pipes, sockets, devices). The `.DS_Store` and
  `Thumbs.db` files of Finder and Explorer are skipped too.
- A .zip is unpacked with the same exclusions; `__MACOSX`, `.DS_Store` and `Thumbs.db` are skipped, and links in it
  are left out.
  When everything in the zip lies in one folder (a zip of `my-app/`), the session gets the contents of that folder;
  a left-out `node_modules/` or `.git/` next to that folder keeps it as a folder, as a folder copy would. Names that
  macOS or Linux tools stored in UTF-8 without the UTF-8 mark come out right. Names that the old "Compressed folder"
  of Windows stored in its own code page come out right only when the hub runs on Windows; a hub on Linux or macOS
  (the Docker sandbox included) can't tell that code page and keeps the zip's cp437 reading. For such a hub, pack
  the zip with UTF-8 names, for example with 7-Zip: `7z a -mcu=on my-app.zip my-app`. On Windows, a zip with names
  Windows cannot store (`a:b`, `CON`) is refused; on Windows and macOS, so is a zip with two names that differ only
  in letter case. Elsewhere such names are refused when the session's folder can't tell them apart (a Linux hub
  whose home is a Docker Desktop folder of Windows or macOS), as is a folder copy with such names.
- Read-only files become writable in the copy, so that the agents can edit them; other local users never get write
  access to them. The setuid and setgid bits are not kept. A file system without Linux permissions (WSL's `/mnt/c`,
  a Windows folder mounted into Docker, FAT, exFAT, NTFS, a network share over SMB) makes the permissions up, often
  showing every file as executable: in the copy, only scripts (starting with `#!`) and programs stay executable.
- If a file can't be read (permissions, a file another program holds), the copy is refused and nothing is kept. A
  file deleted while the copy runs (an editor's temporary file, build output) is simply not in the copy, and the
  reply lists it. A folder deleted, or the source folder moved or renamed, while it is copied refuses the copy, and
  so does a folder replaced by a link meanwhile: the link is never followed.

Where the copy goes and git:

- Without `--workspace`, the copy goes into the session's own folder `sessions/<id>/workspace`. With
  `--workspace DIR` (`workspace` in MCP and REST), it goes into that folder, which must be empty or not exist yet: a
  folder with files is refused, and the answer names a few of them: a hidden `.git` (after `git init`) or
  `.DS_Store` counts too. To work right with the files of that folder, leave out the copy. A second session
  created, or moved, in the same folder (or a folder inside it or containing it) while the first one's copy still
  runs is refused at once.
- With git on (`[workspace] git`), DreamTeam creates the repository itself, and the copied files are its first
  commit, "Initialize workspace": at the first start, or at once with `--workspace`. The source's `.git` is never
  copied, so the history starts fresh. The source's own `.gitignore` is copied and decides what is committed: the
  files it ignores are in the folder but not in the commit. Without one, DreamTeam writes its default `.gitignore`.

Limits and refusals:

- `[workspace] copy_max_mb` (1024 by default) and `copy_max_files` (100000) cap one copy: its size in MB and its
  number of files, without the folders left out. A folder is counted before anything is written. `copy_max_mb` also
  caps an uploaded .zip and what it unpacks to. For a .zip, the archive's own size counts against `copy_max_mb`, and
  every entry in it, left-out ones (`node_modules`, `.git`) and folders included, against a limit of twice
  `copy_max_files`: zip the folder without `node_modules`, `.venv` and the like. The limits are global ("Settings"
  → "Workspace" or [`dreamteam.toml`](configuration.md)): a session's own value has no effect.
- A source is refused when it is: a relative or missing path; a file that is not a .zip; the root of a drive (on
  Linux and macOS also a whole disk mounted in `/mnt`, `/media` or `/Volumes`; on Linux one folder mounted there,
  such as docker's `-v ./my-app:/mnt/app`, is fine), your user folder itself or a folder
  that contains it; the DreamTeam home folder or DreamTeam's own folders in
  `~/.dreamteam` (the MCP and skills libraries, the skill environments, the hub tokens), a folder inside one of them
  or one that contains it; another DreamTeam home or a folder in it (its `dreamteam.toml` can hold API keys);
  `/proc`, `/sys` or `/dev`. A source inside the target folder, or one that contains it,
  is refused too. All checks run before anything is copied, and a failed or cancelled copy leaves no session behind.
- In the [Docker sandbox](sandbox.md) (and with `docker compose` or `docker run` of the image) the hub sees only the
  DreamTeam home folder of your computer, which is never copied: upload the folder as a .zip in the "New session"
  dialog. A path the container can't see gets an answer that says so.

A large copy may take longer than the client waits:

- The terminal interface and `dreamteam -p` wait 15 minutes. If they stop waiting, the hub still finishes the copy
  and creates the session, so check `dreamteam sessions` or the sessions page before trying again.
- An MCP client that gives up waiting (its own tool timeout) cancels the call: the hub stops the copy and keeps
  nothing. For a large copy, raise the client's tool timeout (in Claude Code: `MCP_TOOL_TIMEOUT`, in milliseconds),
  or create the session with the command line or the web dialog.

Nothing returns to the original folder by itself. The session's files are in its folder (`/cd` in the terminal
interface shows the path, the web board has them on the "Files" tab): copy what you need from there, or take the
agents' changes as a patch with git. Stop the session first (`/stop`), so that its last changes are committed. The
commands run in bash, Git Bash or PowerShell (not in cmd):

```bash
cd ~/DreamTeam/sessions/012-export/workspace
git log --oneline                     # the agents' commits after "Initialize workspace"
git diff --binary --output="$HOME/export.patch" $(git rev-list --max-parents=0 HEAD) HEAD
cd ~/code/my-app
git apply -v "$HOME/export.patch"     # also works in a folder that is not in any git repository
```

`git apply` run in a subfolder of another repository (a package of a monorepo, a project in a home folder kept in
git) silently skips every file: it reads the paths from that repository's root. `-v` shows that as "Skipped patch";
`git apply --check -v` tries without changing anything. Run it from the repository's root with the subfolder as
the prefix instead:

```bash
git -C ~/code/monorepo apply -v --directory=services/api "$HOME/export.patch"
```
