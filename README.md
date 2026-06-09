# Harness M64

Self-driving LLM agent runtime. Single Docker container, three TCP interfaces.

## Status

Design phase. See [docs/architecture/](docs/architecture/) for C4 diagrams.

## Design principles

- **Model decides, code reacts** — there is no planner in code. The LLM is both planner and executor: at each round it sees the full chat history and chooses one next step (call a tool, or reply to the user). The agent loop just dispatches the decision and feeds the result back; the chain terminates the moment the model responds without `function_call`. Code holds only safety nets — `HARNESS_MAX_ROUNDS`, per-tool error containment, sandboxed filesystem access. Wire walkthrough: [docs/architecture/multi-tool-chain.puml](docs/architecture/multi-tool-chain.puml).
- **Observability & scaling** — every tool call and LLM round is an event; metrics are first-class.
- **From simple to complex** — start with single-process, file-backed; add concurrency, persistent stores, distributed bus when actually needed.
- **Loose coupling, high cohesion** — LLM adapter, memory store, tool registry all behind small interfaces; each unit owns its data and exposes only verbs.

## Three TCP interfaces (single container)

| Port | Surface | Protocol | Purpose |
|------|---------|----------|---------|
| 8080 | UI | HTTP + SSE | HTMX-based web chat. Submit prompts, stream agent response, view recent episodes. |
| 9090 | Monitoring | HTTP | `/metrics` (Prometheus exporter), `/process-map` (live dashboard: counters, FSM state, task history) |
| 7000 | Control | line-based TCP | Interactive Cisco/Juniper-style shell: `show status / tools / sessions`, `restart`, `stop`, `emergency stop`, `?` |

All three surfaces are thin clients of the same in-process **Core API**. Splitting by port (not URL prefix) lets the operator firewall each independently. The Core API is the only contract between clients and the agent — agent runs even with all three surfaces disabled.

```
operator                                    ops scraper
   │                                            │
   ▼ HTMX/SSE :8080                             ▼ HTTP :9090
+--------+   +-----------+   +---------+   +---------+
|  UI    |   |  Control  |   |Monitor  |   |harness- |
| server |   |  shell    |   |server   |   |cli (ext)|
+---┬----+   +-----┬-----+   +----┬----+   +----┬----+
    │              │ TCP :7000     │             │
    └──────────────┴───────────────┘             │
                   │  Core API                   │
                   ▼                             │
            +-------------+  ◄──────TCP──────────┘
            |  Agent loop |
            +-------------+
```

## Core components

- **Agent core** (no port) — main loop: read memory layers → assemble prompt → call LLM → execute tool calls → emit events.
- **LLM adapter** — GigaChat client (OAuth token cache + chat completions). Hides behind a small `LLMClient` interface; swappable for any OpenAI-compatible endpoint.
- **Memory store** — hierarchical file layers (see below).
- **Tool registry** — file-based plugins; one tool per file in `tools/`, discovered by a `get_tools()` convention.
- **Event bus** — in-process pub/sub. Tool start/end, LLM usage, errors. Consumed by monitoring server.
- **Runtime state** — sessions, queue, metric snapshots. Starts as JSON files; upgrade to SQLite when monitoring queries get non-trivial.

## Memory layers (hierarchical)

| Layer | File / dir | Mutability | Purpose |
|-------|------------|------------|---------|
| `level_0` | `memory/level_0.md` | operator-only | Highest-priority instructions; loaded first into every prompt |
| `level_1` | `memory/level_1.md` | operator-only | Next-priority instructions |
| `level_N` | `memory/level_N.md` | operator-only | Add as many levels as needed; loaded in numeric order |
| Scratchpad | `memory/scratchpad.md` | agent-writable | Working memory between rounds |
| Knowledge | `memory/knowledge/*.md` | agent-writable | Long-term facts, one topic per file |
| Episodes | `memory/episodes/*.jsonl` | append-only | Chat / round history |

The level number IS the priority — lower = higher priority. To add a layer, drop another `level_N.md` file; the prompt builder picks it up automatically.

These are **just files**. No special framework treats them as sacred — the agent loop concatenates `level_*.md` in ascending order to build the prompt. The agent may write only to scratchpad, knowledge, and episodes — never to `level_*.md`.

## LLM adapter — GigaChat

Two authentication modes selected by `GIGACHAT_AUTH_MODE`:

