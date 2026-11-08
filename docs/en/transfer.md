# Moving to another computer and backups

**English** · [Русский](../ru/transfer.md) · [All docs](README.md)

`dreamteam export FOLDER` writes the settings, skills, MCP servers and sessions of a DreamTeam home into one folder.
On another computer, `dreamteam import FOLDER` adds from it what is not there yet and rewrites what has changed.
The same two commands make a backup: export the home to an external disk, and import it back when you need it.

## Before you start

- **Version.** The commands exist in DreamTeam 0.1.1 and newer, and both computers need them: check
  `dreamteam --version`. 0.1.0 has no such commands: there `dreamteam export FOLDER` fails with
  `unrecognized arguments`, and `dreamteam export` without a folder opens the terminal interface with the goal
  "export". Keep the same version on both computers: an older one refuses what a newer one wrote (a new export
  format, new columns in a session's database, a new tool set of the agents).
- **Export** works while DreamTeam runs: the home is only read. A session that runs at that moment is exported as
  it is on disk right now, and the report warns about it.
- **Import** is refused while another DreamTeam process serves the target home: `dreamteam serve`, the terminal
  interface, `dreamteam mcp`. Close it and run the import again. The plan (`--dry-run`) can be shown while
  DreamTeam runs.
- **Which home.** Without `--home`, both commands use the current folder when it holds `dreamteam.toml`, and
  otherwise `$DREAMTEAM_HOME` or `~/DreamTeam`. A `dreamteam run` project (a `dreamteam.toml` without a `sessions/`
  folder) is not a home: run from it, both commands are refused; pass the home with `--home`.

## Export

```
dreamteam export FOLDER [--include CATEGORIES]... [--session ID]... [--dry-run] [--home HOME] [--quiet]
```

```powershell
dreamteam export E:\dreamteam                                 # everything: settings, skills, MCP, all sessions
dreamteam export E:\dreamteam --include settings,skills       # only the settings and the skills
dreamteam export E:\dreamteam --include sessions --session 003-snake
```

```bash
dreamteam export /media/usb/dreamteam --dry-run               # what would be written, and its size
dreamteam export ~/backups/dreamteam --home ~/work/DreamTeam  # another home
```

The destination must be a new or empty folder. A folder that already holds an earlier export (it has
`dreamteam-export.json`) is replaced: the new export is built next to it and takes its place only when it is
complete. Any other folder with files in it is refused. You cannot export to the root of a drive, inside the home
(its `sessions/` and `.dreamteam/`), inside the workspace of any session, or inside the skills or MCP library when
that category is exported. On Linux and macOS the export folder is created readable by you only (mode 0700).

At the end the report lists what was written per category, what was left out and why, the warnings, and the
command to run on the other computer.

### Categories

Without `--include` everything is exported. `--include` takes a comma list and may repeat; an unknown word is a
usage error (exit code 2).

- `settings` — every section of `dreamteam.toml`, including the model profiles `[models.*]`, the role catalog
  `[[roles]]` and the saved teams `[[teams]]`, but without `[mcp.external]` and without API keys.
- `skills` — the skills library: each skill's files and a record of where it came from.
- `mcp` — the MCP library (code, description and your approval) and the `[mcp.external]` servers: command, folder,
  description and the names of their `env` variables, without the values.
- `sessions` — whole sessions: the board, messages, agent histories, traces, the session's MCP servers, attached
  skills, background job logs and the workspace files.

`--session ID` (repeatable; a session's uid works too) picks sessions and adds the `sessions` category by itself.
The other categories still follow `--include`: `--session 003-snake` without `--include` also exports the
settings, skills and MCP; for that one session only, use `--include sessions --session 003-snake`.

A session's workspace is copied byte for byte, with `.git` and the agents' own `.scratch/<agent>` folders. Left out
are environments and caches that are rebuilt on the other computer (`node_modules`, `.venv`, `venv`,
`__pycache__`), the agents' copy of the skills `.scratch/_skills` (rebuilt at start) and links (symlinks,
junctions): the report warns about skipped links. The "executable" and "read-only" bits, and on Linux and macOS
the bits of files that only you may read (a key file with mode 0600, for example), are recorded in the export and
restored on import, so a FAT or exFAT stick is fine. A file of 4 GiB or more does not fit on FAT32: that
session is left out; use a drive formatted exFAT or NTFS.

A session that works in a folder outside the home (your project after `/cd` or `--workspace`) travels without the
files of that folder: only its path goes into the export. The report lists such sessions; on the other computer,
put the project at the same path or run `/cd` in the session (see
[Session folder and an existing project](projects.md)).

### What never goes into an export

- The API keys of the model profiles (`api_key`), also a key pasted into the variable-name field (`api_key_env`),
  and secret-looking `extra` fields of profiles (headers such as `Authorization`), with a warning. A variable name
  (`api_key_env = "ANTHROPIC_API_KEY"`) does travel.
- The `env` values of the `[mcp.external]` servers, in the global settings and in the settings of each session:
  only the variable names travel.
- Credentials in the commands of MCP servers: a password in a URL (`postgres://user:password@…`), the values of
  flags such as `--token`, `--api-key`, `--password`, `Authorization: …` headers, `?api_key=` parameters. They are
  removed from the command, and the report asks you to enter them again on the other computer. Their copies in
  the session's database (the event journal, the agents' histories) are removed too when the value has 8
  characters or more; a shorter one (`--password abc123`) stays there, and the secret check does not look for it
  either.
