# OpenCode Persistency Analysis

## Overview

OpenCode uses the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html) to determine where to store its data. The primary data directory is `~/.local/share/opencode/` (i.e. `$XDG_DATA_HOME/opencode`).

Source code reference: `packages/core/src/global.ts`

---

## 1. XDG Directory Layout

OpenCode creates and uses the following XDG-derived directories:

| XDG Variable       | Default Path                    | OpenCode Subdirectory  | Purpose                                        |
|--------------------|---------------------------------|------------------------|------------------------------------------------|
| `$XDG_DATA_HOME`  | `~/.local/share/opencode/`      | (root)                 | Primary persistent data (DB, auth, repos, etc.)|
| `$XDG_CACHE_HOME` | `~/.cache/opencode/`            | `bin/`                 | Downloaded LSP binaries and tools              |
| `$XDG_CONFIG_HOME` | `~/.config/opencode/`          | (root)                 | Global user configuration files                |
| `$XDG_STATE_HOME` | `~/.local/state/opencode/`      | (root)                 | File locks (flock) for process coordination    |
| (system temp)      | `/tmp/opencode/`                | (root)                 | Temporary files                                |

---

## 2. Data Directory Structure (`~/.local/share/opencode/`)

```
~/.local/share/opencode/
├── opencode.db              # Main SQLite database (WAL mode)
├── opencode.db-wal          # SQLite Write-Ahead Log
├── opencode.db-shm          # SQLite Shared Memory (WAL index)
├── ._opencode.db            # macOS AppleDouble resource fork (see below)
├── ._opencode.db-wal        # macOS AppleDouble resource fork (see below)
├── ._opencode.db-shm        # macOS AppleDouble resource fork (see below)
├── auth.json                # Provider API key/token storage (mode 0600)
├── bin/                     # Downloaded LSP server binaries (gopls, eslint, etc.)
├── log/                     # Application log files (structured key=value format)
├── repos/                   # Cloned reference repositories cache (for GitHub refs)
├── snapshot/                # Git-based file snapshot storage (per project+worktree)
├── storage/                 # General-purpose file storage
└── tool-output/             # Bounded tool output files (max 7-day retention)
```

---

## 3. File-by-File Explanation

### `opencode.db` — Main SQLite Database
The single source of truth for sessions, messages, projects, permissions, credentials, and accounts. Uses WAL mode with `synchronous = NORMAL` and a 5000ms busy timeout. Detailed schema in Section 5.

### `opencode.db-wal` (Write-Ahead Log)
SQLite WAL mode companion file. Contains committed transactions not yet checkpointed into the main DB file. **CRITICAL**: if this file is non-empty and you copy only `opencode.db` without it, you lose recent writes and may get a corrupted database. Always copy as a trio (`opencode.db` + `opencode.db-wal` + `opencode.db-shm`) or checkpoint first.

### `opencode.db-shm` (Shared Memory)
Memory-mapped index for the WAL. Used for concurrent reader coordination. Without this file, SQLite will rebuild it on next open — it's safe to lose this one alone (unlike the WAL), but copying it along prevents a brief re-index cost.

### `._opencode.db`, `._opencode.db-wal`, `._opencode.db-shm` — macOS AppleDouble Resource Forks

These files are **NOT created by OpenCode**. They are macOS filesystem artifacts. When you copy files from an APFS/HFS+ volume to a non-Apple filesystem (ext4, NFS, CIFS, a Docker volume, etc.), macOS writes extended attributes and Finder metadata into "sidecar" `._` files using the AppleDouble format.

**Binary header**: `00 05 16 07` followed by `Mac OS X` — this is the standard AppleDouble magic number.

**Contents**: Finder flags, resource forks, spotlight metadata, quarantine bits. Typically 163 bytes for a minimal entry with no actual resource data.

**Relevance to persistence**:
- These files are **completely irrelevant** to OpenCode's operation.
- They should be **excluded from backups and migrations** (`--exclude='._*'` in rsync).
- They will appear whenever the data directory is on a non-Apple filesystem accessed from macOS (common when using Docker Desktop on macOS, or copying to a Linux server).
- They are safe to delete at any time.
- On Linux-only environments, they will never appear unless the directory was originally synced from macOS.

### `auth.json`
Stores per-provider authentication credentials as a JSON object keyed by provider ID. Written with restrictive permissions (`0600`). Supports three credential types:
- `"api"` — API key (`{ "type": "api", "key": "sk-..." }`)
- `"oauth"` — OAuth tokens (`{ "type": "oauth", "refresh": "...", "access": "...", "expires": ... }`)
- `"wellknown"` — Well-known key+token pair

Can be overridden entirely by the environment variable `OPENCODE_AUTH_CONTENT` (JSON string).

### `bin/`
Cache for auto-downloaded LSP server binaries (gopls, rubocop, eslint, zls, clangd, elixir-ls, etc.). Technically lives under `$XDG_CACHE_HOME/opencode/bin/` in code, but the startup ensures the path exists. Platform-specific — not transferable across architectures.

### `log/`
Structured log files in `key=value` format. Each log entry includes timestamp, level, run ID, and structured annotations. Ephemeral — informational only.

### `repos/`
Cloned Git repositories used as reference caches. Path structure: `repos/{host}/{owner}/{repo}` (e.g., `repos/github.com/Effect-TS/effect`). Used by the GitHub handler and reference resolution system. Can be regenerated on demand.

### `snapshot/`
Per-project Git object storage for file snapshots. Path: `snapshot/{project_id}/{hash_of_worktree}`. Uses a bare Git repository (`--git-dir`) to track and restore file states during sessions. Enables undo/revert functionality. The hash is computed from the worktree absolute path.

### `storage/`
General file storage for future/optional use.

### `tool-output/`
Bounded storage for large tool call outputs (bash, webfetch, etc.). Files are:
- Named by session ID + tool call ID (via `Identifier`)
- Capped at 2000 lines / 50 KB per output
- Auto-pruned after 7 days (`RETENTION = Duration.days(7)`)

---

## 4. Naming Conventions

### ID Generation

All entity IDs use a custom scheme from `packages/core/src/util/identifier.ts`:

```
Format: {6-byte-timestamp-hex}{14-char-base62-random}
Total:  26 characters
```

- Timestamp is Unix ms, multiplied by `0x1000` + monotonic counter
- "Descending" IDs use bitwise NOT on the timestamp portion (newer = lexicographically smaller for sort)
- Session IDs are prefixed: `ses_` + descending ID (e.g., `ses_ff8a3b2c1d0eFk9Qm2xWnT4p7R`)

### Database File Naming

| Channel | File Name |
|---------|-----------|
| `latest`, `beta`, `prod` | `opencode.db` |
| Other (e.g., `local`, `dev`) | `opencode-{channel}.db` |
| `OPENCODE_DISABLE_CHANNEL_DB=1` | `opencode.db` (forced) |
| `OPENCODE_DB` env var | Custom path/name |

### Project IDs

Project IDs are deterministic — derived from `projectV2.resolve()` which hashes based on the Git worktree root path. This means the **same project on a different machine will get a different project ID** if the absolute path differs. A special `"global"` project ID exists for sessions without a project context.

### Snapshot Paths

`snapshot/{project_id}/{Hash.fast(worktree_absolute_path)}/` — the path is doubly tied to the machine's filesystem layout.

