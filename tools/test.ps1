# 一键运行网页、Max 执行器、服务端三套自动化测试。
# 用法：& "<工作台目录>\tools\test.ps1"
# 临时目录全在仓库的 tests\tmp 下，不写 C 盘；全部通过后清理本次留下的临时子目录，失败时保留现场。
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
$parent = Split-Path $root -Parent
$repo = Split-Path $root -Leaf
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
$node = Get-Command node -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $node) { throw '找不到 node，无法运行网页测试' }

$tmpRoot = Join-Path $root 'tests\tmp'
$before = @{}
if (Test-Path -LiteralPath $tmpRoot) {
    Get-ChildItem -LiteralPath $tmpRoot -Force | ForEach-Object { $before[$_.Name] = $true }
}
$sysTmp = Join-Path $tmpRoot '_sys'
New-Item -ItemType Directory -Force $sysTmp | Out-Null

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:TEMP = $sysTmp
$env:TMP = $sysTmp

$suites = @(
    @{ Name = '网页测试'; File = $node.Source; Args = @('--test', "$repo/tests/web") }
    @{ Name = 'Max 执行器测试'; File = $python; Args = @('-B', '-m', 'unittest', 'discover', '-s', "$repo/max/tests", '-p', 'test_*.py') }
    @{ Name = '服务端测试'; File = $python; Args = @('-B', '-m', 'unittest', 'discover', '-s', "$repo/tests", '-p', 'test_*.py') }
)

$results = @()
Push-Location $parent
try {
    foreach ($suite in $suites) {
        Write-Host "`n=== $($suite.Name) ==="
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $cmdArgs = $suite.Args
        & $suite.File @cmdArgs
        $code = $LASTEXITCODE
        $watch.Stop()
        $results += [PSCustomObject]@{
            Name = $suite.Name
            Ok = ($code -eq 0)
            Code = $code
            Seconds = $watch.Elapsed.TotalSeconds
        }
    }
} finally {
    Pop-Location
}

Write-Host "`n=== 测试汇总 ==="
foreach ($item in $results) {
    $label = if ($item.Ok) { '通过' } else { "失败（退出码 $($item.Code)）" }
    Write-Host ('{0}：{1}（{2:N1}s）' -f $item.Name, $label, $item.Seconds)
}

$failed = @($results | Where-Object { -not $_.Ok })
if ($failed.Count -eq 0) {
    # 全部通过：清理本次运行在 tests\tmp 下留下的临时目录和系统临时目录
    if (Test-Path -LiteralPath $tmpRoot) {
        Get-ChildItem -LiteralPath $tmpRoot -Force | Where-Object { $_.Name -eq '_sys' -or -not $before.ContainsKey($_.Name) } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Write-Host '全部测试通过，已清理本次临时目录。'
    exit 0
}

Write-Host "有 $($failed.Count) 项测试失败，已保留 tests\tmp 现场。"
exit 1
