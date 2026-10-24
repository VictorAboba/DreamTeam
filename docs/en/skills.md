# Skills

**English** · [Русский](../ru/skills.md) · [All docs](README.md)

A skill is a folder in Anthropic's Agent Skills format:
`SKILL.md` with a name and a description in the YAML header, instructions, and any files next to it (scripts,
templates, reference). Agents see the names and descriptions of the attached skills at every wake-up and read
`SKILL.md` when a task fits. Skills written for Claude Code work as they are: their tools map to `shell`, `read_file`,
`write_file` and `edit_file`. DreamTeam itself runs nothing from a skill.

- **Library** (`~/.dreamteam/skills`, `[skills] library_dir`) is shared by all sessions. You add skills to it from a
  folder, a `.zip` archive, a Git repository (`https://github.com/org/repo/tree/main/skills/pdf` or
  `URL#branch:folder`) and from Claude Code: skills from `~/.claude/skills` are offered by themselves, while
  `<project folder>/.claude/skills` only when you name them explicitly (`claude-project:name`), because agents can
  write there. If a source has several skills, you choose which ones to take. You can manage the library on the
  "Skills" tab of the start page or with the MCP tool `skill_library`.
- **Attaching to a session.** You attach a skill in the new session dialog, on the board's "Skills" tab, with the
  `--skill` flag or with the `/skills add` command in the terminal and the console. You can also attach one on the fly
  to a running session: the members get a notice at once. Over MCP, skills are passed as the `skills=[...]` parameter
  of `session_create` and `session_start` or attached with the `skill_attach` tool. The session keeps a copy of the
  skill, so changes in the library do not touch it until you update the skill. Agents read the copy in
  `.scratch/_skills/<name>/`, and in a project folder it is deleted when the session leaves that folder.

  ```bash
  dreamteam --skill pdf --skill ./my-skills/review "Build a report on the PDF files in the docs folder"
  dreamteam serve --goal "..." --skill https://github.com/org/skills/tree/main/pdf
  ```

- **Roles.** A role in the catalog can have a `skills` list (`[[roles]]`): an agent hired with this role gets them
  automatically. If you detached a skill, the role does not attach it again.
- **Members' proposals.** An agent can write its own skill and propose it to the swarm (`skill_propose`): all members
  see it at once, and you are asked whether to save it to the library (`/skills approve name` or
  `/skills reject name [reason]`, a dialog in the terminal, buttons on the board and on the session card,
  `skill_approve`). You can also decide in a stopped session. A session with undecided proposals left is deleted only
  with a choice: save them to the library or delete them together with the session (the delete dialog on the start
  page and in the terminal, `dreamteam sessions rm ID --skills save` or `--skills discard`).
- **Script dependencies.** If a skill has `requirements.txt`, `pyproject.toml` (Python) or `package.json`
  (Node), the packages are installed automatically on attach into a separate environment for this skill
  (`~/.dreamteam/skill-envs`; Python with uv or pip, Node with `npm ci` / `npm install` without lifecycle scripts).
  The environment is shared by all sessions with the same skill and the same set of dependencies. Agents get the path
  to a ready Python, and Node packages are found by themselves. Before attaching, you see what will be installed.
  Without a manifest, agents install what `SKILL.md` asks for themselves. `/skills reinstall name` reinstalls the
  packages, `/skills log name` shows the install log. Unused environments are removed after `env_retention_days` days
  (the server checks this once a day); to remove them now, use `/skills clean-envs` or the button on the start page.
  Installing packages runs their code with your rights, so `[skills] install = false` turns it off, and
  `install_proposals = false` turns it off only for skills that agents proposed.
- **Docker sandbox.** The host's library is mounted into the container read-only (the source `host-library:name`); the
  sandbox has its own library in `<home>/.dreamteam/skills-library`. `~/.claude/skills` is not mounted: first add such
  a skill to the library on the host. Folder paths in the sandbox are container paths, so a zip is more convenient for
  uploading. There is no Node in the image.

```toml
[skills]
library_dir = "~/.dreamteam/skills"
claude = true             # offer Claude Code skills
max_attached = 40         # skills in one session
max_proposals = 10        # members' proposals in one session (0 means no proposals)
install = true            # install script dependencies automatically
install_proposals = true  # also for skills proposed by agents
env_retention_days = 30   # unused environments are removed after this many days (0 means never)
```