- Files of this computer: `dreamteam.toml.bak`, `.dreamteam/hub.json`, the markers of running processes
  `launcher-*.pid`, the DreamTeam logs (`.dreamteam/*.log` of the home and of each session; the logs of background
  jobs do travel with the session), the terminal interface's input history, the skills' dependency environments
  (`~/.dreamteam/skill-envs`), hub tokens (`DREAMTEAM_HUB_TOKEN` and `~/.dreamteam/hub-tokens`).
- The internal folders `sessions/.deleted-*`, `.importing-*` and `.replaced-*`.
- The `main` session of an old project: it cannot be moved yet (the report says so). A `dreamteam run` project is
  not a home, and the export refuses it (`is not a DreamTeam home`).
- A session whose workspace move did not finish (`.dreamteam/workspace-move.json`): the report says how to fix it.

### The secret check

Before the export is complete, DreamTeam searches every file it wrote for the secrets of this computer:

- the stored API keys of the profiles, and the values of the variables the profiles name in `api_key_env`;
- the values of environment variables the agents' console treats as secret (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
  `*_TOKEN`, `*PASSWORD*` and others, see [Security](security.md)), and the header values of
  `ANTHROPIC_CUSTOM_HEADERS` and `OPENAI_CUSTOM_HEADERS`;
- the values of the `[shell] pass_env` and `[skills] install_env` variables;
- the `env` values and the command credentials of the `[mcp.external]` servers, global and of every session, also
  of the sessions not exported;
- the hub token.

Each value is searched for as UTF-8 and as UTF-16 (how Windows PowerShell 5.1 writes files), and in its escaped
JSON form. Session databases are read row by row, and for every exported git repository its whole history is read,
deleted files included. Checking the history needs git: without it, an export with repositories fails. Values
shorter than 8 characters and the placeholder keys of local servers (`ollama`, `EMPTY`, `lm-studio`) are not
searched for.

When a secret is found, everything written is removed (an earlier export in that folder stays as it was), the
command exits with code 1 and names where it was found (a file, `table.column:rowid` in a database, or a git
object) and what kind of secret it is, never the value. Remove the secret there, or leave that part out
(`--include`, `--session`), and export again. When the secret is in a session's git history, deleting the file is
not enough: leave that session out. The credentials of the session's own MCP commands are removed from its
database by the export itself (see above); only the copies an agent wrote into workspace files or committed to git
stop the export.

The check knows only the secrets of this computer. Any other token an agent printed in its console or wrote to a
file travels with the session.

`--dry-run` writes nothing and shows what would be exported and its size; the secret check runs only when the
folder is really written.

## Import

```
dreamteam import FOLDER [--include CATEGORIES]... [--session ID|UID]... [--dry-run] [--yes] [--home HOME] [--quiet]
```

