# Build the Windows desktop app (folder app, no PyInstaller):
#   dist\AgentBridge\AgentBridge.exe     tiny launcher (csc.exe, .NET Framework) -> runtime\pythonw.exe -m agent_bridge desktop
#   dist\AgentBridge\runtime\            official embeddable Python + pywebview / qrcode / tkinter
#   dist\AgentBridge\app\agent_bridge\   the source
# Usage:  build_desktop.cmd [-Zip] [-NoTest]
# Needs: a system Python 3.11 (x64) for pip and the tkinter files; internet for the first download.
param(
    [switch]$Zip,
    [switch]$NoTest,
    [string]$PyVersion = "3.11.9",
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Out) { $Out = Join-Path $root "dist\AgentBridge" }
$build = Join-Path $root "build"
New-Item -ItemType Directory -Force $build | Out-Null

function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }

# 1. system Python 3.11 (same minor as the embedded runtime, so wheels and _tkinter match)
Step "find system Python 3.11"
$sysPy = $null
$probe = "import sys; print(sys.executable if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else '')"
foreach ($cmd in @("py -3.11", "python")) {
    try {
        $parts = $cmd.Split(" ")
        $exe = if ($parts.Length -gt 1) { & $parts[0] $parts[1] -c $probe 2>$null } else { & $parts[0] -c $probe 2>$null }
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { $sysPy = $exe.Trim(); break }
    } catch {}
}
if (-not $sysPy) { throw "Need a 64-bit Python 3.11 on PATH (py -3.11 or python) to build." }
$sysDir = Split-Path -Parent $sysPy
Write-Host "    $sysPy"

# 2. embeddable Python
$embedZip = Join-Path $build "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    Step "download python-$PyVersion-embed-amd64.zip"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip" -OutFile "$embedZip.part" -UseBasicParsing
    Move-Item -Force "$embedZip.part" $embedZip
}
if (Test-Path $Out) {
    Step "clean $Out"
    Remove-Item -Recurse -Force $Out
}
$rt = Join-Path $Out "runtime"
$app = Join-Path $Out "app"
New-Item -ItemType Directory -Force $rt, $app | Out-Null
Step "unpack runtime"
Expand-Archive -Path $embedZip -DestinationPath $rt -Force
$pth = Get-ChildItem $rt -Filter "python3*._pth" | Select-Object -First 1
$zipName = (Get-ChildItem $rt -Filter "python3*.zip" | Select-Object -First 1).Name
$lines = @($zipName, ".", "Lib", "Lib\site-packages", "..\app", "import site")
[IO.File]::WriteAllText($pth.FullName, ($lines -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding $false))

# 3. packages (pywebview pulls pythonnet / proxy_tools / bottle; Pillow is only needed to build the icon, not at runtime)
Step "pip install pywebview qrcode -> runtime\Lib\site-packages"
& $sysPy -m pip install --disable-pip-version-check --no-warn-script-location --upgrade `
    --target (Join-Path $rt "Lib\site-packages") "pywebview>=5,<7" "qrcode>=7"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Get-ChildItem (Join-Path $rt "Lib\site-packages") -Directory -Filter "bin" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 4. tkinter (the desktop letter animation fx.py uses it); embeddable Python ships without it
Step "copy tkinter from $sysDir"
$tkOk = $true
foreach ($f in @("DLLs\_tkinter.pyd", "DLLs\tcl86t.dll", "DLLs\tk86t.dll")) {
    $src = Join-Path $sysDir $f
    if (Test-Path $src) { Copy-Item $src $rt } else { $tkOk = $false }
}
foreach ($d in @("Lib\tkinter", "tcl")) {
    $src = Join-Path $sysDir $d
    if (Test-Path $src) { Copy-Item -Recurse $src (Join-Path $rt $d) } else { $tkOk = $false }
}
if (-not $tkOk) { Write-Warning "tkinter files not found in $sysDir - the desktop letter animation will be skipped." }
foreach ($junk in @("tcl\tix8.4.3", "tcl\tk8.6\demos", "Lib\tkinter\test")) {
    $p = Join-Path $rt $junk
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

# 5. app source
Step "copy agent_bridge"
$dst = Join-Path $app "agent_bridge"
Copy-Item -Recurse (Join-Path $root "agent_bridge") $dst
Get-ChildItem $dst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
foreach ($f in @("LICENSE", "README.md")) { Copy-Item (Join-Path $root $f) $Out }
if (Test-Path (Join-Path $root "licenses")) { Copy-Item -Recurse (Join-Path $root "licenses") (Join-Path $Out "licenses") }

# 6. launcher
Step "compile AgentBridge.exe"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { throw "csc.exe (.NET Framework 4.x) not found" }
& $csc /nologo /target:winexe /platform:anycpu /optimize+ "/win32icon:$(Join-Path $root 'agent_bridge\icon.ico')" `
    "/out:$(Join-Path $Out 'AgentBridge.exe')" /r:System.Windows.Forms.dll (Join-Path $root "tools\launcher\AgentBridge.cs")
if ($LASTEXITCODE -ne 0) { throw "csc failed" }

# 7. smoke test with the embedded python (imports + bus selftest in a temp project, hooks pointed at the bundled python)
if (-not $NoTest) {
    Step "smoke test"
    $rpy = Join-Path $rt "python.exe"
    Push-Location $app
    try {
        & $rpy -c "import webview, qrcode, sqlite3, tomllib, json, ctypes, agent_bridge.server, agent_bridge.desktop, agent_bridge.bus; print('imports ok')"
        if ($LASTEXITCODE -ne 0) { throw "import test failed" }
        if ($tkOk) {
            & $rpy -c "import tkinter; t = tkinter.Tcl(); print('tkinter ok, tcl', t.eval('info patchlevel'))"
            if ($LASTEXITCODE -ne 0) { throw "tkinter test failed" }
        }
        $env:AGENT_BRIDGE_BUNDLED = "1"; $env:AGENT_BRIDGE_HIDE_SYSTEM_PY = "1"; $env:PYTHONUTF8 = "1"
        & $rpy -m agent_bridge selftest
        $st = $LASTEXITCODE
        Remove-Item Env:AGENT_BRIDGE_BUNDLED, Env:AGENT_BRIDGE_HIDE_SYSTEM_PY, Env:PYTHONUTF8
        if ($st -ne 0) { throw "selftest with the embedded python failed" }
    } finally { Pop-Location }
    Get-ChildItem $app -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
}

$size = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("done: {0}  ({1:N1} MB)" -f (Join-Path $Out "AgentBridge.exe"), ($size / 1MB)) -ForegroundColor Green

if ($Zip) {
    $z = Join-Path (Split-Path -Parent $Out) "AgentBridge-windows-x64.zip"
    if (Test-Path $z) { Remove-Item -Force $z }
    Step "zip -> $z"
    Compress-Archive -Path $Out -DestinationPath $z -CompressionLevel Optimal
    Write-Host ("zip: {0} ({1:N1} MB)" -f $z, ((Get-Item $z).Length / 1MB)) -ForegroundColor Green
}
