$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if (!(Test-Path (Join-Path $PSScriptRoot '.pal-install'))) { throw '请运行安装目录内的卸载脚本。' }
$ErrorActionPreference = 'Continue'
$stopOutput = & (Join-Path $PSScriptRoot 'runtime\pal.exe') web stop 2>&1
if ($LASTEXITCODE -ne 0 -and ($stopOutput -join '') -notlike '*没有可关闭的 PAL Web 服务记录*') { throw '请先停止 PAL Web 服务。' }
$ErrorActionPreference = 'Stop'
$bin = Join-Path $PSScriptRoot 'runtime'
$entries = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object { $_ -and $_ -ne $bin })
[Environment]::SetEnvironmentVariable('Path', ($entries -join ';'), 'User')
Remove-Item $PSScriptRoot -Recurse -Force
Write-Host 'PAL 程序已卸载。库、配置和 CLI 插件保留。'
