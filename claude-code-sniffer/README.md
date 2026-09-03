# Claude Code API Sniffer

A transparent HTTP proxy that intercepts and logs all API calls between Claude Code and Anthropic's servers.

Captures data not available in JSONL transcripts: raw system prompts, full request/response bodies, HTTP headers, latency timing, and the hidden compaction summary API call.

## How It Works

```
Claude Code  ──plain HTTP──▶  Sniffer (localhost:7735)  ──HTTPS──▶  api.anthropic.com
                                    │
                                    ▼
                          ~/.claude/api-sniffer/*.jsonl
```

Uses `ANTHROPIC_BASE_URL` (officially supported by Claude Code) to redirect API traffic through a local proxy. The proxy receives plain HTTP, logs everything, then forwards to Anthropic over HTTPS. No TLS interception or certificates needed.

## Usage

```bash
# Terminal 1: Start the sniffer
claudetui sniffer

# Terminal 2: Launch Claude Code through the sniffer
claudetui sniff
claudetui sniff --resume abc123
```

`claudetui sniff` auto-detects the sniffer port and launches Claude Code through the proxy. If the sniffer isn't running, it falls back to launching Claude Code directly.

Multiple sniffers can run on different ports:

```bash
claudetui sniffer --port 7735    # project 1
claudetui sniffer --port 7736    # project 2
claudetui sniff --port 7736      # connect to specific sniffer
```

You can also set the env var manually:

```bash
ANTHROPIC_BASE_URL=http://localhost:7735 claude
```

Or add a shell function to `~/.zshrc` (or `~/.bashrc`) that auto-detects the sniffer:

```bash
claude-sniff() {
    if nc -z localhost 7735 2>/dev/null; then
        ANTHROPIC_BASE_URL=http://localhost:7735 claude "$@"
    else
        echo "⚠ Sniffer not running — starting claude without proxy"
        claude "$@"
    fi
}
```

All API calls flow through the proxy and are logged to `~/.claude/api-sniffer/`.

## Custom upstream

By default the sniffer forwards to `https://api.anthropic.com`. If you already route Claude Code through a gateway or proxy of your own, point the sniffer at it instead:

```bash
claudetui sniffer --upstream http://localhost:4000
claudetui sniffer --upstream https://gateway.corp/anthropic
```

A base path is honoured — `https://gateway.corp/anthropic` sends `/v1/messages` to `/anthropic/v1/messages`, the same path Claude Code would have used with that base URL. Plain-HTTP upstreams work too.

If `--upstream` is not given, the sniffer falls back to `CLAUDETUI_UPSTREAM`, then to whatever `ANTHROPIC_BASE_URL` is already exported in its shell — so the usual case needs no flags:

```bash
export ANTHROPIC_BASE_URL=https://gateway.corp/anthropic
claudetui sniffer     # forwards to the gateway
claudetui sniff       # launches claude through the sniffer
```

An inherited `ANTHROPIC_BASE_URL` that points at the sniffer's own port is ignored rather than looped back.

Only the scheme, host, port and base path are used. A URL carrying a query string or inline `user:password@` credentials is rejected rather than quietly truncated.

Two env vars cover gateways that need more:

| Variable | Effect |
|---|---|
| `CLAUDETUI_UPSTREAM_TOKEN` | Sends `Authorization: Bearer <token>` upstream, unless the client already sent its own `Authorization` header |
| `CLAUDETUI_UPSTREAM_INSECURE=1` | Skips upstream TLS verification (same as `--insecure`), for self-signed certificates |

Both apply **only to a custom upstream**. If the sniffer resolves to `https://api.anthropic.com`, they are ignored with a note — so a variable left exported from a gateway session can never disable TLS verification against Anthropic or forward an internal token there. `--insecure` without a custom `--upstream` is an error.

The token is read from the environment rather than a flag so it stays out of shell history and `ps` output. It is redacted from logs like any other credential.

Note that cost figures depend on the model IDs in the response. A gateway that renames or remaps models may report `$0.000` even though tokens and latency are still tracked correctly.

## Options

```
--port PORT     Proxy port (default: 7735)
--upstream URL  Upstream API base URL (default: https://api.anthropic.com)
--insecure      Skip upstream TLS verification (self-signed gateways)
--full          Log complete request/response bodies (warning: large files)
--no-redact     Include API keys in logs (dangerous)
--quiet         Suppress terminal output, log only
```

## Terminal Output

