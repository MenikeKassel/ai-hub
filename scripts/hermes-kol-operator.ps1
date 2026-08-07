[CmdletBinding()]
param(
    [ValidateSet(
        "status", "doctor", "start", "open", "collect", "review", "market", "returns",
        "import-zhihu", "onboard-zhihu",
        "list-kols", "add-kol", "set-kol-status", "list-drafts", "approve-draft",
        "reject-draft", "list-events", "event-action"
    )]
    [string]$Action = "status",
    [string]$RepoRoot = "",
    [int]$Port = 8123,
    [int]$Id = 0,
    [string]$Handle = "",
    [string]$DisplayName = "",
    [ValidateSet("X", "Zhihu")]
    [string]$Platform = "X",
    [string]$ProfileUrl = "",
    [string]$Domain = "",
    [int]$BatchSize = 8,
    [ValidateSet("", "active", "paused")]
    [string]$Status = "",
    [string]$ReviewDate = (Get-Date -Format "yyyy-MM-dd"),
    [string]$Note = "",
    [string]$EventId = "",
    [ValidateSet("", "activate", "exclude", "restore", "archive")]
    [string]$EventAction = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$url = "http://127.0.0.1:$Port"
$fetchPidPath = Join-Path $RepoRoot "_runtime\trading\kol\logs\kol-post-fetch.pid"
$uiRuntime = Join-Path $RepoRoot "_runtime\trading\kol\ui"
$uiPidPath = Join-Path $uiRuntime "server.pid"
$uiMetadataPath = Join-Path $uiRuntime "server.process.json"

function Write-Result {
    param($Value)
    $Value | ConvertTo-Json -Depth 10 -Compress
}

function Invoke-Utf8Json {
    param(
        [ValidateSet("GET", "POST", "PATCH")]
        [string]$Method,
        [string]$Uri,
        $Body = $null,
        [int]$TimeoutSec = 60
    )
    $parameters = @{
        Uri = $Uri
        Method = $Method
        TimeoutSec = $TimeoutSec
        UseBasicParsing = $true
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json; charset=utf-8"
        $parameters.Body = $Body | ConvertTo-Json -Depth 10 -Compress
    }
    $response = Invoke-WebRequest @parameters
    $stream = $response.RawContentStream
    $stream.Position = 0
    $buffer = New-Object System.IO.MemoryStream
    try {
        $stream.CopyTo($buffer)
        $text = [System.Text.Encoding]::UTF8.GetString($buffer.ToArray())
    } finally {
        $buffer.Dispose()
    }
    if (-not $text.Trim()) { return $null }
    return $text | ConvertFrom-Json
}

function Get-PipelineStatus {
    try {
        return Invoke-Utf8Json "GET" "$url/api/pipeline/status" $null 10
    } catch {
        return $null
    }
}

function Get-ListenerProcess {
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Get-ServerProcessInfo {
    $serverPid = 0
    if (Test-Path -LiteralPath $uiPidPath) {
        try { $serverPid = [int](Get-Content -LiteralPath $uiPidPath -Raw).Trim() } catch { $serverPid = 0 }
    }
    $process = if ($serverPid -gt 0) { Get-Process -Id $serverPid -ErrorAction SilentlyContinue } else { $null }
    if ($serverPid -gt 0 -and -not $process -and (Test-Path -LiteralPath $uiPidPath)) {
        Remove-Item -LiteralPath $uiPidPath -Force -ErrorAction SilentlyContinue
        $serverPid = 0
    }
    $metadata = $null
    if (Test-Path -LiteralPath $uiMetadataPath) {
        try { $metadata = Get-Content -LiteralPath $uiMetadataPath -Raw | ConvertFrom-Json } catch { $metadata = $null }
    }
    return @{
        pid = $serverPid
        alive = [bool]$process
        python = if ($metadata) { [string]$metadata.python } else { "" }
        python_version = if ($metadata) { [string]$metadata.python_version } else { "" }
        started_at = if ($metadata) { [string]$metadata.started_at } else { "" }
    }
}

function Get-ServerProbe {
    $status = Get-PipelineStatus
    $listeners = @(Get-ListenerProcess)
    $process = Get-ServerProcessInfo
    if ($status) {
        return @{ state = "ready"; listeners = $listeners; process = $process; status = $status }
    }
    if ($listeners.Count -gt 0) {
        return @{ state = "unhealthy"; listeners = $listeners; process = $process; status = $null }
    }
    return @{ state = "stopped"; listeners = @(); process = $process; status = $null }
}

function Get-StartupErrorDetails {
    $errorPath = Join-Path $RepoRoot "_runtime\trading\kol\ui\server.err.log"
    $stderr = if (Test-Path -LiteralPath $errorPath) {
        ((Get-Content -LiteralPath $errorPath -Tail 80 -ErrorAction SilentlyContinue) -join [Environment]::NewLine)
    } else { "" }
    if ($stderr.Length -gt 12000) { $stderr = $stderr.Substring($stderr.Length - 12000) }
    return @{ error_log = $errorPath; stderr_tail = $stderr }
}

function Require-Server {
    $status = Get-PipelineStatus
    if (-not $status) { throw "KOL research console is not reachable. Run -Action start first." }
    return $status
}

function Invoke-KolApi {
    param(
        [ValidateSet("GET", "POST", "PATCH")]
        [string]$Method,
        [string]$Path,
        $Body = $null
    )
    Require-Server | Out-Null
    return Invoke-Utf8Json $Method "$url$Path" $Body 60
}

function Start-KolTask {
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { throw "Scheduled task not found: $TaskName" }
    if ($task.State -eq "Running") {
        return @{ ok = $true; action = "already_running"; task = $TaskName; url = $url }
    }
    Start-ScheduledTask -TaskName $TaskName
    return @{ ok = $true; action = "triggered"; task = $TaskName; url = $url }
}

function Test-FetchRunning {
    foreach ($taskName in @("KOL_Post_Fetch_Daily", "KOL_Post_Fetch_Manual_X", "KOL_Post_Fetch_Manual_Zhihu")) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq "Running") { return $true }
    }
    if (-not (Test-Path -LiteralPath $fetchPidPath)) { return $false }
    try {
        $processId = [int](Get-Content -LiteralPath $fetchPidPath -Raw).Trim()
        return $null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Test-CodexBlocked {
    param($PipelineStatus)
    if (-not $PipelineStatus -or -not $PipelineStatus.morning_delivery) { return $false }
    $errors = @($PipelineStatus.morning_delivery.errors) -join " "
    return $errors -match "(?i)(usage limit|quota|rate limit|codex unavailable)"
}

function Select-PipelineStatus {
    param($PipelineStatus)
    if (-not $PipelineStatus) {
        $listeners = @(Get-ListenerProcess)
        $process = Get-ServerProcessInfo
        return @{
            ok = $false
            running = $false
            url = $url
            state = if ($listeners.Count -gt 0) { "unhealthy" } else { "stopped" }
            error = if ($listeners.Count -gt 0) {
                "KOL research console has a listener but its health endpoint is not ready"
            } else { "KOL research console is not reachable" }
            listener_processes = $listeners
            process = $process
        }
    }
    $process = Get-ServerProcessInfo
    $delivery = $PipelineStatus.morning_delivery
    return @{
        ok = $true
        running = $true
        url = $url
        latest_fetch_at = $PipelineStatus.latest_fetch_at
        latest_fetch_status = $PipelineStatus.latest_fetch_status
        latest_ai_at = $PipelineStatus.latest_ai_at
        pending_ai = $PipelineStatus.pending_ai
        codex_blocked = Test-CodexBlocked $PipelineStatus
        delivery_status = $delivery.status
        delivery_completed_at = $delivery.completed_at
        delivery_errors = @(
            $delivery.errors | Select-Object -First 3 | ForEach-Object {
                $text = [string]$_
                if ($text.Length -gt 320) { $text.Substring(0, 320) + "..." } else { $text }
            }
        )
        next_preview = $PipelineStatus.next_preview
        latest_trade_date = $PipelineStatus.latest_trade_date
        market_status = $PipelineStatus.market_status
        lagging_symbol_count = @($PipelineStatus.lagging_symbols).Count
        fetch_running = Test-FetchRunning
        process = $process
        listener_processes = @(Get-ListenerProcess)
    }
}

switch ($Action) {
    "status" {
        Write-Result (Select-PipelineStatus (Get-PipelineStatus))
    }
    "doctor" {
        $pipeline = Select-PipelineStatus (Get-PipelineStatus)
        $health = if ($pipeline.running) {
            Invoke-Utf8Json "GET" "$url/api/system/health" $null 20
        } else { $null }
        Write-Result @{
            ok = [bool]$pipeline.running
            pipeline = $pipeline
            codex_cli = if ($health) { $health.codex_cli } else { "" }
            twitter_credentials_configured = if ($health) { $health.twitter_credentials_configured } else { $false }
            rapid_ocr_available = if ($health) { $health.rapid_ocr_available } else { $false }
            tasks = if ($health) {
                @{
                    fetch = $health.post_fetch_task
                    morning = $health.morning_pipeline_task
                    market = $health.market_sync_task
                    returns = $health.return_task
                }
            } else { @{} }
        }
    }
    "start" {
        $probe = Get-ServerProbe
        if ($probe.state -eq "ready") {
            Write-Result @{ ok = $true; action = "already_running"; codex_used = $false; url = $url }
            break
        }
        if ($probe.state -eq "unhealthy") {
            Write-Result @{
                ok = $false
                action = "port_conflict"
                codex_used = $false
                url = $url
                listener_processes = $probe.listeners
                error = "Port $Port is occupied, but the KOL health endpoint is not ready."
            }
            break
        }
        try {
            & (Join-Path $RepoRoot "scripts\start-kol-ui.ps1") -RepoRoot $RepoRoot -Port $Port -NoBrowser
            if (-not (Get-PipelineStatus)) {
                $details = Get-StartupErrorDetails
                Write-Result @{
                    ok = $false
                    action = "startup_failed"
                    codex_used = $false
                    url = $url
                    error = "KOL research console did not become ready"
                    error_log = $details.error_log
                    stderr_tail = $details.stderr_tail
                }
                break
            }
            Write-Result @{ ok = $true; action = "started"; codex_used = $false; url = $url }
        } catch {
            $details = Get-StartupErrorDetails
            Write-Result @{
                ok = $false
                action = "startup_failed"
                codex_used = $false
                url = $url
                error = $_.Exception.Message
                error_log = $details.error_log
                stderr_tail = $details.stderr_tail
            }
        }
    }
    "open" {
        $probe = Get-ServerProbe
        if ($probe.state -eq "unhealthy") {
            Write-Result @{
                ok = $false
                action = "port_conflict"
                codex_used = $false
                url = $url
                listener_processes = $probe.listeners
                error = "Port $Port is occupied, but the KOL health endpoint is not ready."
            }
            break
        }
        if ($probe.state -eq "stopped") {
            try {
                & (Join-Path $RepoRoot "scripts\start-kol-ui.ps1") -RepoRoot $RepoRoot -Port $Port -NoBrowser
            } catch {
                $details = Get-StartupErrorDetails
                Write-Result @{
                    ok = $false
                    action = "startup_failed"
                    codex_used = $false
                    url = $url
                    error = "KOL research console did not become ready"
                    error_log = $details.error_log
                    stderr_tail = $details.stderr_tail
                }
                break
            }
            if (-not (Get-PipelineStatus)) {
                $details = Get-StartupErrorDetails
                Write-Result @{
                    ok = $false
                    action = "startup_failed"
                    codex_used = $false
                    url = $url
                    error = "KOL research console did not become ready"
                    error_log = $details.error_log
                    stderr_tail = $details.stderr_tail
                }
                break
            }
        }
        Start-Process $url
        Write-Result @{ ok = $true; action = if ($probe.state -eq "ready") { "opened" } else { "started_and_opened" }; codex_used = $false; url = $url }
    }
    "collect" {
        if (Test-FetchRunning) {
            Write-Result @{ ok = $true; action = "already_running"; codex_used = $false; url = $url }
            break
        }
        $taskName = if ($Platform -eq "Zhihu") { "KOL_Post_Fetch_Manual_Zhihu" } else { "KOL_Post_Fetch_Manual_X" }
        $result = Start-KolTask $taskName
        $result.operation = "fetch_only"
        $result.codex_used = $false
        Write-Result $result
    }
    "review" {
        $pipeline = Get-PipelineStatus
        if ((Test-CodexBlocked $pipeline) -and -not $Force) {
            Write-Result @{
                ok = $true
                action = "blocked"
                reason = "codex_quota_or_rate_limit"
                posts_preserved = $true
                manual_review_available = [bool]$pipeline
                url = $url
            }
            break
        }
        Write-Result (Start-KolTask "KOL_Morning_Pipeline")
    }
    "market" { Write-Result (Start-KolTask "Market_Data_Sync_Daily") }
    "returns" { Write-Result (Start-KolTask "KOL_Return_Tracker_Daily") }
    "import-zhihu" {
        $python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
        $cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
        $output = & $python $cli kol-import --platform zhihu --from-linked-profiles 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw $output.Trim() }
        Write-Output $output.Trim()
    }
    "onboard-zhihu" {
        $python = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
        $cli = Join-Path $RepoRoot "_automation\trading_research\trading_cli.py"
        $output = & $python $cli kol-zhihu-onboard --advance --batch-size $BatchSize --backfill 20 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw $output.Trim() }
        Write-Output $output.Trim()
    }
    "list-kols" {
        $items = Invoke-KolApi "GET" "/api/kols"
        Write-Result @($items | Select-Object id, display_name, platform, handle, status, fetch_status, last_fetched_at, consecutive_failures)
    }
    "add-kol" {
        if (-not $Handle -or -not $DisplayName) { throw "add-kol requires -Handle and -DisplayName" }
        $trackingMode = if ($Platform -eq "Zhihu") { "direct_profile" } else { "all" }
        Write-Result (Invoke-KolApi "POST" "/api/kols" @{
            handle = $Handle.TrimStart("@")
            display_name = $DisplayName
            platform = $Platform
            profile_url = $ProfileUrl
            domain = $Domain
            tracking_mode = $trackingMode
        })
    }
    "set-kol-status" {
        if ($Id -le 0 -or $Status -notin @("active", "paused")) {
            throw "set-kol-status requires -Id and -Status active|paused"
        }
        Write-Result (Invoke-KolApi "PATCH" "/api/kols/$Id" @{ status = $Status })
    }
    "list-drafts" {
        $review = Invoke-KolApi "GET" "/api/morning-review?review_date=$ReviewDate&limit=200"
        Write-Result @{
            review_date = $ReviewDate
            summary = $review.summary
            delivery = $review.delivery
            drafts = @($review.drafts | Select-Object id, post_id, handle, display_name, posted_at, symbol, security_name, direction, action, horizon, strength, thesis, status, attention_reasons, url)
        }
    }
    "approve-draft" {
        if ($Id -le 0) { throw "approve-draft requires an explicit -Id" }
        Write-Result (Invoke-KolApi "POST" "/api/recommendation-drafts/$Id/approve" @{ note = $Note })
    }
    "reject-draft" {
        if ($Id -le 0) { throw "reject-draft requires an explicit -Id" }
        Write-Result (Invoke-KolApi "POST" "/api/recommendation-drafts/$Id/reject" @{ note = $Note })
    }
    "list-events" {
        $items = Invoke-KolApi "GET" "/api/events"
        Write-Result @($items | Select-Object event_id, kol_name, platform, posted_at, symbol, security_name, direction, thesis, status, exclusion_reason, baseline_date, baseline_price_raw, source_url)
    }
    "event-action" {
        if (-not $EventId -or -not $EventAction) {
            throw "event-action requires -EventId and -EventAction"
        }
        $body = @{ action = $EventAction }
        if ($EventAction -eq "exclude") {
            if (-not $Note) { throw "excluding an event requires -Note" }
            $body.exclusion_reason = $Note
        }
        Write-Result (Invoke-KolApi "PATCH" "/api/events/$EventId" $body)
    }
}
