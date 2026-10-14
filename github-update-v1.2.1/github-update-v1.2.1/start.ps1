$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    $pythonTool = Get-Command py -ErrorAction SilentlyContinue
    $pythonArguments = @('-3')
    if (-not $pythonTool) {
        $pythonTool = Get-Command python -ErrorAction SilentlyContinue
        $pythonArguments = @()
    }
    if (-not $pythonTool) { throw 'Установите Python 3.11 или новее, затем запустите start.cmd снова.' }
    & $pythonTool.Source @pythonArguments -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Требуется установленный Python 3.11 или новее.' }
    & $pythonTool.Source @pythonArguments -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python' }
}
$bootstrapFile = Join-Path $PSScriptRoot 'requirements-bootstrap.txt'
$bootstrapStamp = Join-Path $PSScriptRoot '.venv\bootstrap.sha256'
$bootstrapHashProvider = [System.Security.Cryptography.SHA256]::Create()
try {
    $bootstrapHash = [BitConverter]::ToString($bootstrapHashProvider.ComputeHash([System.IO.File]::ReadAllBytes($bootstrapFile))).Replace('-', '')
} finally {
    $bootstrapHashProvider.Dispose()
}
$bootstrapInstalled = if (Test-Path -LiteralPath $bootstrapStamp) { (Get-Content -LiteralPath $bootstrapStamp -Raw).Trim() } else { '' }
if ($bootstrapInstalled -ne $bootstrapHash) {
    & $runtime -m pip install --upgrade -r $bootstrapFile
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось обновить установщик зависимостей' }
    Set-Content -LiteralPath $bootstrapStamp -Value $bootstrapHash -Encoding Ascii
}
$requirementsFile = Join-Path $PSScriptRoot 'requirements.txt'
$hashProvider = [System.Security.Cryptography.SHA256]::Create()
try {
    $requirementsHash = [BitConverter]::ToString($hashProvider.ComputeHash([System.IO.File]::ReadAllBytes($requirementsFile))).Replace('-', '')
} finally {
    $hashProvider.Dispose()
}
$stampFile = Join-Path $PSScriptRoot '.venv\requirements.sha256'
$installedHash = if (Test-Path -LiteralPath $stampFile) { (Get-Content -LiteralPath $stampFile -Raw).Trim() } else { '' }
if ($installedHash -ne $requirementsHash) {
    & $runtime -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
    Set-Content -LiteralPath $stampFile -Value $requirementsHash -Encoding Ascii
}
& $runtime run.py @args
exit $LASTEXITCODE
