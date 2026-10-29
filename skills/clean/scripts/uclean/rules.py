"""Explicit cache boundaries. Never infer deletability from a directory name alone."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    path: str
    title: str
    level: int
    consequence: str
    processes: tuple = ()


DOWNLOAD = "缓存将永久删除，之后使用工具需要重新下载；离线使用可能受影响。"
REBUILD = "生成结果将永久删除，下次构建或索引需要重新生成，可能耗时较长。"
RULES = (
    Rule("npm", ".npm/_cacache", "npm 下载缓存", 0, DOWNLOAD, ("npm", "node", "npx")),
    Rule("pip", "Library/Caches/pip", "pip 下载缓存", 0, DOWNLOAD, ("pip", "python")),
    Rule("pip-xdg", ".cache/pip", "pip 用户缓存", 0, DOWNLOAD, ("pip", "python")),
    Rule("homebrew", "Library/Caches/Homebrew", "Homebrew 下载缓存", 0, DOWNLOAD, ("brew", "ruby")),
    Rule("yarn", "Library/Caches/Yarn", "Yarn 下载缓存", 0, DOWNLOAD, ("yarn", "node")),
    Rule("yarn-xdg", ".cache/yarn", "Yarn 用户缓存", 0, DOWNLOAD, ("yarn", "node")),
    Rule("go-build", "Library/Caches/go-build", "Go 编译缓存", 0, REBUILD, ("go", "compile", "link")),
    Rule("go-xdg", ".cache/go-build", "Go 用户编译缓存", 0, REBUILD, ("go", "compile", "link")),
    Rule("cargo-downloads", ".cargo/registry/cache", "Cargo crate 下载包", 0, DOWNLOAD, ("cargo", "rustc")),
    Rule("cocoapods", "Library/Caches/CocoaPods", "CocoaPods 下载缓存", 0, DOWNLOAD, ("pod", "ruby")),
    Rule("ms-playwright", "Library/Caches/ms-playwright", "Playwright 浏览器下载", 1, DOWNLOAD, ("node", "playwright", "chromium")),
    Rule("puppeteer", ".cache/puppeteer", "Puppeteer 浏览器下载", 1, DOWNLOAD, ("node", "chrome", "chromium")),
    Rule("electron", "Library/Caches/electron", "Electron 下载缓存", 0, DOWNLOAD, ("node", "electron")),
    Rule("electron-builder", "Library/Caches/electron-builder", "Electron 打包缓存", 0, DOWNLOAD, ("node", "electron-builder")),
    Rule("cypress", "Library/Caches/Cypress", "Cypress 下载缓存", 1, DOWNLOAD, ("node", "cypress")),
    Rule("gradle", ".gradle/caches", "Gradle 构建缓存", 1, REBUILD, ("gradle", "java")),
    Rule("gradle-downloads", ".gradle/wrapper/dists", "Gradle 发行包", 1, DOWNLOAD, ("gradle", "java")),
    Rule("maven", ".m2/repository", "Maven 本地仓库（仅分析）", 2, "可能包含本地唯一的安装产物；请用 Maven 管理。", ("mvn", "java")),
    Rule("xcode-derived", "Library/Developer/Xcode/DerivedData", "Xcode 构建与索引", 1, REBUILD, ("xcode", "xcodebuild", "swift", "clang", "sourcekit")),
    Rule("xcode-device", "Library/Developer/Xcode/iOS DeviceSupport", "Xcode 设备支持文件", 1, "调试旧设备时可能需要重新连接设备并下载支持文件。", ("xcode", "xcodebuild")),
    Rule("swiftpm", "Library/Caches/org.swift.swiftpm", "Swift 包缓存", 0, DOWNLOAD, ("swift", "xcode", "xcodebuild")),
    Rule("sccache", "Library/Caches/Mozilla.sccache", "sccache 编译缓存", 0, REBUILD, ("sccache", "rustc")),
    Rule("ccache", "Library/Caches/ccache", "ccache 编译缓存", 0, REBUILD, ("ccache", "clang", "gcc")),
    Rule("uv", ".cache/uv", "uv 缓存（仅分析）", 2, "可能与环境共享硬链接；请用 uv cache clean 管理。", ("uv",)),
    Rule("pnpm", "Library/pnpm/store", "pnpm store（仅分析）", 2, "可能与项目共享文件；请用 pnpm store prune 管理。", ("pnpm", "node")),
    Rule("pnpm-xdg", ".local/share/pnpm/store", "pnpm 用户 store（仅分析）", 2, "请用 pnpm store prune 管理共享文件。", ("pnpm", "node")),
)

REPORT_ONLY = {"maven", "uv", "pnpm", "pnpm-xdg"}
# These locations are inspected only when the user asks to go deeper.
DIAGNOSTICS = (
    ("docker", "Library/Containers/com.docker.docker/Data", "Docker 镜像与容器", "打开 Docker Desktop，查看 Images / Build cache；保留 volumes，逐项移除不用的资源。"),
    ("simulator", "Library/Developer/CoreSimulator", "iOS 模拟器", "打开 Xcode → Settings → Platforms，或 Devices and Simulators，选择不再使用的运行时/设备。"),
    ("archives", "Library/Developer/Xcode/Archives", "Xcode 发布归档", "归档可能用于符号化崩溃和重新分发；确认备份后在 Xcode Organizer 中管理。"),
    ("backups", "Library/Application Support/MobileSync/Backup", "iPhone / iPad 备份", "在 Finder 中选中设备 → 管理备份，保留仍需恢复的备份。"),
    ("trash", ".Trash", "废纸篓", "移入废纸篓不会释放空间；请在 Finder 检查并自行清倒。此插件不清空废纸篓。"),
    ("models", ".ollama/models", "本地 AI 模型", "用 ollama list 查看模型，再用 ollama rm 移除明确不再使用的模型。"),
    ("huggingface", ".cache/huggingface", "Hugging Face 模型", "模型可能是离线工作必需；使用 Hugging Face 缓存管理工具选择删除。"),
    ("applications", "Applications", "用户应用", "用应用自己的卸载功能或 Finder 管理；共享数据不自动删除。"),
)

PROTECTED = (
    "Documents", "Pictures", "Music", "Movies", ".ssh", ".gnupg", ".aws", ".kube",
    ".config", ".codex", ".claude", ".Trash", "Library/Mail", "Library/Messages",
    "Library/Keychains", "Library/Safari", "Library/CloudStorage", "Library/Mobile Documents",
    "Library/Application Support", "Library/Containers", "Library/Group Containers",
    "Library/Developer/Xcode/Archives", "Library/Developer/CoreSimulator", ".ollama",
)
PROTECTED_NAMES = {".git", ".svn", ".hg", ".ssh", ".gnupg", ".aws", ".kube", ".env"}
PROTECTED_SUFFIXES = (".photoslibrary", ".photolibrary", ".xcarchive", ".dsym", ".backupdb", ".pem", ".key", ".p12", ".mobileprovision")
LEVELS = {"quick": 0, "recommended": 1, "deep": 2}
