[CmdletBinding()]
param(
    [string]$HermesRoot = "$env:LOCALAPPDATA\hermes\hermes-agent"
)

$ErrorActionPreference = "Stop"
$adapter = Join-Path $HermesRoot "gateway\platforms\qqbot\adapter.py"
if (-not (Test-Path -LiteralPath $adapter)) {
    throw "Hermes QQ adapter not found: $adapter"
}

$content = [System.IO.File]::ReadAllText($adapter, [System.Text.Encoding]::UTF8)
if ($content.Contains("QQ 4009 reconnect attempt failed after")) {
    Write-Output "Hermes QQ 4009 logging patch is already installed."
    exit 0
}

$oldClose = @'
                logger.warning(
                    "[%s] WebSocket closed: code=%s reason=%s",
'@
$newClose = @'
                close_logger = logger.info if code == 4009 else logger.warning
                close_logger(
                    "[%s] WebSocket closed: code=%s reason=%s",
'@
$oldReconnect = @'
                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                        self._mark_disconnected()
                        return
'@
$oldReconnectV1 = @'
                reconnect_started = time.monotonic()
                if await self._reconnect(backoff_idx):
                    reconnect_elapsed = time.monotonic() - reconnect_started
                    if code == 4009 and reconnect_elapsed > 30:
                        logger.warning(
                            "[%s] QQ 4009 reconnect recovered after %.1fs",
                            self._log_tag,
                            reconnect_elapsed,
                        )
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                        self._mark_disconnected()
                        return
'@
$newReconnect = @'
                reconnect_started = time.monotonic()
                if await self._reconnect(backoff_idx):
                    reconnect_elapsed = time.monotonic() - reconnect_started
                    if code == 4009 and reconnect_elapsed > 30:
                        logger.warning(
                            "[%s] QQ 4009 reconnect recovered after %.1fs",
                            self._log_tag,
                            reconnect_elapsed,
                        )
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    reconnect_elapsed = time.monotonic() - reconnect_started
                    backoff_idx += 1
                    if code == 4009 and (
                        reconnect_elapsed > 30 or backoff_idx >= MAX_RECONNECT_ATTEMPTS
                    ):
                        logger.warning(
                            "[%s] QQ 4009 reconnect attempt failed after %.1fs (attempt %d)",
                            self._log_tag,
                            reconnect_elapsed,
                            backoff_idx,
                        )
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                        self._mark_disconnected()
                        return
'@

if (-not $content.Contains($oldClose) -and
    -not $content.Contains("close_logger = logger.info if code == 4009 else logger.warning")) {
    throw "Hermes QQ close-log source block no longer matches the supported version."
}
if (-not $content.Contains($oldReconnect) -and -not $content.Contains($oldReconnectV1)) {
    throw "Hermes QQ reconnect source block no longer matches the supported version."
}

$backup = "$adapter.before-qq4009-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item -LiteralPath $adapter -Destination $backup
if ($content.Contains($oldClose)) {
    $content = $content.Replace($oldClose, $newClose)
}
if ($content.Contains($oldReconnectV1)) {
    $content = $content.Replace($oldReconnectV1, $newReconnect)
} else {
    $content = $content.Replace($oldReconnect, $newReconnect)
}
$temporary = "$adapter.tmp"
[System.IO.File]::WriteAllText(
    $temporary,
    $content,
    (New-Object System.Text.UTF8Encoding($false))
)
Move-Item -LiteralPath $temporary -Destination $adapter -Force
Write-Output "Installed Hermes QQ 4009 logging patch. Backup: $backup"