```
  ClaudeTUI API Sniffer — listening on http://127.0.0.1:7735

  Use:  ANTHROPIC_BASE_URL=http://localhost:7735 claude
  Log:  ~/.claude/api-sniffer/sniffer-20260314-103000.jsonl

  #1   POST /v1/messages  opus-4-6  45.2k->1.5k  $0.120  2312ms  740KB/4.2KB  98%c  [Tt]
  #2   POST /v1/messages  opus-4-6  48.1k->0.8k  $0.094  1134ms  741KB/2.1KB  99%c  [TU]  Edit
  #3   POST /v1/messages  opus-4-6  50.3k->52    $0.081  1823ms  742KB/0.3KB  100%c  [U]  Glob,Grep
  #4   POST /v1/messages  opus-4-6  45.2k->1.5k  $2.460  4254ms  740KB/4.2KB  0%c   [Tt]
  #5   POST /v1/messages  opus-4-6  12.3k->2.1k  $0.041  3412ms  42KB/6.8KB   95%c  [Tt]  compaction
  #6   POST /v1/messages  sonnet-4-6  14.3k->2.1k  $0.008  2341ms  42KB/6.8KB  [Tt]  +agent.1
  #7   POST /v1/messages  sonnet-4-6  16.5k->1.2k  $0.006  1823ms  48KB/3.2KB  [TU]  Read  agent.1

  Summary: 7 requests | $2.810 | 232k in | 9.1k out | 3.3MB sent | 32KB recv | 1 sub-agent
```

Each line: `#id  method path  model  input->output  $cost  latency  sent/recv  cache  [blocks]  stop  [agent]  [flags]`

- **input->output** — total input tokens (including cache read/write) and output tokens per request
- **$cost** — estimated cost at API rates, calculated locally from token usage (not sent by Anthropic). Useful for understanding relative cost per request even on a Claude Pro/Max subscription
- **sent/recv** — raw HTTP traffic size (request body / response body) in bytes
- **cache** — cache hit ratio: `100%c` = fully cached (cheapest), `0%c` in red = cache miss (12.5x more expensive). Cache expires after ~5 min idle
- **[blocks]** — content block types:
  - `T` = thinking, `t` = text, `U` = tool_use
  - `S` = server_tool_use, `W` = web_search_tool_result
  - `M` = mcp_tool_use, `m` = mcp_tool_result
- **stop** — tool names when Claude calls tools (e.g. `Read`, `Edit,Write`), `max_tokens` (red) when response was truncated
- **agent** — `+agent.1` = new sub-agent spawned, `agent.1` = known sub-agent request
- **compaction** — flagged when message history shrinks dramatically between requests (per-session, same model only)

## What It Captures

| Data | In Transcript? | In Sniffer? |
|------|:-:|:-:|
| Token usage (input/output/cache) | Yes | Yes |
| Raw system prompt | No | Yes |
| Full conversation history per request | No | Yes (with `--full`) |
| Request parameters (max_tokens, temperature) | No | Yes |
| HTTP headers (anthropic-beta, version) | No | Yes |
| Request/response latency | No | Yes |
| Hidden compaction API call | No | Yes |
| Sub-agent API calls | No | Yes |
| Error response bodies | Partial | Yes |
| Streaming SSE events | No | Yes |
| Tool definitions (full schema) | No | Yes (with `--full`) |

## Log Format

JSONL file with request/response pairs:

```json
{"type":"request","id":1,"timestamp":"2026-03-14T10:30:00.123Z","method":"POST","path":"/v1/messages","headers":{"anthropic-version":"2023-06-01"},"body":{"model":"claude-opus-4-6-20260301","max_tokens":16384,"stream":true,"system_length":14328,"message_count":42,"body_length":145000,"tool_count":12}}
{"type":"response","id":1,"timestamp":"2026-03-14T10:30:02.456Z","status":200,"latency_ms":2333,"streaming":true,"model":"claude-opus-4-6-20260301","stop_reason":"end_turn","usage":{"input_tokens":45000,"output_tokens":1500,"cache_read_input_tokens":40000},"content_blocks":["thinking","text","tool_use"],"tool_names":["Read"],"event_count":127}
```

## Sub-Agent Detection

Claude Code uses the **same session ID** for sub-agents and the main session. The sniffer detects sub-agents by checking the request's tool list: the main session always has the `Agent` tool, sub-agents never do. Sub-agents are grouped by model + system prompt length for labeling (`agent.1`, `agent.2`, etc.).

## Compaction Detection

The sniffer detects compaction by comparing consecutive **main-session** requests (sub-agents are ignored). When the message history shrinks dramatically (>50% reduction in message count or >70% reduction in body size), it flags the request:

```
  #12  POST /v1/messages  opus-4-6  12.3k->2.1k  $0.041  3412ms  compaction
```

Tracking is per session ID, so a new Claude Code session connecting to a running sniffer won't trigger a false compaction.

## Security

- **Localhost only** — binds to `127.0.0.1`, never `0.0.0.0`
- **API keys redacted** — `x-api-key` and `authorization` headers stripped from logs by default
- **Restricted permissions** — log files created with `0o600` (owner read/write only)
- **Local plaintext** — API key transits in plain text only over the loopback interface

## Requirements

- Python 3.13+, stdlib only — no external dependencies
- Claude Code with `ANTHROPIC_BASE_URL` support
