# Hermes Desktop Cold Start Hotfix

Date: 2026-07-11

## Problem

Hermes Desktop can fail with:

```text
Hermes backend did not become ready: Timed out connecting to Hermes backend after 15000ms
HERMES_DASHBOARD_READY port=3138
```

The backend prints `HERMES_DASHBOARD_READY`, then Desktop immediately probes
`/api/status`. On a cold Windows launch, `/api/status` may synchronously load
`gateway.config.load_gateway_config()`, including platform/plugin discovery.
That can stall the event loop long enough for the Desktop probe to time out.

## Local Fix

Patched repository:

```text
C:\Users\YOUR_USER\AppData\Local\hermes\hermes-agent
```

Changed files:

```text
hermes_cli/web_server.py
tests/hermes_cli/test_web_server_boot_handshake.py
```

Behavior:

- `_warm_gateway_module()` now imports `hermes_cli.gateway` and calls
  `gateway.config.load_gateway_config()`.
- In `HERMES_DESKTOP=1`, lifespan awaits that warmup before the server reaches
  the ready announcement path.
- `/api/status` loads gateway config through `asyncio.to_thread(...)`, so a slow
  config load does not block unrelated dashboard requests.

## Verify

Run:

```powershell
E:\aiworkspace\ai-hub\scripts\test-hermes-desktop-hotfix.ps1
```

Expected result:

```text
6 passed
```

Manual smoke check:

```powershell
hermes status --deep
```

Then restart Hermes Desktop. If it still fails, inspect:

```text
C:\Users\YOUR_USER\AppData\Local\hermes\logs\desktop.log
C:\Users\YOUR_USER\AppData\Local\hermes\logs\gui.log
```

## Notes For Future Agents

This is a local upstream hotfix for a Desktop readiness race. Do not delete
platform folders, do not repair install first, and do not touch
`website/tsconfig.json` unless the user explicitly asks.

If Hermes updates overwrite the patch, re-read `hermes_cli/web_server.py` first.
The fix should remain narrowly scoped to readiness warmup and `/api/status`
event-loop blocking.
