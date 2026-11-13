#requires -Version 7
<#
  SWAT+ 水文建模工作台 桌面启动器 · 构建脚本（PowerShell 7）
  用法：
    & 'C:\Program Files\PowerShell\7\pwsh.exe' -NoProfile -File '.\Workbench\launcher\build.ps1' -NoDeploy
  编译便携启动器。完整程序与安装包由 distribution/build_release.py 构建。
#>
[CmdletBinding()]
param(
    [switch]$NoDeploy   # 只编译到 launcher 目录，不复制到桌面
)
$ErrorActionPreference = 'Stop'
$Dir = $PSScriptRoot
$ExeName = 'SWATPlus Workbench.exe'
$ExePath = Join-Path $Dir $ExeName
$CsPath = Join-Path $Dir 'Launcher.cs'
$IcoPath = Join-Path $Dir 'launcher.ico'

# 1) 图标：缺失时重新生成
if (-not (Test-Path $IcoPath)) {
    & (Join-Path $Dir 'make_icon.ps1')
}

# 2) 源码统一为 UTF-8 BOM，保证旧版 csc 正确读取中文字符串
$utf8Bom = [System.Text.UTF8Encoding]::new($true)
$src = [System.IO.File]::ReadAllText($CsPath, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($CsPath, $src, $utf8Bom)

# 3) 查找本机 C# 编译器：.NET Framework csc → VS Roslyn → dotnet SDK Roslyn
$csc = $null
foreach ($p in @(
    (Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)) { if (Test-Path $p) { $csc = $p; break } }
if (-not $csc) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $roslyn = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\Roslyn\csc.exe' | Select-Object -First 1
        if ($roslyn -and (Test-Path $roslyn)) { $csc = $roslyn }
    }
}
if (-not $csc) {
    $sdkRoot = Join-Path $env:ProgramFiles 'dotnet\sdk'
    if (Test-Path $sdkRoot) {
        $latest = Get-ChildItem $sdkRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
        $cscDll = Join-Path $latest.FullName 'Roslyn\bincore\csc.dll'
        if (Test-Path $cscDll) { $csc = "dotnet-exec:$cscDll" }
    }
}
if (-not $csc) { throw '未找到可用的 C# 编译器（.NET Framework csc / VS Roslyn / dotnet SDK）。' }
Write-Host "编译器: $csc"

# 4) 编译为 Windows GUI 子系统 EXE（无控制台），并内嵌图标
if (Test-Path $ExePath) { Remove-Item $ExePath -Force }
$cscArgs = @('/nologo', '/target:winexe', '/platform:anycpu', '/optimize+', ('/win32icon:' + $IcoPath), ('/out:' + $ExePath), $CsPath)
if ($csc -like 'dotnet-exec:*') {
    & dotnet ($csc.Substring('dotnet-exec:'.Length)) @cscArgs
} else {
    & $csc @cscArgs
}
if ($LASTEXITCODE -ne 0) { throw '编译失败，请检查上方输出。' }

# 5) 便携启动器需和 Workbench、runtime 并列放置。
if (-not $NoDeploy) {
    Write-Host '请使用 distribution/build_release.py 构建完整分享包，安装器会创建桌面快捷方式。'
}
Write-Host ("构建产物:   {0}" -f $ExePath)
