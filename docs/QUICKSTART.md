# PAL 快速开始

产品名称为 PAL · 个人能力库（Personal Ability Library）；命令、包名、插件调用名和配置路径
统一使用 PAL 标识。控制台已采用完整 pal 字标和明暗主题标识。

适用：PAL 0.3.0；提供 macOS、Linux 与原生 Windows 运行包。
需要已安装并配置的 Claude Code 与 Codex；PAL 运行包自带 Python，无需 uv。
代码最低 CLI 门槛为 Claude Code 2.1.205、Codex 0.147.0；版本达标仍需能力探针通过。

## 设置一次

从 [Releases](https://github.com/Y2ggg/pal/releases) 选择系统/CPU 对应运行包并解压。
Windows x64 在 PowerShell 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`；
macOS（Apple Silicon 或 Intel）、Linux x64 执行 `sh install.sh`。
新开终端；macOS/Linux 确保 `~/.local/bin` 在 PATH。运行：

```sh
pal --version
pal quickstart
```

源码或 wheel 安装需要 Python 3.11+ 和 uv：源码根目录执行 `uv tool install .`，
或 `uv tool install ./pal-0.3.0-py3-none-any.whl`。此方式找不到命令时运行 `uv tool update-shell`。
平台要求、验证范围、安装位置和卸载见[使用手册](USER-GUIDE.md)。

选择独立的 Skill 库保存位置并确认，不要选择程序源码或普通业务项目目录。已有 PAL 库会复用，
新路径或空目录会初始化为空库。向导准备两端系统创建入口和默认库绑定；保留检查进度，最终
显示中文摘要，不创建 Skill、不发布、不改变待同步内容。
自动化答案模式 `--answers` 继续输出 JSON，详见维护说明。
设置失败时会列出已完成步骤；修复所报问题后使用相同路径重试，不表示已经完成的安装会自动撤销。

## 在 CLI 中创建和使用

从任意业务目录运行 `claude` 或 `codex`，调用：

- Codex：`$pal-create-skill`。
- Claude Code：`/pal:pal-create-skill`。

告诉 CLI 要创建或更新哪个 Skill。可携带 `references/`、`scripts/`、`assets/` 等完整资源。
以开发提交成功为保存标志；会话中的草稿不等于已经入库。只要求创建或更新时，保存到开发库；
再说“发布这个 Skill”，保存到生产库；说“同步这个 Skill 到 CLI”，只挂载所选生产内容。
明确要求“发布并同步”可连续执行；此前主动卸载的 Skill 仍
保持停用，需要明确要求“重新挂载”，或在 Web 点击“挂载到 CLI”。

同步完成后**新开会话**使用生产 Skill。已经打开的会话不会热加载。
从 CLI 的 Skill 列表选择实际名称，不按文件夹名拼接；常见调用名为 `pal-<库ID>:<SkillID>`，
Codex 前加 `$`、Claude Code 前加 `/`，命名规则见[使用手册](USER-GUIDE.md)。
全库 `pal sync` 会处理所有启用成员及待完成删除，只在你明确需要整个库时使用。
存在多个库或自定义配置目录时，先按[维护说明](MAINTENANCE.md)明确操作对象；
Skill 格式限制见[使用手册](USER-GUIDE.md)，不保证任意第三方 Skill 原样导入。

## 打开控制台

```sh
pal web
```

默认 `http://127.0.0.1:8787`，服务占用当前终端。在 Skill 集合逐项发布、挂载、卸载；名称和
“查看详情”打开概览及真实文件目录。读取中显示圆环与进度文字，“减少动画”模式保留静态提示。
开发、生产、CLI 挂载按三个编号步骤展示。

卸载保留生产，可再次挂载；放弃修改恢复当前生产版；删除有 CLI 挂载时显示“删除待完成”，
继续明确卸载后才退出列表。主动卸载且已处理完成不算“未同步”。
卸载不撤回旧会话已加载的内容，需新开会话确认。

“CLI 与系统”管理系统入口升级与挂载修复；“库与维护”分别检查开发、生产、上次挂载
内容，并提供异常恢复。若提示“源文件异常”，按页面指引恢复源目录；不会提供无效的自动修复。
顶部“使用说明”可打开侧边抽屉，右上角可切换夜间模式。

## 升级和关闭

如果 Web 正在运行，先在另一终端执行 `pal web stop`；没有运行则跳过。再在新版源码根目录
运行（或将 `.` 换成本地新版 wheel）；运行包用户重新运行新版安装脚本：

运行包升级须沿用原安装目录和命令目录；自定义过 POSIX 的 `PAL_INSTALL_DIR`/`PAL_BIN_DIR`
或 Windows 的 `-InstallDir` 时继续使用原值。需要迁移位置时，先从原安装目录卸载，再按新位置安装。

```sh
uv tool install --force --reinstall .
pal web
```

然后在“CLI 与系统”按提示更新创建入口到当前版本，并打开新会话。升级入口不会同步业务 Skill。
当前入口为 `0.3.0+native.8`；`pal --version` 可查看 PAL 版本和构建。

换端口：`pal web --port 8899`；关闭该服务：`pal web stop --port 8899`。
`--port 0` 自动选端口，`--no-browser` 只启动服务。端口占用会显示当前地址；同库同构建可复用，
旧构建提示重启。关闭网页不会停止服务。Web 只接受本机同源访问，不支持远程代理或公开转发。
自动选端口后，停止时也要填写启动输出的实际端口。

分析、评分和日常记录延期。详细说明见[使用手册](USER-GUIDE.md)，底层及旧工具见
[维护与兼容命令](MAINTENANCE.md)。

操作中断时按控制台指引处理；恢复方法见[使用手册](USER-GUIDE.md)。
