# ACP REPL v2 — Design (Additions over v1)

This document covers what v2 adds on top of the original `acp_repl.py`. The base ACP client, permission handling, fs requests, session lifecycle, and sandbox support are unchanged.

---

## 1. SSE Event Stream

v1 communicates only via ACP stdio. v2 adds a parallel SSE connection to opencode's HTTP server for events that the ACP protocol doesn't cover.

### How it connects

- v2 picks a free port via `socket.bind(("127.0.0.1", 0))`
- Passes `--port <N>` to the `opencode acp` subprocess
- `SSEListener` class connects to `GET /global/event` with `Accept: text/event-stream`
- Runs in a daemon thread, auto-reconnects on disconnect (2s backoff)
- Parses SSE wire format: `event:`, `data:`, blank line delimiter

### Why SSE works alongside ACP

When you run `opencode acp`, the HTTP server starts unconditionally in the same process. ACP stdio and HTTP coexist as concurrent fibers. Both subscribe to the same internal `GlobalBus`.

Source: `packages/opencode/src/cli/cmd/acp.ts` — calls `Server.listen(opts)` then sets up stdin/stdout as ndjson stream.
Source: `packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts` — SSE handler subscribes to GlobalBus.

### Port discovery problem

ACP does NOT advertise its HTTP port. It does NOT write a `server.json` lock file (only `opencode serve --register` does). Default port is `0` (OS-assigned random).

Source: `packages/opencode/src/server/server.ts` (port selection)
Source: `packages/opencode/src/acp/service.ts:91-136` — `initialize` response has no port field.

**Solution:** acp_repl_v2.py picks a free port and passes `--port <N>` to the subprocess.

### Events consumed

| SSE Event | Purpose | Source |
|-----------|---------|--------|
| `question.v2.asked` | Interactive question from the agent | `packages/sdk/js/src/v2/gen/types.gen.ts:1344-1355` |
| `todo.updated` | Todo list changed externally | `packages/sdk/js/src/v2/gen/types.gen.ts:1372-1379` |
| `message.part.updated` | Subagent tool activity in child sessions | `packages/sdk/js/src/v2/gen/types.gen.ts:7-95` |

Initial event: `server.connected` sent immediately on connect, then heartbeats every 10s.
Source: `packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts`

---

## 2. Interactive Question Handling

Questions are completely outside the ACP protocol. They flow through HTTP API and internal events only.

### How questions differ from permissions

| Aspect | Permissions | Questions |
|--------|------------|-----------|
| ACP method | `session/request_permission` | **None** (not in ACP) |
| Trigger | Tool execution needs approval | LLM explicitly asks user a choice |
| Options | Fixed: once/always/reject | Dynamic: arbitrary labels from LLM |
| Multi-select | No | Yes (`multiple` flag) |
| Custom text | No | Yes (`custom` flag) |
| Multi-tab | No | Yes (multiple questions in one request) |

Source (permissions): `packages/opencode/src/acp/permission.ts`
Source (questions): `packages/opencode/src/question/index.ts`

### Question lifecycle

1. LLM calls `question` tool — `packages/opencode/src/tool/question.ts:26-55`
2. Question service creates pending request, publishes `question.asked` event, blocks on Deferred — `packages/opencode/src/question/index.ts:158-182`
3. SSE delivers `question.v2.asked` to client
4. User selects answer → `POST /session/{sessionID}/question/{requestID}/reply` — `packages/opencode/src/server/routes/instance/httpapi/groups/question.ts:36-44`
5. Question service resolves Deferred → tool returns answers to LLM — `packages/opencode/src/tool/question.ts:54-76`
6. Or user rejects → `POST /session/{sessionID}/question/{requestID}/reject` → `RejectedError`

### Data model

**QuestionV2Option** — `packages/sdk/js/src/v2/gen/types.gen.ts:2901`:
```
{ label: string, description: string }
```