| Mode | What carries the credential | Required env |
|------|------------------------------|--------------|
| `credentials` (default) | `Authorization: Basic <auth_key>` on the token request | `GIGACHAT_AUTH_KEY` (base64 of `client_id:client_secret`), `GIGACHAT_SCOPE` |
| `mtls` | The TLS client certificate itself — Sberbank validates the chain at handshake; no `Authorization` header | `GIGACHAT_CLIENT_CERT`, `GIGACHAT_CLIENT_KEY`, optional `GIGACHAT_CLIENT_KEY_PASSWORD` |

Either mode obtains an access token; the token is cached in memory and refreshed 60 s before expiry. All chat requests carry `Authorization: Bearer <token>` regardless of mode.

TLS verification (orthogonal to auth mode):

| Env | Effect |
|-----|--------|
| `GIGACHAT_TLS_INSECURE=1` | Skip cert verification entirely. Dev/testing only — logs a warning. |
| `GIGACHAT_CA_BUNDLE=/path/ca.pem` | Pin a custom CA bundle (typical: Russian Trust CA chain). |
| *(unset)* | Use the system trust store (Debian/Ubuntu `ca-certificates`). |

For mTLS, mount your certs into the container — uncomment the `./certs:/app/certs:ro` volume in `docker-compose.yml` and point the env vars there.

Other request-level options:

| Env | Default | Effect |
|---|---|---|
| `GIGACHAT_PROFANITY_CHECK` | `true` | Server-side profanity filter (sent on every chat request). Set to `false` to disable. |

Wire-format quirks of GigaChat function-calling (vs OpenAI tools) are documented separately in [docs/gigachat-functions.md](docs/gigachat-functions.md).

## Runtime behavior

**Chat thread (multi-turn memory).** Every user prompt and final assistant reply is appended to `state/chat.jsonl` and to the in-memory thread. On start, the file is loaded back, so the model keeps full conversational context across container restarts. Tool calls happening *inside* one round are NOT logged here — only the user-visible turns. The UI fetches `/api/chat` on load and renders the full thread.

**Persistent task queue.** Each in-flight prompt is mirrored as `state/queue/<session_id>.json` with a snapshot of the prior chat history. If the container dies mid-LLM-call, the next start scans the queue and re-runs the unfinished prompts sequentially in the background. The user message stays in the chat thread; the assistant reply lands as soon as the restored task finishes. Failed/cancelled sessions drop their queue entry to avoid restart-crash loops (one chance only).

