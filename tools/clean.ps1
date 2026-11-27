# 清理测试临时目录、打包中间目录和 Python 缓存。支持 -WhatIf 预览。
# 用法：& "<工作台目录>\tools\clean.ps1" [-WhatIf]
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$targets = New-Object System.Collections.Generic.List[string]

$tmpRoot = Join-Path $root 'tests\tmp'
if (Test-Path -LiteralPath $tmpRoot) {
    Get-ChildItem -LiteralPath $tmpRoot -Force | ForEach-Object { $targets.Add($_.FullName) }
}
foreach ($rel in 'packaging\build', 'packaging\dist', 'max\tests\_t') {
    $path = Join-Path $root $rel
    if (Test-Path -LiteralPath $path) { $targets.Add($path) }
}

# 递归查找 __pycache__，跳过 pylibs、.venv、.git、exchange 以及上面已列入清理的目录
$skipDirs = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($rel in 'pylibs', '.venv', '.git', 'exchange', 'tests\tmp', 'packaging\build', 'packaging\dist', 'max\tests\_t') {
    [void]$skipDirs.Add((Join-Path $root $rel))
}
function Find-PyCache([string]$dir) {
    foreach ($child in Get-ChildItem -LiteralPath $dir -Directory -Force -ErrorAction SilentlyContinue) {
        if ($skipDirs.Contains($child.FullName)) { continue }
        if ($child.Name -eq '__pycache__') {
            $child.FullName
        } else {
            Find-PyCache $child.FullName
        }
    }
}
Find-PyCache $root | ForEach-Object { $targets.Add($_) }

$removed = 0
foreach ($target in $targets) {
    if ($PSCmdlet.ShouldProcess($target, '删除')) {
        Remove-Item -LiteralPath $target -Recurse -Force
        $removed++
    }
}

if ($targets.Count -eq 0) {
    Write-Host '没有需要清理的目录。'
} elseif ($WhatIfPreference) {
    Write-Host "预览：共 $($targets.Count) 项待清理。"
} else {
    Write-Host "已清理 $removed 项。"
}