**QuestionV2Info** — `packages/sdk/js/src/v2/gen/types.gen.ts:2912`:
```
{ question, header, options: QuestionV2Option[], multiple?: bool, custom?: bool }
```

**QuestionV2Request** — `packages/sdk/js/src/v2/gen/types.gen.ts:4175`:
```
{ id, sessionID, questions: QuestionV2Info[], tool?: string }
```

**Reply payload** — `packages/opencode/src/server/routes/instance/httpapi/groups/question.ts:36-44`:
```
{ answers: Array<Array<string>> }  // One answer array per question (tab)
```

### Detection approach

v2 uses SSE `question.v2.asked` which includes the full `QuestionV2Request` with request ID. This is cleaner than the hybrid approach (detect via ACP tool_call + poll HTTP for ID).

The ACP tool_call for `toolName == "question"` IS visible but does NOT include the `requestID` needed to reply.
Source: `packages/opencode/src/acp/tool.ts:103-180`

### ACP event handler confirms questions are NOT routed

Source: `packages/opencode/src/acp/event.ts:7-113` — only handles `permission.asked`, `message.part.updated`, `message.part.delta`.

### Display modes

- **Single-select**: numbered list, user types one number
- **Multi-select**: `[ ]` checkboxes, comma-separated numbers
- **Multi-tab**: sequential prompts `(1/N)`, `(2/N)` for each question in the array
- **Custom text**: extra numbered option when `custom: true`
- **Reject**: `0`, `q`, empty, or Ctrl+C

### Converting questions to chat-style Q&A

Questions render as inline messages (like the agent asking). The user's next input is routed to the question handler (not `session/prompt`) while `_current_question_id` is set. This is state-based: the prompt implicitly changes purpose while a question is pending.

---

## 3. Inline Todo Display

### Design principle

Todos are messages, not commands. When the agent updates todos, the display appears inline immediately — no `/todo` command needed.

### How todowrite works

- LLM calls `todowrite` tool which **replaces the entire todo list atomically**
  - Source: `packages/opencode/src/tool/todo.ts:25-57`
  - Source: `packages/opencode/src/session/todo.ts:43-65` (update is delete-all then insert)
- After update, publishes `todo.updated` event: `{sessionID, todos: Array<Todo>}`
  - Source: `packages/opencode/src/session/todo.ts:21-28`
- Tool is denied for subagents by default
  - Source: `packages/opencode/src/tool/task.ts:130-135`

### Data model

Source: `packages/sdk/js/src/v2/gen/types.gen.ts:656-669`
```
Todo = { content: string, status: "pending"|"in_progress"|"completed"|"cancelled", priority: "high"|"medium"|"low" }
```

Database schema: `packages/core/src/session/sql.ts:99-116`

### Two detection sources

1. **ACP `tool_call_update`** — When `todowrite` completes, `rawOutput.metadata.todos` contains the full list
   - Source: `packages/opencode/src/tool/todo.ts:43-52` (tool output structure)
   - Source: `packages/opencode/src/acp/tool.ts:186-202` (ACP delivery via completedToolUpdate)

2. **SSE `todo.updated`** — Real-time push when todos change from any source
   - Source: `packages/sdk/js/src/v2/gen/types.gen.ts:1372-1379`
   - TUI consumer: `packages/tui/src/context/sync.tsx:242-243`

### HTTP API (polling fallback)

`GET /session/{sessionID}/todo` returns `Array<Todo>`
Source: `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:156-167`

### Display format

```
[todo]
  ◐ Create tests/conftest.py with shared fixtures [high]
  ○ Create tests/test_acp_web_v2.py with all test cases [high]
  ○ Run tests to verify they pass [high]
```

Icons: `○` pending, `◐` in_progress, `●` completed, `⊘` cancelled

---

## 4. Rich Diff/Patch Display

### Design principle

Diffs are messages. When a file is edited or patched, the diff appears inline — no `/diff` command needed.

### For the `edit` tool — via ACP content array

