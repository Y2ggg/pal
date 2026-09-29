# 版本与发布

PAL 使用现有公开仓库 [Y2ggg/pal](https://github.com/Y2ggg/pal)，正常保留 Git 历史。
0.2.x 是内部交付与历史公开候选，未创建正式 tag/Release；跨平台首次发行编号为 0.3.0。

## 版本规则

- 已发布的 `vX.Y.Z` 标签与同名资产不可覆盖。发布后的修复使用新 PATCH。
- 0.x 中，新增平台或不兼容行为使用新 MINOR；不为品牌或整理历史而重建仓库。
- 程序版本在 pyproject.toml、`pal.__version__`、uv.lock 及当前用户文档保持一致。
- 创建入口修订（本版 native.8）、库协议与程序版本独立；升级程序不自动改写库与旧投影。

## 候选门禁

```sh
uv sync --locked --extra test --extra bundle
uv run ruff format --check src tests packaging
uv run ruff check src tests packaging
uv run pytest -n 4
uv build
uv run python packaging/build.py
uv run python packaging/smoke.py
```

`-n 4` 仅将独立测试分配到四个进程；串行 `uv run pytest` 仍受支持。旧 POSIX 特有对象用例
在 Windows 不适用，Windows 另有真实路径/联接点/跨进程锁回归。跳过项须如实列明。

独立运行包固定 Python 3.11.15，在各 OS/架构原生构建，不能把 macOS 二进制当作 Windows/Linux 文件交付。
CI 覆盖 Windows、macOS、Ubuntu × Python 3.11/3.14，以及四种运行包。对打包产物执行安装、
版本、多文件创建、发布、doctor、Web 启停、卸载；临时目录包括中文和空格。

CI 另安装固定版本的官方 Claude Code 与 Codex，并用 `packaging/smoke.py --native-clis`
验证隔离 Quickstart、系统插件与业务插件安装、消费文件校验。它不登录或调用模型，不能记作
模型任务验收。不能给 CI 配置作者本机凭据，也不能覆盖用户正式库。

## 发行内容与审查

- Windows x64 zip、macOS arm64/x86_64 tar.gz、Linux x64 tar.gz；每包包含运行时、安装/卸载脚本、
  INSTALL.md、BUILD.json 与许可证。当前不产出 MSI、DMG 或系统包管理器软件包。
- 普通 wheel、sdist、SHA256SUMS、版本说明与真实兼容矩阵。包内版本必须一致。
- `packaging/build.py` 使用锁定的 PyInstaller 和依赖，复制 PAL 源码用于构建指纹。
  删除安装来源 direct_url 元数据，保留运行时及第三方许可；不包含厂商 CLI 或凭据。
- 逐项复核用户文档、源码行为、平台边界和实测结果；扫描路径/凭据、许可与文件清单。
  对解压后的 sdist 实测 `uv sync --locked --extra test` 及门禁，不能只检查文件名。
- CI action 固定完整 commit SHA；保留验证 commit 和运行链接。下载 CI 运行包后重新计算摘要。
- 尚无 Apple 公证或 Windows Authenticode 签名，不把校验和称为代码签名。公开文档说明这一点。

内部工作区仍通过白名单导出到独立公开树；不推送私有工程历史、个人目录、POC 或原始会话。
发布到既有远仓后保留正常历史。远端上传必须有用户授权；本轮用户已授权三平台准备及发行。
当前没有 PyPI 上传配置，也没有确认 PyPI 包名归属；不以此任务隐含索引发布授权。

最终候选通过后，在对应公开 commit 创建 `v0.3.0` 和 Release，上传资产及 SHA256SUMS。
发布后从公开下载地址重新验证文件摘要和安装。若平台门禁未通过，保留候选、如实报告，不能
发布部分包却宣称已完成三平台交付。
