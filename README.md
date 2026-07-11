<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo.png">
    <img src="assets/logo-light.png" alt="acpdbg" width="320">
  </picture>
</p>

<p align="center"><strong>Debug native crashes by handing them to your coding agent.</strong></p>

`acpdbg` is an LLDB assistant. When a C/C++/Rust/Swift program stops (crash,
signal, or breakpoint), it captures an enriched snapshot of the failure — the
backtrace, every frame's arguments, and the surrounding source — and asks an AI
to explain the root cause and propose a fix. The AI can then run *live* debugger
commands to confirm its hypothesis, just like you would.

acpdbg speaks the [**Agent Client Protocol (ACP)**](https://agentclientprotocol.com)
to whatever coding agent you already run — [GitHub Copilot CLI](https://github.com/github/copilot-cli),
the [Gemini CLI](https://github.com/google-gemini/gemini-cli),
the [Claude Code ACP adapter](https://github.com/zed-industries/claude-code-acp),
or the **bundled zero-setup mock agent** so you can try it in ten seconds.

Real output, `acpdbg --agent copilot -- ./samples/crash`:

```
acpdbg → copilot (investigating…)

• Run crash under lldb and inspect variables at breakpoint (completed)
Confirmed live: `s` (second parameter to `describe`) is 0x0000000000000000 —
a NULL pointer — right at the point where strlen(s) dereferences it, matching
the reported crash address 0x0.

Root cause: In main, `name` is set to NULL whenever the program is run with no
arguments (argc == 1). That NULL is passed into describe(..., name) and handed
to strlen(s) with no NULL-check, so strlen dereferences 0x0 → SIGSEGV.

One-line fix (guard in describe):
    return s ? strlen(s) : 0;
```

---

## Install

acpdbg is a normal Python package (Python 3.10+). The only dependency is the ACP SDK.

```bash
pip install acpdbg          # or: pipx install acpdbg
```

> **You do _not_ need to install acpdbg into LLDB's own Python.** The LLDB plugin
> is pure standard library, so it loads even in an old embedded interpreter (for
> example, Xcode's LLDB ships Python 3.9). The agent itself runs in a separate
> helper process on the Python you installed acpdbg into. `acpdbg
> --install-lldbinit` wires the two together automatically — see
> [Load it in every session](#load-it-in-every-session-lldbinit).

You also need `lldb` itself:

- **macOS**: `xcode-select --install`
- **Debian/Ubuntu**: `sudo apt install lldb`
- **Fedora**: `sudo dnf install lldb python3-lldb`

## Try it in 10 seconds (nothing else to install)

```bash
git clone https://github.com/acpdbg/acpdbg && cd acpdbg/samples
make                     # builds ./crash with -g
acpdbg -- ./crash        # runs it; on the crash the bundled mock agent explains
```

The **mock agent** is a real ACP agent that reads your crashing source over the
protocol and streams back a diagnosis. It proves the whole loop works offline.
Swap in a real agent when you want real analysis (below).

> The CLI runs your program under `lldb` and triggers the agent from a **stop
> hook** when the process faults. If your LLDB build doesn't stop on the fault
> under batch mode, use the interactive path below — it always works.

## Use a real coding agent

Point acpdbg at any ACP-compatible agent:

```bash
# GitHub Copilot CLI  (npm i -g @github/copilot, then `copilot` on PATH)   ← verified
acpdbg --agent copilot -- ./crash

# Google Gemini CLI  (npm i -g @google/gemini-cli, then `gemini` on PATH)
acpdbg --agent gemini -- ./crash

# Claude Code ACP adapter  (npm i -g @zed-industries/claude-code-acp)
acpdbg --agent claude-code -- ./crash

# Any other ACP agent: pass its launch command verbatim
acpdbg --agent "my-agent --acp" -- ./crash
```

> **GitHub Copilot CLI** (`copilot --acp`) is the reference agent used in the
> example above. Different agents bring different tools: Copilot investigates with
> its own shell and lldb, while agents that honour ACP `mcpServers` drive acpdbg's
> built-in `debugger_command` / `get_backtrace` / `get_locals` tools against the
> *already stopped* process. Either way the answer is streamed back to your terminal.

## Use it inside an interactive LLDB session

acpdbg is also a plain LLDB plugin — drive it straight from an LLDB session:

```lldb
$ lldb ./crash
(lldb) command script import acpdbg.lldb_plugin
(lldb) run
...
Process stopped: EXC_BAD_ACCESS (SIGSEGV)
(lldb) ask why did this stop and how do I fix it?
(lldb) acpdbg config agent copilot     # switch agents on the fly
```

Commands added to LLDB:

| Command | What it does |
| --- | --- |
| `ask <question>` | Investigate the stopped program and answer. |
| `why` | Shorthand for "why did this stop?" |
| `acpdbg <question>` | Same as `ask`. |
| `acpdbg config` | Show configuration. |
| `acpdbg config <key> <value>` | Change a setting (see below). |
| `copilot <question>` | Ask GitHub Copilot CLI, one-off. |
| `claude <question>` | Ask Claude Code (via the ACP adapter), one-off. |
| `gemini <question>` | Ask Gemini CLI, one-off. |

The per-agent commands are registered only for agents actually installed (their
executable resolves on `PATH` when the plugin loads). They ask that agent for a
single question without changing the configured default agent:

```lldb
(lldb) why                        # uses the configured agent
(lldb) claude and what would you change to fix it?   # one-off Claude opinion
```

### Load it in every session (`.lldbinit`)

Add the commands to your `~/.lldbinit` once so they're available in every LLDB
session automatically — including inside **Xcode**:

```bash
acpdbg --install-lldbinit          # or: acpdbg --print-lldbinit  to see the snippet
```

On load you'll see a confirmation line so you know it's active:

```
acpdbg 0.1.0 loaded — commands: ask, why, acpdbg  (agent: mock; debugger Python 3.9).
  Stop your program (crash or breakpoint), then: ask why did this stop?
```

The snippet puts acpdbg on LLDB's path and records which Python to run the agent
helper with, so it works no matter what Python your LLDB embeds. It's managed by
markers and is idempotent — **re-run `acpdbg --install-lldbinit` after upgrading**
to refresh it (it preserves the rest of your `~/.lldbinit`).

> **Xcode:** Xcode's LLDB uses an older embedded Python (3.9). That's fine — the
> plugin runs there and launches the agent out-of-process. Just make sure the
> `acpdbg --install-lldbinit` snippet is in `~/.lldbinit`, then in Xcode's
> debugger console (`(lldb)`), at any stop, type `ask …`.

#### Choosing the agent for Xcode

The agent is selected by the `ACPDBG_AGENT` environment variable (default
`mock`). Xcode is launched from the macOS GUI, so it does **not** inherit your
shell — an `export ACPDBG_AGENT=…` in `~/.zshrc` won't reach it, and its PATH
won't include `/opt/homebrew/bin`. Bake both into `~/.lldbinit` in one step:

```bash
acpdbg --install-lldbinit --agent copilot
```

This records `ACPDBG_AGENT=copilot` and adds the agent's directory to PATH so it
resolves under Xcode's minimal environment. Verify from Xcode's console with
`acpdbg config` — it should show `agent = copilot (/opt/homebrew/bin/copilot --acp)`.
You can still switch per session with `acpdbg config agent <name>`.

### Not just crashes — analyze live state at any stop

`ask` works at **any** stop: a breakpoint, a watchpoint, a signal, or a manual
`process interrupt` — not only crashes. Stop wherever you want to reason about
the program and ask about the current state. The agent sees each frame's live
argument values and can run debugger commands (`frame variable`, `p expr`, …) to
inspect anything else it needs:

```lldb
(lldb) breakpoint set --name process_request
(lldb) run
...
Process stopped at breakpoint 1.1
(lldb) ask why is `retry_count` already 3 here? which caller set it?
```

### Let the agent drive execution (debug like a human)

By default the agent can only *observe*. Turn on **control mode** and it also gets
tools to step and run the program like you would at the prompt:

```lldb
(lldb) acpdbg config control on          # or: acpdbg --control -- ./prog
(lldb) ask set a breakpoint in parse(), continue to it, then step until `n` goes negative
```

Control tools: `step_over`, `step_into`, `step_out`, `continue_execution`,
`set_breakpoint`, and `run_to_line`. After each step the agent is told where the
program stopped and the source line, so it can watch state evolve and stop once
it has found the cause. Control is **off by default** — during crash triage you
usually don't want to resume past the fault and lose it.

> These live tools reach agents that honour ACP's `mcpServers` (e.g. Gemini CLI).
> Some agents (including GitHub Copilot CLI) prefer their own built-in shell and
> debugger instead — they still debug from acpdbg's captured context, they just
> don't call these specific tools.

### Drive it from an external app (Claude Code, Copilot, Cursor…)

Normally acpdbg launches the agent for you. But you can also **expose a live lldb
session as an MCP server** and drive it from an MCP-capable app you already have
open — it connects to the debugger you're sitting in front of:

```lldb
(lldb) acpdbg config control on     # optional: include step/continue tools
(lldb) run                          # stop somewhere (crash or breakpoint)
(lldb) acpdbg serve
acpdbg: debugger exposed to external MCP clients — step/continue/breakpoint tools INCLUDED.
  bridge file: ~/.acpdbg/bridge.json

  claude mcp add acpdbg --env ACPDBG_BRIDGE_FILE=~/.acpdbg/bridge.json --env ACPDBG_CONTROL=1 -- /path/to/acpdbg-mcp
```

`acpdbg serve` keeps a bridge alive and writes its connection info to a **stable
file**, so the external app's MCP config never changes between sessions. Add the
printed `acpdbg` MCP server to the app once; from then on, whenever an lldb
session is `serve`-ing, that app can call `get_backtrace`, `get_locals`,
`debugger_command`, and (in control mode) `step_over` / `continue_execution` /
`set_breakpoint` / … against your live process. Run `acpdbg serve stop` to end.

> Keep the lldb session stopped and don't type lldb commands while an external
> app is driving it — one driver at a time. The bridge is a local, token-guarded
> socket that only exists while you're serving.

## How it works

```
   ┌──────────────────────────── your machine ───────────────────────────┐
   │                                                                      │
   │   lldb  (your stopped program)          acpdbg-session  (helper,     │
   │   └─ acpdbg plugin  ── prompt ──────────►  modern Python)            │
   │        (pure stdlib,          captures      │                        │
   │         any Python)           bt+args+src   │  ACP (JSON-RPC/stdio)  │
   │            ▲                                 ▼                        │
   │            │                          coding agent ◄─ streams ─┐     │
   │            │                          (copilot / gemini / mock)│     │
   │            │  bridge socket                  │ MCP tool calls   │     │
   │            └──── live lldb commands ◄── acpdbg-mcp ◄────────────┘     │
   │                   (bt, frame variable, p …)                          │
   └──────────────────────────────────────────────────────────────────────┘
```

1. The **LLDB plugin** (pure standard library, so it loads in any LLDB Python)
   captures a `CrashContext` and builds a prompt.
2. It launches the **`acpdbg-session` helper** on a modern Python and streams its
   output back to the console. The helper is the **ACP client**: it launches the
   agent, grants it read access to your source files, and sends the prompt.
3. To let the agent inspect *live* state, acpdbg exposes an **MCP server**
   (`acpdbg-mcp`) offering `debugger_command`, `get_backtrace`, and
   `get_locals` — plus, in control mode, `step_over` / `step_into` / `step_out` /
   `continue_execution` / `set_breakpoint` / `run_to_line`. Those calls travel
   over a private socket back into the running LLDB session. A safety filter
   blocks any read command that would resume or mutate the process (override with
   `--unsafe`); the control tools are the explicit, opt-in exception.
4. The agent's reasoning is **streamed** to your terminal.

This split is what lets acpdbg run inside debuggers whose embedded Python is
older than the ACP SDK supports (e.g. Xcode's LLDB).

## Configuration

Every option is available as a CLI flag, an environment variable, or the
`acpdbg config` command inside LLDB.

| Setting | CLI flag | Env var | Default |
| --- | --- | --- | --- |
| Agent | `--agent` | `ACPDBG_AGENT` | `mock` |
| Permission handling | `--permission auto\|prompt` | `ACPDBG_PERMISSION` | `auto` |
| Live debugger tools | `--no-mcp` to disable | `ACPDBG_MCP` | on |
| Execution control (step/continue) | `--control` | `ACPDBG_CONTROL` | off |
| Allow file writes | `--writes` | `ACPDBG_ALLOW_WRITES` | off |
| Disable safety filter | `--unsafe` | `ACPDBG_UNSAFE` | off |
| Agent turn timeout (s) | — | `ACPDBG_TIMEOUT` | `300` |
| Show agent stderr | `--agent-stderr` | `ACPDBG_AGENT_STDERR` | off |
| Debug log | — | `ACPDBG_DEBUG` | off |

## Troubleshooting (especially inside Xcode)

If a session seems to hang or output never appears, turn on the debug log:

```lldb
(lldb) acpdbg config debug on
```

Every acpdbg process — the lldb plugin, the session helper, the ACP client, the
MCP tool server, and the agent's own stderr — then appends timestamped lines to
`~/.acpdbg/acpdbg.log`. Follow it live from a terminal while you debug in Xcode:

```bash
tail -f ~/.acpdbg/acpdbg.log
```

Inside lldb (or Xcode's debugger console):

```lldb
(lldb) acpdbg log            # show the last 40 log lines
(lldb) acpdbg log 100        # …or more
(lldb) acpdbg log clear      # start fresh
(lldb) acpdbg last           # re-print the previous session's full output
```

The log shows each phase (`initialize`, `session/new`, `session/prompt`, tool
calls, streamed chunks) with timestamps, so a stall points directly at the
culprit. Two things that look like hangs but aren't:

- **Copilot CLI session startup is slow** — `session/new` can take one to two
  minutes before the first output. acpdbg prints a heartbeat note every 20 s
  (`(copilot is starting the session… 20s)`) while it waits.
- A turn that exceeds the timeout (default 300 s) is cancelled with
  `(agent timed out after 300s)` rather than hanging forever.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest
pytest                       # unit + end-to-end (uses the mock agent, offline)
acpdbg --dry-run -- ./x      # print the exact lldb command acpdbg would run
```

## Requirements

- Python 3.10–3.14
- `lldb` on `PATH`
- Programs compiled with debug info (`-g`) for useful results

## License

MIT — see [LICENSE](LICENSE).
