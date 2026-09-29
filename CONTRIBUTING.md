# 参与 PAL

欢迎报告问题、改进文档和提交修复。请先阅读 [产品范围](docs/PRODUCT.md) 和
[架构说明](docs/ARCHITECTURE.md)，了解开发、生产和 CLI 挂载的手动边界。

## 本地开发

需要 Python 3.11+ 和 uv。建议使用仓库或完整源码包，其中包含 CI 与双 CLI 协作入口；Python
sdist 同样包含测试和 `uv.lock`，支持以下开发检查。在项目根目录运行：

```sh
uv sync --locked --extra test
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest
```

自动化测试使用临时库及 CLI 替身，不需要模型凭据，也不应修改真实安装。测试的全局防护会
阻止未替换的真实插件安装/卸载调用。不要为了通过测试关闭防护或使用个人正式 Skill 库。
`uv run pal` 用于源码调试；普通用户安装后使用 `pal`。

浏览器检查位于 `tests/browser/`，脚本接收 Playwright 的 `page` 对象；测试前在隔离库启动
Web 并打开页面，再调用脚本。这些文件是异步函数表达式，不能仅用 `node 文件.js` 运行；
需要自行准备 Node.js、Playwright 和浏览器，并由驱动脚本读取函数后传入 `page`。
Python 的 `test` 依赖、`pytest` 和当前 GitHub CI 均不包含这组浏览器检查。
大部分 API 请求由脚本模拟；主题测试需要至少一个示例 Skill，截图输出目录需提前创建。
普通 CLI 创建入口调用 PATH 中的 `pal`；进行源码实机验收前核对其版本和构建，避免测到旧安装。
CLI 实机验收需要独立的厂商 CLI home/PAL 配置和显式授权。插件安装检查不需要模型凭据；
模型任务验收另需双端认证，不能用自动化或插件安装通过替代模型消费结果。

## 报告问题

提供操作步骤、期望与实际结果、操作系统、`pal --version` 和相关 CLI 版本。日志先删除
用户名、绝对路径、任务内容、凭据和业务数据。安全问题按照 [SECURITY.md](SECURITY.md) 报告。

## 提交变更

1. 围绕一个具体问题提交，描述触发场景、修改后的行为和验证方法。
2. 新功能或行为取舍先通过 Issue 讨论，避免恢复已退出的快照、评分等流程。
3. 持久化变更更新 schema，并验证旧记录可读、不可变产物不被原地改写。
4. 用户行为变化同步 README、快速开始和使用手册。协作规则同时更新 AGENTS.md、CLAUDE.md。
5. 新增行为测试应验证实际边界或故障恢复，避免仅复述实现细节。

源码注释中的 PRD/ACC 等标识用于追踪产品与验收约束，当前范围以 `docs/PRODUCT.md` 为准。
提交信息采用 `fix:`、`feat:`、`docs:`、`test:` 或 `refactor:` 前缀。文档和用户输出以简体中文
为主，代码标识与机器字段保留英文。提交贡献时，你同意按本项目的 MIT 许可证提供该贡献；
请确保有权提交这些内容，不附带他人的私有 Skill、模型会话或凭据。
