#!/usr/bin/env python3
"""
ACP REPL v2 — Enhanced Python REPL for opencode via Agent Client Protocol.

Adds SSE event stream, interactive questions, todo tracking, rich diffs,
and subagent visibility on top of the original ACP REPL.

Usage:
    python3 acp_repl_v2.py [--cwd /path/to/project] [--opencode-bin opencode] [--http-port PORT]

Spawns `opencode acp --port <PORT>` as a subprocess and communicates over
JSON-RPC/ndJSON on stdio, plus HTTP/SSE for questions, todos, diffs.
"""

import json
import os
import queue
import re
import signal
import shlex
import socket
import subprocess
import sys
import threading
import argparse
import urllib.request
import urllib.parse
import urllib.error
import time

# ANSI colors
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
WHITE = "\033[37m"
BG_GREEN = "\033[42m"
BG_RED = "\033[41m"

TODO_ICONS = {
    "pending": "○",
    "in_progress": "◐",
    "completed": "●",
    "cancelled": "⊘",
}


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url: str, timeout: float = 10) -> dict | list | None:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def http_post(url: str, body: dict | None = None, timeout: float = 10) -> dict | list | None:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


class SSEListener:
    """Connects to opencode's SSE stream at /global/event."""

    def __init__(self, http_base: str, callback):
        self._http_base = http_base
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _listen(self):
        url = f"{self._http_base}/global/event"
        while self._running:
            try:
                req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    event_type = None
                    data_lines: list[str] = []
                    while self._running:
                        raw = resp.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                        elif line == "":
                            if event_type and data_lines:
                                try:
                                    data = json.loads("\n".join(data_lines))
                                    self._callback(event_type, data)
                                except (json.JSONDecodeError, Exception):
                                    pass
                            event_type = None
                            data_lines = []
            except Exception:
                if self._running:
                    time.sleep(2)