```powershell
dreamteam import E:\dreamteam --dry-run                       # the plan only
dreamteam import E:\dreamteam                                 # the plan, the [y/N] question, the import
dreamteam import E:\dreamteam --include sessions --session 003-snake
```

```bash
dreamteam import /media/usb/dreamteam --yes                   # no question (scripts, schedulers)
dreamteam import /media/usb/dreamteam --home ~/DreamTeam-copy # into a new empty folder: it becomes a home
```

First the command reads the manifest `dreamteam-export.json`. A folder without it (the export did not finish, or
it is another folder) and an export format this version does not know are refused. Then it prints the plan, one
line per item (`add`, `rewrite`, `unchanged`, `refused` or `skip` with the reason), and the warnings:

```
Import E:\dreamteam into C:\Users\anna\DreamTeam (exported on LAPTOP, 2026-11-05 20:32, DreamTeam 0.1.1):
  settings  dreamteam.toml: rewrite (sections: swarm; kept as they are here: server.port)
  skills    csv-tool: add
  sessions  003-ui: rewrite (the export has newer work in it; the old copy is kept in sessions/.replaced-003-ui-<time>)
  sessions  004-report: add as 005-report: 004-report is taken here
  mcp       external fetch: unchanged
Warnings:
  - enter TOKEN for fetch in Settings
Nothing else here changes: what the export does not carry stays as it is.
A rewritten session keeps its previous copy in sessions/.replaced-<id>-<time>/ (only the last one of each session).
Import? [y/N]
```

