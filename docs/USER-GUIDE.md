# PAL 使用手册

产品名称为 PAL · 个人能力库（Personal Ability Library）；命令、包名、插件调用名和配置路径
统一使用 PAL 标识。控制台已采用完整 pal 字标和明暗主题标识。

适用版本：0.4.0；日期：2026-11-23。首次设置见[快速开始](QUICKSTART.md)，
版本变化见[更新记录](../CHANGELOG.md)，数据和访问边界见[安全说明](../SECURITY.md)。

## 一套库、三个阶段

PAL 程序目录保存工具本身，外挂库目录保存你的 Skill。开发和生产是同一个外挂库内的两个
阶段，不需要分别选择保存位置。CLI 私有插件目录保存消费副本，不能当成外挂库事实源。
PAL 配置目录保存默认库、挂载、安装与恢复状态。

| 内容 | 用途 | 改变它的动作 |
|---|---|---|
| 开发库 | 创建和修改中的完整 Skill | 在 CLI 中创建/更新并提交 |
| 生产库 | 保存明确发布的内容 | 发布所选 Skill |
| CLI 挂载库 | Claude Code/Codex/DeepSeek Harness 新会话实际发现的副本 | 单项挂载/同步、卸载或明确全库同步 |

三层分别手动推进。开发更新不改变生产，发布不自动安装；待同步可以持续存在。明确要求
“发布并同步这个 Skill”可连续执行。已主动停止挂载的 Skill 在再次发布或全库同步时仍保持停用。
“发布并同步”也保留这个停用选择；想恢复使用时，明确要求“重新挂载这个 Skill”。

内部保存不可变修订、release 和运行恢复材料，但产品不提供分支、快照管理或任意历史回退。
旧调用名迁移与底层构建仅供维护，日常无需操作这些对象。

## 安装与首次设置

PAL 0.4.0 支持 macOS、Linux 与原生 Windows；不依赖 WSL。
运行包自带 Python 及运行依赖，不包含或自动认证 Claude Code/Codex/DeepSeek Harness。
目标 CLI 至少安装一个，可只使用其中一个；各自必须自行安装，
实际模型任务需要认证或 provider 配置。PAL 会检查版本、官方插件命令和行为。

| 运行包 | 目标环境 | 验证边界 |
|---|---|---|
| Windows x64 | 原生 PowerShell，推荐 Windows 11 | Windows runner 的自动化/安装验证，不等于模型任务验收 |
| macOS arm64 / x86_64 | Apple Silicon / Intel 分别下载 | 两种架构原生构建；历史模型验收在 Apple Silicon |
| Linux x64 | glibc 2.35+（例如 Ubuntu 22.04+） | Linux runner 的自动化/安装验证，不包含 musl/Alpine |

macOS 运行包在 macOS 15 验证，尚未验证更早版本；BUILD.json 记录实际构建环境，
不表示所有更早版本都能运行。Windows ARM64、Linux ARM64 没有本次原生运行包。Skill 自带脚本和外部工具
仍可能有自己的平台要求，PAL 不会自动转换它们。

