# The session folder and an existing project

**English** · [Русский](../ru/projects.md) · [All docs](README.md)

By default, a session works in its own folder `sessions/<id>/workspace`. You can move it to any other folder or point
the swarm at an existing project.

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
  would get mixed into their commits. Commit or `git stash` first. A repository in detached HEAD state is refused too,
  as is an unfinished merge, rebase, cherry-pick or revert (the agent's next commit would finish it under the agent's
  name), and a subfolder of a repository (give its root).
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
folder from the web interface, only with the `/cd` command, through MCP or REST.

The `main` session of an old project (`dreamteam run`) can't be moved.
