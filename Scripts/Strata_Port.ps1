<#
    The port the Strata service serves on, in one place.

    WHY A FILE OF ITS OWN

    The shortcut (StrataStack.ps1), the 15-minute check (Strata-EnsureRunning.ps1) and
    Strata_Start.bat all have to agree on where Strata answers. They used to say 8080 in
    three places, which is right until another program owns 8080 - a llama.cpp service
    does on this computer - and then every one of them reports "not answering" while
    Strata is serving happily on another port.

    WHERE THE ANSWER COMES FROM, in order:

        port= in service.conf     the service's own setting (C:\ProgramData\Strata\service)
        port in the model config  the config= line of the same file, or the newest config
        8080                      Strata's documented default

    Read-only: nothing is written and nothing is started. Prints the number alone, so
    cmd can use it in a for /f and PowerShell can cast it straight to [int].

        powershell -NoProfile -ExecutionPolicy Bypass -File D:\Strata\Scripts\Strata_Port.ps1
#>
param([string]$Root = 'D:\Strata')

$ErrorActionPreference = 'SilentlyContinue'

function Read-Conf([string]$path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $path) {
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#') -or $text.StartsWith(';')) { continue }
        if ($text -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { continue }
        $values[$matches[1].ToLowerInvariant()] = $matches[2].Trim().Trim('"').Trim("'")
    }
    return $values
}

$confPath = if ($env:STRATA_SERVICE_ROOT) {
    Join-Path $env:STRATA_SERVICE_ROOT 'service.conf'
} elseif ($env:ProgramData) {
    Join-Path $env:ProgramData 'Strata\service\service.conf'
} else {
    'C:\ProgramData\Strata\service\service.conf'
}

$conf = Read-Conf $confPath

# 1. the service's own port=
if ($conf['port'] -match '^\d+$') {
    [int]$conf['port']
    exit 0
}

# 2. the port its model config names
$config = $conf['config']
if (-not $config) {
    $newest = Get-ChildItem -LiteralPath $Root -Filter 'strata-*.json' |
        Where-Object { $_.Name -notlike '*.shared-settings.json' -and $_.Name -notlike '*.bak*' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) { $config = $newest.FullName }
}
if ($config) {
    try {
        $data = ConvertFrom-Json (Get-Content -Raw -LiteralPath $config)
        if ($data.port -match '^\d+$') { [int]$data.port; exit 0 }
    } catch { }
}

# 3. the documented default
8080