**Factory reset** — via the [control shell](#three-tcp-interfaces-single-container) on port 7000:

```
harness> reset chat         # wipe only the chat thread (state/chat.jsonl)
harness> reset metrics      # wipe monitor counters + task history (UI + Prometheus)
harness> reset all          # wipe chat + metrics + scratchpad + knowledge + episodes + state
harness> show queue         # peek at pending in-flight prompts
```

`reset all` never touches `memory/level_*.md` or operator-supplied `tools/` — only agent-writable areas.

**Process map dashboard** at `http://127.0.0.1:9090/process-map`:
- **Counters** — `prompts`, `rounds ok/fail`, `llm calls`, `tool calls`, tokens in/out, last LLM latency. Reset only via `reset metrics` / `reset all`.
- **FSM strip** — `idle → prompted → llm ↔ tool → reply`, current state highlighted in orange; under it, the active tool name / model when relevant.
- **Task history** — per-prompt cards with status (running/done/failed), duration, total tokens. Each card shows the step chain: `user → llm → now → llm → reply` (graphical pills for ≤ 10 steps, text list above that). Updates are scroll-safe — only small DOM leaves change.

## Built-in tools

The agent ships with 9 tools, auto-discovered at startup. Drop a `.py` file into `tools/` to add more (see [tools/README.md](tools/README.md)).

| Tool | Scope | What it does |
|---|---|---|
| `echo` | — | returns its input verbatim (smoke target) |
| `now` | — | current UTC time in `iso` / `epoch` / `human` formats |
| `knowledge_write` | `memory/knowledge/<topic>.md` | save a long-term fact under a topic slug |
| `knowledge_read` | `memory/knowledge/<topic>.md` | read it back |
| `knowledge_list` | `memory/knowledge/` | enumerate known topics |
| `read_url` | open web | HTTP GET, strips HTML by default, capped at 20K chars (hard cap 100K) |
| `file_write` | `state/sandbox/` | write a UTF-8 file inside the agent's sandbox (max 200KB) |
| `file_read`  | `state/sandbox/` | read a file from the sandbox |
| `file_list`  | `state/sandbox/` | recursively list files in the sandbox |

`file_*` paths are relative; absolute paths and `..`-traversals are refused. `knowledge_*` topic slugs must match `[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}` (no slashes, no spaces).

### Try it

After `docker compose up -d`, open `http://127.0.0.1:8080/` and feed the agent these prompts. The expected effect is described in the «Check» column.

| Prompt | Expected agent action | Check |
|---|---|---|
| «Меня зовут <имя>, запомни это» | calls `knowledge_write(topic="user", content="...")` | `cat ~/harness-M64/memory/knowledge/user.md` |
| «Как меня зовут?» (новый сеанс / после reset chat) | calls `knowledge_list` → `knowledge_read(topic="user")`, отвечает имя | `docker compose logs harness \| grep -E 'tool_call_(start\|end)'` |
| «Открой example.com и кратко расскажи, что там» | calls `read_url(url="https://example.com/")` | в логах `tool_call_start name=read_url`; узел `read_url` оранжевый на `/process-map` |
| «Сохрани в файл `notes.md` план статьи о ...» | calls `file_write(path="notes.md", content="...")` | `ls ~/harness-M64/state/sandbox/`, `cat ~/harness-M64/state/sandbox/notes.md` |
| «Какое сейчас время по Москве?» | calls `now(format="iso")`, конвертирует ответ | в логах `tool_call_end name=now ok=True latency_sec=0.001` |

### Where to look when something goes wrong

- **Логи**: `docker compose logs -f harness` — видны все события шины (`prompt_submitted → agent_round_start → tool_call_* → llm_call_* → agent_round_end`).
- **Метрики**: `curl -s http://127.0.0.1:9090/metrics | grep harness_tool_calls_total` — счётчики по каждому tool×outcome.
- **Process map**: `http://127.0.0.1:9090/process-map` — карточки и Mermaid с активными узлами в реальном времени.
- **Чат**: `cat ~/harness-M64/state/chat.jsonl` — полная история user/assistant turns.
- **Эпизоды**: `~/harness-M64/memory/episodes/<YYYY-MM-DD>.jsonl` — детальные записи каждого round'а с latency и usage.
- **Очередь**: `nc localhost 7000` → `show queue` — pending tasks (если что-то зависло).

### Hard limits

| Что | Лимит | Где меняется |
|---|---|---|
| `file_read` / `file_write` | 200 KB | `_MAX_READ_BYTES` / `_MAX_WRITE_BYTES` в [files.py](src/harness/tools/files.py) |
| `file_list` | 500 записей | `_MAX_LIST_ITEMS` там же |
| `read_url` default | 20 000 chars | параметр `max_chars` запроса |
| `read_url` hard cap | 100 000 chars | `_HARD_MAX_CHARS` в [web.py](src/harness/tools/web.py) |
| `read_url` timeout | 20 сек | `_TIMEOUT_SEC` |
| Knowledge topic slug | до 64 символов, `[a-zA-Z0-9_-]` | regex в [knowledge.py](src/harness/tools/knowledge.py) |

### Multi-tool chains

The agent can call tools **sequentially** in one prompt — e.g. «создай notes.txt и запиши туда текущее время» triggers `now` → `file_write` → final reply. The chain length is capped by `HARNESS_MAX_ROUNDS` (default 8).

Mechanism: `functions[]` is re-sent on **every** round, not just the first. Without it the model has no tool schemas in scope after the first result and can only emit text. Full wire-format walkthrough in [docs/architecture/multi-tool-chain.puml](docs/architecture/multi-tool-chain.puml); per-round invariants in [docs/gigachat-functions.md](docs/gigachat-functions.md).

## Roadmap

1. **Architecture** ← we are here. C4 diagrams in [docs/architecture/](docs/architecture/).
2. Skeleton: directory layout, ports, three empty HTTP servers, Dockerfile + compose.
3. LLM adapter: GigaChat OAuth + chat completions behind `LLMClient` interface.
4. Memory store: interface + file-based implementation.
5. Tool registry + 1–2 example tools (echo, datetime).
6. Agent loop: single round → multi-round with tool calls.
7. UI server: minimal web for chat (HTMX or vanilla), CLI shim.
8. Monitoring server: Prometheus exporter + process map page.
9. Control server: lifecycle endpoints.
10. End-to-end run inside Docker.

## Out of scope

- Telegram integration.
- Git automation / code review on agent commits — overhead.
- Self-evolution / `BIBLE.md`-style philosophical machinery. Constitution is just a memory file with rules.
