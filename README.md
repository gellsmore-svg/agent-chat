# Agent Chat v1.1

Agent Chat runs a structured technical conversation between exactly two CLI agents on one machine. Both agents invoke one installed Python package. Each agent runs that package as its own worker process. The workers share a local folder and maintain one readable `conversation.md`. The skill tells an interactive agent how to launch the installed runner. The runner handles registration, polling, backend calls, deadlines, commits and recovery.

## Requirements

- Python 3.11 or newer. Install this repository once with pip. Both participants invoke that installation's `agent-chat` command.
- Any two participants using Codex CLI, official Grok Build, or Claude Code. Both participants may use the same backend.
- A local filesystem with OS file locking and atomic rename. Run both workers under the same OS runtime and user account. For WSL, use two WSL workers and Linux backends. For PowerShell, use two native Windows workers and Windows backends. Cloud-synced folders and network shares are outside the protocol's guarantees.
- Authenticate using each CLI's normal login flow. Provider account limits and billing apply.

## Install Agent Chat

The private [`gellsmore-svg/agent-chat` repository](https://github.com/gellsmore-svg/agent-chat) is the only implementation of the skill, the Python library, the CLI, the backend adapters, the protocol, the installer and the tests. Clone it with a GitHub account that has access, then install one shared copy of the package. Put that installation's command directory on the `PATH` of both participants, or give both agents the full path to the same executable. Keep each conversation directory outside this repository. A later agent joins or runs a session with the installed command.

**Unix and WSL**

```sh
git clone https://github.com/gellsmore-svg/agent-chat.git
python3 -m venv "$HOME/.local/share/agent-chat/venv"
"$HOME/.local/share/agent-chat/venv/bin/python" -m pip install ./agent-chat
export PATH="$HOME/.local/share/agent-chat/venv/bin:$PATH"
agent-chat install
agent-chat --version
```

**Windows PowerShell**

```powershell
git clone https://github.com/gellsmore-svg/agent-chat.git
$agentChatEnv = Join-Path $env:LOCALAPPDATA "AgentChat\venv"
py -3 -m venv $agentChatEnv
& "$agentChatEnv\Scripts\python.exe" -m pip install .\agent-chat
$env:Path = "$agentChatEnv\Scripts;$env:Path"
agent-chat install
agent-chat --version
```

Run the `PATH` step in each terminal or add the command directory to your shell profile. Use `agent-chat doctor` to verify CLI availability and discovery paths. `python -m agent_chat` in the same environment is equivalent to `agent-chat`.

The installer links the packaged skill directory and falls back to copying its single `SKILL.md` if symlinks are unavailable. Either form invokes the same installed Python package. Use `agent-chat install --root PATH` for project-level discovery, repeat `--backend` to select backends, or pass `--copy` to force a Markdown copy. Re-run installation after upgrades. If a different copied skill already occupies the destination, move it aside first. Linked installs follow the asset in the package installation.

| Backend | Default user root | Project root | Invocation |
| --- | --- | --- | --- |
| Codex CLI | `~/.agents/skills/` | `.agents/skills/` | `$agent-chat` |
| Grok Build | `~/.grok/skills/` | `.grok/skills/` | `/agent-chat` |
| Claude Code | `~/.claude/skills/` | `.claude/skills/` | `/agent-chat` |

The installer honors `GROK_HOME` and `CLAUDE_CONFIG_DIR` for user installs. Open a new agent session after installation if discovery has already run. You can ask the agent, “Use agent-chat in this folder. My participant identity is Eve. Join the existing conversation and run until completion.” If an interactive permission policy prevents a long-running shell process, start the worker directly in a terminal.

Discovery references: [Codex skills](https://learn.chatgpt.com/docs/build-skills), [Grok Build skills](https://docs.x.ai/build/features/skills-plugins-marketplaces), [Claude Code skills](https://code.claude.com/docs/en/skills).

## Two-terminal quick start

Run these examples from a working directory outside the source checkout. Keep conversation folders separate from the implementation and use a fresh path per session. Use the same working directory in both terminals, or pass absolute paths. Initialize once.

### Grok and Codex on Unix or WSL

```sh
agent-chat init \
  --dir ./agent-chats/scl-review --a Eve:grok --b Reviewer:codex --first Eve \
  --title "SCL architecture review" \
  --objective "Identify weak assumptions in the proposed governance design and recommend improvements."
```

Terminal 1

```sh
agent-chat run --dir ./agent-chats/scl-review --agent Eve
```

Terminal 2

```sh
agent-chat run --dir ./agent-chats/scl-review --agent Reviewer
```

Read `agent-chats/scl-review/conversation.md` while the workers run or after they finish. The transcript gives every contribution a speaker and timestamp, shows their recorded decisions, challenges and actions, and ends with the reason the session stopped. Mutual completion refers to the proposal and acceptance in the corresponding turns.

### Claude and Codex on Windows

```powershell
agent-chat init --dir ".\agent-chats\design review" --a Analyst:claude --b Reviewer:codex --objective "Compare approval policies and propose a decision."
```

Terminal 1

```powershell
agent-chat run --dir ".\agent-chats\design review" --agent Analyst
```

Terminal 2

```powershell
agent-chat run --dir ".\agent-chats\design review" --agent Reviewer
Get-Content ".\agent-chats\design review\conversation.md" -Encoding utf8
```

### Claude and Grok with a design document

```sh
agent-chat init \
  --dir ./agent-chats/design --a Analyst:claude --b Eve:grok \
  --objective-file ./objective.md --project ./spec --include "design notes.md"
```

Start `agent-chat run --dir ./agent-chats/design --agent Analyst` and `agent-chat run --dir ./agent-chats/design --agent Eve` in separate terminals. Repeat `--include` to select additional review inputs. The runner snapshots selected UTF-8 text in `session.json` at initialization. Share only content that both model providers may receive.

For a long file, use `--range "design notes.md=80-110"` with `--project`. The range includes both endpoints, counts from line 1, and retains the original path and line numbers for citation. Repeat `--range` to select multiple excerpts. Choose either whole-file input or ranges for each path. A range can skip up to 1 MiB of preceding bytes.

Each whole file or excerpt has a 24,000-byte limit. A snapshot can contain up to 30 selections and 48,000 bytes. `--context-chars` sets the separate prompt budget, up to 100,000 characters.

## Session commands

Use `agent-chat COMMAND` on both Unix and PowerShell.

| Command | Behavior |
| --- | --- |
| `init --dir DIR --a NAME:BACKEND --b NAME:BACKEND --objective TEXT` | Create a two-party configuration. Use `--first`, `--title`, or `--objective-file` to customize it. Repeating `init` recognizes a valid existing session and preserves its settings. |
| `run --dir DIR --agent NAME` | Register one worker, wait for the peer, and stay active until a terminal outcome. Matching is case-sensitive. |
| `status --dir DIR` | Show registration, next speaker, remaining time, last message and outcome. |
| `stop --dir DIR` | Publish cancellation. Workers observe it while waiting or generating. |
| `recover --dir DIR` | Validate state, reconcile a committed turn interrupted mid-publication and rebuild the Markdown. |
| `export --dir DIR [--summary] [--output FILE]` | Write a fresh view to stdout or a new file outside the session. The optional summary lists speaker-attributed decisions, actions and questions, including earlier questions that may later have been resolved. |
| `doctor [--dir DIR] [--backend NAME] [--smoke]` | Check Python, local filesystem, backend identity, required flags, known sign-in indicators and skill locations. `--smoke` makes a live call and may consume provider usage. |
| `install [--root PATH] [--backend NAME] [--copy]` | Register the packaged skill in discovery directories. |

By default, the first worker waits 120 seconds for the second. The active 10-minute clock starts at the second registration. The default cap is 12 contributions. Tune `--join-timeout`, `--minutes`, `--max-turns`, `--poll` (1 second), `--timeout` (120 seconds per call), and `--worker-grace` (15 seconds to replace an idle crashed worker). Except for `--minutes`, time settings use seconds. A call is limited by both its configured timeout and the remaining active session time.

The runner ends with mutual completion, active deadline expiry, maximum turns, cancellation, or failure. A backend error or per-invocation timeout fails the session. Invalid or exact normalized repeated contributions receive one retry. The retry prompt and diagnostics identify the failure without including the rejected response. A failed worker exits with code 1. A normal terminal outcome returns 0. A directly interrupted worker returns 130.

## Backend configuration

`doctor` checks capabilities rather than assuming a fixed minimum release number. Use official versions with the flags it reports. Add each CLI to `PATH`, pass `run --executable PATH`, or set `AGENT_CHAT_CODEX_BIN`, `AGENT_CHAT_GROK_BIN` or `AGENT_CHAT_CLAUDE_BIN`. On Windows, use a native executable such as `codex.exe`, `grok.exe` or `claude.exe` in place of shell-script shims. The optional `run --model MODEL` selects the model for that worker. The runner launches each turn in a fresh temporary working directory and supplies all session context explicitly.

| Backend | Headless integration |
| --- | --- |
| Codex | `codex exec` in read-only mode, ephemeral sessions, approval set to never, disabled shell and external tools, JSONL final message and output schema. User config is ignored. CLI authentication remains available. [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) |
| Grok | Official Grok Build's single-prompt file mode, equivalent to `-p` for prompts read from a file. JSON envelope, read-only sandbox, tool filtering, and deny rules that cover tool calls including MCP. Uses a schema when the CLI exposes `--json-schema`. [Grok headless scripting](https://docs.x.ai/build/cli/headless-scripting) |
| Claude | `claude --print` with JSON schema, disabled built-in tools, strict empty MCP config, denied MCP calls, non-interactive permissions and session persistence disabled. [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) |

CLI output is transient. The runner records numeric usage and provider-reported cost where available. Reported costs are informational and may differ from billing totals. Missing cost information indicates that the CLI did not expose it.

## Permissions and context

Discussion is the only operating mode. The model receives bounded text from the objective, original turns, the runner's derived memory, and project files explicitly selected by the operator. It receives no directory or command authority. The project and session folder are outside the backend working directory. Protocol and input paths reject parent traversal and links or reparse points. Trust the local executable, CLI settings and managed policy. Provider-specific managed settings, Grok's native user hooks and backend-local logs are controlled by the CLI and its administrator.

Treat model and project text as untrusted when reading the transcript. The runner interprets only the fixed response fields and intent enum. It neither executes contribution text nor uses it as a path or shell argument. The runner suppresses raw subprocess output in diagnostics and rejects common credential forms and matching environment secrets. Credential detection is heuristic. Exclude sensitive files and secrets before initialization.

The conversation context retains the objective, the newest original exchange and historical speaker-attributed notes. When older full turns no longer fit, the prompt identifies how many were omitted and labels the extracted notes as derived memory. If even those notes and the latest exchange exceed the budget, the session fails with a context error and retains its history. Use `--context-chars` and `--message-chars` to tune the bounds.

## Files, recovery and troubleshooting

The package version and protocol version are separate. `agent-chat --version` prints both. Every worker checks the session's exact protocol version and machine binding before probing the backend or registering. Existing protocol-v1 sessions created before machine binding remain readable and their stored worker hostnames still apply. If you see a protocol mismatch, use a compatible package installation for both participants or create a new session. Keep the original session so its history can be read by a compatible release.

```text
conversation-folder/
  session.json       immutable identities, objective, limits, selected source snapshots
  control.json       checked lifecycle state, registrations and in-flight turn
  turns/             checksummed canonical JSON records, one per turn
  conversation.md    atomically refreshed transcript
  leases/            worker locks and heartbeat records
  diagnostics/       classified backend errors and execution metadata
  write.lock         permanent OS lockfile
  lock-owner.json    most recent commit-lock owner
```

- After an interrupted write, run `recover --dir DIR`. It recognizes a committed turn even if control or Markdown publication was interrupted. It reports corruption and preserves the damaged file. Keep the session directory for inspection.
- If a worker dies between invocations, restart that identity within its grace period. The journal determines the next turn. An abandoned in-flight invocation fails the session to prevent duplicate calls. A forced process kill can leave a CLI child behind. Its output cannot commit. Use `stop` for routine cancellation.
- A leftover lockfile or owner record is expected. The OS releases the lock when its owning process exits. Keep the lockfile in place. If a writer is still holding the lock, retry after it exits.
- If the picked identity fails to join, run `doctor`, verify the spelling and backend executable, and check authentication for the actual worker account. Grok credential detection checks presence only. A live smoke test verifies usable access.
- If a session directory contains an incomplete initialization, keep it for inspection and choose a fresh empty directory. Terminal sessions keep their results. Start a new folder for a new conversation.

Version 1 uses one host, two participants and fresh headless calls. It detects exact normalized repeats, while semantic loops remain subject to the turn and time limits. Another backend is a change to this package: capability discovery, argument building and output parsing stay beside the existing adapters and leave the journal unchanged. A future mode that can change a project would need its own explicit permission and tool policy in this package.

## Development and verification

From the checkout, use a Python virtual environment and run `python -m pip install -e .` followed by `python -m unittest discover -s tests -v`. The same commands work in PowerShell with the virtual environment's Python. The package exposes one command, `agent-chat`, and bundles the canonical [skill definition](src/agent_chat/skill/agent-chat/SKILL.md).

The fake backend suite uses two independent worker processes, tests each backend pairing, and injects interruption, malformed output, contention and other failures without provider calls. It also checks incompatible session joins and skill installation. See the [filesystem protocol](references/protocol.md) for the transaction and recovery rules.

The runner's initial live verification used WSL Codex CLI 0.172.0, Grok Build 1.0.92, and Claude Code 2.1.331. An independent two-worker Grok–Codex session reached mutual completion in two turns. Native Windows Grok Build 1.0.41 passed the capability check and required sign-in for a live call.
