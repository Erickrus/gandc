# OpenCode Persistency Solution

## Context

OpenCode stores all its state in a local SQLite database at `~/.local/share/opencode/opencode.db`. It assumes a single-user, single-writer, stable-filesystem environment.

We run OpenCode on a k8s cluster where each staff member gets one instance. Pods are semi-ephemeral — they can restart, reschedule, or be reprovisioned with different sandbox images (node, python, browser/GUI runtimes). We need:

1. Sessions survive pod restarts
2. UI can list sessions for a user to pick and resume
3. Sessions are bound to a sandbox type (runtime image) so the correct environment is provisioned on restore

**Key constraint**: 1 staff = 1 OpenCode instance at a time. No concurrent writes, no merge problem.

---

## OpenCode Data Layout (Source References)

### XDG Directories

All paths resolved via `xdg-basedir` package:

| Directory | Default Path | Purpose | Source |
|-----------|-------------|---------|--------|
| data | `~/.local/share/opencode/` | DB, auth, snapshots, logs, repos | `packages/core/src/global.ts:11` |
| config | `~/.config/opencode/` | config.json / opencode.json | `packages/core/src/global.ts:13` |
| cache | `~/.cache/opencode/` | LSP binaries | `packages/core/src/global.ts:12` |
| state | `~/.local/state/opencode/` | flock files | `packages/core/src/global.ts:14` |

### Key Files in Data Directory

| File/Dir | Purpose | Source |
|----------|---------|--------|
| `opencode.db` | Main SQLite database (WAL mode) | `packages/core/src/database/database.ts:53` |
| `opencode.db-wal` | Write-ahead log | SQLite WAL mode (`database.ts:27`) |
| `opencode.db-shm` | Shared memory index for WAL | SQLite internal |
| `auth.json` | API keys for providers | `packages/opencode/src/auth/index.ts:10` |
| `snapshot/{project_id}/{hash}/` | Git object store for undo/revert | `packages/opencode/src/snapshot/index.ts:79` |
| `log/` | Application logs | `packages/core/src/global.ts:23` |
| `repos/` | Cloned reference repositories | `packages/core/src/global.ts:24` |
| `._*` files | macOS AppleDouble resource forks (NOT OpenCode data) | Finder metadata |

### Environment Variable Overrides

| Variable | Effect | Source |
|----------|--------|--------|
| `OPENCODE_DB` | Override DB file path (absolute or relative to data dir) | `packages/core/src/flag/flag.ts:47`, `database.ts:44-46` |
| `OPENCODE_CONFIG_DIR` | Override config directory ONLY (not data/DB path) | `packages/core/src/flag/flag.ts:63-64`, `global.ts:64` |
| `OPENCODE_DISABLE_CHANNEL_DB` | Force `opencode.db` name regardless of channel | `packages/core/src/flag/flag.ts` |

### Database Details

- **WAL mode**: enabled at `database.ts:27` — requires all 3 files (`*.db`, `*.db-wal`, `*.db-shm`) to be present together
- **Channel-based naming**: non-prod channels use `opencode-{channel}.db` (`installation/version.ts:7-8`)
- **Migration runner**: sequential TypeScript migrations, auto-applied on startup (`database/migration.ts:69-75`)
- **Schema baseline**: 18 tables defined in `database/schema.gen.ts:8-274`

### Session Table Schema

Defined at `packages/core/src/session/sql.ts:22-63`:

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | `ses_` + descending timestamp ID (`session/schema.ts:15`) |
| project_id | TEXT FK | References project.id (cascade delete) |
| workspace_id | TEXT | Optional workspace binding |
| parent_id | TEXT | For forked sessions |
| directory | TEXT | Absolute working directory |
| title | TEXT | User-visible title |
| agent | TEXT | Agent name used |
| model | JSON | `{id, providerID, variant?}` |
| cost | REAL | Total cost in dollars |
| tokens_input/output/reasoning/cache_* | INTEGER | Token usage counters |
| time_created/time_updated | INTEGER | Unix millis timestamps |
| time_archived | INTEGER | Null = active |

### Session ID Generation

Defined at `packages/core/src/util/identifier.ts:14,28-46`:
- 6-byte **descending** timestamp (bitwise NOT of millis) → hex (12 chars)
- 14 random base62 characters
- Prefixed with `ses_` → total 30 chars
- Descending = newest sessions sort first lexicographically

### Project ID Resolution

Defined at `packages/core/src/project.ts:78,110-117`:
- Derived from git remote URL: `Hash.fast("git-remote:{normalized_url}")`
- Falls back to git root commit hash
- Falls back to `"global"` if not in a git repo
- **Machine-independent** (based on remote URL, not local path)

### Project Sandboxes Field