class ACPClient:
    def __init__(self, opencode_bin: str, cwd: str, http_port: int, debug: bool = False):
        self.opencode_bin = opencode_bin
        self.cwd = cwd
        self.debug = debug
        self._http_port = http_port
        self._http_base = f"http://127.0.0.1:{http_port}"
        self._id = 0
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._prompt_done = threading.Event()
        self._tool_calls: dict[str, dict] = {}
        self._cancelled = False
        self.proc: subprocess.Popen | None = None
        self._log_file = open("acp_debug.log", "w")
        self._permission_queue: queue.Queue = queue.Queue()
        self._question_queue: queue.Queue = queue.Queue()
        self._print_lock = threading.Lock()
        self._permission_active = threading.Event()
        self._replaying = False
        self._available_models: list[dict] = []
        self._current_model: str = ""
        self._available_commands: list[dict] = []
        self._todos: list[dict] = []
        self._subagents: dict[str, dict] = {}
        self._current_question_id: str | None = None
        self._sse: SSEListener | None = None

    # ── Hand / Sandbox helpers (unchanged from v1) ──

    def _hand_storage_dir(self) -> str:
        base = os.environ.get("XDG_DATA_HOME") or os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(base, "opencode", "storage", "hand")

    def _use_sandbox_backend(self) -> bool:
        return os.path.isdir(os.path.join(self.cwd, ".opencode", "plugin", "hand"))

    def _to_local_path(self, path_value: str) -> str:
        path_value = (path_value or "").strip()
        if not path_value:
            raise RuntimeError("ACP fs request missing path")
        if os.path.isabs(path_value):
            return path_value
        return os.path.abspath(os.path.join(self.cwd, path_value))

    def _hand_working_dir(self) -> str:
        if os.environ.get("HAND_WORKING_DIR"):
            return os.environ["HAND_WORKING_DIR"]
        for candidate in (
            os.environ.get("HAND_CONFIG_PATH"),
            os.path.join(self.cwd, "hand.config.yaml"),
            os.path.join(self.cwd, "hand.config.yml"),
        ):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    text = f.read()
                match = re.search(r"^\s*workingDir\s*:\s*['\"]?([^'\"\r\n]+)", text, re.MULTILINE)
                if match:
                    return match.group(1).strip()
            except OSError:
                pass
        return "/workspace"

    def _hand_k8s_namespace(self) -> str:
        if os.environ.get("HAND_K8S_NAMESPACE"):
            return os.environ["HAND_K8S_NAMESPACE"]
        for candidate in (
            os.environ.get("HAND_CONFIG_PATH"),
            os.path.join(self.cwd, "hand.config.yaml"),
            os.path.join(self.cwd, "hand.config.yml"),
        ):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    text = f.read()
                match = re.search(r"^\s*namespace\s*:\s*['\"]?([^'\"\r\n]+)", text, re.MULTILINE)
                if match:
                    return match.group(1).strip()
            except OSError:
                pass
        return "hand-sandboxes"

    def _hand_provider(self) -> str:
        for candidate in (
            os.environ.get("HAND_CONFIG_PATH"),
            os.path.join(self.cwd, "hand.config.yaml"),
            os.path.join(self.cwd, "hand.config.yml"),
        ):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("provider:"):
                            return line.split(":", 1)[1].strip().strip('"').strip("'")
            except OSError:
                pass
        return "docker"

    def _hand_ark_config(self) -> dict:
        config: dict = {"baseUrl": "", "namespace": "hand-sandboxes"}
        for candidate in (
            os.environ.get("HAND_CONFIG_PATH"),
            os.path.join(self.cwd, "hand.config.yaml"),
            os.path.join(self.cwd, "hand.config.yml"),
        ):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    text = f.read()
                in_ark = False
                for line in text.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("ark:"):
                        in_ark = True
                        continue
                    if in_ark and line and not line[0].isspace():
                        in_ark = False
                    if in_ark and ":" in stripped:
                        key, _, val = stripped.partition(":")
                        val = val.strip().strip('"').strip("'")
                        if key.strip() == "baseUrl":
                            config["baseUrl"] = val
                        elif key.strip() == "namespace":
                            config["namespace"] = val
                        elif key.strip() == "apiToken" and val:
                            config["apiToken"] = val
            except OSError:
                pass
            break
        if os.environ.get("HAND_ARK_BASE_URL"):
            config["baseUrl"] = os.environ["HAND_ARK_BASE_URL"]
        return config

    def _ark_request(self, method: str, path: str, body: dict | None = None) -> dict:
        ark = self._hand_ark_config()
        url = ark["baseUrl"].rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        if ark.get("apiToken"):
            headers["Authorization"] = f"Bearer {ark['apiToken']}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())

    def _ark_read_file(self, sandbox_id: str, file_path: str) -> str:
        ark = self._hand_ark_config()
        ns = ark["namespace"]
        path = f"/sandboxes/{sandbox_id}/files?path={urllib.parse.quote(file_path, safe='')}&namespace={urllib.parse.quote(ns, safe='')}"
        res = self._ark_request("GET", path)
        return res.get("content", "")

    def _ark_write_file(self, sandbox_id: str, file_path: str, content: str) -> None:
        ark = self._hand_ark_config()
        self._ark_request("PUT", f"/sandboxes/{sandbox_id}/files", {
            "path": file_path,
            "content": content,
            "namespace": ark["namespace"],
        })

    def _ark_exec(self, sandbox_id: str, command: str, working_dir: str | None = None) -> dict:
        ark = self._hand_ark_config()
        return self._ark_request("POST", f"/sandboxes/{sandbox_id}/exec", {
            "command": command,
            "working_dir": working_dir or self._hand_working_dir(),
            "namespace": ark["namespace"],
        })

    def _resolve_hand_session(self, session_id: str | None) -> dict:
        if not session_id:
            raise RuntimeError("ACP fs request missing sessionId")
        storage_dir = self._hand_storage_dir()
        if not os.path.isdir(storage_dir):
            raise RuntimeError(f"Hand storage not found: {storage_dir}")
        for name in os.listdir(storage_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(storage_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            session = (payload.get("sessions") or {}).get(session_id)
            if session:
                return {
                    "container_id": session.get("containerId", ""),
                    "worktree": payload.get("worktree") or self.cwd,
                }
        raise RuntimeError(f"No Hand sandbox found for session {session_id}")

    def _to_container_path(self, path_value: str, worktree: str) -> str:
        path_value = (path_value or "").strip()
        if not path_value:
            raise RuntimeError("ACP fs request missing path")
        if path_value.startswith("/"):
            return path_value
        working_dir = self._hand_working_dir().rstrip("/")
        if os.path.isabs(path_value):
            rel = os.path.relpath(path_value, worktree).replace("\\", "/")
            if rel == ".":
                return working_dir
            if rel.startswith("../") or rel == "..":
                raise RuntimeError(f"Path escapes worktree: {path_value}")
            return f"{working_dir}/{rel}"
        rel = path_value.replace("\\", "/").lstrip("/")
        return f"{working_dir}/{rel}" if rel else working_dir

    def _sandbox_exec(self, container_id: str, script: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        provider = self._hand_provider()
        if provider == "ark-sandbox":
            res = self._ark_exec(container_id, script)
            exit_code = res.get("exit_code", 0)
            stdout = res.get("stdout", "")
            stderr = res.get("stderr", "")
            if exit_code != 0:
                raise RuntimeError(f"ark exec exit {exit_code}: {stderr or stdout}")
            return subprocess.CompletedProcess(args=["ark", "exec"], returncode=0, stdout=stdout, stderr=stderr)
        commands = [
            ["docker", "exec", "-i", container_id, "sh", "-lc", script],
            ["kubectl", "exec", "-i", container_id, "-n", self._hand_k8s_namespace(), "--", "sh", "-lc", script],
        ]
        failures: list[str] = []
        for cmd in commands:
            try:
                result = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
            except OSError as exc:
                failures.append(f"{' '.join(cmd[:2])}: {exc}")
                continue
            if result.returncode == 0:
                return result
            failures.append(f"{' '.join(cmd[:4])}: {result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'}")
        raise RuntimeError("; ".join(failures))

    def _handle_fs_request(self, msg: dict) -> bool:
        method = msg.get("method", "")
        if method not in {"fs/write_text_file", "fs/read_text_file"}:
            return False
        req_id = msg.get("id")
        params = msg.get("params", {})
        try:
            if not self._use_sandbox_backend():
                local_path = self._to_local_path(params.get("path") or params.get("filePath") or "")
                if method == "fs/read_text_file":
                    with open(local_path, "r", encoding="utf-8") as f:
                        self._send_response(req_id, {"content": f.read()})
                    return True
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(params.get("content", ""))
                self._send_response(req_id, {})
                return True
            resolved = self._resolve_hand_session(params.get("sessionId") or self._session_id)
            container_path = self._to_container_path(params.get("path") or params.get("filePath") or "", resolved["worktree"])
            provider = self._hand_provider()
            if provider == "ark-sandbox":
                if method == "fs/read_text_file":
                    content = self._ark_read_file(resolved["container_id"], container_path)
                    self._send_response(req_id, {"content": content})
                    return True
                self._ark_write_file(resolved["container_id"], container_path, params.get("content", ""))
                self._send_response(req_id, {})
                return True
            quoted_path = shlex.quote(container_path)
            if method == "fs/read_text_file":
                result = self._sandbox_exec(resolved["container_id"], f"cat {quoted_path}")
                self._send_response(req_id, {"content": result.stdout})
                return True
            content = params.get("content", "")
            directory = os.path.dirname(container_path).replace("\\", "/")
            mkdir = f"mkdir -p {shlex.quote(directory)} && " if directory else ""
            self._sandbox_exec(resolved["container_id"], f"{mkdir}cat > {quoted_path}", input_text=content)
            self._send_response(req_id, {})
            return True
        except Exception as exc:
            self._send_response(req_id, None, error={"code": -32603, "message": str(exc)})
            return True

    # ── Core protocol ──

    def _log(self, direction: str, msg):
        self._log_file.write(f"{direction} {json.dumps(msg)}\n")
        self._log_file.flush()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def start(self):
        env = os.environ.copy()
        existing = env.get("OPENCODE_PERMISSION", "")
        try:
            perm = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            perm = {}
        perm.setdefault("bash", "ask")
        perm.setdefault("edit", "ask")
        env["OPENCODE_PERMISSION"] = json.dumps(perm)
        self.proc = subprocess.Popen(
            [self.opencode_bin, "acp", "--cwd", self.cwd, "--port", str(self._http_port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()
        self._permission_thread = threading.Thread(target=self._permission_loop, daemon=True)
        self._permission_thread.start()
        self._question_thread = threading.Thread(target=self._question_loop, daemon=True)
        self._question_thread.start()
        self._sse = SSEListener(self._http_base, self._handle_sse_event)
        self._sse.start()

    def _send(self, method: str, params: dict, is_notification: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not is_notification:
            msg_id = self._next_id()
            msg["id"] = msg_id
            event = threading.Event()
            with self._lock:
                self._pending[msg_id] = event

        line = json.dumps(msg) + "\n"
        if self.debug:
            print(f"{DIM}>>> {line.strip()}{RESET}", file=sys.stderr)
        self._log(">>>", msg)
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except BrokenPipeError:
            print(f"{RED}Connection to opencode lost.{RESET}")
            sys.exit(1)

        if is_notification:
            return None

        event.wait(timeout=300)
        with self._lock:
            self._pending.pop(msg_id, None)
            result = self._results.pop(msg_id, None)
        if result and "error" in result:
            err = result["error"]
            raise RuntimeError(f"ACP error {err.get('code')}: {err.get('message')}")
        return result.get("result") if result else None

    def _read_loop(self):
        while True:
            try:
                line = self.proc.stdout.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_message(msg)

    def _stderr_loop(self):
        while True:
            try:
                line = self.proc.stderr.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            if self.debug:
                print(f"{DIM}[stderr] {line.strip()}{RESET}", file=sys.stderr)

    def _handle_message(self, msg: dict):
        self._log("<<<", msg)
        if self.debug:
            preview = json.dumps(msg)[:300]
            print(f"{DIM}<<< {preview}{RESET}", file=sys.stderr)

        method = msg.get("method", "")
        params = msg.get("params", {})

        if "id" in msg and ("result" in msg or "error" in msg) and not method:
            msg_id = msg["id"]
            with self._lock:
                self._results[msg_id] = msg
                event = self._pending.get(msg_id)
            if event:
                event.set()
            return

        if method == "session/update":
            update = params.get("update", params)
            update_type = update.get("sessionUpdate", "")
            if update_type == "available_commands_update":
                self._available_commands = update.get("availableCommands", [])
            with self._print_lock:
                self._handle_session_update(update)
            if "id" in msg:
                self._send_response(msg["id"], {})
            return

        if method == "session/request_permission":
            self._handle_permission_request(msg)
            return

        if self._handle_fs_request(msg):
            return

        if "id" in msg and method:
            self._log("!!!", f"unhandled request: {method}")
            self._send_response(msg["id"], None, error={"code": -32601, "message": f"Method not found: {method}"})
            return

        self._log("!!!", f"unhandled notification: {method}")

    def _send_response(self, req_id, result, error=None):
        msg = {"jsonrpc": "2.0", "id": req_id}
        if error:
            msg["error"] = error
        else:
            msg["result"] = result
        line = json.dumps(msg) + "\n"
        self._log(">>>", msg)
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass

    # ── Permission handling (unchanged from v1) ──

    def _handle_permission_request(self, msg: dict):
        self._permission_queue.put(msg)

    def _permission_loop(self):
        while True:
            msg = self._permission_queue.get()
            req_id = msg.get("id")
            params = msg.get("params", {})
            tool_call = params.get("toolCall", {})
            title = tool_call.get("title", params.get("title", "unknown"))
            kind = tool_call.get("kind", "")
            raw_input = tool_call.get("rawInput", params.get("rawInput", {}))
            options = params.get("options", [])
            with self._print_lock:
                print(f"\n{YELLOW}[permission required] {title}{RESET}")
                if kind:
                    print(f"  {DIM}kind: {kind}{RESET}")
                if raw_input:
                    for k, v in raw_input.items():
                        val = str(v) if not isinstance(v, str) else v
                        if len(val) > 200:
                            val = val[:200] + "..."
                        print(f"  {BOLD}{k}:{RESET} {val}")
                opt_labels = ", ".join(f"[{o.get('optionId', '?')[0]}]{o.get('optionId', '?')[1:]}" for o in options)
                if not opt_labels:
                    opt_labels = "[y]es / [n]o / [a]lways"
                try:
                    answer = input(f"{YELLOW}  {opt_labels}: {RESET}").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "reject"
                    print(f"{YELLOW}rejected{RESET}")
            option_map = {"y": "once", "o": "once", "a": "always", "r": "reject", "n": "reject"}
            option_id = option_map.get(answer, answer)
            valid_ids = {o.get("optionId") for o in options}
            if option_id not in valid_ids:
                option_id = "reject"
            self._send_response(req_id, {"outcome": {"outcome": "selected", "optionId": option_id}})
            if option_id == "reject":
                self.cancel()

    # ── SSE event dispatch ──

    def _handle_sse_event(self, event_type: str, data: dict):
        props = data.get("properties", data)

        if event_type == "question.v2.asked":
            request = props.get("request", props)
            request_id = request.get("id", "")
            session_id = request.get("sessionID", "")
            questions = request.get("questions", [])
            if request_id and questions and session_id == self._session_id:
                self._question_queue.put({
                    "requestID": request_id,
                    "sessionID": session_id,
                    "questions": questions,
                    "source": "sse",
                })

        elif event_type == "todo.updated":
            session_id = props.get("sessionID", "")
            if session_id == self._session_id:
                todos = props.get("todos", [])
                self._todos = todos
                with self._print_lock:
                    self._render_todos(todos, header="[todo updated]")

        elif event_type == "message.part.updated":
            part = props.get("part", {})
            session_id = part.get("sessionID", props.get("sessionID", ""))
            if session_id in self._subagents:
                self._handle_subagent_part_update(session_id, part)

    def _handle_subagent_part_update(self, session_id: str, part: dict):
        if part.get("type") != "tool":
            return
        sub = self._subagents.get(session_id)
        if not sub:
            return
        label = sub.get("label", "subagent")
        tool = part.get("tool", "")
        state = part.get("state", {})
        status = state.get("status", "")
        title = state.get("title", tool)
        with self._print_lock:
            if status == "running":
                print(f"    {DIM}[{label}] -> {tool}: {title}{RESET}")
            elif status == "completed":
                print(f"    {DIM}[{label}] {GREEN}ok{RESET}{DIM} {tool}: {title}{RESET}")

    # ── Question handling ──

    def _question_loop(self):
        while True:
            item = self._question_queue.get()
            request_id = item.get("requestID", "")
            questions = item.get("questions", [])
            if not questions or not request_id:
                continue

            self._current_question_id = request_id

            all_answers: list[list[str]] = []
            rejected = False

            for qi, q in enumerate(questions):
                header = q.get("header", "")
                question_text = q.get("question", "")
                options = q.get("options", [])
                multiple = q.get("multiple", False)
                custom = q.get("custom", True)

                with self._print_lock:
                    total = len(questions)
                    if total > 1:
                        print(f"\n{CYAN}--- Question ({qi+1}/{total}) ---{RESET}")
                    else:
                        print(f"\n{CYAN}--- Question ---{RESET}")
                    if header:
                        print(f"  {BOLD}{header}{RESET}")
                    if question_text:
                        print(f"  {question_text}")
                    print()

                    for i, opt in enumerate(options, 1):
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                        desc_str = f" {DIM}-- {desc}{RESET}" if desc else ""
                        if multiple:
                            print(f"  {BOLD}{i}){RESET} [ ] {label}{desc_str}")
                        else:
                            print(f"  {BOLD}{i}){RESET} {label}{desc_str}")

                    if custom:
                        n = len(options) + 1
                        print(f"  {BOLD}{n}){RESET} {DIM}[Type your own answer]{RESET}")

                    print(f"  {DIM}0) Skip / reject this question{RESET}")
                    print()

                answers = self._collect_question_answers(options, multiple, custom)
                if answers is None:
                    self._reject_question(request_id)
                    rejected = True
                    break
                all_answers.append(answers)

            if not rejected:
                self._reply_question(request_id, all_answers)

            self._current_question_id = None

    def _collect_question_answers(self, options: list[dict], multiple: bool, custom: bool) -> list[str] | None:
        max_n = len(options) + (1 if custom else 0)
        try:
            if multiple:
                raw = input(f"  {YELLOW}Select (comma-separated, e.g. 1,3) or 0 to skip: {RESET}").strip()
            else:
                raw = input(f"  {YELLOW}Select (1-{max_n}) or 0 to skip: {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw.lower() in ("0", "q", "skip", "reject", ""):
            return None

        if multiple:
            indices = []
            for part in raw.replace(",", " ").split():
                if part.isdigit():
                    idx = int(part)
                    if 1 <= idx <= max_n:
                        indices.append(idx - 1)
            answers: list[str] = []
            for i in indices:
                if i < len(options):
                    answers.append(options[i]["label"])
                elif custom:
                    try:
                        text = input(f"  {YELLOW}Custom text: {RESET}").strip()
                    except (EOFError, KeyboardInterrupt):
                        return None
                    if text:
                        answers.append(text)
            return answers if answers else None
        else:
            if not raw.isdigit():
                return [raw]
            choice = int(raw) - 1
            if 0 <= choice < len(options):
                return [options[choice]["label"]]
            elif custom and choice == len(options):
                try:
                    text = input(f"  {YELLOW}Custom text: {RESET}").strip()
                except (EOFError, KeyboardInterrupt):
                    return None
                return [text] if text else None
            return None

    def _reply_question(self, request_id: str, answers: list[list[str]]):
        url = f"{self._http_base}/question/{request_id}/reply"
        result = http_post(url, {"answers": answers})
        if result is not None:
            with self._print_lock:
                print(f"  {GREEN}[answer sent]{RESET}")
        else:
            with self._print_lock:
                print(f"  {RED}[reply failed]{RESET}")

    def _reject_question(self, request_id: str):
        url = f"{self._http_base}/question/{request_id}/reject"
        http_post(url)
        with self._print_lock:
            print(f"  {YELLOW}[question rejected]{RESET}")

    # ── Session update handling (enhanced) ──

    def _handle_session_update(self, params: dict):
        update_type = params.get("sessionUpdate", "")

        if self._replaying:
            return

        if update_type == "agent_message_chunk":
            content = params.get("content", {})
            text = content.get("text", "")
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()

        elif update_type == "agent_thought_chunk":
            content = params.get("content", {})
            text = content.get("text", "")
            if text:
                sys.stdout.write(f"{DIM}{text}{RESET}")
                sys.stdout.flush()

        elif update_type == "tool_call":
            tc_id = params.get("toolCallId", "")
            kind = params.get("kind", "")
            title = params.get("title", "")
            raw_input = params.get("rawInput", {})
            self._tool_calls[tc_id] = {"status": "pending", "kind": kind, "shown_params": False}

            if kind == "think":
                agent_type = raw_input.get("subagent_type", "agent")
                desc = raw_input.get("description", title)
                print(f"\n{MAGENTA}[subagent:{agent_type}]{RESET} {desc} {DIM}starting...{RESET}")
            else:
                label = title or kind or "tool"
                print(f"\n{CYAN}[{label}]{RESET} {DIM}pending{RESET}")

        elif update_type == "tool_call_update":
            self._handle_tool_call_update(params)

        elif update_type == "usage_update":
            used = params.get("used", 0)
            size = params.get("size", 0)
            cost = params.get("cost", {})
            amount = cost.get("amount", 0)
            pct = (used / size * 100) if size else 0
            print(f"\n{BLUE}[usage] {pct:.0f}% context | ${amount:.4f}{RESET}")

        elif update_type == "plan":
            entries = params.get("entries", [])
            if entries:
                print(f"\n{MAGENTA}[plan]{RESET}")
                for e in entries:
                    status = e.get("status", "pending")
                    icon = "x" if status == "completed" else "-" if status == "in_progress" else " "
                    print(f"  [{icon}] {e.get('content', '')}")

    def _handle_tool_call_update(self, params: dict):
        tc_id = params.get("toolCallId", "")
        status = params.get("status", "")
        kind = params.get("kind", "")
        title = params.get("title", "")
        raw_input = params.get("rawInput", {})
        raw_output = params.get("rawOutput", {})
        prev = self._tool_calls.get(tc_id, {})
        prev_status = prev.get("status", "")
        shown_params = prev.get("shown_params", False)

        # ── Subagent (task) tool ──
        if kind == "think":
            self._handle_subagent_update(tc_id, status, raw_input, raw_output, params)
            self._tool_calls[tc_id] = {"status": status, "kind": kind, "shown_params": True}
            return

        # ── Todo tool ──
        metadata = raw_output.get("metadata", {})
        if "todos" in metadata and status == "completed":
            self._todos = metadata["todos"]
            self._render_todos(self._todos, header="[todo]")
            self._tool_calls[tc_id] = {"status": status, "kind": kind, "shown_params": True}
            return

        # ── Regular tool ──
        color = GREEN if status == "completed" else RED if status == "failed" else YELLOW
        label = title or kind or "tool"

        if status == "in_progress" and prev_status == "in_progress" and shown_params:
            pass
        else:
            print(f"{CYAN}[{label}]{RESET} {color}{status}{RESET}")
            if raw_input and not shown_params:
                skip = {"description"}
                desc = raw_input.get("description", "")
                if desc:
                    print(f"  {DIM}{desc}{RESET}")
                for k, v in raw_input.items():
                    if k in skip:
                        continue
                    val = str(v) if not isinstance(v, str) else v
                    if len(val) > 200:
                        val = val[:200] + "..."
                    print(f"  {BOLD}{k}:{RESET} {DIM}{val}{RESET}")
                shown_params = True

        self._tool_calls[tc_id] = {"status": status, "kind": kind, "shown_params": shown_params}

        if status in ("completed", "failed"):
            contents = params.get("content", [])
            has_diff = False
            for c in contents:
                if c.get("type") == "diff":
                    has_diff = True
                    self._render_inline_diff(c)
                elif c.get("type") == "content":
                    text = c.get("content", {}).get("text", "")
                    if text:
                        lines = text.strip().split("\n")
                        if len(lines) > 10:
                            for line in lines[:5]:
                                print(f"  {DIM}{line}{RESET}")
                            print(f"  {DIM}... ({len(lines) - 5} more lines){RESET}")
                        else:
                            for line in lines:
                                print(f"  {DIM}{line}{RESET}")

            if not has_diff and status == "completed":
                files = metadata.get("files", [])
                if files:
                    self._render_patch_files(files)
                else:
                    filediff = metadata.get("filediff")
                    if filediff:
                        self._render_single_filediff(filediff)

    # ── Subagent display ──

    def _handle_subagent_update(self, tc_id: str, status: str, raw_input: dict, raw_output: dict, params: dict):
        agent_type = raw_input.get("subagent_type", "agent")
        desc = raw_input.get("description", "")
        metadata = raw_output.get("metadata", {})
        child_id = metadata.get("sessionId", "")

        if status == "in_progress" and child_id:
            self._subagents[child_id] = {
                "label": f"{agent_type}: {desc}",
                "status": "running",
                "toolCallId": tc_id,
                "description": desc,
                "agent_type": agent_type,
            }

        elif status == "completed":
            output = raw_output.get("output", "")
            match = re.search(r"<task_result>\s*([\s\S]*?)\s*</task_result>", output)
            result_text = match.group(1).strip() if match else ""

            if child_id and child_id in self._subagents:
                self._subagents[child_id]["status"] = "completed"

            print(f"{MAGENTA}[subagent:{agent_type}]{RESET} {desc} {GREEN}completed{RESET}")
            if result_text:
                lines = result_text.strip().split("\n")
                show = lines[:8]
                for line in show:
                    print(f"  {DIM}{line}{RESET}")
                if len(lines) > 8:
                    print(f"  {DIM}... ({len(lines) - 8} more lines){RESET}")
            if child_id:
                print(f"  {DIM}session: {child_id[:12]}...{RESET}")

        elif status == "failed":
            if child_id and child_id in self._subagents:
                self._subagents[child_id]["status"] = "error"
            output = raw_output.get("output", "") or raw_output.get("error", "")
            match = re.search(r"<task_error>\s*([\s\S]*?)\s*</task_error>", output)
            error_text = match.group(1).strip() if match else output[:200]
            print(f"{MAGENTA}[subagent:{agent_type}]{RESET} {desc} {RED}failed{RESET}")
            if error_text:
                print(f"  {RED}{error_text}{RESET}")

    # ── Diff rendering ──

    def _render_inline_diff(self, diff_content: dict):
        path = diff_content.get("path", "")
        old_text = diff_content.get("oldText", "")
        new_text = diff_content.get("newText", "")
        print(f"  {MAGENTA}diff: {path}{RESET}")
        if old_text or new_text:
            old_lines = old_text.split("\n") if old_text else []
            new_lines = new_text.split("\n") if new_text else []
            for line in old_lines:
                if line:
                    print(f"  {RED}- {line}{RESET}")
            for line in new_lines:
                if line:
                    print(f"  {GREEN}+ {line}{RESET}")

    def _render_patch_files(self, files: list[dict]):
        for f in files:
            rel = f.get("relativePath", f.get("filePath", ""))
            op = f.get("type", "update")
            adds = f.get("additions", 0)
            dels = f.get("deletions", 0)
            print(f"  {MAGENTA}{op}: {rel}{RESET} {GREEN}+{adds}{RESET} {RED}-{dels}{RESET}")
            patch = f.get("patch", "")
            if patch:
                self._render_unified_diff(patch, indent="    ")

    def _render_single_filediff(self, filediff: dict):
        rel = filediff.get("file", "")
        adds = filediff.get("additions", 0)
        dels = filediff.get("deletions", 0)
        print(f"  {MAGENTA}edit: {rel}{RESET} {GREEN}+{adds}{RESET} {RED}-{dels}{RESET}")
        patch = filediff.get("patch", "")
        if patch:
            self._render_unified_diff(patch, indent="    ")

    def _render_unified_diff(self, patch: str, indent: str = "  ", max_lines: int = 30):
        lines = patch.split("\n")
        shown = 0
        for line in lines:
            if shown >= max_lines:
                remaining = len(lines) - shown
                if remaining > 0:
                    print(f"{indent}{DIM}... ({remaining} more lines){RESET}")
                break
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("@@"):
                print(f"{indent}{CYAN}{line}{RESET}")
            elif line.startswith("+"):
                print(f"{indent}{GREEN}{line}{RESET}")
            elif line.startswith("-"):
                print(f"{indent}{RED}{line}{RESET}")
            else:
                print(f"{indent}{DIM}{line}{RESET}")
            shown += 1

    # ── Todo rendering ──

    def _render_todos(self, todos: list[dict], header: str = "[todo]"):
        if not todos:
            return
        print(f"\n{BLUE}{header}{RESET}")
        for t in todos:
            status = t.get("status", "pending")
            icon = TODO_ICONS.get(status, "?")
            content = t.get("content", "")
            priority = t.get("priority", "")
            color = YELLOW if status == "in_progress" else GREEN if status == "completed" else DIM if status == "cancelled" else ""
            pri_str = f" {DIM}[{priority}]{RESET}" if priority and priority != "medium" else ""
            print(f"  {color}{icon} {content}{RESET}{pri_str}")

    # ── Session / prompt API ──

    def initialize(self) -> dict:
        result = self._send("initialize", {"protocolVersion": 1})
        agent_info = result.get("agentInfo", {})
        print(f"{GREEN}Connected to {agent_info.get('name', 'agent')} v{agent_info.get('version', '?')}{RESET}")
        return result

    def _extract_models(self, result: dict):
        for opt in result.get("configOptions", []):
            if opt.get("id") == "model":
                self._current_model = opt.get("currentValue", "")
                self._available_models = opt.get("options", [])
                return
        models = result.get("models", {})
        self._current_model = models.get("currentModelId", "unknown")

    def new_session(self) -> str:
        result = self._send("session/new", {"cwd": self.cwd, "mcpServers": []})
        self._session_id = result.get("sessionId", "")
        self._extract_models(result)
        print(f"{GREEN}Session: {self._session_id} | Model: {self._current_model}{RESET}")
        return self._session_id

    def list_sessions(self) -> list[dict]:
        result = self._send("session/list", {"cwd": self.cwd})
        return result.get("sessions", []) if result else []

    def load_session(self, session_id: str) -> str:
        self._replaying = True
        try:
            result = self._send("session/load", {
                "sessionId": session_id,
                "cwd": self.cwd,
                "mcpServers": [],
            })
        finally:
            self._replaying = False
        self._session_id = session_id
        if result:
            self._extract_models(result)
        print(f"{GREEN}Resumed: {self._session_id} | Model: {self._current_model}{RESET}")
        return self._session_id

    def set_model(self, model_id: str):
        result = self._send("session/set_config_option", {
            "sessionId": self._session_id,
            "configId": "model",
            "value": model_id,
        })
        self._current_model = model_id
        print(f"{GREEN}Model: {model_id}{RESET}")
        return result

    def prompt(self, text: str) -> dict | None:
        self._cancelled = False
        self._tool_calls.clear()
        result = self._send("session/prompt", {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": text}],
        })
        return result

    def cancel(self):
        if self._current_question_id:
            self._reject_question(self._current_question_id)
            self._current_question_id = None
        if self._session_id:
            self._cancelled = True
            self._send("session/cancel", {"sessionId": self._session_id}, is_notification=True)
            with self._lock:
                for event in self._pending.values():
                    event.set()

    def stop(self):
        if self._sse:
            self._sse.stop()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)


def pick_session(client: ACPClient) -> bool:
    sessions = client.list_sessions()
    if not sessions:
        print(f"{DIM}No existing sessions. Creating new session.{RESET}")
        client.new_session()
        return True
    display = sessions[:10]
    print(f"\n{BOLD}Recent sessions:{RESET}")
    for i, s in enumerate(display, 1):
        title = s.get("title", "untitled")
        updated = s.get("updatedAt", "")[:16].replace("T", " ")
        sid = s.get("sessionId", "")
        print(f"  {BOLD}{i}.{RESET} {title} {DIM}({updated}) [{sid}]{RESET}")
    print()
    try:
        choice = input(f"Select [1-{len(display)}] or Enter for new session: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not choice:
        client.new_session()
        return True
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(display):
            client.load_session(display[idx]["sessionId"])
            return True
    except ValueError:
        pass
    print(f"{YELLOW}Invalid choice. Creating new session.{RESET}")
    client.new_session()
    return True


def main():
    parser = argparse.ArgumentParser(description="ACP REPL v2 for opencode")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for the agent")
    parser.add_argument("--opencode-bin", default="opencode", help="Path to opencode binary")
    parser.add_argument("--debug", action="store_true", help="Print all raw JSON-RPC messages to stderr")
    parser.add_argument("--session", default=None, help="Resume a specific session by ID")
    parser.add_argument("--http-port", type=int, default=0, help="HTTP port for SSE/API (0 = auto)")
    args = parser.parse_args()

    http_port = args.http_port if args.http_port > 0 else find_free_port()

    client = ACPClient(args.opencode_bin, args.cwd, http_port, debug=args.debug)

    def handle_sigint(sig, frame):
        client.cancel()
        print(f"\n{YELLOW}Cancelled.{RESET}")

    signal.signal(signal.SIGINT, handle_sigint)

    print(f"""{CYAN}
                                         __
  ____  ____  ___  ____  _________  ____/ /__     ____ __________
 / __ \\/ __ \\/ _ \\/ __ \\/ ___/ __ \\/ __  / _ \\   / __ `/ ___/ __ \\
/ /_/ / /_/ /  __/ / / / /__/ /_/ / /_/ /  __/  / /_/ / /__/ /_/ /
\\____/ .___/\\___/_/ /_/\\___/\\____/\\__,_/\\___/   \\__,_/\\___/ .___/
    /_/                                                  /_/
{RESET}{DIM}            ACP Client v2 — CLI + SSE {RESET}
""")
    print(f"Connecting to opencode (HTTP port: {http_port})...")
    client.start()

    try:
        client.initialize()
        if args.session:
            client.load_session(args.session)
        else:
            client.new_session()
    except Exception as e:
        print(f"{RED}Failed to initialize: {e}{RESET}")
        client.stop()
        sys.exit(1)

    print(f"{DIM}Type prompts below. /new /sessions /models /skills /mcps /exit{RESET}\n")

    while True:
        try:
            user_input = input(f"{BOLD}> {RESET}")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped.lower() in ("exit", "quit", "/exit"):
            break
        if stripped.lower() == "/new":
            client.new_session()
            continue
        if stripped.lower() == "/sessions":
            pick_session(client)
            continue
        if stripped.lower() == "/skills":
            cmds = client._available_commands
            if not cmds:
                print(f"{DIM}No skills available.{RESET}")
                continue
            print(f"\n{BOLD}Available skills:{RESET}")
            for i, c in enumerate(cmds, 1):
                print(f"  {BOLD}{i}.{RESET} /{c['name']} {DIM}-- {c.get('description', '')}{RESET}")
            print(f"\n{DIM}Use /<name> in your prompt to invoke a skill.{RESET}\n")
            continue
        if stripped.lower() == "/mcps":
            print(f"\n{DIM}MCP server status is not available via ACP protocol.")
            print(f"MCP servers are configured in your opencode config file.")
            print(f"Run 'opencode' TUI and use /mcps there to check status.{RESET}\n")
            continue
        if stripped.startswith("/"):
            skill_name = stripped[1:].split()[0].lower()
            skill_args = stripped[1 + len(skill_name):].strip()
            known = {c["name"].lower(): c["name"] for c in client._available_commands}
            if skill_name in known:
                prompt_text = f"/{known[skill_name]}"
                if skill_args:
                    prompt_text += f" {skill_args}"
                try:
                    result = client.prompt(prompt_text)
                    print()
                except RuntimeError as e:
                    print(f"\n{RED}{e}{RESET}\n")
                continue
        if stripped.lower() == "/models":
            if not client._available_models:
                print(f"{DIM}No models available.{RESET}")
                continue
            print(f"\n{BOLD}Available models:{RESET} {DIM}(current: {client._current_model}){RESET}")
            for i, m in enumerate(client._available_models, 1):
                name = m.get("name", m.get("value", ""))
                value = m.get("value", "")
                marker = f" {GREEN}<-{RESET}" if value == client._current_model else ""
                print(f"  {BOLD}{i}.{RESET} {name} {DIM}[{value}]{RESET}{marker}")
            print()
            try:
                choice = input(f"Select [1-{len(client._available_models)}] or Enter to keep current: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if not choice:
                continue
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(client._available_models):
                    client.set_model(client._available_models[idx]["value"])
                else:
                    print(f"{YELLOW}Invalid choice.{RESET}")
            except (ValueError, RuntimeError) as e:
                print(f"{RED}{e}{RESET}")
            continue

        try:
            result = client.prompt(user_input)
            print()
            if result:
                usage = result.get("usage")
                if usage:
                    inp = usage.get("inputTokens", 0)
                    out = usage.get("outputTokens", 0)
                    print(f"{DIM}[tokens: {inp} in / {out} out]{RESET}")
            print()
        except RuntimeError as e:
            print(f"\n{RED}{e}{RESET}\n")
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}\n")

    print(f"\n{DIM}Session: {client._session_id}{RESET}")
    print(f"{DIM}Resume with: python3 acp_repl_v2.py --session {client._session_id}{RESET}")

    # Stop Hand sandboxes based on hand.config.yaml (if present)
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand.config.yaml")
    provider = None
    k8s_namespace = "hand-sandboxes"
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path) as f:
                hand_cfg = yaml.safe_load(f) or {}
            provider = hand_cfg.get("provider")
            k8s_namespace = hand_cfg.get("kubernetes", {}).get("namespace", k8s_namespace)
        except ImportError:
            with open(config_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("provider:"):
                        provider = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if line.startswith("namespace:"):
                        k8s_namespace = line.split(":", 1)[1].strip().strip('"').strip("'")
        except Exception as e:
            print(f"{DIM}Could not read hand.config.yaml: {e}{RESET}")

    if provider == "docker":
        try:
            ids = subprocess.run(
                ["docker", "ps", "-q", "--filter", "label=opencode.hand=true"],
                capture_output=True, text=True, timeout=5,
            )
            container_ids = ids.stdout.strip().split()
            if container_ids:
                print(f"{DIM}Stopping {len(container_ids)} sandbox container(s)...{RESET}")
                subprocess.run(
                    ["docker", "stop", "-t", "2"] + container_ids,
                    capture_output=True, timeout=15,
                )
                print(f"{DIM}Containers stopped.{RESET}")
        except Exception as e:
            print(f"{DIM}Docker cleanup: {e}{RESET}")
    elif provider == "kubernetes":
        try:
            pods = subprocess.run(
                ["kubectl", "get", "pods", "-n", k8s_namespace,
                 "-l", "opencode.hand=true", "-o", "jsonpath={.items[*].metadata.name}"],
                capture_output=True, text=True, timeout=10,
            )
            pod_names = pods.stdout.strip().split()
            if pod_names:
                print(f"{DIM}Deleting {len(pod_names)} sandbox pod(s) in {k8s_namespace}...{RESET}")
                subprocess.run(
                    ["kubectl", "delete", "pod", "-n", k8s_namespace,
                     "--grace-period=5"] + pod_names,
                    capture_output=True, timeout=30,
                )
                print(f"{DIM}Pods deleted.{RESET}")
        except Exception as e:
            print(f"{DIM}Kubernetes cleanup: {e}{RESET}")
    elif provider == "ark-sandbox":
        try:
            ark_config = client._hand_ark_config()
            base_url = ark_config.get("baseUrl", "").rstrip("/")
            ark_ns = ark_config.get("namespace", "hand-sandboxes")
            if base_url:
                list_url = f"{base_url}/sandboxes?namespace={urllib.parse.quote(ark_ns, safe='')}"
                headers = {"Content-Type": "application/json"}
                if ark_config.get("apiToken"):
                    headers["Authorization"] = f"Bearer {ark_config['apiToken']}"
                req = urllib.request.Request(list_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                sandboxes = data if isinstance(data, list) else data.get("sandboxes", [])
                if sandboxes:
                    print(f"{DIM}Deleting {len(sandboxes)} ARK sandbox(es)...{RESET}")
                    for sb in sandboxes:
                        sb_id = sb.get("sandbox_id") or sb.get("name") or sb.get("sandbox_name", "")
                        if sb_id:
                            try:
                                del_url = f"{base_url}/sandboxes/{sb_id}?namespace={urllib.parse.quote(ark_ns, safe='')}"
                                del_req = urllib.request.Request(del_url, headers=headers, method="DELETE")
                                urllib.request.urlopen(del_req, timeout=10)
                            except Exception:
                                pass
                    print(f"{DIM}ARK sandboxes deleted.{RESET}")
        except Exception as e:
            print(f"{DIM}ARK cleanup: {e}{RESET}")

    client.stop()


if __name__ == "__main__":
    main()
