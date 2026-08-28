# Hermes QQBot Setup

Hermes uses Tencent's official QQ Bot API v2. Credentials live only in the
runtime private environment file under `HERMES_HOME`; never store them in this
repository.

## Runtime Settings

Required environment keys:

```text
QQ_APP_ID
QQ_CLIENT_SECRET
QQ_ALLOW_ALL_USERS=false
```

The default QQ direct-message policy is `pairing`. Group access remains closed
until explicitly configured.

## Local Compatibility Fix

The Hermes source currently requires every platform adapter to implement:

```python
async def connect(self, *, is_reconnect: bool = False) -> bool:
```

If QQBot fails with `unexpected keyword argument 'is_reconnect'`, check:

```text
C:\Users\YOUR_USER\AppData\Local\hermes\hermes-agent\gateway\platforms\qqbot\adapter.py
```

The QQ adapter does not need special reconnect behavior, but it must accept the
keyword to satisfy the gateway contract. The regression test is in
`tests/gateway/test_qqbot.py`.

## Verify

```powershell
powershell -ExecutionPolicy Bypass -File E:\aiworkspace\ai-hub\scripts\test-hermes-qqbot.ps1
```

A healthy runtime log contains `Access token refreshed`, `WebSocket connected`,
`Identify sent`, `qqbot connected`, and `Ready`. Do not print credential values
while diagnosing.
