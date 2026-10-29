# UClean

**让 AI 编程时的空间不足，有一个能不断继续的处理流程。**

Mac 本地磁盘清理插件，支持 Codex 和 Claude Code。一套 Python 标准库引擎，提供聊天选项和本地按钮面板：

**快速释放 → 继续清理 → 深度查找 → 回到原来的工作。**

需要 **macOS、Python 3.10+、普通用户权限**。无需 pip 依赖、云端 API 或遥测服务。首版为 `0.1.0` 预览版；自建市场分发与官方目录收录是不同状态。当前功能和测试情况见 [RELEASE.md](RELEASE.md)。

## 安装

Claude Code：

```sh
claude plugin marketplace add Neonbe/uclean-plugin
claude plugin install uclean@uclean-marketplace
```

新会话中使用 `/uclean:clean`，或说“磁盘空间不够，帮我清理”。

Codex（支持插件命令的当前客户端）：

```sh
codex plugin marketplace add Neonbe/uclean-plugin
codex plugin add uclean@uclean-marketplace
```

也可以在桌面应用的插件目录中选择 UClean 自建市场并安装。重启/新建会话后调用 UClean 的 clean 技能。若系统 `codex` 命令损坏或版本过旧，请使用正常的桌面客户端或更新 CLI；插件不替换你的客户端。

不通过市场也能使用：

```sh
git clone https://github.com/Neonbe/uclean-plugin.git
cd uclean-plugin
python3 skills/clean/scripts/clean.py doctor
python3 skills/clean/scripts/clean.py ui
```

最后一条命令会启动只监听 `127.0.0.1` 的本地面板并打开浏览器。面板会话四小时有效，终端需保持运行。**不要分享带令牌的本地地址。** Ctrl+C 结束服务。发行 ZIP 内无需构建即可运行同样命令。

## 一次清理

1. 快速检查已知缓存，不修改目标文件。
2. 点击“一键清理”查看精确范围，再确认执行。
3. 显示缓存删除、废纸篓移动、实际空闲空间变化和跳过项目。
4. 空间还不够，就继续推荐层，再进入深度层。

每个状态只有一到两个主按钮。高成本项单独勾选；不存在“继续一次，就永久放开所有删除权限”。全部场景见 [场景表](skills/clean/references/scenarios.md)。

| 层级 | 自动发现 | 处理方式 |
|---|---|---|
| 快速 | npm、pip、Homebrew、Yarn、Go、Cargo 下载包、CocoaPods、Electron、SwiftPM、编译缓存 | 精确缓存目录，显示后永久删除 |
| 推荐 | Xcode DerivedData / DeviceSupport、Gradle、Playwright / Puppeteer / Cypress、旧安装包 | 解释成本后选择；安装包进废纸篓 |
| 深度 | 已验证项目产物、大文件、Docker、模拟器、归档、设备备份、AI 模型、未知缓存、pnpm/uv/Maven | 项目产物经 Git 检查后可选清理；其余提供占用与专用工具建议 |

深度查找不是全盘穷举。项目扫描覆盖 `Developer`、`Projects`、`Code`、`src`、`Desktop`、`Downloads`，有深度、数量和时间上限。自定义缓存位置、系统与外置盘不在自动清理范围。

## 执行与恢复

- 扫描只读元数据，不读取个人文件内容。计划 10 分钟过期、单次执行，并校验本地记录签名。
- 执行前重检实际路径、设备、inode、元数据指纹、Git 跟踪、应用/打开文件状态；目标或祖先为符号链接、挂载越界、保护路径和不完整测量均不能执行；缓存内部链接只移除链接本身，不跟随其目标。
- 目标先原子移入同卷私有暂存目录。每个文件再次移至私有随机名并重检后才删除，避免原路径正在写入时误删新文件。异常留下可追溯的 `recovery_paths`，**不是事务回滚**，里面可能只剩部分内容。
- 不使用 sudo，不结束应用，不清空废纸篓，不删除 Time Machine 快照、不删 Docker 数据卷或云同步内部数据。
- 永久缓存删除没有通用撤销；旧安装包可通过面板详情或 `restore` 恢复，原位置有文件时拒绝覆盖。
- 硬链接不重复计入释放估算；稀疏文件按块计量。APFS 克隆/快照共享以及并发写入使估算不能等于保证释放量，以真实磁盘可用空间差值为准。
- 文件系统无法对同一用户下主动攻击进程提供隔离权限；本工具不声称抵抗已取得同用户控制权的恶意进程。请关闭正在使用目标的应用，并检查最终范围。

自定义保护路径：在 `~/Library/Application Support/UCleanPlugin/config.json` 写入：

```json
{"protected_paths": ["/absolute/path/to/keep"]}
```

目录不存在时也保留保护规则；路径别名和卷大小写按对象身份检查。CLI 单次可用 `--protect /absolute/path`。

## CLI

```sh
python3 skills/clean/scripts/clean.py scan --level quick --target-free-gb 20
python3 skills/clean/scripts/clean.py scan --level recommended
python3 skills/clean/scripts/clean.py scan --level deep --budget 120
python3 skills/clean/scripts/clean.py plan --scan SCAN_ID --items CANDIDATE_ID
# 仅在用户已经确认上一步显示的完整范围后：
python3 skills/clean/scripts/clean.py apply --plan PLAN_ID --confirm CONFIRMATION
python3 skills/clean/scripts/clean.py verify --id RECEIPT_OR_PLAN_ID
python3 skills/clean/scripts/clean.py restore --receipt RECEIPT_ID --item CANDIDATE_ID
python3 skills/clean/scripts/clean.py records --prune-days 30
```

`--target-free-gb` 是期望可用空间，不是要删除的数量。JSON 中 `next` 返回下一场景和一到两个主操作。`--redact` 置于子命令前，可隐藏 CLI 输出的候选路径；本机记录仍保留准确路径以便核验。

## 开发与验收

```sh
python3 -m unittest discover -s tests -v
python3 scripts/package.py
```

破坏性测试全部在带标记的临时测试 home 中进行，不清理开发者真实缓存。`--sandbox` 只接受系统临时目录内的测试 home；不能用它跳过真实 home 的保护。

主要文件：`skills/clean/scripts/uclean/` 引擎和 HTTP 服务；`skills/clean/assets/` 本地面板；`skills/clean/SKILL.md` 两端共享编排。两个平台 manifest 与市场目录由同一源码维护。已测试 macOS 原生文件语义；`scripts/github-actions.yml` 备有 macOS/Linux、Python 3.10/3.13 的 CI 模板，移到 `.github/workflows/test.yml` 后可启用。当前发布账号缺少 workflow 权限，尚未运行云端 CI。

## English

UClean is a local macOS disk-cleanup plugin for Codex and Claude Code. Start with known caches, explicitly select higher-cost cleanup, then inspect deeper storage consumers. A signed, expiring preview precedes every cleanup. It reports measured disk-space changes separately from files moved to Trash. Python 3.10+; standard library only; no cloud API, telemetry, privilege escalation, or background cleanup. Chat instructions adapt to the user's language; the optional local panel is Chinese in this first release.

Support: [GitHub Issues](https://github.com/Neonbe/uclean-plugin/issues). [Privacy](PRIVACY.md) · [Terms](TERMS.md) · [MIT License](LICENSE).

The implementation is original; no upstream cleanmymac code is copied. Product direction follows the supplied disk-cleaning plugin plan. Platform packaging follows [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins) and [Claude Code plugin documentation](https://code.claude.com/docs/en/plugins/create).
