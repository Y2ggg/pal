# 安装 PAL

运行包包含 Python 和 PAL 运行依赖。按系统和 CPU 选择对应文件；不包含 Claude Code/Codex。
先停止旧版本服务：`pal web stop`。通过 uv 安装过的用户先运行 `uv tool uninstall pal`。

macOS / Linux：解压后在终端运行 `sh install.sh`。命令安装到 `~/.local/bin/pal`，请将
`~/.local/bin` 加入 PATH；程序目录为 `~/.local/share/pal-program`。可以通过
`PAL_INSTALL_DIR`、`PAL_BIN_DIR` 指定其他绝对路径。卸载时运行安装目录内的 `uninstall.sh`。

Windows：解压到本地磁盘，在 PowerShell 中运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`。
默认安装到 `%LOCALAPPDATA%\Programs\PAL`，加入当前用户 PATH；新开终端生效。
可以用 `-InstallDir` 指定其他目录。卸载时运行安装目录内的 `uninstall.ps1`。

运行 `pal --version` 确认版本，再执行 `pal quickstart` 配置能力库；运行 `pal web` 打开控制台。
安装和卸载程序均保留业务库、PAL 配置和已有 CLI 插件。切换版本后可在控制台的“CLI 与系统”
更新系统创建入口；业务 Skill 的发布和挂载仍须手动执行。

程序未做 Apple 公证或 Windows Authenticode 签名。仅从官方 Release 下载并核对 SHA256SUMS；
受组织设备策略限制时遵循本机管理要求，不关闭全局安全策略。