---

## 5. Database Schema (SQLite)

The database uses Drizzle ORM and is migrated via a sequential migration system (35+ TypeScript migration files). The current schema has 18 tables:

### Table: `session`
The central entity — represents a conversation/task session.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | `ses_` + descending ID |
| `project_id` | TEXT FK→project | CASCADE delete |
| `workspace_id` | TEXT | Optional workspace association |
| `parent_id` | TEXT | For forked/child sessions |
| `slug` | TEXT | Human-readable slug |
| `directory` | TEXT | Working directory (absolute path, normalized to `/`) |
| `path` | TEXT | Optional subpath within project |
| `title` | TEXT | Session title |
| `version` | TEXT | App version that created it |
| `share_url` | TEXT | Public share URL if shared |
| `summary_additions` | INTEGER | Lines added |
| `summary_deletions` | INTEGER | Lines deleted |
| `summary_files` | INTEGER | Files changed |
| `summary_diffs` | TEXT (JSON) | Array of `FileDiff` objects |
| `metadata` | TEXT (JSON) | Arbitrary metadata |
| `cost` | REAL | Accumulated cost ($) |
| `tokens_input` | INTEGER | Total input tokens |
| `tokens_output` | INTEGER | Total output tokens |
| `tokens_reasoning` | INTEGER | Reasoning tokens |
| `tokens_cache_read` | INTEGER | Cache read tokens |
| `tokens_cache_write` | INTEGER | Cache write tokens |
| `revert` | TEXT (JSON) | Revert info (messageID, snapshot, diff) |
| `permission` | TEXT (JSON) | Session-level permission ruleset |
| `agent` | TEXT | Agent ID used |
| `model` | TEXT (JSON) | `{id, providerID, variant?}` |
| `time_created` | INTEGER | Unix timestamp ms |
| `time_updated` | INTEGER | Unix timestamp ms |
| `time_compacting` | INTEGER | When compaction started |
| `time_archived` | INTEGER | When archived |

Indexes: `session_project_idx`, `session_workspace_idx`, `session_parent_idx`

### Table: `session_message`
The v2 message projection — ordered messages within a session.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Message ID |
| `session_id` | TEXT FK→session | CASCADE delete |
| `type` | TEXT | Message type (user/assistant/system/etc.) |
| `seq` | INTEGER | Sequence number within session |
| `time_created` | INTEGER | Unix timestamp ms |
| `time_updated` | INTEGER | Unix timestamp ms |
| `data` | TEXT (JSON) | Full message content |

Indexes: unique `(session_id, seq)`, `(session_id, type, seq)`, `(session_id, time_created, id)`, `(time_created)`

### Table: `session_input`
Queued user inputs for a session (inbox pattern for async delivery).

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Input ID |
| `session_id` | TEXT FK→session | CASCADE delete |
| `prompt` | TEXT (JSON) | The prompt content |
| `delivery` | TEXT | Delivery mode |
| `admitted_seq` | INTEGER | When admitted to queue |
| `promoted_seq` | INTEGER | When promoted to active |
| `time_created` | INTEGER | Unix timestamp ms |

### Table: `session_context_epoch`
Tracks context window compaction state per session.

| Column | Type | Notes |
|--------|------|-------|
| `session_id` | TEXT PK FK→session | CASCADE delete |
| `baseline` | TEXT | Baseline context hash |
| `agent` | TEXT | Agent ID (default: 'build') |
| `snapshot` | TEXT (JSON) | SystemContext snapshot |
| `baseline_seq` | INTEGER | Sequence of baseline |
| `replacement_seq` | INTEGER | When replacement happened |
| `revision` | INTEGER | Epoch revision counter |

### Table: `message` (v1 legacy)
Legacy message storage format.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Message ID |
| `session_id` | TEXT FK→session | CASCADE delete |
| `time_created` / `time_updated` | INTEGER | Unix timestamp ms |
| `data` | TEXT (JSON) | Full message data |

### Table: `part` (v1 legacy)
Legacy message parts (tool calls, text segments).

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Part ID |
| `message_id` | TEXT FK→message | CASCADE delete |
| `session_id` | TEXT | Session reference |
| `time_created` / `time_updated` | INTEGER | Unix timestamp ms |
| `data` | TEXT (JSON) | Part content |

### Table: `project`
Represents a code project (identified by git worktree root).

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Deterministic from worktree path |
| `worktree` | TEXT | Absolute path to worktree root (normalized `/`) |
| `vcs` | TEXT | Version control system type |
| `name` | TEXT | Display name |
| `icon_url` / `icon_url_override` / `icon_color` | TEXT | Visual metadata |
| `time_created` / `time_updated` / `time_initialized` | INTEGER | Timestamps |
| `sandboxes` | TEXT (JSON) | Array of absolute paths |
| `commands` | TEXT (JSON) | `{start?: string}` |

### Table: `project_directory`
Maps additional directories to a project.

| Column | Type | Notes |
|--------|------|-------|
| `project_id` | TEXT FK→project | CASCADE delete |
| `directory` | TEXT | Absolute path |
| `type` | TEXT | `"main"`, `"root"`, or `"git_worktree"` |
| `strategy` | TEXT | Copy/link strategy |

PK: `(project_id, directory)`

### Table: `workspace`
Logical workspace grouping.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Workspace ID |
| `type` | TEXT | Workspace type |
| `name` | TEXT | Display name |
| `branch` / `directory` | TEXT | Git branch / path |
| `extra` | TEXT (JSON) | Additional data |
| `project_id` | TEXT FK→project | CASCADE delete |
| `time_used` | INTEGER | Last usage timestamp |

### Table: `permission`
Saved permission rules per project.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Permission ID |
| `project_id` | TEXT FK→project | CASCADE delete |
| `action` | TEXT | e.g., "write", "execute" |
| `resource` | TEXT | Resource pattern |

Unique index: `(project_id, action, resource)`

### Table: `account`
OAuth account storage for control-plane authentication.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Account ID |
| `email` / `url` | TEXT | Identity |
| `access_token` / `refresh_token` | TEXT | Tokens |
| `token_expiry` | INTEGER | Expiry timestamp |

### Table: `account_state`
Singleton — tracks which account/org is currently active.

### Table: `control_account` (Legacy)
Legacy control-plane accounts (PK: `email` + `url`).

### Table: `credential`
Integration credentials (API keys for integrations/connectors).

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Credential ID |
| `integration_id` / `connector_id` / `method_id` | TEXT | References |
| `label` | TEXT | Human name |
| `value` | TEXT (JSON) | Credential payload |
| `active` | INTEGER (bool) | Active flag |

### Table: `event_sequence` / `event`
Event sourcing tables. `event_sequence` holds aggregate roots with sequence counters. `event` stores individual events with `(aggregate_id, seq)` uniqueness.

### Table: `todo`
Task list items within a session. PK: `(session_id, position)`.

### Table: `session_share`
Shared session URLs/secrets. PK: `session_id`.

### Table: `migration` / `data_migration`
Schema and data migration tracking tables.

---

## 6. Entity Relationship Diagram