The Project table already tracks sandbox directories natively:
- `packages/core/src/project/sql.ts:16` — `sandboxes: absoluteArrayColumn().notNull()`
- `packages/opencode/src/project/project.ts:54` — exposed in schema as `sandboxes: Schema.Array(Schema.String)`
- `packages/opencode/src/project/project.ts:273-275` — auto-populated when a new directory is detected

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          User's UI                               │
│                                                                  │
│  "Pick a session to resume"                                      │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │ ses_abc  "Fix auth bug"     node:20       3 min ago     │    │
│   │ ses_def  "Add dashboard"    browser:ch126  1 hour ago   │    │
│   │ ses_ghi  "ML pipeline"      python:3.11    yesterday    │    │
│   └─────────────────────────────────────────────────────────┘    │
└────────────────────────────────────┬─────────────────────────────┘
                                     │ query / resume
                                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Backend / Orchestrator                       │
│                                                                  │
│  1. List sessions   → SELECT from Postgres                       │
│  2. Restore session → read sandbox field → provision pod         │
│  3. Mount PVC       → start OpenCode → resume → connect SSE/ACP  │
└───────────────────────────────┬──────────────────────────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
     │ Sandbox Pod  │  │ Sandbox Pod  │  │   Postgres       │
     │ (node:20)    │  │ (python:3.11)│  │                  │
     │              │  │              │  │ session_index    │
     │ opencode     │  │ opencode     │  │ (metadata only)  │
     │ sync-agent   │  │ sync-agent   │  │                  │
     │ PVC mounted  │  │ PVC mounted  │  └──────────────────┘
     └──────────────┘  └──────────────┘
              │                 │
              ▼                 ▼
     ┌──────────────────────────────────┐
     │   Per-User PVC (RWO, 1Gi)        │
     │                                  │
     │   opencode.db  (full data)       │
     │   opencode.db-wal                │
     │   opencode.db-shm                │
     │   auth.json                      │
     │   snapshot/                      │
     └──────────────────────────────────┘
```

---

## Components

### 1. Per-User PVC (Source of Truth)

Each staff member gets a persistent volume. It holds the full OpenCode state. Survives pod restarts and rescheduling.

```yaml
volumeClaimTemplates:
- metadata:
    name: opencode-data
  spec:
    accessModes: ["ReadWriteOnce"]
    resources:
      requests:
        storage: 1Gi
