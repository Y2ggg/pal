param([string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\PAL'))
$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if (Test-Path $InstallDir) {
    if ((Get-Item $InstallDir).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '安装目录不能是链接。' }
    if (!(Test-Path (Join-Path $InstallDir '.pal-install'))) { throw '目录不属于 PAL 安装器，请另选目录。' }
    & (Join-Path $InstallDir 'runtime\pal.exe') web stop
    if ($LASTEXITCODE -ne 0) { throw '请先停止 PAL Web 服务。' }
}
$parent = Split-Path $InstallDir -Parent
New-Item -ItemType Directory -Force $parent | Out-Null
$stage = Join-Path $parent ('.pal-install-' + [guid]::NewGuid().ToString('N'))
$backup = "$stage.previous"
New-Item -ItemType Directory $stage | Out-Null
try {
    foreach ($item in @('runtime', 'licenses', 'LICENSE', 'THIRD-PARTY-NOTICES.md', 'BUILD.json', 'uninstall.ps1')) {
        Copy-Item (Join-Path $PSScriptRoot $item) $stage -Recurse
    }
    Set-Content (Join-Path $stage '.pal-install') 'PAL user installation' -Encoding ASCII
    & (Join-Path $stage 'runtime\pal.exe') --version
    if ($LASTEXITCODE -ne 0) { throw '安装包运行验证失败。' }
    if (Test-Path $InstallDir) { Move-Item $InstallDir $backup }
    try { Move-Item $stage $InstallDir } catch {
        if (Test-Path $backup) { Move-Item $backup $InstallDir }
        throw
    }
    $bin = Join-Path $InstallDir 'runtime'
    $entries = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object { $_ })
    if ($entries -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', (($entries + $bin) -join ';'), 'User')
    }
    $env:Path = "$bin;$env:Path"
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
    Write-Host 'PAL 已安装。请新开终端并运行 pal quickstart。'
} finally {
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
}