```
project (1)
  ├── workspace (many)
  │     └── session.workspace_id
  ├── project_directory (many)
  ├── permission (many)
  └── session (many)
        ├── session_message (many, ordered by seq)
        ├── session_input (many, queued prompts)
        ├── session_context_epoch (0-1, compaction state)
        ├── message [v1 legacy] (many)
        │     └── part [v1 legacy] (many)
        ├── todo (many, ordered by position)
        ├── session_share (0-1)
        └── session.parent_id → session (fork parent)

account (many)
  └── account_state.active_account_id (singleton pointer)

control_account [legacy] (many, keyed by email+url)
credential (many, standalone)
event_sequence → event (1:many, event sourcing)
migration / data_migration (tracking tables)
```

---

## 7. Environment Variable Reference

### `OPENCODE_CONFIG_DIR`

**What it does**: Adds an extra config-reading directory. In `Global.make()`:
```ts
config: Flag.OPENCODE_CONFIG_DIR ?? Path.config
```
And in config resolution, it's appended as an additional directory to scan for `config.json`/`opencode.json`/`opencode.jsonc`.

**What it does NOT change**:
- The **data directory** (`~/.local/share/opencode/`) — DB, auth.json, snapshots all remain at their XDG paths
- The **cache**, **state**, or **tmp** directories
- The **database path** (always derives from `Global.Path.data`)

So `OPENCODE_CONFIG_DIR` is purely about where config files are **read from**, not where any persistent data is **stored**.

### Other Env Vars

| Variable | Effect |
|----------|--------|
| `OPENCODE_DB` | Override DB file path (`:memory:`, absolute, or relative to data dir) |
| `OPENCODE_AUTH_CONTENT` | Override `auth.json` entirely with JSON string |
| `OPENCODE_DISABLE_CHANNEL_DB` | Force DB name to `opencode.db` regardless of channel |
| `OPENCODE_DISABLE_PROJECT_CONFIG` | Skip `.opencode/` dirs in project tree |
| `XDG_DATA_HOME` | Override data directory base |
| `XDG_CONFIG_HOME` | Override config directory base |
| `XDG_CACHE_HOME` | Override cache directory base |
| `XDG_STATE_HOME` | Override state directory base |

---

## 8. The Multi-Instance Session Restore & Merge Problem

### Problem Statement

You have multiple OpenCode instances (different servers, containers with separate PVCs), each with their own `~/.local/share/opencode/opencode.db`. You want to:

1. **List sessions** across all instances elegantly
2. **Restore/continue a session** from one instance on another
3. **Merge** databases from multiple instances into a unified view

### Why This Is Hard

1. **Project IDs are path-dependent** — A project ID is deterministically derived from the absolute worktree path. The same repo at `/home/alice/myapp` and `/home/bob/myapp` produces different project IDs. Sessions FK to projects.

2. **Directory columns store absolute paths** — `session.directory`, `project.worktree`, `project_directory.directory` are all absolute paths. These become meaningless or conflicting across machines.

3. **Snapshot data is path-coupled** — The `snapshot/` directory uses `{project_id}/{hash(worktree_path)}`. Restoring a session's revert/undo capability requires the corresponding snapshot Git repo.

4. **Session IDs use descending timestamps** — They're globally unique (crypto random + timestamp), so no collision risk from merging. But `seq` numbers within `session_message` could theoretically be resumed incorrectly if a session was active on both sides.

5. **No built-in "origin" marker** — The DB has no concept of "which instance produced this row."

---

## 9. Proposed Solutions

### Solution A: Centralized Database with OPENCODE_DB

**Concept**: Point all instances to a shared SQLite file on a network volume (NFS/EFS) or use `OPENCODE_DB` to redirect.

```bash
export OPENCODE_DB=/shared-pvc/opencode.db
```

**Pros**:
- Zero merge logic needed — single source of truth
- Sessions immediately visible across instances
- Native OpenCode behavior, no custom code

**Cons**:
- SQLite over NFS is fragile — WAL mode requires shared memory, which NFS doesn't support well. Corruption risk is high.
- Performance degrades significantly over network filesystems
- Single point of failure
- Concurrent writers from multiple instances can hit `SQLITE_BUSY` despite the 5s timeout
- `snapshot/` and `tool-output/` still local — undo/revert breaks cross-machine

**Verdict**: Only viable if you use a single instance at a time (mutex access). Not recommended for concurrent use.

---

### Solution B: Export/Import with SQLite Dump + Remap Script

**Concept**: Write a Python script that exports sessions from one DB and imports them into another, remapping project IDs and paths as needed.

```python
# Pseudocode
def export_sessions(source_db, session_ids) -> dict:
    """Extract session + all related rows as portable JSON"""

def import_sessions(target_db, data, path_mapping):
    """Insert sessions, remap project_id and directory columns"""
```

**Key operations**:
1. Export: `session` + `session_message` + `session_input` + `session_context_epoch` + `todo` + `session_share` for selected session IDs
2. On import: either reuse existing project (if same worktree path exists on target) or create a new project entry
3. Remap `session.directory` if the project lives at a different absolute path on the target

**Pros**:
- Full control over conflict resolution
- Can handle path remapping cleanly
- Works with any storage backend (dump to JSON, store in Postgres, S3, etc.)
- Selective — export only what you need

**Cons**:
- Snapshot/revert data is NOT portable (tied to Git object store on source machine)
- `session_context_epoch` baseline/snapshot references may be invalidated
- v1 `message`/`part` tables need handling for older sessions
- Must handle FK ordering carefully (project first, then session, then messages)
- Event sourcing tables (`event`/`event_sequence`) may reference sessions indirectly

**Implementation sketch** (Python):

```python
import sqlite3
import json
import os

def export_session(db_path, session_id):
    """Export a session and all its children to a portable dict."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    session = dict(conn.execute(
        "SELECT * FROM session WHERE id = ?", (session_id,)
    ).fetchone())

    messages = [dict(r) for r in conn.execute(
        "SELECT * FROM session_message WHERE session_id = ? ORDER BY seq",
        (session_id,)
    ).fetchall()]

    inputs = [dict(r) for r in conn.execute(
        "SELECT * FROM session_input WHERE session_id = ?", (session_id,)
    ).fetchall()]

    epoch = conn.execute(
        "SELECT * FROM session_context_epoch WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    todos = [dict(r) for r in conn.execute(
        "SELECT * FROM todo WHERE session_id = ?", (session_id,)
    ).fetchall()]

    # v1 legacy
    v1_messages = [dict(r) for r in conn.execute(
        "SELECT * FROM message WHERE session_id = ?", (session_id,)
    ).fetchall()]
    v1_parts = []
    for msg in v1_messages:
        v1_parts.extend([dict(r) for r in conn.execute(
            "SELECT * FROM part WHERE message_id = ?", (msg['id'],)
        ).fetchall()])

    project = dict(conn.execute(
        "SELECT * FROM project WHERE id = ?", (session['project_id'],)
    ).fetchone() or {})

    conn.close()
    return {
        "version": 1,
        "source_db": db_path,
        "session": session,
        "messages": messages,
        "inputs": inputs,
        "epoch": dict(epoch) if epoch else None,
        "todos": todos,
        "v1_messages": v1_messages,
        "v1_parts": v1_parts,
        "project": project,
    }


def import_session(db_path, data, directory_remap=None):
    """Import a session into target DB, optionally remapping paths."""
    conn = sqlite3.connect(db_path)

    session = data["session"]
    project = data["project"]

    # Remap directory if needed
    if directory_remap and session.get("directory"):
        for old_prefix, new_prefix in directory_remap.items():
            if session["directory"].startswith(old_prefix):
                session["directory"] = new_prefix + session["directory"][len(old_prefix):]
            if project.get("worktree", "").startswith(old_prefix):
                project["worktree"] = new_prefix + project["worktree"][len(old_prefix):]

    # Upsert project
    existing = conn.execute(
        "SELECT id FROM project WHERE id = ?", (project["id"],)
    ).fetchone()
    if not existing and project:
        cols = ", ".join(project.keys())
        placeholders = ", ".join("?" * len(project))
        conn.execute(f"INSERT OR IGNORE INTO project ({cols}) VALUES ({placeholders})",
                     list(project.values()))

    # Insert session
    cols = ", ".join(session.keys())
    placeholders = ", ".join("?" * len(session))
    conn.execute(
        f"INSERT OR REPLACE INTO session ({cols}) VALUES ({placeholders})",
        list(session.values())
    )

    # Insert messages
    for msg in data["messages"]:
        cols = ", ".join(msg.keys())
        placeholders = ", ".join("?" * len(msg))
        conn.execute(
            f"INSERT OR REPLACE INTO session_message ({cols}) VALUES ({placeholders})",
            list(msg.values())
        )

    # ... similar for inputs, epoch, todos, v1_messages, v1_parts

    conn.commit()
    conn.close()
```

