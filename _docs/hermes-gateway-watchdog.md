# Hermes Gateway Watchdog

Hermes has two complementary health checks on this machine:

- Codex automation `Hermes 每小时健康巡检`: hourly platform and log audit.
- Windows Scheduled Task `Hermes_Gateway_Watchdog`: five-minute process check
  and external restart when the Gateway is absent.

Install or refresh the Windows task with:

```powershell
powershell -ExecutionPolicy Bypass -File E:\aiworkspace\ai-hub\scripts\install-hermes-watchdog.ps1 -RunNow
```

The watchdog runs `scripts/hermes-watchdog.ps1`. It does not read credentials,
change configuration, delete sessions, or run Repair install. Its log is under
`_runtime/watchdog/hermes-watchdog.log` and is excluded from Git.

## 2026-07-11 Failure

The Gateway was terminated by a Hermes QQ session that executed:

```text
taskkill //F //PID <gateway-pid>
```

Two conditions allowed it:

1. The managed `SOUL.md` had a UTF-8 BOM and was rejected as
   `invisible_unicode_U+FEFF`, so Codex delegation rules were not loaded.
2. The terminal lifecycle guard blocked `hermes gateway restart` but did not
   recognize `schtasks /end /tn Hermes_Gateway` or a raw kill of its own PID.

The installer now writes UTF-8 without BOM. Hermes additionally blocks Windows
Task Scheduler commands that end the Gateway task and process-kill commands
targeting the current Gateway PID. Regression coverage lives in
`tests/hermes_cli/test_gateway_restart_loop.py` in the local Hermes repository.
