# PAL 协作入口

本文件与 `AGENTS.md` 同步维护。开始工作先完整阅读 `AGENTS.md`、`docs/PRODUCT.md`、
`docs/ARCHITECTURE.md`、`CONTRIBUTING.md`；这些文件在公开仓库内自包含，不需要作者本机环境。

多库/自定义配置的选择顺序以 `docs/MAINTENANCE.md` 为准；不要假设省略参数总选默认库。
源码命令与 PATH 中已安装的 `pal` 可能不同，原生 CLI 验收须核对实际运行构建。

## 产品与工程边界

- 普通 Claude Code/Codex 内创建、更新和使用 Skill；两类挂载各司其职，完整覆盖目标 CLI。
- 开发 → 生产 → CLI 挂载均手动触发；单项操作不带入其他修改，全库同步须明确。
- 卸载保留内容，放弃修改恢复生产对应开发版，删除挂载内容时保留待完成状态。
- 日常记录、分析、评分和自动迭代延期；不恢复用户快照、任意回退、回收站或批量发布。
- 修改方案先核对要求、事实与候选状态；不将技术限制改写为用户前提。
- 文档逐项核对源码与实测结果，区分当前、历史和未验证能力；链接有效不能代替内容复核。
  修正文档后同步发行候选和包内说明，避免网站、源码与安装包各说一套。
- schema 变更保持旧数据可读；不可变投影不原地改写，程序版本与格式版本分离。
- 故障拒绝继续，未知不算健康；锁序 maintenance → publication → activation。
- 安装通过官方 CLI 与 PAL 状态核查，不靠修改用户全局配置绕过协议。
- 测试使用临时库，禁止修改真实安装；实机验收需要独立隔离与明确授权。
- 0.3.0 正在验收 macOS/Linux/原生 Windows，平台安装与模型任务分开报告。CLI 挂载变化须新开会话。
  Web 仅供本机同源访问，不公开转发。

## 常用检查与导航

```sh
uv sync --locked --extra test
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest
```

代码导航见 `docs/ARCHITECTURE.md`。自动化、浏览器、CLI 原生验证分别报告，不能复用旧结果
声称新构建已经通过。测试防护位于 `tests/conftest.py`，不要关闭它以执行真实插件安装。

发行前从解压后的 sdist 验证 `uv sync --locked --extra test`；CI action 固定到完整提交 SHA，
附注标签需解析到 commit。发行源码、依赖和文档应与实际验证对象一致。

协作/工程规则变化必须同次修改 `AGENTS.md` 与 `CLAUDE.md`；行为变化同步 README、快速开始、
使用手册和 CHANGELOG。文档/用户输出用简体中文；机器字段与标识用英文。保留源码中的追踪
标识，新代码说明产品或验收边界。提交前检查凭据、个人路径和私有内容；公开上传或发布需
明确授权，先完成可审阅的本地候选与验证。

跨平台文件锁在 `locking.py`，原生路径/进程/外部 CLI 启动在 `platform_support.py`。
包构建与安装器在 `packaging/`；发行规则见 `docs/RELEASING.md`。新建库使用 portable-1
路径 schema 标识，旧 v1 catalog 保持原字节；禁止通过重新写入摘要绕过旧材料校验。
