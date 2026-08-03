param(
    [string]$ExternalRoot = "<AI_HUB_HOME>\ai-hub\_external"
)

$ErrorActionPreference = "Stop"

$dailyStockPath = Join-Path $ExternalRoot "daily_stock_analysis"
$dailyStockRepo = "https://github.com/ZhuLinsen/daily_stock_analysis.git"

New-Item -ItemType Directory -Force -Path $ExternalRoot | Out-Null

if (Test-Path -LiteralPath $dailyStockPath) {
    Write-Output "daily_stock_analysis already exists: $dailyStockPath"
    exit 0
}

Write-Output "Cloning daily_stock_analysis into $dailyStockPath"
git clone --depth 1 $dailyStockRepo $dailyStockPath

Write-Output "Done. Next inspect README/config before running any report task."