ACP generates `ToolCallContent` with `type: "diff"` in the `content` array of `tool_call_update`:
```
{ type: "diff", path: "/absolute/path", oldText: "...", newText: "..." }
```

Source: `packages/opencode/src/acp/tool.ts:328-341` (`diffContent` function)

The function fires for tools where `toToolKind(toolName) === "edit"`:
- Tools classified as `"edit"` kind: `edit`, `apply_patch`, `patch`, `write`
- Source: `packages/opencode/src/acp/tool.ts:48-55`

However, `apply_patch` uses `patchText` as input (not `oldString`/`newString`), so `diffContent()` returns empty for it.

### For `apply_patch` — via rawOutput.metadata

The `rawOutput` field contains rich per-file metadata:

Source: `packages/opencode/src/tool/apply_patch.ts:194-303`

```
rawOutput.metadata.files = [
  {
    filePath: "/absolute/path",
    relativePath: "src/foo.ts",
    type: "add"|"update"|"delete"|"move",
    patch: "--- a/...\n+++ b/...\n@@ ... @@\n...",
    additions: 5,
    deletions: 3,
    movePath: null
  }
]
```

Each file's `patch` is generated by `createTwoFilesPatch` from the `diff` npm package, trimmed via `trimDiff()`.
Source: `packages/opencode/src/tool/apply_patch.ts:83` (add), `133` (update), `172` (delete)

### For `edit` tool — metadata fallback

```
rawOutput.metadata.filediff = { file: "relative/path", patch: "...", additions: N, deletions: N }
```

Source: `packages/opencode/src/tool/edit.ts`

### rawOutput delivery via ACP

Source: `packages/opencode/src/acp/tool.ts:233-239` (`completedToolRawOutput`)

### Detection logic (combined)

1. Check `content[]` for `type == "diff"` items → covers `edit` tool inline diff
2. Check `rawOutput.metadata.files[]` → covers `apply_patch` per-file patches
3. Fall back to `rawOutput.metadata.filediff` → covers single-file edit metadata
4. Fall back to `rawOutput.metadata.diff` → concatenated diff string

### Session-level cumulative diff (HTTP)

`GET /session/{sessionID}/diff` returns `Array<SnapshotFileDiff>`:
```
{ file, patch, additions, deletions, status }
```

Source: `packages/sdk/js/src/v2/gen/types.gen.ts:150-156`
Source: `packages/opencode/src/session/session.ts:359-365` (session.diff event)
Source: `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:174`

SSE event: `session.diff` — `packages/sdk/js/src/v2/gen/types.gen.ts:1222-1229`

### Display format

For inline diff (edit tool):
```
  diff: src/utils.ts
  - const old = value;
  + const new = value;
```

For patch metadata (apply_patch):
```
  update: src/utils.ts +5 -2
    @@ -10,5 +10,8 @@
    +new line added
    -old line removed
```

Unified diff truncates at 30 lines with `... (N more lines)`.

### Note: apply_patch does NOT use HTTP

Both `edit` and `apply_patch` diffs arrive entirely through ACP stdio (`tool_call_update`). No HTTP call is needed to display per-tool diffs. The HTTP endpoint is only for the cumulative session diff (all changes since session start).

---

## 5. Subagent Tracking

### How subagents work

1. LLM calls `task` tool with `{description, prompt, subagent_type}`
2. Tool creates a child session with `parentID: parentSessionID`
3. Child runs independently (own messages, tool calls, permissions)
4. On completion, returns XML-wrapped output to parent

Source: `packages/opencode/src/tool/task.ts:64-79` (renderOutput), `81-344` (full tool)

### What ACP delivers

The `task` tool maps to ACP kind `"think"`:
Source: `packages/opencode/src/acp/tool.ts:65-66`

**tool_call (start):**
- `kind: "think"`, `rawInput: {description, prompt, subagent_type}`

**tool_call_update (running):**
- `rawOutput.metadata.sessionId` — the child session ID
- `rawOutput.metadata.parentSessionId` — parent session ID
- Source: `packages/opencode/src/tool/task.ts:171-181`

