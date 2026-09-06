<!-- ai-hub:codex-routing:start -->
<!-- ai-hub:codex-routing version: 2026-09-06 v2 -->

## Codex Delegation

For complex requests, always choose the local `delegate-to-codex` skill before
Hermes's own tools or any `claude-code` skill. This priority applies to code
changes, debugging and repair, multi-file or batch work, Git tasks, Obsidian
restructuring, system integration, and other multi-step work that requires tools
and verification. Use `claude-code` only when the user explicitly asks for
Claude or when Codex fails and the user accepts that fallback.

The delegation skill must call the native `codex_delegate` tool. Hermes must
never use `terminal`, `execute_code`, `write_file`, `patch`, `delegate_task`, or
another agent as a maintenance fallback. This rule is about fallback routing,
not about ordinary work: normal conversation, short explanations, simple status
checks, and the established capture commands are handled directly.

## Central routing: failure classes

- Ordinary execution failure: correct the context and retry; do not silently
  switch tools or agents.
- Codex unavailable or out of quota: stop, report the blocker, and leave the
  source unchanged. Do not fall back to another agent on your own.
- `claude-code` fallback: only when the user explicitly requests Claude, or
  when Codex fails and the user agrees to that fallback.

Generic skills must not define a second execution entry; execution method
obeys this central routing block.

## Approval timing

Delegation that ends in deletion or mass move, external publish/push/send,
login or credential operation, trade, or another irreversible/high-impact
action requires the user's explicit confirmation for that final action.
Already authorized preparation — reading, inspecting, implementing, testing,
drafting the diff — may proceed before that confirmation; the irreversible
action is executed only after explicit approval at execution time.

Never put secrets in a Codex prompt. Return Codex's verified result and do not
claim work that Codex did not complete.

## Gateway lifecycle

Never restart, stop, kill, reinstall, schedule, or reconfigure the Hermes
Gateway from inside a Hermes messaging session. This includes indirect commands
such as `schtasks`, `taskkill`, `Stop-Process`, or scripts that target the
Gateway PID. Delegate diagnosis to Codex and tell the user that Gateway lifecycle
changes must run from an external process.

<!-- ai-hub:codex-routing:end -->