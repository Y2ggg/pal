# PAL 0.4.0 · DeepSeek Harness 第三目标

PAL（Personal Ability Library，个人能力库）在 Claude Code、Codex 与 DeepSeek Harness 中
创建、更新和使用 Skill，在本地 Web 查看完整文件、发布、挂载和维护。采用 MIT 许可。

0.4.0 把 DeepSeek Harness（DSH）加入为与 Claude Code、Codex 平级的第三目标；库格式升到
2.0，旧库通过一次 `pal migrate` 迁移。已发布标签和资产不覆盖。

## 下载与安装

从 [v0.4.0 Release](https://github.com/Y2ggg/pal/releases/tag/v0.4.0) 下载对应文件并核对
SHA256SUMS。

| 系统 | 安装包 |
|---|---|
| Windows x64 | pal-0.4.0-windows-x86_64.zip |
| macOS Apple Silicon | pal-0.4.0-macos-arm64.tar.gz |
| macOS Intel | pal-0.4.0-macos-x86_64.tar.gz |
| Linux x64 | pal-0.4.0-linux-x86_64.tar.gz |

运行包包含 Python 3.11.15、运行依赖和安装/卸载脚本；同时提供 wheel 和源码包。
升级须沿用原安装目录和命令目录；卸载保留用户库、配置和 CLI 插件。

## 本版变化

- 新增 DSH 第三目标：库格式 2.0、`skill-md-v1-shared` 共享 profile、三目标创建/发布/挂载。
- DSH 使用 `~/.dsh/skills` 文件系统技能根，`/` 菜单可发现已挂载 Skill；创建入口为
  `/pal-create-skill`。DSH 不需要 Claude Code/Codex 存在。
- 新增 `pal migrate`：先备份库与配置，再为每个已发布 Skill 生成三目标 revision/release；
  旧记录、旧生产版本和旧投影保持原字节可读。
- 系统创建入口更新为 `0.4.0+native.9`；三目标可独立安装，缺少某个 CLI 不阻塞其他目标。
- 安装监测与 Web 增加 DSH 文件健康；DSH 不提供 `pal run` 运行通道，其 P2 以 `/` 实际
  加载为准。

程序升级后重启 Web，在“CLI 与系统”更新系统入口。开发提交、发布和 CLI 挂载仍分别手动触发。

## 迁移

0.3.0 库需执行：

```sh
pal migrate --library /path/to/library
```

迁移前自动备份到 `PAL Migration Backups`；失败恢复备份。迁移会新建三目标 revision/release，
不改写旧记录。旧两项目标记录仍可只读校验，但不会挂载到 DSH。

## 验证

本机全量 449 项收集，441 通过、8 跳过；Ruff 通过。正式库迁移、三端安装健康、
DSH 文件校验和旧生产只读校验通过，系统入口更新为 `0.4.0+native.9`。
三平台 CI 与安装包生命周期需在公开仓 PR 的矩阵完成后补充；未完成的平台结果不写成通过。

DSH 的文件发现与 `/` 目录已验证；本轮未调用模型，不能把文件发现当作 Skill 效果测试。

## 边界

DSH 的 P2 以用户新会话 `/` 加载为准，不提供自动模型运行或 usage JSONL；`pal run` 和
`launch use --cli dsh` 明确拒绝。三层手动流程、主动卸载语义和日常记录/P3 延期保持。

---

# PAL 0.3.0 · 原生三平台发行


PAL（Personal Ability Library，个人能力库）在普通 Claude Code/Codex 中创建、更新与使用
Skill，在本地 Web 查看完整文件、发布、挂载和维护。采用 MIT 许可。

0.3.0 是首个正式 Release，新增原生 Windows 支持。保留现有仓库历史；0.2.x 为内部交付和历史
公开候选，没有正式 tag/Release。后续发布遵循语义版本规则，已发布标签和资产不覆盖。

## 下载与安装

从 [v0.3.0 Release](https://github.com/Y2ggg/pal/releases/tag/v0.3.0) 下载对应文件并核对 SHA256SUMS。

| 系统 | 安装包 |
|---|---|
| Windows x64 | pal-0.3.0-windows-x86_64.zip |
| macOS Apple Silicon | pal-0.3.0-macos-arm64.tar.gz |
| macOS Intel | pal-0.3.0-macos-x86_64.tar.gz |
| Linux x64 | pal-0.3.0-linux-x86_64.tar.gz |

运行包包含 Python 3.11.15、运行依赖和安装/卸载脚本，不需要先安装 Python。
同时提供普通 wheel 和源码包。包内 INSTALL.md 说明安装步骤；升级须沿用原安装目录和命令目录，
迁移位置时先从原位置卸载。卸载保留用户库、配置和 CLI 插件。

## 本版变化

- 增加 Windows 原生文件锁、持久写入、进程检查、路径校验和官方 CLI 启动适配。
- 系统创建入口更新为 `0.3.0+native.8`，补充 Windows 临时目录、PowerShell 路径引用和 UTF-8 写入。
- 新建库采用 portable-1 schema 标识；旧 v1 schema 和既有投影按原字节验证，不原地改写。
- Windows Web 独占监听端口；重复启动通过轻量身份接口识别已有服务，保持旧版本提示和库匹配检查。
- 安装器核验受管目录和安装记录，拒绝损坏记录与错误目录升级，避免误覆盖或遗留旧命令。

程序升级后重启 Web，在“CLI 与系统”更新系统入口。开发提交、发布和 CLI 挂载仍分别手动触发，
不因程序升级而自动同步业务 Skill。分析、评分、日常记录和自动迭代未开放。

## 验证

[完整 CI](https://github.com/Y2ggg/pal/actions/runs/36551008760) 与四包构建来源为
`e7d871fcdba12edc1e16bd9487a18cf99fd969f3`；发行前的后续提交仅整理说明，不改运行源码、测试或依赖。
完整测试范围为 445 项：

| 环境 | Python | 每版结果 |
|---|---|---|
| macOS | 3.11、3.14 | 435 通过 / 10 跳过 |
| Ubuntu 22.04 | 3.11、3.14 | 435 通过 / 10 跳过 |
| 原生 Windows | 3.11、3.14 | 441 通过 / 4 跳过 |
| 本机解包 sdist | 3.11 | 437 通过 / 8 跳过 |

Windows 每个 Python 的两个互斥分组覆盖全部用例。跳过项仅为平台专属用例及无 Codex runner
上的两项依赖用例；这两项在本机实际执行。独立 sdist 完成 locked sync、Ruff 和全量测试。

四包分别在 Windows Server 2022 x64、macOS 15.7.9 两种架构、Ubuntu 22.04 x64 构建并验证：
安装、升级、完整多文件创建、发布、完整性检查、Web 复用/原端口重启/关闭、卸载与数据保留。
另通过独立 home 验证 Claude Code 2.1.234、Codex 0.158.0 的官方系统及业务插件安装与文件健康。
下载后核对 BUILD.json 来源、全部文件摘要和许可证。

模型任务验收与自动化/真实插件安装分别报告。本轮未调用模型；此前 macOS 模型任务证据仅代表
当时版本。CLI 安装成功不能代替 Skill 自身的效果测试。

## 边界

运行包未做 Apple 公证或 Windows Authenticode 签名。macOS 在 15 验证，更早版本尚未验证；
Linux 要求 glibc 2.35+，不提供 musl、Windows ARM64 或 Linux ARM64 原生包。
Windows 仅支持本地磁盘目录，不支持 UNC、重解析点、设备路径和保留文件名。
库与配置不支持跨 OS 直接搬迁。Skill 自带外部工具仍需自行安装。

Web 仅供本机单用户使用，不支持远程转发或账号隔离。CLI 挂载变化后需新开会话。
安装、更新及维护详见[使用手册](USER-GUIDE.md)。
