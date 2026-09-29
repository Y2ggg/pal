$ErrorActionPreference = 'Stop'
if (!(Test-Path (Join-Path $PSScriptRoot '.pal-install'))) { throw '请运行安装目录内的卸载脚本。' }
& (Join-Path $PSScriptRoot 'runtime\pal.exe') web stop
if ($LASTEXITCODE -ne 0) { throw '请先停止 PAL Web 服务。' }
$bin = Join-Path $PSScriptRoot 'runtime'
$entries = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object { $_ -and $_ -ne $bin })
[Environment]::SetEnvironmentVariable('Path', ($entries -join ';'), 'User')
Remove-Item $PSScriptRoot -Recurse -Force
Write-Host 'PAL 程序已卸载。库、配置和 CLI 插件保留。'
