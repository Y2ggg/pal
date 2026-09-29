# PAL 0.3.0 · 三平台发行候选

PAL（Personal Ability Library，个人能力库）在普通 Claude Code/Codex 中创建、更新与使用
Skill，在本地 Web 查看完整文件、发布、挂载和维护。采用 MIT 许可。

0.3.0 新增原生 Windows 支持，并提供 Windows x64、macOS Apple Silicon/Intel、Linux x64
独立运行包，包含 Python 与运行依赖，同时保留 wheel 和源码包。沿用现有仓库历史；0.2.x 为
内部交付/历史候选，未以正式 Release 发行。

系统创建入口为 `0.3.0+native.8`，补充 Windows 临时目录、PowerShell 路径引用及 UTF-8 写入规则。
新建库采用 portable-1 schema 标识；旧 v1 schema、既有投影按原字节验证，不原地改写。
程序升级后重启 Web，在“CLI 与系统”更新系统入口；业务发布/挂载仍由用户手动触发。

本版验证进行中，尚未创建正式 Release。最终测试数量、原生安装矩阵与下载摘要将在通过门禁后
补入本说明。已完成的 macOS 隔离运行包及双端插件安装不能代替尚未完成的平台验收。

模型任务验收与自动化/真实插件安装分别报告。Windows/Linux 本轮不调用模型；此前 macOS
模型任务证据仍只代表当时版本。分析、评分、日常记录和自动迭代未开放。

运行包未做 Apple 公证或 Windows Authenticode 签名。Linux 包要求 glibc 2.35+；不提供 musl、
Windows ARM64 或 Linux ARM64 原生包。Windows 仅支持本地磁盘目录，不支持 UNC、重解析点、
设备路径和保留文件名。库与配置不支持跨 OS 直接搬迁。Skill 自带外部工具仍需自行安装。

安装、更新、卸载及 PATH 说明见[使用手册](USER-GUIDE.md)。Web 仅供本机单用户使用，
不支持远程转发或账号隔离。CLI 挂载变化后需新开会话。
