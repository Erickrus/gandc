# OpenCode Session Architecture: Shared DB, Separate Skills

## Overview

Run multiple OpenCode instances in isolated sessions where each instance has its own set of skills but all share a single `opencode.db` for session history and state.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Shared Resources                   │
│                                                     │
│  ~/.local/share/opencode/opencode.db   (SQLite WAL) │
│  ~/.config/opencode/opencode.json      (shared cfg) │
└──────────────┬──────────────────┬────────────────────┘
               │                  │
       ┌───────┴───────┐  ┌──────┴────────┐
       │   Session 1   │  │   Session 2   │
       │               │  │               │
       │  /session-1/  │  │  /session-2/  │
       │    skills/    │  │    skills/    │
       │      foo/     │  │      bar/     │
       │    project/   │  │    project/   │
       └───────────────┘  └───────────────┘
```

## How It Works

### Shared Database

The database path is resolved independently of the working directory:

- Default: `~/.local/share/opencode/opencode.db`
- Override: `OPENCODE_DB=/path/to/shared/opencode.db`

SQLite WAL mode with `busy_timeout = 5000ms` ensures safe concurrent access from multiple session instances. Sessions are scoped by `project_id`, so data from different $XDG won't collide.

### Per-Session Skills

OpenCode discovers skills relative to the current working directory using the pattern:

```
{skill,skills}/**/SKILL.md
```

Each session simply has its own `skills/` directory with the relevant SKILL.md files. No configuration change is needed — running opencode from a different directory automatically picks up different skills.

## Directory Layout

```
/$XDG/
├── session-1/
│   ├── skills/
│   │   ├── deploy/
│   │   │   └── SKILL.md
│   │   └── monitor/
│   │       └── SKILL.md
│   └── ... (project files)
│
├── session-2/
│   ├── skills/
│   │   ├── analyze/
│   │   │   └── SKILL.md
│   │   └── report/
│   │       └── SKILL.md
│   └── ... (project files)
│
└── session-3/
    ├── skills/
    │   └── custom-tool/
    │       └── SKILL.md
    └── ... (project files)
```

## Configuration

### Single Shared opencode.json

Place one config at `~/.config/opencode/opencode.json` (or `opencode.jsonc`) with shared settings:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "provider/model-name",
  "permission": [
    // shared permission rules
  ]
}
```

No per-session config files are needed. The `skills` field in config is optional — filesystem discovery handles the differentiation.

### Launching Each Session

```bash
# Session 1
cd /$XDG/session-1 && opencode

# Session 2
cd /$XDG/session-2 && opencode

# Or with explicit DB path if home dirs differ:
cd /$XDG/session-1 && OPENCODE_DB=/shared/opencode.db opencode
```

## Skill File Format

Each skill is a markdown file with YAML frontmatter:

```markdown
---
name: my-skill
description: Short description of what this skill does and when to use it
---

Skill instructions and content go here.
The model reads this when the skill is invoked.
```

## Concurrency Safety

| Concern | Mitigation |
|---------|-----------|
| Concurrent DB writes | SQLite WAL mode + 5s busy timeout |
| Session isolation | Each session has a unique `project_id` tied to its directory |
| Skill conflicts | Each session only sees skills in its own working directory |
| Config conflicts | Single shared config — no per-instance mutation needed |

## Optional: Per-Session Config Override

If $XDG need different model settings or permissions (not just skills), use environment variables:

```bash
# Different config per session
OPENCODE_CONFIG=/configs/session1.jsonc OPENCODE_DB=/shared/opencode.db opencode

# Or different config directory
OPENCODE_CONFIG_DIR=/configs/session1/ OPENCODE_DB=/shared/opencode.db opencode
```
