# 架构说明

PAL 是本地 Python CLI 与标准库 HTTP 控制台。运行依赖为 jsonschema 与 markdown-it-py，
前端使用内嵌 HTML/CSS/JavaScript，无独立 Node 构建。用户的库、PAL 程序和 CLI 安装副本分离。

## 数据与操作

开发库保存规范、逻辑 Skill、适配产物和修订。发布形成不可变 release 与生产集合，current
表示当前生产内容；active 表示上次明确同步的 CLI 挂载集合。它们允许暂时不一致。

配置挂载为创建入口提供库位置、规范与目标 CLI。生产挂载将明确的已发布内容物化为两端原生
plugin，再通过官方命令安装。系统创建入口的升级与业务 Skill 同步是两条独立路径。

核心是 Logical Plugin、Plugin Component、ComponentTypeDriver 与 TargetDriver。目前只注册
Skill 组件。将来新增类型须先验证协议与适配，不通过伪装成 Skill 绕过校验。

## 源码导航

| 模块 | 职责 |
|---|---|
| `cli.py`、`quickstart.py` | 命令入口与首次配置 |
| `library.py`、`integrity.py` | 库骨架、完整内容校验 |
| `config_mount.py`、`adapters.py`、`creation.py` | 库绑定、双端系统创建入口、创建/更新事务 |
| `publishing.py`、`publication.py`、`stages.py` | 不可变发布、生产与挂载选择 |
| `production_mount.py`、`installation_monitor.py` | 物化、官方安装、修复、监测 |
| `skill_state.py`、`status.py`、`skill_actions.py` | 共用三层状态及预览/执行/恢复 |
| `web.py`、`skill_browser.py` | 本机控制台、只读概览与文件预览 |
| `schema_catalog.py`、`paths.py`、`io.py` | 结构校验、路径边界、原子文件写入 |
| `targets/`、`components/` | CLI 与组件类型驱动 |
| `usage.py`、`usage_events.py`、`deletion.py`、`production_history.py` | 旧受控记录、清理与历史数据恢复内核 |

文件均在 `src/pal/`。旧恢复内核的存在不代表重新提供用户快照或日常评分。

## 一致性与失败处理

持久化 schema 版本与程序版本独立。不可变材料按摘要校验，既有投影不因程序升级而原地修改。
预览返回内容基线 token，执行前重新校验；内容变化会拒绝旧预览。写操作按范围持有库锁，
实际 CLI 安装切换另持激活锁；涉及多层时锁序为 maintenance → publication → activation。

未完成事务写入 journal；普通写入遇到需恢复状态时拒绝，recover 按既定顺序继续处理。
损坏、错库、路径越界、软链和未知协议均拒绝，不降级为直接读开发目录供生产消费。
这套机制面向本机一致性，不是分布式数据库、远程服务或系统级沙箱。

Web 校验 Host/Origin、浏览器来源和操作确认；不接受外部 DNS 名称，禁止被页面嵌入。
Markdown 不执行 HTML，文件预览仅从已验证清单读取。访问边界详见 [SECURITY.md](../SECURITY.md)。

## 验证层次

自动化测试覆盖领域动作、schema、路径与故障恢复；浏览器脚本覆盖交互；双端原生模型任务
验证实际发现和消费。这三个层次分别记录，不互相替代。构建指纹涵盖 Python 源码与直接依赖
版本，服务启动后冻结，避免磁盘升级让旧进程冒充新版。
