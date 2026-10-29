# UClean 0.1.0

日期：2026-10-30（Asia/Shanghai）。本地 Mac 插件预览版，自建市场分发；尚未进入 OpenAI 或 Anthropic 官方公开目录。

## 已实现

- Codex 与 Claude Code 共用一个无第三方运行依赖的 Python 引擎。
- 快速缓存 → 推荐项目 → 深层占用的连续流程；聊天选择与中文本地按钮面板。
- scan / plan / apply / verify / restore，精确选择、过期与单次计划、私有签名记录。
- 原生分配空间计量、硬链接去重、权限/超时/部分失败显示。
- 文件身份与指纹重检、全祖先 Git 跟踪保护、禁用 Git fsmonitor、目标和祖先符号链接拒绝、内部链接不跟随、跨卷保护。
- 同卷私有暂存、逐叶身份检查、删除前二次占用检查、并发 apply 锁、取消与中断记录。
- 旧安装包进废纸篓且可恢复；不将同卷移动声称为实际释放。
- 本机面板 Host / Origin / token 校验、无远程资产、无 HTTP 访问日志。
- 双平台发行 ZIP、可重复构建及 SHA-256 校验文件。

## 已验证

- macOS / Python 3.14.5：52 项自动测试通过。删除测试全部使用隔离临时 home。
- 独立只读代码审查完成，已报告的 Critical / Important 问题已修复；额外以真实子进程和 lsof 验证晚到的文件占用拦截，以及 SIGINT 服务退出。
- 本地面板实际浏览器检查：初始页 → 快速扫描 → 精确预览，按钮、选择和布局可用。HTTP 集成测试覆盖预览 → 确认执行 → 结果，以及跨源拒绝与取消。
- Claude Code 2.1.86：manifest / marketplace 校验、从自建本地市场安装成功。
- Codex CLI 0.163.0-alpha.10.2：从自建本地市场安装成功，缓存安装路径的 doctor 可运行。
- Skill 验证器通过；portable manifest 通过已发布 Agent Plugins 1.0.0 JSON Schema 校验。
- 真实 home 仅做只读扫描，没有以真实用户数据进行删除测试。性能数据是单机样本，不是最快/最高检出的普遍保证。

## 本版取舍

首版用经过限定的缓存目录操作，避免工具整类 purge 扩大到未确认范围。Homebrew/npm/pip 的原生命令适配、重复文件哈希、完整卸载、外置盘、Windows/WSL 属于后续工作。uv/pnpm/Maven、模型、备份、Docker、模拟器和未知缓存先分析并引导至专用工具；不把无验证的高风险目录变成一键删除项。

Xcode 仅清理明确的索引、模块缓存与构建中间目录，保留 Products、发布归档与 dSYM。没有完整证据或计量结果的项目不可执行。深度扫描存在目录/数量/时间上限，未完成范围显式呈现。

本地面板目前中文；聊天说明由宿主按用户语言生成。宿主原生选项工具在不同模式可能不可用，此时提供编号回复或本地面板。没有增加会自动删除文件的生命周期 hook。

## 公开目录待办

1. OpenAI：以 Desktop only / Skills only 申报，准备组织提交权限与发布身份验证；在门户确认此本机文件操作产品的审核资格，然后上传 Codex ZIP。相关字段已写入 manifest，不能视为已获审核批准。
2. Anthropic：使用有提交权限的账号，在插件开发者门户提交本仓库、名称、图标、说明、隐私与支持链接。
3. 人工体验验收：在两端新的交互会话中分别体验原生选择项与文字回退；本轮完成安装验证及共用引擎/面板测试，未替平台审核团队作兼容性保证。
4. 视用户机器反馈扩展规则；不能为了检出率放开来源不明的个人数据。
5. GitHub Actions：当前发布凭据缺少 `workflow` 权限，GitHub 明确拒绝工作流文件推送。已将待启用模板保存在 `scripts/github-actions.yml`；获得权限后移到 `.github/workflows/test.yml`，运行跨版本/跨系统矩阵。本轮测试结果只指本地 macOS，未声称云端 CI 通过。

官方依据：[OpenAI 包装](https://developers.openai.com/plugins/build/plugins)、[Desktop-only 提交](https://developers.openai.com/plugins/deploy/desktop-only)、[Claude 发布](https://code.claude.com/docs/en/plugins/publish)。
