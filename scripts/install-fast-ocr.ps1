[CmdletBinding()]
param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$venv = Join-Path $RepoRoot "_runtime\venv-ocr-fast"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $RepoRoot "_automation\trading_research\requirements-ocr.txt"

if (-not (Test-Path -LiteralPath $python)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3.12 -m venv $venv
    } else {
        $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source venv --python 3.11 $venv
        } else {
            $fallback = Join-Path $RepoRoot "_runtime\venv-trading\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $fallback)) {
                throw "A Python launcher or uv.exe is required to create the RapidOCR runtime."
            }
            & $fallback -m venv $venv
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the RapidOCR virtual environment." }
}

$uv = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($uv) {
    & $uv.Source pip install --python $python -r $requirements
} else {
    & $python -m pip install --disable-pip-version-check -r $requirements
}
if ($LASTEXITCODE -ne 0) { throw "Unable to install RapidOCR dependencies." }

& $python -c "from rapidocr import RapidOCR; import onnxruntime; print('RapidOCR runtime ready')"
if ($LASTEXITCODE -ne 0) { throw "RapidOCR import check failed." }