从 [Releases](https://github.com/Y2ggg/pal/releases) 下载并核对 SHA256SUMS：
macOS/Linux 用 `shasum -a 256 文件名` 或 `sha256sum 文件名`；Windows 用 `Get-FileHash 文件名 -Algorithm SHA256`。
解压后按包内 INSTALL.md 操作：macOS/Linux 运行 `sh install.sh`；Windows 在 PowerShell 运行
`powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`。
这只对该次脚本使用执行策略参数，不更改全局策略。运行包未进行 Apple 公证或 Windows Authenticode 签名。

| 系统 | 默认程序位置 | 命令路径 |
|---|---|---|
| macOS / Linux | `~/.local/share/pal-program` | `~/.local/bin/pal`，需要加入 PATH |
| Windows | `%LOCALAPPDATA%\Programs\PAL` | `runtime\pal.exe` 所在目录加入当前用户 PATH，新开终端生效 |

通过 `PAL_INSTALL_DIR`/`PAL_BIN_DIR`（POSIX），或安装脚本的 `-InstallDir`（Windows）可调整程序位置。
卸载使用**安装目录内**的 `uninstall.sh` / `uninstall.ps1`；库、配置及 CLI 插件保留。
升级须沿用原安装目录和命令目录：POSIX 继续传入原 `PAL_INSTALL_DIR`/`PAL_BIN_DIR`，Windows
继续使用原 `-InstallDir`。需要迁移位置时，先从原安装目录卸载，再按新位置安装。
先用对应配置和端口停止所有 PAL Web 服务。不要混用 uv 和运行包覆盖同一命令；切换前先用原工具卸载程序。

源码/wheel 方式需要 Python 3.11+、uv。在源码根目录运行 `uv tool install .`，或将 `.` 替换为
发行 wheel 的路径。此方式找不到命令时运行 `uv tool update-shell` 并新开终端。两种安装完成后均运行：

```sh
pal quickstart
```

直接从源码运行单条命令时，在项目根目录用 `uv run pal` 替代 `pal`。系统创建入口仍调用
CLI 环境 PATH 中的 `pal`；源码调试不会自动把普通 CLI 切换到源码版本。
Quickstart 只询问外挂库保存路径并确认，准备库、各已安装目标的配置挂载、系统创建入口和默认创建库绑定。
新库不会创建演示 Skill、调用模型或产生生产内容；已有挂载只校验现有内容，不安装待同步修改。
交互模式输出中文进度与摘要；`--answers` 为自动化 JSON 模式。

选择新路径、空目录或已有 PAL 库，不要把普通业务项目或程序源码目录当成 Skill 库。建库与
创建入口检查不调用模型，实际创建和使用仍需要 CLI 自身的模型服务可用。
设置在中途失败时可能已建库或安装部分入口；失败信息列出已完成步骤和下一步指引。
修复原因后用相同库与配置路径重试，不能把失败理解为所有已完成步骤均被撤销。

外挂库与 PAL 配置目录需使用长期稳定、彼此不重叠的路径。默认配置目录：macOS 为
`~/Library/Application Support/pal`，Linux 为 `$XDG_CONFIG_HOME/pal`（未设置时 `~/.config/pal`），
Windows 为 `%LOCALAPPDATA%\pal`。Windows 仅支持本地磁盘路径，不支持 UNC、设备路径、
目录联接和其他重解析点；文件名不能包含设备保留名、ADS、结尾点/空格。不支持直接移动已配置库，也不要使用自建
符号链接代替库或配置目录。库与配置不支持跨系统直接搬迁。多库操作应在维护命令中明确 `--library` 和 `--config-root`。
支持省略库参数的命令会先检查环境变量及当前目录所属库，再使用默认库；并非总选 Quickstart
最近配置的库。自定义配置目录的后续用法和选择顺序见[维护说明](MAINTENANCE.md)。

## 在普通 CLI 中创建或更新

从任意业务目录启动 `claude`、`codex` 或 DeepSeek Harness。Codex 调用 `$pal-create-skill`；
Claude Code 调用
`/pal:pal-create-skill`。这是 PAL 系统入口，随 Quickstart 安装，不是用户业务 Skill。

先说“更新已有 Skill”也可以。入口列出候选，取得目标和修改内容后才开始更新；不会把库名称
当成 Skill ID。它将完整文件保存为同一 Skill 的新开发修订；并发基线已变化时会拒绝覆盖。
只有开发提交成功后，关闭会话才不影响已保存内容；尚在生成或校验中的候选不算完成。
已提交内容下一次可以继续更新或在 Web 发布；尚未挂载时其他 CLI 不会把它发现为可用的生产 Skill。
请通过创建入口形成新修订，不要直接编辑已提交的文件，否则会触发摘要不一致。

Skill 可以包含：

```text
SKILL.md
references/policy.json
scripts/render.py
assets/template.txt
```

目录名只是惯例。每个定向产物必须携带自己的完整依赖，支持二进制；拒绝越界、软链接及特殊
文件。PAL 不打包系统解释器和外部依赖，也不保存执行权限，请用 `python3`、`sh` 等运行脚本，
资源相对脚本自身路径定位。动态引用是否完整仍需在真实任务中验证。

“完整目录”指保存受支持 Skill 的随附文件，不表示兼容全部原生 Skill 扩展或提供一键导入。
当前基本格式的文档头只接受 `name`、`description`；Claude 定向格式另外要求 `model`，
PAL 保留该字段，但不保证 CLI 最终采用的模型。`name` 必须与 Skill ID 一致，为最多 64 字符
的小写字母/数字及连字符名称；正文须为非空 Markdown。其他文档头字段会被拒绝，接入已有
Skill 时应通过创建入口按当前支持格式整理。
文档头使用逐行 `字段: 值` 的单行文本，不支持完整 YAML 的多行块、列表等语法。
`description` 最多 1024 字符且不能含尖括号；`SKILL.md` 须为无 BOM 的 UTF-8，大小不超过
1 MiB。随附文件可以更大，在线预览上限仍为 1 MiB。

`PAL_CREATION_COMMITTED` 表示开发提交；`PAL_UNIT_PUBLISHED` 表示发布。通过统一管理动作返回的
`PAL_SKILL_ACTION` 要结合所执行的动作及返回结果理解。不要把发布成功当作 CLI 已更新。

## 发布、挂载和实际使用

在会话内明确要求只发布、只同步该 Skill、发布并同步，或在 Web 逐项操作。
系统入口使用服务端预览和 token 执行管理动作，已经明确授权的动作不反复询问。

挂载成功后打开新会话，从 CLI 的 Skill 列表选择实际调用名。常见形式为
Claude Code 为 `pal-<库ID>:<SkillID>`，Codex 前加 `$`，DeepSeek Harness 为 `/SkillID`。例如库 ID 为 `my-skills`、
Skill ID 为 `meeting-notes` 时，分别使用 `$pal-my-skills:meeting-notes` 和
`/pal-my-skills:meeting-notes`。

库 ID 是 PAL 保存的标识，Quickstart 从目录名生成或复用已有值，不一定等于文件夹名；可在
控制台或 `pal production status` 中查看。特殊或过长 ID 的调用前缀会做规范化并附加短摘要，
以 CLI 列表或发布结果为准，更新 Skill 不会因此改变前缀。旧长调用名迁移见维护说明。
普通会话无需经过 `pal launch` 或 `pal run`；正在进行的会话不会热加载新挂载内容。

单项同步只采用该 Skill 的生产内容。全库 `pal sync` 会处理所有启用成员及待完成删除，必须明确
范围；不会把已卸载 Skill 自动重新启用。发布成功但同步失败时，生产仍保留新内容，先检查再恢复。

## DeepSeek Harness

DeepSeek Harness（DSH）是第三个正式目标，与 Claude Code、Codex 平级。PAL 把已挂载的
生产 Skill 复制到 `~/.dsh/skills/<SkillID>/`，把创建入口安装为
`~/.dsh/skills/pal-create-skill/`。DSH 的 `/` 菜单和模型技能目录读取同一份文件系统注册表。

- 在 DSH 中输入 `/` 选择 Skill，或输入 `/pal-create-skill` 进入创建流程。
- 同步后需要新开 DSH 会话；已打开的会话或菜单可能保留旧目录。
- DSH 没有 `pal run` 运行通道，PAL 不会为 DSH 伪造运行或使用记录；实际加载以 `/` 为准。
- DSH 根默认 `~/.dsh`，可通过 `DSH_HOME` 或显式答案文件指定；PAL 只覆盖自己归属的 Skill
  目录，遇到同名非 PAL 内容会拒绝覆盖。
- 只有 DSH 也能创建、发布、挂载和使用 Skill；Claude Code/Codex 缺席不会阻塞。

## Web 控制台

```sh
pal web
```

默认只监听本机 `http://127.0.0.1:8787`，校验 Host 与浏览器来源并禁止页面嵌入；
请使用启动时输出的地址，不通过代理、隧道或端口转发向远程开放。服务占用当前终端，可用
Ctrl-C 停止；`--no-browser` 只是不自动打开网页，仍会占用终端。
`--port 8899` 指定端口，`--port 0` 自动选空闲端口。关闭浏览器不停止服务；在另一终端运行
`pal web stop`，自定义端口用 `pal web stop --port 8899`。端口占用会输出地址；同库、同配置、
同运行构建可复用，否则提示重启或换端口。
使用 `--port 0` 后，停止命令填写启动输出的实际端口；若启动时指定 `--config-root`，停止时
也使用相同配置目录。

| 页面 | 能做什么 |
|---|---|
| Skill 集合 | 查看三层关系，逐项发布/挂载/卸载，放弃修改或删除 |
| CLI 与系统 | 核查三个目标的系统入口与业务挂载，更新入口、修复现有挂载，明确同步全部待处理项 |
| 库与维护 | 查看库位置与三层数量，检查完整性、处理异常恢复 |

右上角状态来自实际安装核查，可点击查看详情；未知或失败不能显示为健康。页面打开时核查安装，
每分钟的定时核查仅在页面可见、无弹窗且没有其他操作时发起；使用“刷新”重新读取库内容。
“状态正常”表示当前状态与安装检查通过，空库或有未发布修改时也可能正常，不等于存在可用
Skill 或其效果已通过验证。系统创建入口升级与业务内容同步分开操作。

Skill 卡片名称和“查看详情”打开完整概览/文件页；有未发布修改时优先显示开发版。各层有
“查看内容与文件”入口。文件页按实际清单展开目录，支持来源/适用 CLI 切换、Markdown 预览、
源码及行号。二进制、非 UTF-8 或超过 1 MiB 的文件只显示元信息。Web 不编辑或执行 Skill。
“CLI 挂载”详情显示上次同步所依据的库内内容，不直接读取 CLI 缓存；实际安装是否一致要看
“CLI 与系统”。图片仅显示文字占位，不预览本地或远程图片。
读取库、详情和文件时显示统一圆环与进度文字，明暗主题均适用；系统启用“减少动画”时
保留静态提示。

筛选仅全部、未发布、未同步：未发布包含新内容及修改；未同步包含待挂载、待更新、待卸载和
待删除清理。主动卸载且清理完成是正常状态。顶部说明抽屉分开始使用、管理 Skill、检查与维护；
支持键盘、夜间模式和窄屏；写操作统一显示影响、确认、进度和结果。

## 三种管理动作的区别

| 动作 | 开发与生产 | CLI 挂载 |
|---|---|---|
| 放弃未发布修改 | 将开发恢复到当前生产；没有生产时不提供 | 保持 |
| 从 CLI 卸载 | 都保留，可以继续查看并重新挂载 | 移除所选 Skill |
| 删除 Skill | 移除开发和当前生产成员 | 有挂载时标“删除待完成”，明确完成卸载后退出列表 |

普通删除不承诺擦除全部旧 release、使用证据或外部备份；过期材料清理规则另定。
卸载不会撤回旧会话中已经加载的指令或文件；新开 CLI 会话确认停止使用。
旧“只删开发”和“移出生产”管理入口已退出，不能用它们替代卸载/放弃修改/删除。

## 从 0.3.0 迁移

0.4.0 使用库格式 2.0。旧库先执行一次：

```sh
pal migrate --library /path/to/library
```

迁移先完整备份库与 PAL 配置，再新增 DSH 规范和共享 profile，为每个已发布 Skill 生成
三目标 revision/release，组成新生产版本并激活到可用目标。旧 revision/release/production
保持原字节，可继续只读校验；迁移失败会自动恢复备份。旧库在迁移前会被 doctor 拒绝，
不会静默改写。

## 更新 PAL 与故障处理

若 Web 正在运行，先在另一终端执行 `pal web stop`；自定义端口/配置时带上对应参数。没有
运行则跳过停止步骤。然后从新版源码根目录执行，或将 `.` 换成本地新版 wheel：

运行包用户改为重新运行新版安装脚本，并沿用上文所述的原安装目录和命令目录；迁移位置先卸载原安装。

```sh
uv tool install --force --reinstall .
pal web
```

`pal --version` 输出包版本和构建 ID。Web 的构建指纹覆盖后端代码和直接依赖版本，在启动时冻结，
不会因磁盘文件刚被升级而让旧进程冒充新版。在“CLI 与系统”更新系统入口到当前版本，再开新会话。
系统入口安装异常可单独修复；“修复现有挂载”只恢复上次同步内容，不顺带采用待同步生产修改。

完整性检查覆盖当前开发、生产及上次挂载的完整文件，按层报告；开发内容已发布也不跳过
校验。Quickstart 最终检查与 `pal doctor` 使用相同内容校验；`pal doctor` 不读取真实插件
安装清单；Web 的“检查状态”还会一并触发三个目标的安装核查。

系统创建入口“源文件异常”表示 PAL 保存的不可变安装来源损坏：页面显示具体路径且不提供
自动修复按钮。请从同版本完整备份恢复该目录，或安装新版 PAL 后更新系统入口。不要手动
修改清单摘要来掩盖错误。消费缓存异常仍可使用“修复系统入口”。

- **看不到 Skill**：先确认已发布且已挂载，再检查各目标安装并新开会话。已停止挂载无需重复全库同步。
- **预览过期**：重新读取状态并确认影响范围；不要重放旧 token。
- **操作中断**：删除或同步中断后可重新启动 `pal web`，在“库与维护”处理异常恢复；恢复完成前
  普通写操作仍被阻止。也可用 `pal recover --library /absolute/path/to/library`（替换为实际库路径）。
  不要手工修改 current/active、不可变目录或 journal。
- **文件/安装不一致**：按检查结果修复，不绕过摘要或强行标正常。
- **模型服务报错**：核查 CLI 自身认证或 provider；安装检查通过不保证模型服务可用。

同机冷备仍可覆盖完整外挂库和 PAL 配置根，并等待写操作结束；恢复到原路径后核查各目标安装。

换机或换库路径使用下面的导出/导入/接管流程。备份不包含 CLI 自身认证、会话和外部依赖，
不等于回放整个运行环境。
底层诊断、恢复、退出码与旧受控记录见[维护与兼容命令](MAINTENANCE.md)。以当前版本文档为准。

## 换机、导出与接管

库内容（开发、revision、release、生产版本、记录）全部使用相对路径，可以整库搬到另一台
机器；配置根（macOS 默认 `~/Library/Application Support/pal`）和三端 CLI 的安装物是
本机状态，由 quickstart 和 sync 重建，不需要搬运。

```sh
pal doctor --library <库目录>                          # 导出前先确认库健康
pal export --library <库目录> --output <库外路径>.tar.gz
pal import <归档>.tar.gz --library <新库路径>           # 目标不存在：落库并自动接管
pal import <归档>.tar.gz --library <已有库路径> --replace
pal adopt --library <库路径>                           # 直接拷贝来的库：只重绑本机指针
pal sync --library <库路径>                            # 再按需安装到三个目标
```

- 归档自校验：清单记录逐文件 SHA-256、文件数与树摘要；缺文件、摘要不符、路径逃逸或
  成员类型异常都会拒绝导入，不会部分落库。
- 导出先跑完整性检查，并拒绝存在未完成创建/清理事务的库；`.DS_Store`、`.pal/locks` 与
  在途事务记录不进归档。
- 导入不会覆盖已存在的目录；`--replace` 先把旧库整体改名为同级备份保留，再做分歧判定：
  release、production 版本、schema、规范、修订与记录中，凡本机有而归档没有、或同路径
  内容不同，即判为两台机器各有新发布并拒绝覆盖。
- `adopt` 只修「本机缺少 projection / mount 安装来源」这一种情况；bundle 已存在但摘要
  不符仍按真实漂移拒绝，交给诊断与恢复处理。它不改变 active 版本，也不安装到 CLI。
- 导入后 active 版本与生产内容不变，只改写指向本机配置根的绝对指针；随后 `pal sync`
  才安装到目标 CLI，`pal doctor` 可复核。

### 用私有 Git 仓同步（推荐长期做法）

把库本身放进一个私有 Git 仓，换机就是 `git clone`，日常就是 `git pull` / `git commit`。
先在一台机器上准备：

```sh
pal git-init --library ~/PAL/libraries/my-agent-plugins --remote git@github.com:<你>/pal-library.git
cd ~/PAL/libraries/my-agent-plugins
git add -A && git commit -m "chore: track PAL library"
git push -u origin HEAD
```

`git-init` 只写两个控制文件，已存在且内容不同时会拒绝而不是覆盖：

- `.gitignore`：忽略 `.DS_Store`、`.pal/locks/` 与 `.pal/transactions/` 的内容。锁文件和
  在途事务属于本机状态，进仓只会污染历史。
- `.gitattributes`：`* -text`，禁止 Git 做换行转换。PAL 按字节校验完整性，`core.autocrlf`
  一旦改写文件，整库校验会失败。

另一台机器需要先装好 PAL 和至少一个目标 CLI（Claude Code / Codex / DSH）：`adopt` 要按
本机已安装的目标生成挂载记录，`quickstart` 也要求至少一个可用目标。

```sh
git clone <私有仓 URL> ~/PAL/libraries/my-agent-plugins
pal adopt --library ~/PAL/libraries/my-agent-plugins   # 重建本机投影并重绑 active 指针
pal quickstart                                        # 重建配置挂载与创建入口
pal sync --library ~/PAL/libraries/my-agent-plugins     # 安装到三个 CLI
```

之后每次切换：`git pull` → `pal adopt`（幂等；路径相同则不改字节）→ `pal status` 看待同步 →
必要时 `pal sync`。本机有新建或发布时：`pal doctor` → `git add -A && git commit` → `git push`。

- `production/active.json` 是本机挂载指针，也会随库提交。换机器或换配置根后它会被 `adopt`
  改写，改写后请一并提交；否则下次 `git pull` 可能因本地未提交改动被拒。
- 冲突时不要强推：先看 `git status` 与 `pal doctor`。`active.json` 或 `current.json` 冲突意味着
  两台机器各自改过挂载或发布，应先确认哪个生产版本是权威，再用 `pal adopt` + `pal sync` 收敛。
- 私有仓务必为私有：库里有 Skill 全文与使用记录。配置根（`~/Library/Application Support/pal`）
  不要进仓，它由 quickstart 重建。
- git 仓里的 `.git` 目录不参与 `pal export`；导出归档也不包含 VCS 元数据。

两台机器同时或交替使用需要多副本同一性与冲突语义，目前不在承诺范围内；需要时先在产品仓
立决定，再扩展本节能力。

## 当前范围

创建/更新/使用在普通双 CLI；Web 为管理补强。仅 Skill 类型，支持共享和定向多文件产物。
不实施 Web 创建/执行、用户历史回退、日常使用记录、评分、效果归因或自动迭代。
完整性和安装检查不等于 Skill 业务效果测试；这类测试仍在 CLI 中执行真实任务。
