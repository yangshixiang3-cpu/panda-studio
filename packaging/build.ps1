# 一键构建 Panda Studio（原白底工作台）安装程序。
# 用法：在 PowerShell 里运行  & "<工作台目录>\packaging\build.ps1"
# 需要：Python 3.13（已装 pywebview、Pillow、PyInstaller 6.22.3）、Inno Setup 6（D:\Tools\InnoSetup6）。
# 输出：packaging\output\PandaStudio-安装程序-<版本>.exe
# PyInstaller 把进度信息写到 stderr，PowerShell 5.1 会当成错误；一律按退出码判断成败
$ErrorActionPreference = 'Continue'
$packaging = $PSScriptRoot
$root = Split-Path $packaging -Parent
$iscc = @('D:\Tools\InnoSetup6\ISCC.exe', "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw '找不到 Inno Setup 6 的 ISCC.exe' }

Set-Location $root
# 只清理本脚本自己生成的构建目录
foreach ($dir in 'build', 'dist') {
    $path = Join-Path $packaging $dir
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
python -m PyInstaller (Join-Path $packaging 'whitebg.spec') --noconfirm --clean `
    --distpath (Join-Path $packaging 'dist') --workpath (Join-Path $packaging 'build')
if ($LASTEXITCODE) { throw "PyInstaller 失败（$LASTEXITCODE）" }

& $iscc (Join-Path $packaging 'installer.iss')
if ($LASTEXITCODE) { throw "Inno Setup 编译失败（$LASTEXITCODE）" }
Get-ChildItem (Join-Path $packaging 'output') -Filter *.exe | Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
    ForEach-Object { '安装程序：{0}（{1:N1} MB）' -f $_.FullName, ($_.Length / 1MB) }
