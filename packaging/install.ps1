param([string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\PAL'))
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$bin = Join-Path $InstallDir 'runtime'
function Test-PalInstall([string]$Root) {
    $marker = Join-Path $Root '.pal-install'
    if (!(Test-Path $marker -PathType Leaf)) { return $false }
    $lines = @(Get-Content $marker -Encoding UTF8)
    if ($lines.Count -ne 3 -or $lines[0] -ne 'PAL-INSTALL-V1') { return $false }
    if (![String]::Equals([IO.Path]::GetFullPath($lines[1]), $Root, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if (![String]::Equals([IO.Path]::GetFullPath($lines[2]), (Join-Path $Root 'runtime'), [StringComparison]::OrdinalIgnoreCase)) { return $false }
    return (Test-Path (Join-Path $Root 'runtime\pal.exe') -PathType Leaf) -and
        (Test-Path (Join-Path $Root 'BUILD.json') -PathType Leaf) -and
        (Test-Path (Join-Path $Root 'uninstall.ps1') -PathType Leaf)
}
if (Test-Path $InstallDir) {
    if ((Get-Item $InstallDir).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '安装目录不能是链接。' }
    if (!(Test-PalInstall $InstallDir)) { throw '安装目录不属于此版本安装器或安装记录已损坏；请另选目录。' }
    $ErrorActionPreference = 'Continue'
    $stopOutput = & (Join-Path $InstallDir 'runtime\pal.exe') web stop 2>&1
    if ($LASTEXITCODE -ne 0 -and ($stopOutput -join '') -notlike '*没有可关闭的 PAL Web 服务记录*') { throw '请先停止 PAL Web 服务。' }
$ErrorActionPreference = 'Stop'
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
    Set-Content (Join-Path $stage '.pal-install') @('PAL-INSTALL-V1', $InstallDir, $bin) -Encoding UTF8
    & (Join-Path $stage 'runtime\pal.exe') --version
    if ($LASTEXITCODE -ne 0) { throw '安装包运行验证失败。' }
    if (Test-Path $InstallDir) { Move-Item $InstallDir $backup }
    try { Move-Item $stage $InstallDir } catch {
        if (Test-Path $backup) { Move-Item $backup $InstallDir }
        throw
    }
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
