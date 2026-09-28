# PAL 维护与兼容命令

产品名称为 PAL · 个人能力库（Personal Ability Library）；命令、包名、插件调用名和配置路径
统一使用 PAL 标识。控制台已采用完整 pal 字标和明暗主题标识。

适用版本：0.2.4。日常使用见[使用手册](USER-GUIDE.md)。这些命令供故障诊断、自动化与既有
数据兼容使用；不会重新开放历史快照管理、任意回退或日常评分。

## 状态与检查

```sh
pal --version
pal doctor --library /absolute/path/to/library
pal production status --library /absolute/path/to/library
pal compatibility check --cli claude-code
pal compatibility check --cli codex
```

`--version` 包含构建 ID。`doctor` 校验库结构及当前开发、生产、上次挂载内容的完整文件与摘要（失败标明所在层）；`production status` 返回三层成员及
同步待办，与 Web 使用同一判定。已主动卸载不是待同步。实际官方插件清单、启用与安装文件
检查见 Web 的“CLI 与系统”；本地指针存在不等于 CLI 安装正常。

通常成功退出码为 0，领域拒绝为 20，参数解析失败为 2。交互式 Quickstart 的主动取消也返回
0，此时没有完成设置；兼容的 `run` 任务失败返回 1、中断返回 130，`launch` 可传回目标 CLI
的退出码。除交互式 Quickstart 和 Web 启停外，命令主要输出单行 JSON proof；自动化需同时
检查退出码、proof 和具体动作结果，不能仅据 0 或 `synced` 字段推断 Skill 已可用。

## 自动化首次设置

`pal quickstart --answers /path/answers.json` 接受以下结构，成功输出 JSON。将路径换为真实的
绝对路径；已有库的 library_id 必须匹配，库与配置根不能重叠。旧 schema_version 1 包含已退出
的演示 Skill 创建流程，当前拒绝该结构。

```json
{
  "schema_version": 2,
  "library_id": "my-skills",
  "library_path": "/absolute/path/to/my-skills",
  "config_root": "/absolute/path/to/pal-config"
}
```

此例使用自定义配置根。设置后运行管理命令仍需指定同一路径，例如：

```sh
pal production status --library /absolute/path/to/my-skills --config-root /absolute/path/to/pal-config
pal web --library /absolute/path/to/my-skills --config-root /absolute/path/to/pal-config
```

第二条命令在前台运行，需在另一终端用相同 `--config-root` 和实际端口执行 `pal web stop`。
支持配置根的管理命令也可读取 `PAL_CONFIG_ROOT`；显式 `--config-root` 优先。交互式 Quickstart
固定使用平台默认配置根，需要自定义时使用上述答案文件，不靠环境变量改动其选择。

设置失败会报告失败阶段、已完成步骤和下一步指引，不会自动撤销全部前序步骤。
修复原因后使用相同答案文件重试；不要删除已创建的库来重来。

## 指定库与单项操作

库参数可省略的命令按以下顺序定位：显式 `--library` → `PAL_LIBRARY_ROOT` → 当前目录及
父目录中的 `library.json` → 所选配置根的默认创建库绑定。发现无效对象会拒绝，不会跳过错误
继续选下一个库。目录名本身不是库或 Skill 标识；多库操作建议显式指定路径，并先检查
`pal create list` 或 `pal production status` 返回的库信息。

参数支持范围以各命令 `--help` 为准：`init`、`doctor`、`mount config` 必须指定 `--library`；
`doctor`、`release create`、`production compose` 不接受 `--config-root`。

```sh
pal skill preview --unit example --action mount
pal skill apply --unit example --action mount --token '替换为本次预览返回的 token'
```

支持 `publish`、`mount`、`unmount`、`discard`、`delete`。先核对预览影响，再执行其 token；对象
变化会令 token 过期。`pal publish --unit example` 只发布，`--sync` 组合单项发布和同步；主动卸载
的对象保持停用，重新启用要明确挂载。`pal sync` 面向所有启用成员及待完成删除，仅在明确全库
意图时使用。

## 事务与故障恢复

`create inspect/list/begin/commit/abort` 是系统创建入口使用的事务协议；业务用户通过普通 CLI
的 `pal-create-skill` 操作，不需要手工拼装事务。产物和摘要由 PAL 校验，失败不发布半成品。

`pal recover --library ...` 按待完成 Skill 动作、旧清理事务、安装切换、旧 usage 事务的顺序
处理恢复。存在未完成 journal 时普通写操作会拒绝，不要手工删除锁、journal 或改写指针。
Web 的“库与维护”也可触发恢复。生产发布成功而挂载失败不等于生产修改被撤销。

## 底层构建与兼容入口

| 命令 | 保留原因与边界 |
|---|---|
| `init`、`mount config` | 库初始化与配置挂载内核，日常由 Quickstart 编排 |
| `release create`、`production compose` | 不可变产物构建及已有自动化；compose 不替代用户发布/同步 |
| `mount production`、`production activate` | 旧同步别名，只允许当前生产；不允许任意历史激活 |
| `production migrate-names` | 旧长调用名的一次性迁移；需要明确执行，不是日常版本管理 |
| `launch create/use` | 可选预检和启动包装，普通 CLI 创建/使用不依赖它 |
| `run`、`usage validate` | 既有受控任务和旧使用关联记录校验；不代表日常自动记录或效果评测 |

每个命令的完整参数以 `<命令> --help` 为准。`production remove`、旧 Web 发布/开发删除 API、
批量发布、用户快照列表/删除/回退已退出，不应继续用于自动化。这些内部恢复对象不属于日常版本管理。

## 更新与备份

升级程序前保留外挂库和 PAL 配置根的完整备份。0.1.0 的配置挂载和生产投影可由 0.2.4 读取，
保留原生成版本与文件摘要；程序版本变化本身不会重写旧投影。先停止运行中的 Web，更新程序后
重新启动，再逐端更新系统创建入口。更新系统入口不会发布或挂载业务 Skill。

停止服务时使用启动时的配置目录和实际端口；`--port 0` 不是关闭参数。未启动服务时跳过停止，
不必为此运行恢复。备份在没有写操作时进行，并成套恢复到原路径；恢复后核查 CLI 安装，必要时
修复。CLI 的认证、会话及 Skill 外部依赖不属于这份库/配置备份。

库的 schema/协议与程序版本分开：未知协议、摘要漂移或不完整状态仍拒绝。不要编辑不可变文件
以消除错误；先读检查结果，使用对应修复或恢复操作。

删除中断后可以直接重启 Web，在维护页面恢复；服务复用按库、配置和运行构建识别。
旧归档经完整校验后可用于正常重新发布，保留旧证据且不自动同步。

系统创建入口的源文件异常与消费缓存异常分开处理：缓存可重装修复；源目录损坏不会原地
覆盖，需从同版本备份恢复或安装新版 PAL 后更新入口。当前安装监测回归见 `tests/test_installation_monitor.py` 和 `tests/test_integrity_diagnostics.py`。
