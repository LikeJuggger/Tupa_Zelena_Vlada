# Tupa_Zelena_Vlada — установка для Windows 10/11. Можна запускати повторно.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Host.UI.RawUI.WindowTitle = "Tupa_Zelena_Vlada — установка"
function Say($t, $c = "White") { Write-Host $t -ForegroundColor $c }
function RefreshPath { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") + ";" + "$env:LOCALAPPDATA\Microsoft\WinGet\Links" }
function Has($cmd) { $c = Get-Command $cmd -ErrorAction SilentlyContinue; return [bool]($c -and $c.Source -notlike "*WindowsApps*") }
function WG($id, $name) { Say "   Встановлюю $name…" "Yellow"; winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Null; RefreshPath }

Clear-Host
Say ""
Say "   Tupa_Zelena_Vlada" "DarkYellow"
Say ""
RefreshPath
$haveWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

Say "[1/3] Програми: Python, FFmpeg, Git" "Cyan"
$py = $null
foreach ($c in @("python", "py")) { if (Has $c) { $v = & $c --version 2>&1; if ("$v" -match "Python 3\.([89]|1[0-9])") { $py = $c; break } } }
if ($py) { Say "   ✓ $(& $py --version 2>&1)" "Green" }
elseif ($haveWinget) { WG "Python.Python.3.12" "Python 3.12"; foreach ($c in @("python", "py")) { if (Has $c) { $py = $c; break } } }
if (Has "ffmpeg") { Say "   ✓ FFmpeg (для mp3)" "Green" } elseif ($haveWinget) { WG "Gyan.FFmpeg" "FFmpeg" }
if (Has "git") { Say "   ✓ Git (для оновлень)" "Green" } elseif ($haveWinget) { WG "Git.Git" "Git" }
if (-not $py) { Say "   ✗ Python не встановився. Перезавантаж комп'ютер і запусти INSTALL ще раз." "Red"; Read-Host "Enter — закрити"; exit 1 }

Say "[2/3] Ярлик" "Cyan"
$pyw = & $py -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2>$null
if ($pyw -and (Test-Path $pyw)) { [IO.File]::WriteAllText((Join-Path $PSScriptRoot "pythonw.txt"), $pyw) }
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
  Remove-Item (Join-Path $dir "FCPXML2AAF.lnk") -ErrorAction SilentlyContinue
  $lnkPath = Join-Path $dir "Tupa_Zelena_Vlada.lnk"
  $lnk = $ws.CreateShortcut($lnkPath)
  $lnk.TargetPath = "$env:WINDIR\System32\wscript.exe"
  $lnk.Arguments = "`"$PSScriptRoot\START.vbs`""
  $lnk.WorkingDirectory = $PSScriptRoot
  $lnk.IconLocation = "$PSScriptRoot\icon.ico"
  $lnk.Description = "Tupa_Zelena_Vlada — FCPXML у AAF"
  try { $lnk.Save() } catch {}
  if (Test-Path $lnkPath) { Say "   ✓ $lnkPath" "Green" } else { Say "   ✗ не вдалося створити $lnkPath" "Red" }
}

Say "[3/3] Запуск" "Cyan"
Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList "`"$PSScriptRoot\START.vbs`""
Say "Готово. Надалі запускай ярликом «Tupa_Zelena_Vlada» на робочому столі." "Green"
Start-Sleep -Seconds 3