**Verdict**: Best balance of control and feasibility. Recommended for most use cases.

---

### Solution C: Federated Session Index (Meta-DB + Lazy Fetch)

**Concept**: Build a lightweight "session registry" that indexes sessions across multiple PVCs/DBs without merging them. Each source DB stays in place. You query the registry to list sessions, then point OpenCode at the correct source when you want to resume.

```
┌─────────────────────────────────────────────┐
│           Session Registry (Postgres/SQLite) │
│                                             │
│  session_id | title | source | directory    │
│  ses_abc... | "fix" | pvc-1  | /app/myproj  │
│  ses_def... | "feat"| pvc-2  | /app/myproj  │
└─────────────────────────────────────────────┘
         │                    │
    ┌────┴────┐          ┌───┴────┐
    │ PVC-1   │          │ PVC-2  │
    │ .db     │          │ .db    │
    └─────────┘          └────────┘
```

**Implementation**:
1. A cron/daemon scans each mounted PVC's `opencode.db`, syncing session metadata into the registry
2. To restore: copy the session (Solution B's export) from the source PVC into the active instance's DB
3. Or: symlink/bind-mount the correct PVC as `~/.local/share/opencode/` before starting OpenCode

**Pros**:
- No data duplication until you actually restore
- Handles unlimited sources
- Registry can be Postgres for proper multi-tenant access
- Clean separation of "index" vs "data"

**Cons**:
- Complexity — two systems to maintain
- Restore still requires the Solution B import logic
- Stale index if source DBs change without re-scan
- Doesn't solve the path-remapping problem (just defers it)

**Verdict**: Good for "browse and pick" workflows. Overkill if you only have 2-3 sources.

---

### Solution D: WAL-Streaming Replication (Litestream / LiteFS)

**Concept**: Use [Litestream](https://litestream.io/) to continuously replicate each instance's SQLite to S3/GCS. To restore, pull a snapshot and apply.

```bash
# On each instance
litestream replicate ~/.local/share/opencode/opencode.db s3://bucket/instance-1/

# To restore on a new machine
litestream restore -o ~/.local/share/opencode/opencode.db s3://bucket/instance-1/
```

**Pros**:
- Near-real-time backup, minimal data loss window
- Restore is a single command
- No custom code for the replication itself
- Point-in-time recovery possible

**Cons**:
- **Does NOT solve merge** — you restore one instance's DB wholesale, not combining two
- Path dependency remains (projects/sessions reference the source machine's paths)
- Requires S3/GCS infrastructure
- Not supported with `journal_mode=WAL` in some edge cases (Litestream handles this, but it takes over WAL management)
- If you restore instance-1's DB onto instance-2, you overwrite instance-2's sessions entirely

**Verdict**: Excellent for backup/restore of a single instance. Useless for the merge problem without combining with Solution B.

---

### Solution E: Hybrid — Litestream Backup + Python Merge Tool

**Concept**: Combine D (backup) with B (merge). Each instance backs up via Litestream. A merge tool can pull any two snapshots and produce a unified DB.

```
                    ┌─────────────┐
                    │    S3/GCS   │
                    │             │
         ┌─────────┤ instance-1/ │
         │         │ instance-2/ │
         │         │ instance-3/ │
         │         └─────────────┘
         │                │
         ▼                ▼
   ┌───────────┐   ┌───────────┐
   │ Restore 1 │   │ Restore 2 │
   └─────┬─────┘   └─────┬─────┘
         │               │
         ▼               ▼
   ┌─────────────────────────────┐
   │     Python Merge Tool       │
   │                             │
   │  - Deduplicate sessions     │
   │  - Remap project IDs/paths  │
   │  - Resolve conflicts        │
   │  - Output: merged.db        │
   └─────────────────────────────┘
```

**Merge algorithm**:
```python
def merge_databases(db_paths, output_path, path_remaps=None):
    """
    Merge N opencode.db files into one.

    Strategy:
    1. Session IDs are globally unique (timestamp + random) → union without collision
    2. Project IDs are path-derived → may differ across machines for same repo
       - Group by (normalized_repo_url OR project.name) to detect "same project"
       - Pick one canonical project_id, remap all sessions to it
    3. Messages/inputs/todos just follow their session → insert with session
    4. Conflicts: if same session_id exists in multiple DBs (shouldn't happen
       normally), take the one with more messages or newer time_updated
    """
```

**Pros**:
- Backup AND merge solved
- Each instance is independent at runtime (no network FS needed)
- Merge is offline — no risk to running instances
- Can build incrementally (merge only new sessions since last sync)

**Cons**:
- Most complex to implement
- Project-ID unification heuristics may fail (same repo, different branches = same project? different?)
- Snapshot/revert still machine-local (can't undo a tool call from instance-1 while on instance-2)
- S3 cost (though minimal for SQLite files)

**Verdict**: The most robust long-term solution if you genuinely need multi-machine merge.

---

## 10. Comparison Matrix

| Criteria | A: Shared DB | B: Export/Import | C: Federated | D: Litestream | E: Hybrid | F: InitSync | G: Sticky PVC | H: Postgres Store | I: LiteFS |
|----------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Multi-pod k8s | No | Manual | Yes | Backup only | Backup+merge | Single pod | Per-user | Yes | Single-writer |
| Merge sources | N/A | Yes | Partial | No | Yes | No | No | Yes (native) | No |
| Concurrent writes | Dangerous | N/A | N/A | N/A | N/A | No (singleton) | Isolated | Yes | No (primary) |
| Session list (cross-pod) | If shared | Manual | Yes | No | Yes | N/A | No | Yes (SQL query) | Read replicas |
| Survives pod death | If PVC | If exported | Yes | Yes (S3) | Yes | Yes (S3) | Yes (PVC) | Yes (Postgres) | Yes (replicas) |
| Schema upgrade safe | N/A | Fragile | Fragile | Fragile | Fragile | Fragile | Fragile | **Yes (JSONB)** | Fragile |
| Implementation effort | None | Medium | High | Low | High | Medium | Low | Medium-High | Medium |
| Runtime performance | Poor | Native | Native | Native | Native | Native | Native | Native | FUSE overhead |
| Data loss window | Corruption | Manual | Stale index | ~1s | ~1s | ~30s | None | ~5s (tunable) | ~1s |
| Dependencies | NFS | Python | Python+PG | Litestream+S3 | Both | rclone+S3 | StatefulSet | Python+Postgres | LiteFS+Consul |

---

## 11. The Kubernetes Problem

### Why Multiple Instances Happen

In a k8s cluster, OpenCode runs in pods. Each pod gets its own filesystem. The moment OpenCode starts, it calls `fs.mkdir(Path.data, { recursive: true })` and creates `~/.local/share/opencode/` in that container. The DB is created, migrations run, and the session lives there.

The problem scenarios:

```
┌─────────────────────────────────────────────────────────┐
│  Scenario 1: Ephemeral pods (no PVC)                    │
│                                                         │
│  Pod-A starts → creates opencode.db → session happens   │
│  Pod-A dies   → opencode.db gone forever                │
│  Pod-B starts → fresh opencode.db → no history          │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  Scenario 2: Per-pod PVCs                               │
│                                                         │
│  Pod-A (PVC-1) → sessions accumulate in PVC-1           │
│  Pod-B (PVC-2) → sessions accumulate in PVC-2           │
│  Pod-C (PVC-3) → sessions accumulate in PVC-3           │
│                                                         │
│  User via ACP/SSE → routed to random pod                │
│  "Where are my sessions?" → fragmented across PVCs      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  Scenario 3: Shared PVC (ReadWriteMany)                 │
│                                                         │
│  Pod-A ──┐                                              │
│  Pod-B ──┼──→ same NFS/EFS PVC → single opencode.db    │
│  Pod-C ──┘                                              │
│                                                         │
│  SQLite WAL mode + NFS = corruption risk                │
│  Concurrent writes = SQLITE_BUSY storms                 │
└─────────────────────────────────────────────────────────┘
```

### The Core Tension

OpenCode's architecture assumes:
- Single writer (one process owns the DB)
- Stable filesystem identity (same absolute paths persist)
- Long-lived process (session state accumulates in one place)

Kubernetes assumes:
- Pods are disposable
- State belongs in external systems
- Horizontal scaling is normal

### What Happens When OpenCode Upgrades

OpenCode ships new migrations with each version. When a pod starts with a newer binary:
1. `DatabaseMigration.apply(db)` runs automatically on startup
2. New columns/tables are added to whatever DB file it opens
3. If the DB was last used by an older version, it gets migrated forward
4. There is **no downgrade path** — once migrated, older binaries may fail

This means if you have Pod-A running v1.5 and Pod-B running v1.6 sharing a DB, Pod-B will migrate the schema and Pod-A may break.

---

## 12. Kubernetes Deployment Patterns

### Pattern F: Init Container Sync (Recommended for Small Scale)

**Concept**: Use a shared object store (S3/GCS/MinIO) as the source of truth. An init container pulls the latest DB before OpenCode starts. A sidecar or shutdown hook pushes changes back.

```yaml
apiVersion: v1
kind: Pod
spec:
  initContainers:
  - name: sync-pull
    image: rclone/rclone
    command: ["rclone", "copy", "s3:bucket/opencode/opencode.db",
              "/data/opencode/", "--checksum"]
    volumeMounts:
    - name: opencode-data
      mountPath: /data/opencode

  containers:
  - name: opencode
    image: opencode:latest
    env:
    - name: XDG_DATA_HOME
      value: /data
    volumeMounts:
    - name: opencode-data
      mountPath: /data/opencode
    lifecycle:
      preStop:
        exec:
          command: ["/bin/sh", "-c",
            "sqlite3 /data/opencode/opencode.db 'PRAGMA wal_checkpoint(TRUNCATE)' && rclone copy /data/opencode/opencode.db s3:bucket/opencode/ --checksum"]

  - name: sync-sidecar
    image: rclone/rclone
    command: ["/bin/sh", "-c",
      "while true; do sleep 30; sqlite3 /data/opencode/opencode.db 'PRAGMA wal_checkpoint(PASSIVE)' 2>/dev/null; rclone copy /data/opencode/ s3:bucket/opencode/ --checksum --include='opencode.db' --include='auth.json'; done"]
    volumeMounts:
    - name: opencode-data
      mountPath: /data/opencode

  volumes:
  - name: opencode-data
    emptyDir: {}
```

**Flow**:
1. Init container pulls latest DB from S3
2. OpenCode runs, writes to local emptyDir (fast, no NFS issues)
3. Sidecar checkpoints WAL and pushes DB to S3 every 30s
4. PreStop hook does a final sync on pod termination

**Pros**:
- SQLite stays local (no WAL-over-NFS corruption)
- Single source of truth in S3
- Works with ephemeral pods
- Simple to implement

**Cons**:
- **Single-writer only** — if two pods run simultaneously, last-write-wins (data loss)
- 30s sync window = potential data loss on pod crash
- Doesn't solve horizontal scaling (only one pod should be active at a time)

**Verdict**: Works if you run OpenCode as a **Deployment with replicas=1** (singleton pattern). Use a leader election sidecar if you need HA failover.

---

### Pattern G: Sticky Sessions with Per-User PVC

**Concept**: Each user (or session group) gets their own PVC. Route ACP/SSE connections to the pod that has their PVC mounted.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: opencode
spec:
  replicas: 3
  volumeClaimTemplates:
  - metadata:
      name: opencode-data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 1Gi
```

Combined with a service mesh or ingress that routes by user ID → specific pod.

**Pros**:
- Each pod owns its DB — no concurrent write issues
- Clean data isolation per user
- PVC survives pod restarts (StatefulSet guarantees)

**Cons**:
- Sessions are stuck on one pod — no cross-pod visibility
- Pod failure = user blocked until pod reschedules to same PVC
- Doesn't solve the "list all my sessions" problem if you've used multiple pods historically
- PVC count grows with users

**Verdict**: Good for multi-tenant setups where each user has one dedicated pod. Doesn't solve the merge/list-across-pods problem.

---

### Pattern H: Postgres-Backed Session Store (JSON Bundle Approach)

**Concept**: Don't fight SQLite's single-writer nature. Instead, treat each pod's DB as ephemeral working state, and persist session bundles to Postgres as the durable layer.

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│    Pod-A     │     │    Pod-B     │     │    Pod-C     │
│  opencode.db │     │  opencode.db │     │  opencode.db │
│  (working)   │     │  (working)   │     │  (working)   │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│                     Postgres                            │
│                                                         │
│  sessions (id, project, title, data JSONB, updated_at)  │
│  messages (session_id, seq, data JSONB)                  │
│  auth_tokens (provider, credentials JSONB)               │
└─────────────────────────────────────────────────────────┘
```

**Implementation**: A sidecar daemon or in-app hook that:
1. On session create/update → upserts the session row into Postgres
2. On message append → inserts into Postgres messages table
3. On pod start → pulls relevant sessions from Postgres into local SQLite
4. On resume request → loads session from Postgres if not in local DB

```python
# Session sync daemon (runs alongside opencode)
import sqlite3, psycopg2, json, time

def sync_to_postgres(sqlite_path, pg_conn):
    """Push new/updated sessions to Postgres."""
    sq = sqlite3.connect(sqlite_path)
    sq.row_factory = sqlite3.Row

    # Find sessions updated since last sync
    last_sync = get_last_sync_time(pg_conn)
    sessions = sq.execute(
        "SELECT * FROM session WHERE time_updated > ?",
        (last_sync,)
    ).fetchall()

    for sess in sessions:
        sess_dict = dict(sess)
        messages = [dict(r) for r in sq.execute(
            "SELECT * FROM session_message WHERE session_id = ? ORDER BY seq",
            (sess_dict['id'],)
        ).fetchall()]

        pg_conn.execute("""
            INSERT INTO opencode_sessions (id, project_id, title, directory, data, messages, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                data = EXCLUDED.data,
                messages = EXCLUDED.messages,
                updated_at = NOW()
        """, (
            sess_dict['id'],
            sess_dict['project_id'],
            sess_dict['title'],
            sess_dict['directory'],
            json.dumps(sess_dict),
            json.dumps(messages),
        ))

    pg_conn.commit()
    update_last_sync_time(pg_conn)


def restore_from_postgres(sqlite_path, pg_conn, session_id):
    """Pull a session from Postgres into local SQLite."""
    row = pg_conn.execute(
        "SELECT data, messages FROM opencode_sessions WHERE id = %s",
        (session_id,)
    ).fetchone()

    if not row:
        raise ValueError(f"Session {session_id} not found in Postgres")

    session_data = json.loads(row[0])
    messages = json.loads(row[1])

    sq = sqlite3.connect(sqlite_path)
    # Ensure project exists
    ensure_project(sq, session_data['project_id'], session_data['directory'])
    # Insert session
    insert_row(sq, 'session', session_data)
    # Insert messages
    for msg in messages:
        insert_row(sq, 'session_message', msg)
    sq.commit()
```

**Postgres schema**:
```sql
CREATE TABLE opencode_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT,
    directory TEXT,
    data JSONB NOT NULL,        -- full session row as JSON
    messages JSONB NOT NULL,    -- array of session_message rows
    inputs JSONB,               -- session_input rows
    epoch JSONB,                -- session_context_epoch
    todos JSONB,                -- todo rows
    source_pod TEXT,            -- which pod created this
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sessions_project ON opencode_sessions(project_id);
CREATE INDEX idx_sessions_updated ON opencode_sessions(updated_at DESC);
CREATE INDEX idx_sessions_directory ON opencode_sessions(directory);

-- For listing sessions elegantly
CREATE VIEW opencode_session_list AS
SELECT id, title, directory,
       data->>'cost' as cost,
       data->>'tokens_input' as tokens_input,
       data->>'time_created' as time_created,
       source_pod
FROM opencode_sessions
ORDER BY updated_at DESC;
```

**Pros**:
- Postgres handles concurrent access natively
- Schema-upgrade-proof: JSON bundles survive OpenCode upgrades (new fields just appear)
- List all sessions across all pods with a simple query
- Restore to any pod: pull from Postgres → inject into local SQLite
- Pod failure = no data loss (synced within seconds)
- Works with any number of replicas

**Cons**:
- Sync delay (seconds) — a session in progress may lose the last few messages on hard crash
- Requires Postgres infrastructure
- Two sources of truth (local SQLite is working copy, Postgres is durable copy)
- Restore flow isn't transparent to OpenCode (needs the sidecar/daemon)
- Snapshot/revert still local (not in Postgres)
- Need to handle OpenCode's migrations carefully — the local SQLite schema must match what OpenCode expects

**Verdict**: Best overall approach for k8s. Solves listing, restore, and multi-pod elegantly. The sync daemon is ~200 lines of Python.

---

### Pattern I: LiteFS (FUSE-based SQLite Replication)

**Concept**: [LiteFS](https://fly.io/docs/litefs/) is a FUSE filesystem that transparently replicates SQLite across nodes. One node is primary (writes), others are read replicas.

```
┌────────────┐     ┌────────────┐     ┌────────────┐
│  Pod-A     │     │  Pod-B     │     │  Pod-C     │
│  PRIMARY   │     │  REPLICA   │     │  REPLICA   │
│  (writes)  │◄────│  (reads)   │     │  (reads)   │
└─────┬──────┘     └────────────┘     └────────────┘
      │
      ▼
   Consul/etcd (lease-based primary election)
```

**Pros**:
- SQLite stays as the interface — OpenCode doesn't know it's replicated
- Read scaling across replicas
- Automatic primary failover
- No custom sync code

**Cons**:
- Single writer (primary) — doesn't solve concurrent multi-pod writes
- FUSE overhead
- Fly.io oriented — less battle-tested in generic k8s
- Still WAL-mode sensitive
- Requires Consul/etcd for lease management

**Verdict**: Elegant but still single-writer. Better suited for Fly.io deployments than generic k8s.

---

## 13. Recommended Architecture for K8s

### Constraint: 1 Staff = 1 OpenCode Instance (No Concurrency)

Since each staff member runs exactly one OpenCode instance at a time, the problem simplifies from "distributed merge" to "durable single-user state." The merge/conflict problem disappears entirely.

**Recommended: StatefulSet + Per-User PVC (simplest, zero custom code)**

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: opencode
spec:
  serviceName: opencode
  replicas: 1  # per-user deployment (or use a template per staff)
  template:
    spec:
      containers:
      - name: opencode
        image: opencode:latest
        env:
        - name: XDG_DATA_HOME
          value: /data
        - name: OPENCODE_AUTH_CONTENT
          valueFrom:
            secretKeyRef:
              name: opencode-auth-${STAFF_ID}
              key: auth.json
        volumeMounts:
        - name: opencode-data
          mountPath: /data/opencode
  volumeClaimTemplates:
  - metadata:
      name: opencode-data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 1Gi
```

**Why this works**:
- Pod dies → StatefulSet reschedules it → same PVC reattaches → all sessions intact
- No sync daemon, no Postgres, no S3, no merge logic
- OpenCode's SQLite works exactly as designed (local, single-writer)
- Schema upgrades just happen on pod restart (new binary migrates the existing DB)

**When to consider the Postgres approach instead**:
- You need admin visibility across all staff sessions (audit/compliance)
- You want to query "which sessions are active cluster-wide"
- You're managing 100+ staff and want a dashboard
- You need to reassign a session from one staff member to another

If you DO need cross-user visibility, add a lightweight read-only sync agent that pushes session metadata (not full messages) to Postgres for indexing — but the PVC remains the source of truth.

---

### Alternative: S3-backed for clusters where PVCs are expensive/slow

If your cluster doesn't have good block storage (e.g., spot instances, serverless containers, or you want portability across clusters):

```
Pod start  →  pull opencode.db from s3://${STAFF_ID}/opencode.db
Pod run    →  local emptyDir (fast)
Pod stop   →  checkpoint WAL, push to s3://${STAFF_ID}/opencode.db
```

This trades slightly more complexity (init/sidecar containers) for storage flexibility. But for most k8s clusters, a 1Gi RWO PVC is trivial and the StatefulSet approach wins.

---

### Session Index Sync (For UI Session Picker)

You need your UI to show a list of sessions (title, time, cost, etc.) so the user can pick one to resume. The full message data stays in the PVC — you only sync lightweight metadata to Postgres.

**Postgres schema (index only)**:

```sql
CREATE TABLE opencode_session_index (
    id TEXT PRIMARY KEY,               -- ses_xxxx
    staff_id TEXT NOT NULL,            -- maps to your user system
    project_id TEXT,
    title TEXT,
    directory TEXT,
    agent TEXT,
    model_id TEXT,
    sandbox TEXT,                       -- runtime environment binding (see below)
    sandbox_meta JSONB,                -- optional: extra env details for restore
    cost REAL DEFAULT 0,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    time_created BIGINT NOT NULL,      -- unix ms from opencode
    time_updated BIGINT NOT NULL,
    time_archived BIGINT,
    is_resumable BOOLEAN DEFAULT true, -- can this session be restored?
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_session_staff ON opencode_session_index(staff_id, time_updated DESC);
CREATE INDEX idx_session_staff_project ON opencode_session_index(staff_id, project_id);
CREATE INDEX idx_session_sandbox ON opencode_session_index(staff_id, sandbox);
CREATE INDEX idx_session_resumable ON opencode_session_index(staff_id, is_resumable)
    WHERE is_resumable = true AND time_archived IS NULL;
```

**The `sandbox` field** encodes which runtime environment the session is bound to. This is NOT an OpenCode concept — it's your orchestration layer's concept. You populate it when creating the session, and use it at restore time to provision the correct sandbox.

**Sandbox naming convention** (suggested):

```
sandbox = "{runtime_type}:{image_tag}"

Examples:
  "node:20-slim"
  "python:3.11-bookworm"
  "browser:chromium-126"
  "gui:desktop-ubuntu-24.04"
  "custom:my-ml-stack-v3"
```

**`sandbox_meta`** holds anything else needed to recreate the environment:

```json
{
  "image": "registry.internal/sandboxes/node:20-slim",
  "gpu": false,
  "ports": [3000, 8080],
  "env_template": "node-fullstack",
  "volumes": ["project-src:/workspace"],
  "resource_class": "medium"
}
```

**`is_resumable`** — not all sessions can be resumed. Mark as `false` if:
- The sandbox image was deleted or deprecated
- The session errored out in an unrecoverable state
- The workspace was cleaned up

**Session picker query (filtered by compatible sandboxes)**:

```sql
-- Show resumable sessions, optionally filtered by sandbox type
SELECT id, title, directory, sandbox, sandbox_meta,
       cost, message_count, time_updated
FROM opencode_session_index
WHERE staff_id = 'alice'
  AND time_archived IS NULL
  AND is_resumable = true
  AND (sandbox LIKE 'node:%' OR sandbox LIKE 'python:%')  -- filter by available runtimes
ORDER BY time_updated DESC
LIMIT 50;
```

**Restore flow with sandbox binding**:

```
1. UI: user picks session "ses_abc" (sandbox = "node:20-slim")
2. Backend: reads sandbox + sandbox_meta from Postgres
3. Backend: provisions sandbox container with correct image
4. Backend: mounts user's PVC (or syncs DB from S3) into sandbox
5. Backend: starts OpenCode in sandbox, resumes session "ses_abc"
6. Backend: connects user's ACP/SSE to the running sandbox
```

```python
# Restore orchestration (pseudocode)
async def restore_session(staff_id: str, session_id: str):
    # 1. Look up session metadata
    row = await pg.fetchrow(
        "SELECT sandbox, sandbox_meta FROM opencode_session_index WHERE id = $1 AND staff_id = $2",
        session_id, staff_id
    )
    if not row:
        raise NotFound(f"Session {session_id} not found")
    if not row["is_resumable"]:
        raise BadRequest("Session is not resumable")

    # 2. Provision the correct sandbox
    sandbox_spec = json.loads(row["sandbox_meta"]) if row["sandbox_meta"] else {}
    pod = await k8s.create_pod(
        image=sandbox_spec.get("image", default_image_for(row["sandbox"])),
        pvc=f"opencode-{staff_id}",
        env={"STAFF_ID": staff_id},
        resources=sandbox_spec.get("resource_class", "small"),
    )

    # 3. Wait for pod ready, then resume session via ACP/SSE
    await pod.wait_ready()
    return await opencode_client.resume(pod.endpoint, session_id)
```

**Sync agent** (runs as sidecar or post-session hook, ~50 lines):

```python
#!/usr/bin/env python3
"""Lightweight session-index sync: SQLite → Postgres."""

import sqlite3, psycopg2, os, time, json

SQLITE_PATH = os.environ.get("OPENCODE_DB_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db"))
PG_DSN = os.environ["PG_DSN"]
STAFF_ID = os.environ["STAFF_ID"]
POLL_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "10"))

UPSERT_SQL = """
    INSERT INTO opencode_session_index
        (id, staff_id, project_id, title, directory, agent, model_id,
         cost, tokens_input, tokens_output, message_count,
         time_created, time_updated, time_archived)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (id) DO UPDATE SET
        title = EXCLUDED.title,
        cost = EXCLUDED.cost,
        tokens_input = EXCLUDED.tokens_input,
        tokens_output = EXCLUDED.tokens_output,
        message_count = EXCLUDED.message_count,
        time_updated = EXCLUDED.time_updated,
        time_archived = EXCLUDED.time_archived,
        synced_at = NOW()
"""

def sync_once():
    sq = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row
    pg = psycopg2.connect(PG_DSN)
    cur = pg.cursor()

    # Get last sync watermark
    cur.execute(
        "SELECT COALESCE(MAX(time_updated), 0) FROM opencode_session_index WHERE staff_id = %s",
        (STAFF_ID,))
    watermark = cur.fetchone()[0]

    rows = sq.execute("""
        SELECT s.id, s.project_id, s.title, s.directory, s.agent, s.model,
               s.cost, s.tokens_input, s.tokens_output,
               s.time_created, s.time_updated, s.time_archived,
               COUNT(m.id) as message_count
        FROM session s
        LEFT JOIN session_message m ON m.session_id = s.id
        WHERE s.time_updated > ?
        GROUP BY s.id
    """, (watermark,)).fetchall()

    for r in rows:
        model_id = None
        if r["model"]:
            try: model_id = json.loads(r["model"]).get("id")
            except: pass

        cur.execute(UPSERT_SQL, (
            r["id"], STAFF_ID, r["project_id"], r["title"],
            r["directory"], r["agent"], model_id,
            r["cost"], r["tokens_input"], r["tokens_output"],
            r["message_count"],
            r["time_created"], r["time_updated"], r["time_archived"],
        ))

    # Remove sessions deleted from SQLite
    cur.execute("""
        DELETE FROM opencode_session_index
        WHERE staff_id = %s AND id NOT IN (
            SELECT id FROM opencode_session_index WHERE staff_id = %s
            INTERSECT
            SELECT value FROM unnest(%s::text[]) AS value
        )
    """, (STAFF_ID, STAFF_ID, [r["id"] for r in sq.execute("SELECT id FROM session").fetchall()]))

    pg.commit()
    pg.close()
    sq.close()

if __name__ == "__main__":
    while True:
        try:
            sync_once()
        except Exception as e:
            print(f"sync error: {e}")
        time.sleep(POLL_INTERVAL)
```

**Your UI queries Postgres**:
```sql
-- Session picker for a user
SELECT id, title, directory, cost, tokens_input + tokens_output as total_tokens,
       message_count, time_updated
FROM opencode_session_index
WHERE staff_id = 'alice'
  AND time_archived IS NULL
ORDER BY time_updated DESC
LIMIT 50;
```

**Resume flow**:
1. UI shows session list from Postgres (fast, no pod needed yet)
2. User picks a session → your backend routes the ACP/SSE connection to the user's pod
3. Pod already has the session in its PVC SQLite → OpenCode resumes it directly
4. No data transfer needed at resume time (it's already local)

**Why this is enough**:
- The PVC has the full data (messages, context, inputs) — it never needs to leave
- Postgres only holds the "card" shown in the UI (~200 bytes per session)
- Sync is 10s polling on a read-only SQLite connection — won't interfere with OpenCode
- If the pod is down, the UI still works (Postgres has the index). The pod just needs to be up for actual resume.

**Pod manifest addition**:
```yaml
- name: session-sync
  image: python:3.11-slim
  command: ["python", "/scripts/sync.py"]
  env:
  - name: STAFF_ID
    value: "${STAFF_ID}"
  - name: PG_DSN
    valueFrom:
      secretKeyRef:
        name: pg-credentials
        key: dsn
  - name: OPENCODE_DB_PATH
    value: /data/opencode/opencode.db
  volumeMounts:
  - name: opencode-data
    mountPath: /data/opencode
    readOnly: true  # sync agent only reads
```

---

### If You Also Need Centralized Session Listing (Admin Use Case)

Architecture: Singleton Writer + Postgres Durable Store

```
                        ┌─────────────────┐
                        │   Ingress/LB    │
                        │  (ACP + SSE)    │
                        └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
            ┌────────────┐ ┌────────────┐ ┌────────────┐
            │  Pod-A     │ │  Pod-B     │ │  Pod-C     │
            │ opencode   │ │ opencode   │ │ opencode   │
            │ sync-agent │ │ sync-agent │ │ sync-agent │
            │ emptyDir   │ │ emptyDir   │ │ emptyDir   │
            └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
                  │              │              │
                  ▼              ▼              ▼
            ┌─────────────────────────────────────────┐
            │              Postgres                    │
            │  opencode_sessions (JSONB)               │
            │  opencode_auth (JSONB)                   │
            └─────────────────────────────────────────┘
```

**How it works**:
1. Each pod runs OpenCode normally with a local emptyDir SQLite
2. A sync-agent sidecar watches the SQLite (via inotify or polling) and pushes session changes to Postgres
3. On pod startup, sync-agent pulls the user's recent sessions from Postgres into local SQLite
4. Session restore: ACP client calls your API → finds session in Postgres → injects into the requesting pod's SQLite → OpenCode resumes it
5. Session listing: query Postgres directly (fast, indexed, cross-pod)

**Key decisions**:
- Each pod can operate independently (no shared filesystem)
- Postgres is the durable layer, SQLite is the fast local cache
- If a pod dies mid-session, data loss = only the time since last sync (configurable, e.g., 5s)
- OpenCode upgrades: each pod migrates its own local SQLite on start. Postgres stores raw JSON, unaffected by schema changes.

### Handling OpenCode Schema Upgrades

The JSON-bundle approach is deliberately upgrade-proof:
- Postgres stores the session as `JSONB` — if OpenCode v1.6 adds `tokens_reasoning`, it just appears as a new key in the JSON
- On restore: the sync-agent writes whatever fields exist into the target SQLite. If the target OpenCode version expects a column that doesn't exist in the JSON, it gets NULL (which is fine — OpenCode handles NULLs gracefully for new columns)
- On export: the sync-agent reads all columns from SQLite, including any new ones from migrations

The only breaking scenario: OpenCode *removes* a column or *renames* one. This is rare (they've only done additive changes so far) and would require a one-time migration of the Postgres JSON data.

### Phase Plan

| Phase | What | Effort |
|-------|------|--------|
| 1 | Python sync-agent: watch SQLite → push to Postgres | 2-3 days |
| 2 | Restore endpoint: pull from Postgres → inject into pod | 1-2 days |
| 3 | Session list API: query Postgres for all sessions | 1 day |
| 4 | Init container: pre-populate SQLite from Postgres on pod start | 1 day |
| 5 | (Optional) Handle `auth.json` sync to/from a k8s Secret | 1 day |

### Key Caveats
- **Snapshot/revert will NOT work** cross-pod. Accept this or mount a shared volume for the `snapshot/` directory (RWX PVC, low risk since it's append-only Git objects).
- **`session_context_epoch`** baselines may be stale after restore. OpenCode rebuilds context on next `resume` call — test this path.
- **auth.json** should come from a k8s Secret, not synced from Postgres (avoid credential duplication). Use `OPENCODE_AUTH_CONTENT` env var injected from the Secret.
- **session_input** (the inbox queue) may have in-flight entries that haven't been processed. On restore, these will replay — which is probably fine (idempotent prompts) but test for edge cases.

---

## 12. Source Code Package Map

```
packages/
├── core/                    # @opencode-ai/core — shared kernel
│   └── src/
│       ├── global.ts        # XDG path resolution, directory creation
│       ├── flag/flag.ts     # All environment variable flags
│       ├── database/
│       │   ├── database.ts  # DB service, path(), layer
│       │   ├── path.ts      # Column types (absoluteColumn, pathColumn)
│       │   ├── schema.sql.ts # Timestamps helper
│       │   ├── schema.gen.ts # Baseline DDL
│       │   ├── migration.ts  # Migration runner logic
│       │   └── migration/    # 35+ individual migration files
│       ├── session/sql.ts   # Session table definitions
│       ├── project/sql.ts   # Project + project_directory tables
│       ├── account/sql.ts   # Account tables
│       ├── permission/sql.ts
│       ├── credential/sql.ts
│       ├── event/sql.ts     # Event sourcing tables
│       ├── share/sql.ts     # Session share table
│       ├── tool-output-store.ts
│       └── util/identifier.ts # ID generation (timestamp+random)
├── opencode/                # Main application package
│   └── src/
│       ├── auth/index.ts    # auth.json read/write
│       ├── snapshot/index.ts # Git-based snapshot system
│       ├── config/paths.ts  # Config file discovery
│       ├── project/project.ts # Project resolution + ID derivation
│       └── util/repository.ts # repos/ path logic
└── effect-sqlite-node/      # SQLite driver binding
```

---

## 13. Key Design Notes

1. **Single DB file** — All state in one SQLite file. No per-project or per-session databases.
2. **WAL mode** — Enables concurrent reads. Copy requires checkpointing first.
3. **Cascade deletes** — Deleting a project removes ALL its sessions, messages, permissions.
4. **Descending IDs** — Newer sessions sort first lexicographically (useful for listing).
5. **Dual message systems** — `message`+`part` (v1 legacy) and `session_message` (v2 with seq). Both may exist for the same session.
6. **Migrations are TypeScript** — Not SQL files. Applied sequentially, tracked in `migration` table.
7. **Path normalization** — Windows backslashes → forward slashes in DB. Restored platform-appropriate on read.
8. **Flock coordination** — `$XDG_STATE_HOME/opencode/` used for file locks preventing concurrent instance conflicts on same DB.
9. **No origin tracking** — DB has no column indicating which machine/instance created a row. This is the core gap that makes merging hard.
