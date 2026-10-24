# DreamTeam documentation

**English** · [Русский](../ru/README.md) · [Project README](../../README.md)

Getting started:

- [Install and quick start](getting-started.md): installing, the first session, every `dreamteam` command
- [Features](features.md): everything DreamTeam does, in one table

Working with the swarm:

- [Terminal UI and console](terminal.md): slash commands, keys, headless mode (`dreamteam -p`)
- [Session folder and an existing project](projects.md): pointing the swarm at your own repository (`/cd`, `--workspace`)
- [Connecting Claude Code](claude-code.md): the swarm as an MCP server, `join_swarm`
- [Skills](skills.md): Agent Skills for the swarm — library, attaching, agents' proposals, dependencies
- [Agent traces](traces.md): every agent wake-up step by step, with tokens and cost

Setting up and running:

- [Configuration](configuration.md): `dreamteam.toml`, models, roles and the starting team
- [Docker sandbox and sessions without network](sandbox.md)
- [Security](security.md)

How it works inside:

- [How it works](architecture.md): the architecture and the agent loop
- [Agent tools](agent-tools.md): what agents can do and how they write their own MCP servers
- [Module specification](../SPEC.md) and the [terminal UI contract](../TUI.md)
