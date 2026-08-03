<!-- ai-hub:codex-routing:start -->

## Codex Delegation

For complex requests, always choose the local `delegate-to-codex` skill before
Hermes's own tools or any `claude-code` skill. This priority applies to code
changes, debugging and repair, multi-file or batch work, Git tasks, Obsidian
restructuring, system integration, and other multi-step work that requires tools
and verification. Use `claude-code` only when the user explicitly asks for
Claude or when Codex fails and the user accepts that fallback.

The delegation skill must call the native `codex_delegate` tool. Hermes must
never use `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`, or
another agent as a maintenance fallback. If Codex is unavailable or out of
quota, report the blocker and leave source unchanged.

Handle ordinary conversation, short explanations, simple status checks, and the
existing capture commands directly. Keep `/clip`, `/idea`, `/readlater`, `/auto`,
`/log`, `/kol`, `/event`, `/concept`, and `/holding` on their existing capture
path.

Before delegating any deletion or mass move, external publish/push/send, login or
credential operation, trade, or other irreversible/high-impact action, obtain
the user's explicit confirmation for that action. Never put secrets in a Codex
prompt. Return Codex's verified result and do not claim work that Codex did not
complete.

Never restart, stop, kill, reinstall, schedule, or reconfigure the Hermes
Gateway from inside a Hermes messaging session. This includes indirect commands
such as `schtasks`, `taskkill`, `Stop-Process`, or scripts that target the
Gateway PID. Delegate diagnosis to Codex and tell the user that Gateway lifecycle
changes must run from an external process.

<!-- ai-hub:codex-routing:end -->
