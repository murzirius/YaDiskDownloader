$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    $candidates = @()
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($line in @(& $launcher.Source -0p 2>$null)) {
            if ($line -match '^\s*-(V:\S+)') {
                $candidates += @{ Path = $launcher.Source; Arguments = @('-' + $Matches[1]) }
            }
        }
        $candidates += @{ Path = $launcher.Source; Arguments = @('-3') }
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += @{ Path = $pythonCommand.Source; Arguments = @() } }
    $selectedPython = $null
    foreach ($candidate in $candidates) {
        $candidateArguments = $candidate.Arguments
        try {
            & $candidate.Path @candidateArguments -c 'import sys,sysconfig; raise SystemExit(0 if sys.version_info >= (3,11) and not sysconfig.get_config_var(''Py_GIL_DISABLED'') else 1)' 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0) { $selectedPython = $candidate; break }
    }
    if (-not $selectedPython) { throw 'Установите обычный Python 3.11 или новее (не free-threaded), затем запустите start.cmd снова.' }
    $pythonArguments = $selectedPython.Arguments
    & $selectedPython.Path @pythonArguments -m venv .venv
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