In a terminal the command asks `[y/N]` (`y`, `yes`, `д` and `да` count as yes; any other answer prints "Nothing
imported." and exits 0). Without a terminal (a script, a pipe) it needs `--yes` (or `-y`); otherwise nothing is
imported and the exit code is 1. `--dry-run` only prints the plan. Ctrl+C at the question exits with code 130 and
changes nothing. Ctrl+C during the import finishes or undoes the item it is on and then stops: the result marks the rest
`not done`, and running the import again brings them in.

Both commands show their progress on stderr. In a terminal it is one line that is rewritten in place, with a bar
when the size is known: `export  [########............]  40%  250/610 MB  session 003-snake (2/5): files`; the line
is cleared before the plan, the question, the warnings and the result are printed. An export first measures what it
will write (file sizes only), then counts once more while it checks the written files for secrets; a dry run shows
the stages without a bar. When stderr is not a terminal (a pipe, a log file, a scheduler) the progress is one plain
line per stage of each item, such as `export: session 003-snake (2/5): files`. The import also writes the lines of
the import itself (not those of the plan) to the home's log, `.dreamteam/logs/dreamteam.log`.
`--quiet` (`-q`) turns the progress off; the plan, the question, the warnings and the result still print.

An import deletes nothing the export does not carry: whatever is not in it stays as it is. What it does remove or
overwrite here while bringing the export in: the older backup copy of a session it rewrites (only the
last one is kept), the key, key variable and secret `extra` entries stored here for a model profile that now points
at another endpoint, the `env` values stored here for an MCP server whose command or folder changed, and the
`session.json` of a session rewritten in place (no copy is kept); the sections below say when. The settings from
before the import are kept in `dreamteam.toml.bak`. When `--home` names a new or empty folder, it becomes a
DreamTeam home, but only after you say yes.

`--include` and `--session` work as for the export; `--session` takes the id or uid of a session in the export.
A category you named in `--include` that the export does not hold gives a warning and exit code 1.

### Settings

- Sections are merged: the exported values win, keys that exist only here stay.
- Keys that belong to the computer itself keep the value they have here, in the global settings and in the
  settings of sessions: `server.host`, `server.port`, `shell.program`, `skills.library_dir`, `skills.envs_dir`,
  `skills.python`, `mcp.library_dir`. For the global settings the plan lists those whose exported value differs
  under "kept as they are here"; in the settings of sessions they are kept without a mention.
- The goal of `[swarm]` (`goal`) is not imported: goals belong to sessions.
- Keys this version does not know (an export from a newer version) are left out and listed.
- Model profiles are matched by name: new ones are added, existing ones rewritten, profiles that exist only here
  stay. The API key stored here stays as long as the profile keeps the same provider and address (`base_url`).
  When the exported profile points at another provider or address, what is set up here for the old one is
  removed, so that it is never sent to another server: the stored key, the key variable (`api_key_env`) when the
  export names none, and the secret-looking `extra` entries (an `Authorization` header, for example). The plan
  warns about each, for the key with `enter the key for profile <name> again`. A profile without a key on this
  computer and a profile on `localhost` get a warning too.
- Roles are matched by name: added or rewritten, the others stay. When a role's model profile does not exist here,
  the role uses the default profile.
- Teams are matched by name: added or rewritten, the others stay. A team naming a role this computer does not have
  stays, with a warning: fix it in Settings > Teams before choosing it.
- When the export's `default_model` profile does not exist here, this computer's one stays.

### Skills

A skill is matched by its folder name. The same files: `unchanged`; other files: `rewrite`; a skill not here yet:
`add`. The other skills of the library are not touched.

### MCP servers

- **The library.** A server is matched by name: the same code and the same description, approval and project
  folder is `unchanged`; otherwise it is rewritten (the plan says what differs) or added. The approval travels
  with the server: a server approved there is approved here, and when `[mcp] require_approval` is on here, the
  plan warns about it. A server with requirements needs uv on this
  computer. A server that an exported session saved belongs to that session again after the import, whatever id
  the session gets here.
- **`[mcp.external]`.** The command, folder and description come from the export. The `env` values stored here
  stay only when the command (credentials aside) and the folder are unchanged; then the credentials in the command
  stay as well. When the server runs differently, the values stored here are removed and the plan asks you to enter
  them again. For variable names without a value on this computer the plan says
  `enter <NAME> for <server> in Settings`. Servers that exist only here stay. A program not on PATH and a folder
  that does not exist get a warning.

### Sessions

A session is recognized by its identity (the uid in `session.json`), never by its number: two computers easily
each have a different `017-session`. Sessions made before the uid existed get one derived from their creation time,
so a folder you once copied by hand is recognized on both computers too.

- **A new session** is added under the same id when it is free, otherwise under the next free number with a name
  made from its title (`add as 018-quarterly-report: 017-report is taken here` for a session titled "Quarterly
  report").
- **The same history and the same files**: `unchanged`. When only the title, the settings or the pending goal
  differ, only `session.json` is rewritten, with no backup.
- **Otherwise the session is rewritten**, also when this computer has newer work in it or both computers worked on
  it. The plan says why: `the export has newer work in it`, `this computer has newer work in it`, `both computers
  worked on it since they last had the same copy`, or `its files differ` (the same history, other files). The
  result repeats the reason, except `the export has newer work in it`. The previous copy is kept in
  `sessions/.replaced-<id>-<time>/` (`session/` is the session folder, `workspace/` its folder in the home, if it
  had one). Only the last copy of each session is kept: an older one is deleted at the next rewrite of the same
  session. There is no restore command yet: with DreamTeam stopped, move the folders back by hand.
- **What a rewrite keeps from this computer:** the folder outside the home the session works in here (after `/cd`),
  the `env` values of MCP servers in the session's settings (same rule as for the global ones), the computer's own
  keys in its settings, and the `.venv`, `venv` and `node_modules` folders of the old workspace, which move into the
  new one.
- **Two sessions with one uid** (a folder copied by hand) in the export: importing them is refused, naming both
  ids. Import them by id with `--session ID`: each is matched by its id here, and a copy that is added gets a new
  uid when a session with the shared one is already here or comes in with this import.
- **Two sessions with one uid here**: importing the session with that uid is refused, naming both. When the
  exported session has the id of one of them, `--session ID` rewrites that one. Otherwise `--session` does not
  help: delete or move the copy you do not want here, then import again.
- **Only that session is refused** (the others are imported) when its database was made by a newer DreamTeam
  (unknown tables and columns, a newer tool set), when the database changed after the export (its checksum does not
  match), when a move of its workspace did not finish on either computer, when its folder would overlap another
  session's folder or the export folder, when the folder outside the home it works in cannot be used here (see
  below), or when the export carries none of its files while its folder here has some.
- File names this computer's file system cannot hold (characters Windows refuses; names that differ only in case,
  on Windows and macOS) get a warning: such a session cannot be copied until you rename them on the source
  computer.
