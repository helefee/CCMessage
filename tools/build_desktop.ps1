# Build the Windows installer version of AgentBridge (CCMessage):
#   dist\AgentBridge\AgentBridge.exe       Nuitka standalone (onedir), windows GUI, attaches to a console when run from one
#   dist\AgentBridge\bus\bus.exe           Nuitka standalone console exe of agent_bridge\bus.py (project hooks run this)
#   dist\AgentBridge-Setup-<ver>.exe       Inno Setup installer (+ .sha256), per-user, no admin
#   dist\AgentBridge-<ver>-windows-x64.zip the onedir as a zip (-Zip)
# No .py sources end up in dist\AgentBridge; Python is not needed on the target machine.
# Usage:  build_desktop.cmd [-Zip] [-NoTest] [-NoInstaller]
# Needs:  a 64-bit system Python 3.11 (only to build), internet on first run (pip, Nuitka's MinGW64 if there is no MSVC),
#         Inno Setup 6.5+ or 7 (ISCC.exe on PATH, in Program Files, or set $env:ISCC).
param(
    [switch]$Zip,
    [switch]$NoTest,
    [switch]$NoInstaller
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
$distRoot = Join-Path $root "dist"
$out = Join-Path $distRoot "AgentBridge"
New-Item -ItemType Directory -Force $build, $distRoot | Out-Null
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
# run a native tool; its stderr (pip / nuitka warnings) must not turn into a terminating error under "Stop"
function Native { $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"; try { & $args[0] @($args | Select-Object -Skip 1) 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $eap } }

# version: single source agent_bridge\__init__.py
$ver = ([regex]'__version__\s*=\s*"([^"]+)"').Match((Get-Content (Join-Path $root "agent_bridge\__init__.py") -Raw)).Groups[1].Value
if (-not $ver) { throw "no __version__ in agent_bridge\__init__.py" }
$numVer = ([regex]'^\d+(\.\d+){0,3}').Match($ver).Value
Write-Host "AgentBridge $ver"

# 1. build venv from a system Python 3.11 x64
Step "build venv"
$venv = Join-Path $build "venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    $sysPy = $null
    $probe = "import sys; print(sys.executable if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else '')"
    foreach ($cmd in @("py -3.11", "python")) {
        try {
            $parts = $cmd.Split(" ")
            $exe = if ($parts.Length -gt 1) { & $parts[0] $parts[1] -c $probe 2>$null } else { & $parts[0] -c $probe 2>$null }
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { $sysPy = $exe.Trim(); break }
        } catch {}
    }
    if (-not $sysPy) { throw "Need a 64-bit Python 3.11 (py -3.11 or python) to build." }
    & $sysPy -m venv $venv; Check "venv"
}
Native $vpy -m pip install --disable-pip-version-check -q --upgrade pip wheel; Check "pip upgrade"
Native $vpy -m pip install --disable-pip-version-check -q --upgrade -r (Join-Path $root "tools\requirements-build.txt"); Check "pip install"

# 2. C compiler: MSVC if installed (CI), otherwise Nuitka downloads MinGW64 itself
$cc = @()
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$hasMsvc = (Test-Path $vswhere) -and (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
if ($hasMsvc) { $cc = @("--msvc=latest"); Write-Host "    compiler: MSVC" } else { $cc = @("--mingw64"); Write-Host "    compiler: MinGW64 (Nuitka downloads it)" }
$common = @("--standalone", "--assume-yes-for-downloads", "--remove-output", "--python-flag=no_docstrings",
            "--company-name=CCMessage", "--product-name=AgentBridge", "--file-version=$numVer", "--product-version=$numVer",
            "--copyright=MIT", "--windows-icon-from-ico=agent_bridge\icon.ico") + $cc
Push-Location $root
try {
    $env:PYTHONPATH = $root
    # 3. AgentBridge.exe (GUI + all subcommands; bus.py is NOT part of it)
    Step "nuitka AgentBridge.exe"
    Native $vpy -m nuitka @common "--output-dir=build\nuitka-app" "--output-filename=AgentBridge.exe" `
        "--windows-console-mode=attach" "--file-description=AgentBridge" `
        "--include-package=agent_bridge" "--nofollow-import-to=agent_bridge.bus" "--include-package-data=agent_bridge" `
        "--enable-plugin=tk-inter" `
        "tools\nuitka\AgentBridge.py"
    Check "nuitka AgentBridge"
    # 4. bus.exe (console, stdlib only, small and fast to start: project hooks run it every turn)
    Step "nuitka bus.exe"
    Native $vpy -m nuitka @common "--output-dir=build\nuitka-bus" "--output-filename=bus.exe" `
        "--windows-console-mode=force" "--file-description=AgentBridge message bus" "--python-flag=no_site" `
        "agent_bridge\bus.py"
    Check "nuitka bus"
} finally { Pop-Location; Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }

# 5. assemble dist\AgentBridge
Step "assemble $out"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
Copy-Item -Recurse (Join-Path $build "nuitka-app\AgentBridge.dist") $out
Copy-Item -Recurse (Join-Path $build "nuitka-bus\bus.dist") (Join-Path $out "bus")
foreach ($f in @("LICENSE", "README.md")) { Copy-Item (Join-Path $root $f) $out }
if (Test-Path (Join-Path $root "licenses")) { Copy-Item -Recurse (Join-Path $root "licenses") (Join-Path $out "licenses") }
$py = @(Get-ChildItem $out -Recurse -File -Include *.py, *.pyw, *.pyc)
if ($py.Count) { $py | ForEach-Object { Write-Host "    $($_.FullName)" }; throw "python sources found in $out" }
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("    {0:N1} MB, no .py files" -f ($size / 1MB))

# 6. smoke test: bus.exe startup, selftest through bus.exe with no Python on PATH
if (-not $NoTest) {
    Step "smoke test"
    $bus = Join-Path $out "bus\bus.exe"
    $tmp = Join-Path $env:TEMP ("ab-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force (Join-Path $tmp ".msgbus\sessions") | Out-Null
    $times = @()
    foreach ($i in 1..6) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        '{"session_id":"smoke","hook_event_name":"UserPromptSubmit","cwd":"' + ($tmp -replace '\\', '/') + '"}' | & $bus --dir (Join-Path $tmp ".msgbus") hook claude | Out-Null
        Check "bus.exe hook"
        $times += $sw.ElapsedMilliseconds
    }
    Write-Host ("    bus.exe hook: first {0} ms, then {1} ms" -f $times[0], (($times[1..5] | Measure-Object -Average).Average))
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    $oldPath = $env:PATH
    $env:PATH = (($env:PATH -split ';') | Where-Object { $_ -and -not (Test-Path (Join-Path $_ "python.exe")) -and -not (Test-Path (Join-Path $_ "py.exe")) }) -join ';'
    try {
        $p = Start-Process (Join-Path $out "AgentBridge.exe") -ArgumentList "selftest" -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput (Join-Path $build "selftest.out") -RedirectStandardError (Join-Path $build "selftest.err")
        Get-Content (Join-Path $build "selftest.out") -Encoding UTF8 | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" }
        if ($p.ExitCode -ne 0) { Get-Content (Join-Path $build "selftest.err") | Write-Host; throw "selftest failed ($($p.ExitCode))" }
    } finally { $env:PATH = $oldPath }
}

# 7. installer
if (-not $NoInstaller) {
    Step "Inno Setup"
    $iscc = $env:ISCC
    if (-not $iscc) { $c = Get-Command ISCC.exe -ErrorAction SilentlyContinue; if ($c) { $iscc = $c.Source } }
    foreach ($d in @("${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6", "$env:LOCALAPPDATA\Programs\Inno Setup 6", "$env:ProgramFiles\Inno Setup 7", "$env:LOCALAPPDATA\Programs\Inno Setup 7")) {
        if (-not $iscc -and (Test-Path "$d\ISCC.exe")) { $iscc = "$d\ISCC.exe" }
    }
    if (-not $iscc) { throw "ISCC.exe (Inno Setup 6.5+) not found; install it or set `$env:ISCC" }
    Native $iscc /Q "/DAppVersion=$ver" "/DSourceDir=$out" "/DOutputDir=$distRoot" (Join-Path $root "installer\AgentBridge.iss")
    Check "ISCC"
    $setup = Join-Path $distRoot "AgentBridge-Setup-$ver.exe"
    $hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
    [IO.File]::WriteAllText("$setup.sha256", "$hash  AgentBridge-Setup-$ver.exe`n", (New-Object Text.UTF8Encoding $false))
    Write-Host ("    {0} ({1:N1} MB)  sha256 {2}" -f $setup, ((Get-Item $setup).Length / 1MB), $hash) -ForegroundColor Green
}
if ($Zip) {
    $z = Join-Path $distRoot "AgentBridge-$ver-windows-x64.zip"
    if (Test-Path $z) { Remove-Item -Force $z }
    Step "zip -> $z"
    Compress-Archive -Path $out -DestinationPath $z -CompressionLevel Optimal
}
Write-Host "done: $out\AgentBridge.exe" -ForegroundColor Green
