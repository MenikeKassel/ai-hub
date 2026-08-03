function Send-HermesUtf8Message {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [string]$Target = "feishu"
    )

    $temporaryPath = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-message-{0}.txt" -f [Guid]::NewGuid().ToString("N"))
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $Message,
            (New-Object System.Text.UTF8Encoding($false))
        )
        & hermes send --to $Target --file $temporaryPath *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "hermes send exited with code $LASTEXITCODE"
        }
        return $true
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}