- When the export was made on another OS, the agents hired there keep that computer's shell (for example
  PowerShell) in their instructions, and the paths in their histories are that computer's paths.

Imported sessions never start by themselves: start them on the sessions page or with `dreamteam -r ID`.

Where a session works:

- a session with its own folder works in its own folder here too;
- a session with a folder inside the home gets a folder at the same path in this home, or the sibling
  `<folder>-<id>` when that one is taken (the plan says so);
- a session with a folder outside the home keeps only the path, without files. When that folder exists here and
  has files, the session works in it as in your project. An empty folder there is used as the session had it on
  the other computer (as your project only when it was one there). When it does not exist, put the project at
  that path or run `/cd` in the session. The session is refused when that path is a file here, overlaps the export
  folder or another session's folder, or is a folder `/cd` would refuse (the home, your user folder, a library, the
  root of a drive and the like).

When an import stopped halfway through replacing a session (a power cut, for example), the next import first
finishes or undoes that replacement. When that puts a session folder back, this import brings in no session: they
are marked `not done` (`an earlier import was put in order first: import again`), the exit code is 1, and running
the import once more brings them in. When another DreamTeam process starts during the import, the sessions the
import has not reached yet are not added or replaced, and the result names that process; the MCP servers, which
come after the sessions, are still imported.

## Moving the folder

- Copy the export folder as plain files: to a USB stick or an external disk, through a network share or a cloud
  folder (OneDrive, Dropbox, Google Drive, Yandex Disk). Wait until it has synced completely before you import:
  a session whose database arrived incomplete is refused.
- Do not move an export through git: git does not keep the nested `.git` folders of the workspaces and may change
  line endings (`core.autocrlf`), and a changed `server.py` of an MCP server loses its approval and is built again.
- The export folder holds the full agent histories, messages, boards and session files. Keep it private, and import
  only your own exports: the MCP approvals and skills of an export are trusted as yours.

## One DreamTeam folder on two computers

Do not run one DreamTeam home on two computers at the same time (a shared network folder or a synced folder): both
would write to the same databases. The hub records the computer's name in `.dreamteam/hub.json` and removes that
file when it exits. When a hub or the terminal interface starts on a folder whose `hub.json` names another computer
(its hub still runs there, or stopped without removing the file), it warns:

```
warning: <home> was last served by the computer LAPTOP. Do not run one DreamTeam folder on two computers at the same
time; move sessions with dreamteam export / dreamteam import.
```

This is only a warning; the start goes ahead. After a hub that exited normally there is no `hub.json` and no
warning. An import is refused while the folder holds the `hub.json` of another computer's hub when that hub
served the folder under another path (`/Users/anna/DreamTeam` there, `C:\Users\anna\DreamTeam` here), or when its
process number belongs to a running process here: stop DreamTeam there, or delete that file if that hub no longer
runs. When both computers see the folder under the same path, the import cannot tell that hub from a stopped one
and goes ahead: make sure DreamTeam does not run on the other computer first. To work on two computers in turn,
move the sessions with export and import.

## Backups

An export is a backup too: run `dreamteam export D:\backups\dreamteam` from time to time. A new export into the
same folder replaces the previous one. To restore, run `dreamteam import D:\backups\dreamteam` into the home, or
with `--home NEW_FOLDER` into a new one. API keys and `env` values are not in the backup: keep them separately.
Neither are the files of a session that works in a folder outside the home (your project after `/cd` or
`--workspace`): the backup holds only its path, so back up that project yourself.

## Exit codes

- `0`: everything was exported or imported (or is unchanged), and also a "no" to the import question;
- `1`: something was skipped, refused or failed: a secret was found, a session could not be exported, an import
  without a terminal and without `--yes`, an import while DreamTeam runs; for `--dry-run`, when something would be
  refused;
- `2`: a usage error: an unknown category in `--include`, a session that does not exist;
- `130`: Ctrl+C.
