$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$installDir = [IO.Path]::GetFullPath($PSScriptRoot)
$bin = Join-Path $installDir 'runtime'
$marker = Join-Path $installDir '.pal-install'
if (!(Test-Path $marker -PathType Leaf)) { throw '安装记录未知或已损坏，拒绝卸载；请核对安装目录。' }
$lines = @(Get-Content $marker -Encoding UTF8)
if ($lines.Count -ne 3 -or $lines[0] -ne 'PAL-INSTALL-V1' -or
    ![String]::Equals([IO.Path]::GetFullPath($lines[1]), $installDir, [StringComparison]::OrdinalIgnoreCase) -or
    ![String]::Equals([IO.Path]::GetFullPath($lines[2]), $bin, [StringComparison]::OrdinalIgnoreCase) -or
    !(Test-Path (Join-Path $installDir 'runtime\pal.exe') -PathType Leaf) -or
    !(Test-Path (Join-Path $installDir 'BUILD.json') -PathType Leaf) -or
    !(Test-Path (Join-Path $installDir 'uninstall.ps1') -PathType Leaf)) {
    throw '安装记录未知或已损坏，拒绝卸载；请核对安装目录。'
}
$ErrorActionPreference = 'Continue'
$stopOutput = & (Join-Path $PSScriptRoot 'runtime\pal.exe') web stop 2>&1
if ($LASTEXITCODE -ne 0 -and ($stopOutput -join '') -notlike '*没有可关闭的 PAL Web 服务记录*') { throw '请先停止 PAL Web 服务。' }
$ErrorActionPreference = 'Stop'
$entries = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object { $_ -and $_ -ne $bin })
[Environment]::SetEnvironmentVariable('Path', ($entries -join ';'), 'User')
Remove-Item $PSScriptRoot -Recurse -Force
Write-Host 'PAL 程序已卸载。库、配置和 CLI 插件保留。'
