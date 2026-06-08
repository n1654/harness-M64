# Harness M64

Self-driving LLM agent runtime. Single Docker container, three TCP interfaces.

## Status

Design phase. See [docs/architecture/](docs/architecture/) for C4 diagrams.

## Design principles

- **Observability & scaling** — every tool call and LLM round is an event; metrics are first-class.
- **From simple to complex** — start with single-process, file-backed; add concurrency, persistent stores, distributed bus when actually needed.
- **Loose coupling, high cohesion** — LLM adapter, memory store, tool registry all behind small interfaces; each unit owns its data and exposes only verbs.

## Three TCP interfaces (single container)

| Port | Surface | Protocol | Purpose |
|------|---------|----------|---------|
| 8080 | UI | HTTP + SSE | HTMX-based web chat. Submit prompts, stream agent response, view recent episodes. |
| 9090 | Monitoring | HTTP | `/metrics` (Prometheus exporter), `/process-map` (Mermaid graph rendered client-side) |
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
