# 一键构建 Panda Studio（原白底工作台）安装程序。
# 用法：在 PowerShell 里运行  & "<工作台目录>\packaging\build.ps1"
# 需要：Python 3.13（包见 requirements-build.txt；仓库里有 .venv 时优先用它）、Inno Setup 6（D:\Tools\InnoSetup6，或 PATH / 注册表里能找到的）。
# 版本号只改仓库根目录的 VERSION：exe 版本信息、安装程序版本、/api/health 和界面左下角都从它来。
# 输出：packaging\output\PandaStudio-安装程序-<版本>.exe
# PyInstaller 把进度信息写到 stderr，PowerShell 5.1 会当成错误；一律按退出码判断成败
$ErrorActionPreference = 'Continue'
$packaging = $PSScriptRoot
$root = Split-Path $packaging -Parent
$version = (Get-Content (Join-Path $root 'VERSION') -Raw -Encoding UTF8).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION 格式不对：$version" }

function Find-Iscc {
    $candidates = @('D:\Tools\InnoSetup6\ISCC.exe')
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { $candidates += $command.Source }
    foreach ($key in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
                     'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
                     'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1') {
        $location = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallLocation
        if ($location) { $candidates += Join-Path $location 'ISCC.exe' }
    }
    $candidates += "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                   "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
$iscc = Find-Iscc
if (-not $iscc) { throw '找不到 Inno Setup 6 的 ISCC.exe' }
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

Set-Location $root
# 只清理本脚本自己生成的构建目录
foreach ($dir in 'build', 'dist') {
    $path = Join-Path $packaging $dir
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
# 缓存和临时文件都放在 packaging\build\cache，不写 C 盘
$cache = Join-Path $packaging 'build\cache'
New-Item -ItemType Directory -Force (Join-Path $cache 'tmp') | Out-Null
$env:PIP_CACHE_DIR = Join-Path $cache 'pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $cache 'pyinstaller'
$env:TEMP = Join-Path $cache 'tmp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'

"Panda Studio $version（Python：$python）"
& $python -m PyInstaller (Join-Path $packaging 'whitebg.spec') --noconfirm --clean `
    --distpath (Join-Path $packaging 'dist') --workpath (Join-Path $packaging 'build')
if ($LASTEXITCODE) { throw "PyInstaller 失败（$LASTEXITCODE）" }

$dist = Join-Path $packaging 'dist\PandaStudio'
# 发布前检查：安装包里的文本文件不能出现公司素材路径、本机用户名等（同 tools\export_public.py 的 FORBIDDEN）
$forbidden = '(?<![A-Za-z0-9])sk-(?!fake)[A-Za-z0-9]{16,}'
$problems = Get-ChildItem $dist -Recurse -File -Include *.json, *.js, *.mjs, *.cjs, *.html, *.css, *.py, *.ms, *.txt |
    Select-String -Pattern $forbidden -CaseSensitive -Encoding UTF8 |
    ForEach-Object { '{0}:{1}: {2}' -f $_.Path.Substring($dist.Length + 1), $_.LineNumber, $_.Matches[0].Value }
if ($problems) { throw "安装包里有不该带的内容：`n$(($problems | Select-Object -First 40) -join "`n")" }
$files = Get-ChildItem $dist -Recurse -File
'程序文件夹：{0} 个文件，{1:N1} MB' -f $files.Count, (($files | Measure-Object Length -Sum).Sum / 1MB)

New-Item -ItemType Directory -Force $env:TEMP | Out-Null
& $iscc "/DAppVersion=$version" (Join-Path $packaging 'installer.iss')
if ($LASTEXITCODE) { throw "Inno Setup 编译失败（$LASTEXITCODE）" }
Get-Item (Join-Path $packaging "output\PandaStudio-安装程序-$version.exe") |
    ForEach-Object { '安装程序：{0}（{1:N1} MB）' -f $_.FullName, ($_.Length / 1MB) }