```

The PVC is mounted into whatever sandbox pod the user is currently using. Only one pod mounts it at a time (RWO guarantees this).

### 2. Postgres Session Index (For UI)

A lightweight table holding session metadata — enough to render a picker list and route a restore request. NOT a full replica of the SQLite data.

```sql
CREATE TABLE opencode_session_index (
    id TEXT PRIMARY KEY,
    staff_id TEXT NOT NULL,
    project_id TEXT,
    title TEXT,
    directory TEXT,
    agent TEXT,
    model_id TEXT,
    sandbox TEXT NOT NULL,
    sandbox_meta JSONB,
    cost REAL DEFAULT 0,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    time_created BIGINT NOT NULL,
    time_updated BIGINT NOT NULL,
    time_archived BIGINT,
    is_resumable BOOLEAN DEFAULT true,
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_session_staff ON opencode_session_index(staff_id, time_updated DESC);
CREATE INDEX idx_session_sandbox ON opencode_session_index(staff_id, sandbox);
CREATE INDEX idx_session_resumable ON opencode_session_index(staff_id, is_resumable)
    WHERE is_resumable = true AND time_archived IS NULL;
```

**Key fields**:
- `sandbox` — runtime type identifier (e.g., `node:20-slim`, `python:3.11`, `browser:chromium-126`)
- `sandbox_meta` — JSONB with provisioning details (image, ports, gpu, resource class)
- `is_resumable` — false if the sandbox image was deprecated or session is broken

### 3. Sync Agent (Sidecar)

A lightweight process that reads the local SQLite (read-only) and upserts session metadata into Postgres every N seconds.

**What it syncs**: id, title, directory, cost, token counts, message count, timestamps.
**What it does NOT sync**: message content, context epochs, tool outputs.

It runs as a sidecar container with the PVC mounted `readOnly: true`.

### 4. Sandbox Field Convention

```
sandbox = "{runtime_type}:{image_tag}"
```

| Value | Meaning |
|-------|---------|
| `node:20-slim` | Node.js 20 runtime |
| `python:3.11-bookworm` | Python 3.11 runtime |
| `browser:chromium-126` | Browser/GUI sandbox with Chromium |
| `gui:desktop-ubuntu-24.04` | Full desktop environment |
| `custom:ml-stack-v3` | Custom ML image |

The `sandbox_meta` JSONB holds full provisioning context:

```json
{
  "image": "registry.internal/sandboxes/node:20-slim",
  "gpu": false,
  "ports": [3000, 8080],
  "env_template": "node-fullstack",
  "resource_class": "medium"
}
```

---

## Flows

### Create Session

1. Orchestrator provisions sandbox pod with user's PVC
2. OpenCode starts, creates session in local SQLite
3. Sync-agent detects new session, writes index row to Postgres with `sandbox` = current runtime
4. UI can immediately show the session in the list

### List Sessions (UI Picker)

```sql
SELECT id, title, sandbox, cost, message_count, time_updated
FROM opencode_session_index
WHERE staff_id = $1
  AND time_archived IS NULL
  AND is_resumable = true
ORDER BY time_updated DESC;
```

No pod needs to be running for this query to work.

### Resume Session

1. UI sends resume request with `session_id`
2. Backend reads `sandbox` + `sandbox_meta` from Postgres
3. Backend provisions a new pod with the correct image
4. Backend mounts the user's PVC into the pod
5. OpenCode starts, finds the session in its local SQLite, resumes it
6. Backend connects user's ACP/SSE to the pod

### Pod Restart / Reschedule

Nothing special needed. StatefulSet reattaches the same PVC. All sessions are intact. Sync-agent resumes pushing updates.

### Sandbox Image Deprecated

Mark affected sessions as non-resumable:

```sql
UPDATE opencode_session_index
SET is_resumable = false
WHERE sandbox = 'python:3.9-buster';
```

Or, if you have a migration path (3.9 → 3.11), update the sandbox field and let the restore use the new image.

---

## What Lives Where

| Data | Location | Why |
|------|----------|-----|
| Full session + messages | PVC (SQLite) | Single-writer, fast, native OpenCode |
| Session index (title, cost, sandbox) | Postgres | Cross-pod queryable, UI-friendly |
| Auth credentials | k8s Secret → `OPENCODE_AUTH_CONTENT` env | Don't store secrets in PVC |
| Snapshot/revert data | PVC (`snapshot/`) | Git object store, path-coupled |
| Tool outputs | PVC (`tool-output/`) | Ephemeral (7-day TTL), not worth syncing |
| Sync watermark | Postgres (derived from MAX(time_updated)) | No extra state needed |

---

## What NOT to Do

| Approach | Why it fails |
|----------|-------------|
| Store full SQLite in a Postgres column | Can't query it, schema upgrades break it |
| Mirror every SQLite column to Postgres | Fragile — OpenCode adds columns with every release |
| Share SQLite over NFS (ReadWriteMany) | WAL mode + NFS = corruption |
| Run multiple pods writing same DB | SQLITE_BUSY storms, data corruption |
| Merge databases from multiple instances | Over-engineered given 1:1 staff:instance constraint |

---

## Handling OpenCode Upgrades

OpenCode ships ~2 schema migrations per release. Because:
- The PVC holds the actual SQLite, and OpenCode migrates it forward on startup automatically
- Postgres only holds a flat index (not schema-coupled), new SQLite columns just get ignored until you add them to the sync query

Your only maintenance: if OpenCode adds a useful field you want in the picker UI, add it to the sync agent's SELECT and the Postgres table.

---

## Implementation Checklist

| Step | Component | Effort |
|------|-----------|--------|
| 1 | Postgres table + indexes | 1 hour |
| 2 | Sync agent (Python sidecar, ~80 lines) | 1 day |
| 3 | Pod template with PVC + sidecar | 1 day |
| 4 | Backend: session list API (query Postgres) | Half day |
| 5 | Backend: restore endpoint (read sandbox → provision pod → mount PVC → resume) | 1-2 days |
| 6 | UI: session picker with sandbox filter | 1 day |
| 7 | Admin: mark sessions non-resumable on image deprecation | Half day |

Total: ~5-6 days for a working end-to-end flow.

---

## Edge Cases

| Scenario | Handling |
|----------|----------|
| Pod crashes mid-session | PVC has data up to last SQLite write (milliseconds). Postgres index may be ~10s stale — acceptable. |
| User switches sandbox type mid-project | New session, new sandbox field. Old session stays resumable in its original sandbox. |
| OpenCode version mismatch (PVC was last used by v1.5, new pod is v1.6) | OpenCode auto-migrates on startup. No action needed. |
| Sandbox image deleted from registry | Set `is_resumable = false` for affected sessions. Optionally offer migration to new image. |
| PVC storage full | Monitor PVC usage. SQLite grows ~1-5MB per 100 sessions. The 1Gi default handles thousands. |
| Sync agent can't reach Postgres | Sessions still work locally. Index catches up when connectivity returns (watermark-based). |