**tool_call_update (completed):**
- `rawOutput.output` — XML with `<task_result>...</task_result>`
- `rawOutput.metadata.sessionId` — child session ID
- Source: `packages/opencode/src/acp/tool.ts:186-202` (completedToolUpdate)
- Source: `packages/opencode/src/acp/tool.ts:233-239` (completedToolRawOutput)

### Limitation: no streaming of child internals via ACP

ACP events are filtered to the connected session. Child session events are separate. You do NOT see via ACP:
- Subagent's individual tool calls
- Subagent's streaming messages
- Subagent's reasoning

### How v2 gets live subagent activity

SSE `message.part.updated` events include a `sessionID` field. When that ID matches a known child session, v2 shows the activity inline.

Source (TUI approach): `packages/opencode/src/cli/cmd/run/subagent-data.ts:329-356`

v2 tracks active subagents in `_subagents: dict[childSessionID → metadata]`. On SSE events for those sessions, it prints tool activity under the subagent label.

### HTTP API for on-demand detail

| Endpoint | Returns | Source |
|----------|---------|--------|
| `GET /session/{childID}/messages` | Full message/part history | `packages/sdk/js/src/v2/gen/sdk.gen.ts:3640` |
| `GET /session/{parentID}/children` | All child sessions | `packages/sdk/js/src/v2/gen/sdk.gen.ts:3542-3570` |
| `GET /session/{childID}` | Session metadata | `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:82,145-155` |

### Display format

Start:
```
[subagent:explore] Search for auth patterns starting...
```

Activity (via SSE):
```
    [explore: Search for auth patterns] -> grep: searching "middleware"
    [explore: Search for auth patterns] ok read: src/auth/middleware.ts
```

Completion:
```
[subagent:explore] Search for auth patterns completed
  Found authentication middleware in 3 files...
  session: a1b2c3d4e5f6...
```

Result extraction: parses `<task_result>...</task_result>` XML, shows max 8 lines.

---

## 6. Cancel Enhancement

### Problem

ACP's `session/cancel` does NOT reject pending questions. The question Deferred is left hanging.

Source: `packages/opencode/src/acp/service.ts:349-352` (cancel → `session.abort`)
Source: `packages/opencode/src/session/run-state.ts:77-86` (RunState.cancel — no question handling)

### All ways a question gets cancelled

| Trigger | Mechanism | Source |
|---------|-----------|--------|
| Escape in TUI | `POST /question/{id}/reject` | `tui/src/routes/session/question.tsx:153` |
| Dismiss in Web | `POST /question/{id}/reject` | `app/src/pages/session/composer/session-question-dock.tsx:223-233` |
| Explicit API | `POST /question/{id}/reject` | `server/routes/instance/httpapi/groups/question.ts:46-56` |
| Service shutdown | Fails all pending Deferreds | `question/index.ts:140-146` |
| ACP `session/cancel` | **Does NOT reject** | (gap in implementation) |

### Solution in v2

`cancel()` does three things:
1. Rejects pending question via `POST /question/{requestID}/reject`
2. Sends `session/cancel` ACP notification
3. Unblocks all pending RPC waits (sets all `threading.Event` objects)

---

## 7. Multi-Instance Support

### Problem

Multiple `opencode acp` processes on the same machine collide on HTTP port. Default tries port 4096, then falls back to random (OS-assigned).

Source: `packages/opencode/src/server/server.ts` (listen logic)

ACP `initialize` response has no port field.
Source: `packages/opencode/src/acp/service.ts:91-136`

### Solution

acp_repl_v2.py picks a free port with `socket.bind(("127.0.0.1", 0))` and passes `--port <N>` to the subprocess. Each instance is self-contained — no external coordination.

```
Instance 1:  acp_repl_v2.py  ←stdio→  opencode acp --port 51234  ←http→  :51234/global/event
Instance 2:  acp_repl_v2.py  ←stdio→  opencode acp --port 52789  ←http→  :52789/global/event
```

