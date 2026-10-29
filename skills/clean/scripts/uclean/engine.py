import hashlib
import os
import platform
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .fs import Rejected, identity, measure, parent_fd, protected_name, remove_tree_at
from .rules import DIAGNOSTICS, LEVELS, PROTECTED, REPORT_ONLY, RULES
from .store import Store

GIB = 1024 ** 3
PROJECT_MARKERS = {
    ".next": "package.json", ".nuxt": "package.json", ".svelte-kit": "package.json",
    ".pytest_cache": "pyproject.toml", ".mypy_cache": "pyproject.toml",
    "target": "Cargo.toml", ".build": "Package.swift",
}


def within(path, root):
    return path == root or root in path.parents


def same_object(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def run(argv, timeout=4):
    # Never run a shell, package hooks, aliases or untrusted project scripts.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(argv, capture_output=True, timeout=timeout, text=True, env=env)


def git_read(path, *args):
    git = shutil.which("git")
    if not git:
        raise Rejected("git_check_unavailable")
    return run([git, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", str(path), *args])


def processes():
    result = run(["/bin/ps", "-axo", "pid=,args="], timeout=3)
    if result.returncode:
        raise Rejected("process_check_unavailable")
    return [line.strip().split(None, 1)[1].lower() for line in result.stdout.splitlines()
            if len(line.strip().split(None, 1)) == 2 and int(line.strip().split(None, 1)[0]) != os.getpid()]


def process_busy(names, commands):
    # Match executable/tool tokens, never arbitrary substrings (e.g. every
    # /opt/homebrew binary contains "brew"). Generic runtimes alone are not
    # evidence of a cache owner; selected paths also receive an lsof check.
    generic = {"node", "python", "ruby", "java"}
    aliases = {"npm-cli.js": "npm", "npx-cli.js": "npx", "pnpm.cjs": "pnpm", "pnpm.js": "pnpm", "yarn.js": "yarn"}
    for command in commands:
        words = command.split()
        if not words:
            continue
        executable = Path(words[0]).name.lower()
        tokens = [executable]
        if executable in generic or executable.startswith("python"):
            tokens += [Path(w).name.lower() for w in words[1:5] if not w.startswith("-")]
        tokens = [aliases.get(t, t) for t in tokens]
        for name in names:
            if name not in generic and any(t == name or t.startswith(name + "-") for t in tokens):
                return True
    return False


class Engine:
    def __init__(self, home=None, state=None, sandbox=False, protected=(), process_reader=processes):
        self.home = Path(home or Path.home()).resolve()
        self.sandbox = sandbox
        if sandbox:
            temp = Path(tempfile.gettempdir()).resolve()
            if not within(self.home, temp) or self.home == temp or not (self.home / ".uclean-test-home").is_file():
                raise Rejected("sandbox_requires_marked_temporary_home")
        elif self.home != Path.home().resolve():
            raise Rejected("custom_home_requires_sandbox")
        self.state_path = Path(state or self.home / "Library/Application Support/UCleanPlugin").absolute()
        if self.state_path.resolve() != self.state_path:
            raise Rejected("state_symlink")
        if sandbox and not within(self.state_path, self.home):
            raise Rejected("sandbox_state_must_be_inside_home")
        self.store = Store(self.state_path)
        self.protected = [self.home / p for p in PROTECTED] + [self.state_path]
        self.protected += [Path(p).expanduser().resolve() for p in protected]
        config = self.state_path / "config.json"
        if config.exists():
            import json
            with config.open() as handle:
                settings = json.load(handle)
            self.protected += [Path(p).expanduser().resolve() for p in settings.get("protected_paths", [])]
        self.process_reader = (lambda: []) if sandbox else process_reader
        self.cancel = threading.Event()

    def disk(self):
        usage = shutil.disk_usage(self.home)
        return {"total_bytes": usage.total, "free_bytes": usage.free, "used_bytes": usage.used}

    def guard(self, path):
        path = Path(path)
        if not path.is_absolute() or not within(path, self.home) or path == self.home:
            raise Rejected("outside_home")
        for protected in self.protected:
            if within(path, protected) or within(protected, path):
                raise Rejected("protected_path")
            # APFS can be case-insensitive; Path.resolve() does not canonicalize
            # case. Identity-based ancestry protects aliases in either direction.
            if any(same_object(protected, p) for p in (path, *path.parents)) or any(same_object(path, p) for p in (protected, *protected.parents)):
                raise Rejected("protected_path")
        if any(protected_name(part) for part in path.relative_to(self.home).parts):
            raise Rejected("protected_path")
        with parent_fd(path) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode) or st.st_dev != self.home.stat().st_dev or st.st_uid != os.getuid():
                raise Rejected("symlink_volume_or_owner_mismatch")
        return path

    def _candidate(self, rule_id, path, title, level, consequence, method="delete_cache", busy=(), evidence=None):
        return dict(id=hashlib.sha256((rule_id + str(path)).encode()).hexdigest()[:24], rule_id=rule_id,
                    path=str(path), title=title, level=level, consequence=consequence, method=method,
                    process_names=list(busy), evidence=evidence or "已知工具缓存的精确路径；不调用项目脚本。")

    def _known(self, level, warnings):
        candidates = []
        for rule in RULES:
            if rule.level > level:
                continue
            path = self.home / rule.path
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                warnings.append({"path": str(path), "reason": type(exc).__name__})
                continue
            method = "report" if rule.id in REPORT_ONLY else "delete_cache"
            if rule.id == "xcode-derived" and path.is_dir() and not path.is_symlink():
                # Keep build Products and dSYM; measure index/intermediate caches separately.
                try:
                    roots = [path / name for name in ("ModuleCache.noindex", "SDKStatCaches.noindex", "CompilationCache.noindex")]
                    with parent_fd(path / "probe") as (root_fd, _), os.scandir(root_fd) as entries:
                        for entry in entries:
                            if entry.is_dir(follow_symlinks=False) and not entry.name.endswith(".noindex"):
                                roots += [path / entry.name / rel for rel in ("Index.noindex", "Build/Intermediates.noindex")]
                    for root in roots:
                        if os.path.lexists(root):
                            candidates.append(self._candidate(rule.id, root, rule.title + " / " + root.parent.name + " / " + root.name,
                                rule.level, rule.consequence, method, rule.processes))
                except OSError as exc:
                    warnings.append({"path": str(path), "reason": type(exc).__name__})
                continue
            candidates.append(self._candidate(rule.id, path, rule.title, rule.level, rule.consequence, method, rule.processes))
        return candidates

    def _extras(self, level, deadline, warnings):
        candidates = []
        if level >= 1:
            # Old installers remain user data: only move individually selected files to Trash.
            root = self.home / "Downloads"
            try:
                self.guard(root)
                with os.scandir(root) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline or self.cancel.is_set():
                            warnings.append({"path": str(root), "reason": "scan_limit"})
                            break
                        if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in (".dmg", ".pkg", ".iso", ".xip"):
                            st = entry.stat(follow_symlinks=False)
                            if time.time() - st.st_mtime > 30 * 86400:
                                candidates.append(self._candidate("installer", Path(entry.path), entry.name, 1,
                                    "旧安装包将移入废纸篓，可以恢复；同一磁盘上不会立即释放空间。", "trash", evidence="Downloads 中超过 30 天的安装包；仍需确认是否保留。"))
            except FileNotFoundError:
                pass
            except (OSError, Rejected) as exc:
                warnings.append({"path": str(root), "reason": str(exc) if isinstance(exc, Rejected) else type(exc).__name__})
        if level < 2:
            return candidates
        for rule_id, rel, title, consequence in DIAGNOSTICS:
            path = self.home / rel
            if os.path.lexists(path):
                candidates.append(self._candidate(rule_id, path, title, 2, consequence, "report", evidence="占用分析；此类型没有自动删除权限。"))
        # Find unknown cache consumers, without guessing that their contents are expendable.
        cache = self.home / "Library/Caches"
        known = [self.home / rule.path for rule in RULES]
        try:
            self.guard(cache)
            with os.scandir(cache) as entries:
                for e in entries:
                    p = Path(e.path)
                    if time.monotonic() >= deadline or self.cancel.is_set():
                        warnings.append({"path": str(cache), "reason": "scan_limit"})
                        break
                    if not e.is_symlink() and not any(within(p, k) or within(k, p) for k in known):
                        candidates.append(self._candidate("unknown-cache", p, e.name, 2,
                            "尚无经过验证的清理规则，请在应用设置中清理缓存。", "report", evidence="缓存目录位置，仅用于定位占用。"))
        except FileNotFoundError:
            pass
        except (OSError, Rejected) as exc:
            warnings.append({"path": str(cache), "reason": str(exc) if isinstance(exc, Rejected) else type(exc).__name__})
        return candidates

    def _project_valid(self, item):
        path = Path(item["path"])
        marker = PROJECT_MARKERS.get(path.name)
        if not marker or not (path.parent / marker).is_file() or not (path.parent / ".git").exists():
            return False
        try:
            # An ignored path can still contain tracked files. Check both.
            tracked = git_read(path.parent, "ls-files", "-z", "--", path.name)
            ignored = git_read(path.parent, "check-ignore", "-q", "--", path.name)
            return tracked.returncode == 0 and not tracked.stdout and ignored.returncode == 0
        except (OSError, Rejected, subprocess.TimeoutExpired):
            return False

    def _git_guard(self, path):
        # A known cache can itself live in a source-controlled dotfiles directory.
        # Never delete tracked content, irrespective of the cache rule's name.
        path = Path(path)
        for parent in (path, *path.parents):
            if not within(parent, self.home):
                break
            if os.path.lexists(parent / ".git"):
                relative = str(path.relative_to(parent))
                tracked = git_read(parent, "ls-files", "-z", "--", relative)
                if tracked.returncode:
                    raise Rejected("git_check_failed")
                if tracked.stdout:
                    raise Rejected("git_tracked_content")

    def _deep_walk(self, deadline, warnings):
        candidates = []
        queue = deque((self.home / p, 0) for p in ("Developer", "Projects", "Code", "src", "Desktop", "Downloads"))
        visited = 0
        while queue and time.monotonic() < deadline and visited < 50000 and not self.cancel.is_set():
            path, depth = queue.popleft()
            if depth > 6:
                warnings.append({"path": str(path), "reason": "depth_limit"})
                continue
            try:
                self.guard(path)
                with os.scandir(path) as entries:
                    for entry in entries:
                        visited += 1
                        if visited >= 50000 or time.monotonic() >= deadline or self.cancel.is_set():
                            warnings.append({"path": str(path), "reason": "scan_limit"})
                            break
                        p = Path(entry.path)
                        if entry.is_symlink() or protected_name(entry.name):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in PROJECT_MARKERS:
                                item = self._candidate("project", p, path.name + " / " + entry.name, 2,
                                    "Git 忽略且没有跟踪文件的构建结果将永久删除；重新构建可能耗时。", busy=("node", "npm", "yarn", "pnpm", "cargo", "rustc", "swift", "xcode", "python"),
                                    evidence="项目标记 + Git 忽略规则 + 无 Git 跟踪文件；执行前再次验证。")
                                if self._project_valid(item):
                                    candidates.append(item)
                            elif entry.name == "node_modules":
                                candidates.append(self._candidate("dependencies", p, path.name + " / node_modules", 2,
                                    "可能包含本地改动或离线依赖；请在项目内用包管理器处理。", "report"))
                            elif not entry.name.startswith(".") and not entry.name.endswith((".app", ".bundle")):
                                queue.append((p, depth + 1))
                        elif entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_blocks * 512 >= 256 * 1024**2:
                            # Arbitrary large files are always analysis-only, not inferred junk.
                            candidates.append(self._candidate("large-file", p, entry.name, 2,
                                "大文件并不等于垃圾，请确认用途并用 Finder 管理。", "report", evidence="分配空间至少 256 MiB；仅元数据分析。"))
            except FileNotFoundError:
                pass
            except (OSError, Rejected) as exc:
                warnings.append({"path": str(path), "reason": str(exc) if isinstance(exc, Rejected) else type(exc).__name__})
        if queue:
            warnings.append({"path": "deep discovery", "reason": "scan_limit"})
        return candidates

    def scan(self, level="quick", target_free_bytes=10 * GIB, budget=None, progress=None, reset_cancel=True):
        if level not in LEVELS or not 0 <= target_free_bytes <= 1000000 * GIB:
            raise Rejected("invalid_scan_options")
        if reset_cancel:
            self.cancel.clear()
        started = time.monotonic()
        budget = min(max(float(budget or {"quick": 20, "recommended": 40, "deep": 90}[level]), 0.05), 300)
        deadline = started + budget
        warnings = []
        candidates = self._known(LEVELS[level], warnings)
        candidates += self._extras(LEVELS[level], deadline, warnings)
        if level == "deep":
            candidates += self._deep_walk(min(deadline, started + budget * 0.3), warnings)
        # Deduplicate both IDs and ancestor/descendant ranges before measuring.
        unique = []
        for item in sorted(candidates, key=lambda i: (i["method"] == "report", len(Path(i["path"]).parts), i["path"])):
            if not any(within(Path(item["path"]), Path(existing["path"])) or within(Path(existing["path"]), Path(item["path"])) for existing in unique):
                unique.append(item)
        try:
            active = self.process_reader()
            process_error = None
        except (OSError, Rejected, subprocess.TimeoutExpired):
            active, process_error = [], "process_check_unavailable"

        def inspect(item):
            item = dict(item)
            result = measure(item["path"], min(deadline, time.monotonic() + (12 if level == "deep" else 8)), cancel=self.cancel,
                             guard_names=item["method"] != "report")
            item.update(result)
            item["blocked"] = None
            if item["method"] != "report":
                try:
                    self.guard(item["path"])
                    if item["method"] == "delete_cache" and not stat.S_ISDIR(os.lstat(item["path"]).st_mode):
                        raise Rejected("cache_root_must_be_directory")
                    self._git_guard(item["path"])
                except (OSError, Rejected, subprocess.TimeoutExpired) as exc:
                    item["blocked"] = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
                if process_error:
                    item["blocked"] = process_error
                elif process_busy(item["process_names"], active):
                    item["blocked"] = "application_running"
            if not result["complete"]:
                item["blocked"] = "scan_incomplete"
            item["eligible"] = item["method"] != "report" and not item["blocked"] and result["complete"] and result["allocated_bytes"] > 0
            return item

        items = []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="uclean-scan") as pool:
            for item in pool.map(inspect, unique):
                if item["logical_bytes"] or not item["complete"]:
                    items.append(item)
                if progress:
                    progress({"checked": len(items), "total": len(unique), "elapsed_seconds": round(time.monotonic()-started, 2)})
        items.sort(key=lambda i: (not i["eligible"], i["method"] == "trash", i["level"], -i["allocated_bytes"]))
        data = dict(kind="scan", level=level, home=str(self.home), disk=self.disk(), items=items, warnings=warnings,
                    target_free_bytes=target_free_bytes, elapsed_seconds=round(time.monotonic() - started, 3),
                    cancelled=self.cancel.is_set(), coverage="已知缓存精确路径；进阶检查 Downloads 旧安装包；深度检查常用项目目录及大型占用。非全盘扫描。",
                    eligible_bytes=sum(i["allocated_bytes"] for i in items if i["eligible"] and i["method"] == "delete_cache"),
                    trash_bytes=sum(i["allocated_bytes"] for i in items if i["eligible"] and i["method"] == "trash"),
                    incomplete_count=len(warnings)+sum(not i["complete"] for i in items))
        data["next"] = next_step(data)
        return self.store.save(data)

    def _busy(self, item):
        if self.sandbox:
            return
        active = self.process_reader()
        if process_busy(item["process_names"], active):
            raise Rejected("application_running")
        lsof = "/usr/sbin/lsof"
        if not Path(lsof).is_file():
            raise Rejected("open_file_check_unavailable")
        try:
            args = [lsof, "-nP", "-t"]
            args += ["+D", item["path"]] if Path(item["path"]).is_dir() else ["--", item["path"]]
            check = run(args, timeout=6)
        except subprocess.TimeoutExpired:
            raise Rejected("open_file_check_timeout")
        if check.returncode not in (0, 1) or check.stderr.strip():
            raise Rejected("open_file_check_incomplete")
        if check.stdout.strip():
            raise Rejected("files_in_use")

    def _validate_item(self, item, expected=True):
        self.guard(item["path"])
        if item["method"] == "delete_cache" and not stat.S_ISDIR(os.lstat(item["path"]).st_mode):
            raise Rejected("cache_root_must_be_directory")
        self._git_guard(item["path"])
        if item["rule_id"] == "project" and not self._project_valid(item):
            raise Rejected("project_evidence_changed")
        self._busy(item)
        current = measure(item["path"], time.monotonic()+30, cancel=self.cancel)
        if not current["complete"]:
            raise Rejected("recheck_incomplete")
        if expected and (current["identity"] != item["identity"] or current["fingerprint"] != item["fingerprint"]):
            raise Rejected("target_changed_rescan")
        return current

    def plan(self, scan_id, ids=None, reset_cancel=True):
        scan = self.store.load(scan_id, "scan")
        if scan["home"] != str(self.home) or time.time()-scan["created_at"] > 1800 or scan["cancelled"]:
            raise Rejected("scan_expired_or_cancelled")
        if ids is None:
            # Only quick-mode caches are preselected. Higher risk needs explicit selection.
            ids = [i["id"] for i in scan["items"] if i["eligible"] and i["level"] == 0 and i["method"] == "delete_cache"]
        if not ids or len(ids) != len(set(ids)):
            raise Rejected("empty_or_duplicate_selection")
        selected = {i["id"]: i for i in scan["items"]}
        if any(i not in selected or not selected[i]["eligible"] for i in ids):
            raise Rejected("selection_not_eligible")
        if reset_cancel:
            self.cancel.clear()
        items, skipped = [], []
        for item_id in ids:
            item = selected[item_id]
            try:
                self._validate_item(item)
                items.append(item)
            except (OSError, Rejected, subprocess.TimeoutExpired) as exc:
                skipped.append({"id": item_id, "path": item["path"], "reason": str(exc) if isinstance(exc, Rejected) else type(exc).__name__})
        if not items:
            raise Rejected("no_unchanged_available_items:" + ",".join(i["reason"] for i in skipped))
        return self.store.save(dict(kind="plan", scan_id=scan_id, home=str(self.home), level=scan["level"],
            target_free_bytes=scan["target_free_bytes"], items=items, skipped=skipped, expires_at=time.time()+600,
            confirmation=secrets.token_hex(16), status="ready", delete_bytes=sum(i["allocated_bytes"] for i in items if i["method"] == "delete_cache"),
            trash_bytes=sum(i["allocated_bytes"] for i in items if i["method"] == "trash"),
            notice="确认只适用于列出的项目。缓存永久删除；移入废纸篓不计为释放空间。估算分配空间不保证等于可用空间增量。"))

    def _delete(self, item, journal):
        path = Path(item["path"])
        with parent_fd(path) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if identity(st) != item["identity"]:
                raise Rejected("target_changed_rescan")
            container = ".uclean-pending-" + secrets.token_hex(16)
            recovery = str(path.parent / container)
            journal(recovery)
            os.mkdir(container, 0o700, dir_fd=fd)
            quarantine_fd = os.open(container, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                self._delete_staged(item, fd, name, st, quarantine_fd, recovery)
            finally:
                os.close(quarantine_fd)
            os.rmdir(container, dir_fd=fd)

    def _delete_staged(self, item, fd, name, st, quarantine_fd, recovery):
            staged = "target"
            os.rename(name, staged, src_dir_fd=fd, dst_dir_fd=quarantine_fd)
            # Rename changes ctime. Verify the staged object and the entire subtree
            # again before unlinking; failures leave it in a recorded recovery path.
            staged_st = os.stat(staged, dir_fd=quarantine_fd, follow_symlinks=False)
            if identity(staged_st)[:5] != identity(st)[:5]:
                raise Rejected("target_changed_during_staging")
            check = measure(Path(recovery) / staged, time.monotonic()+30, cancel=self.cancel, root_identity=item["identity"], manifest=True)
            if not check["complete"]:
                raise Rejected("staged_recheck_incomplete")
            # Rename changes root ctime only. Descendants must match the plan.
            if check["fingerprint"] != item["fingerprint"]:
                raise Rejected("target_changed_during_staging")
            self._busy(dict(item, path=str(Path(recovery) / staged)))
            remove_tree_at(quarantine_fd, staged, st.st_dev, check["objects"], cancel=self.cancel, quarantine_fd=quarantine_fd)

    def _trash(self, item, journal):
        path = Path(item["path"])
        trash = self.home / ".Trash"
        trash.mkdir(mode=0o700, exist_ok=True)
        with parent_fd(trash / "probe") as (trash_fd, _), parent_fd(path) as (source_fd, name):
            if os.fstat(trash_fd).st_dev != item["identity"][0]:
                raise Rejected("trash_different_volume")
            if identity(os.stat(name, dir_fd=source_fd, follow_symlinks=False)) != item["identity"]:
                raise Rejected("target_changed_rescan")
            target = secrets.token_hex(8) + "-" + name
            journal(str(trash / target))
            os.rename(name, target, src_dir_fd=source_fd, dst_dir_fd=trash_fd)
            if identity(os.stat(target, dir_fd=trash_fd, follow_symlinks=False))[:5] != item["identity"][:5]:
                raise Rejected("target_changed_during_staging")
            self._busy(dict(item, path=str(trash / target)))
            return str(trash / target)

    def apply(self, plan_id, confirmation, progress=None, reset_cancel=True):
        if not self.sandbox and (platform.system() != "Darwin" or os.geteuid() == 0):
            raise Rejected("macos_non_root_required")
        if reset_cancel:
            self.cancel.clear()
        with self.store.lock():
            plan = self.store.load(plan_id, "plan")
            if plan["home"] != str(self.home) or plan["status"] != "ready" or plan["expires_at"] < time.time():
                raise Rejected("plan_expired_or_already_used")
            if not secrets.compare_digest(str(confirmation), plan["confirmation"]):
                raise Rejected("confirmation_does_not_match_plan")
            before = self.disk()
            plan["status"] = "applying"
            self.store.save(plan)
            receipt = self.store.save(dict(kind="receipt", plan_id=plan_id, home=str(self.home), status="applying", level=plan["level"],
                target_free_bytes=plan["target_free_bytes"], before=before, items=[], recovery_paths=[]))
            plan["receipt_id"] = receipt["id"]
            self.store.save(plan)
            for item in plan["items"]:
                result = dict(id=item["id"], path=item["path"], title=item["title"], method=item["method"], allocated_bytes=item["allocated_bytes"], status="pending")
                receipt["items"].append(result)
                if self.cancel.is_set():
                    result["status"], result["reason"] = "cancelled", "user_cancelled"
                    continue
                def journal(path):
                    result["recovery_path"] = path
                    receipt["recovery_paths"].append(path)
                    self.store.save(receipt)
                try:
                    self._validate_item(item)
                    if self.cancel.is_set():
                        raise Rejected("user_cancelled")
                    if item["method"] == "delete_cache":
                        self._delete(item, journal)
                        result["status"] = "deleted"
                    elif item["method"] == "trash":
                        result["trash_path"] = self._trash(item, journal)
                        result["status"] = "trashed"
                    else:
                        raise Rejected("unsupported_method")
                    if result.get("recovery_path") in receipt["recovery_paths"]:
                        receipt["recovery_paths"].remove(result["recovery_path"])
                    result.pop("recovery_path", None)
                except (OSError, Rejected, subprocess.TimeoutExpired) as exc:
                    result["status"] = "partial" if result.get("recovery_path") and os.path.lexists(result["recovery_path"]) else "skipped"
                    result["reason"] = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
                    if result["status"] == "skipped" and result.get("recovery_path") in receipt["recovery_paths"]:
                        receipt["recovery_paths"].remove(result.pop("recovery_path"))
                self.store.save(receipt)
                if progress:
                    progress({"completed": len(receipt["items"]), "total": len(plan["items"])})
            receipt["after"] = self.disk()
            receipt["free_delta_bytes"] = receipt["after"]["free_bytes"] - before["free_bytes"]
            receipt["deleted_allocated_bytes"] = sum(i["allocated_bytes"] for i in receipt["items"] if i["status"] == "deleted")
            receipt["trashed_allocated_bytes"] = sum(i["allocated_bytes"] for i in receipt["items"] if i["status"] == "trashed")
            receipt["status"] = "complete" if all(i["status"] in ("deleted", "trashed") for i in receipt["items"]) else "partial"
            receipt["next"] = next_step(receipt)
            plan["status"] = "used"
            self.store.save(plan)
            return self.store.save(receipt)

    def verify(self, record_id):
        record = self.store.load(record_id)
        if record["kind"] == "plan" and record.get("receipt_id"):
            record = self.store.load(record["receipt_id"], "receipt")
        if record["kind"] != "receipt":
            raise Rejected("receipt_required")
        result = dict(record, current_disk=self.disk())
        if record["status"] == "applying":
            result["notice"] = "执行尚未完成或已中断。不要重复执行；检查 recovery_paths 中的剩余内容。"
        result["recoverable_trash"] = [i for i in record["items"] if i.get("trash_path") and os.path.lexists(i["trash_path"])]
        return result

    def restore(self, receipt_id, item_id):
        with self.store.lock():
            receipt = self.store.load(receipt_id, "receipt")
            item = next((i for i in receipt["items"] if i["id"] == item_id and i["status"] == "trashed"), None)
            if not item or receipt["home"] != str(self.home):
                raise Rejected("not_a_recoverable_item")
            destination, source = Path(item["path"]), Path(item["trash_path"])
            with parent_fd(destination) as (dest_fd, name), parent_fd(source) as (src_fd, trash_name):
                # Hard-linking is atomic and fails if the destination exists. Trash
                # candidates are regular installer files on the same filesystem.
                st = os.stat(trash_name, dir_fd=src_fd, follow_symlinks=False)
                plan = self.store.load(receipt["plan_id"], "plan")
                original = next(i for i in plan["items"] if i["id"] == item_id)
                if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns] != original["identity"][:5]:
                    raise Rejected("trash_item_changed")
                if os.path.lexists(destination):
                    raise FileExistsError("restore_destination_exists")
                container = ".uclean-restore-" + secrets.token_hex(16)
                recovery = str(source.parent / container)
                receipt["recovery_paths"].append(recovery)
                item["recovery_path"] = recovery
                self.store.save(receipt)
                os.mkdir(container, 0o700, dir_fd=src_fd)
                held_fd = os.open(container, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_fd)
                try:
                    os.rename(trash_name, "target", src_dir_fd=src_fd, dst_dir_fd=held_fd)
                    moved = os.stat("target", dir_fd=held_fd, follow_symlinks=False)
                    if identity(moved)[:5] != original["identity"][:5]:
                        raise Rejected("trash_item_changed")
                    os.link("target", name, src_dir_fd=held_fd, dst_dir_fd=dest_fd, follow_symlinks=False)
                    os.unlink("target", dir_fd=held_fd)
                except (OSError, Rejected):
                    item["status"] = "partial"
                    receipt["status"] = "partial"
                    receipt["next"] = next_step(receipt)
                    self.store.save(receipt)
                    raise
                finally:
                    os.close(held_fd)
                os.rmdir(container, dir_fd=src_fd)
                receipt["recovery_paths"].remove(recovery)
                item.pop("recovery_path", None)
            item["status"] = "restored"
            self.store.save(receipt)
            return {"status": "restored", "path": str(destination)}


def next_step(record):
    """One or two actions for every state, shared by chat and the local panel."""
    level = record.get("level", "quick")
    later = "recommended" if level == "quick" else "deep"
    more = {"id": "scan", "level": later, "label": "继续清理" if later == "recommended" else "深度查找"}
    if record["kind"] == "receipt":
        if record.get("recovery_paths"):
            return {"scenario": "partial_failure", "message": "部分内容未完成处理，保留了恢复位置。", "actions": [{"id": "details", "label": "查看未完成项目"}, more]}
        if record.get("after", {}).get("free_bytes", 0) >= record["target_free_bytes"]:
            return {"scenario": "enough_space", "message": "可用空间已达到目标，可以继续原来的任务。", "actions": [{"id": "done", "label": "完成"}, more]}
        if record.get("trashed_allocated_bytes") and record.get("deleted_allocated_bytes", 0) == 0:
            return {"scenario": "trash_not_freed", "message": "文件已移入废纸篓，可恢复。检查后自行清倒才会释放空间。", "actions": [{"id": "trash", "label": "查看废纸篓"}, more]}
        if any(i["status"] in ("skipped", "cancelled") for i in record["items"]):
            return {"scenario": "skipped_or_cancelled", "message": "已保留未处理的项目，可查看原因后重新扫描。", "actions": [{"id": "scan", "level": level, "label": "重新检查"}, more]}
        if level == "deep":
            return {"scenario": "exhausted", "message": "自动清理已完成。继续查看大文件、备份、模型和虚拟磁盘的专用处理建议。", "actions": [{"id": "scan", "level": "deep", "label": "查看深层占用"}, {"id": "done", "label": "暂时结束"}]}
        return {"scenario": "still_low", "message": "空间仍不足，下一步按风险与收益继续检查。", "actions": [more, {"id": "done", "label": "暂时结束"}]}
    if record.get("cancelled"):
        return {"scenario": "cancelled", "message": "扫描已停止，文件没有被修改。", "actions": [{"id": "scan", "level": level, "label": "重新扫描"}, {"id": "done", "label": "结束"}]}
    if record.get("eligible_bytes", 0) or record.get("trash_bytes", 0):
        return {"scenario": "candidates", "message": "按风险从低到高排列；勾选项会进入本次清理预览。", "actions": [{"id": "plan", "label": "一键清理" if level == "quick" else "预览所选项目"}, more]}
    if any(i.get("blocked") == "application_running" for i in record.get("items", [])):
        return {"scenario": "app_running", "message": "相关应用正在运行。自行退出后可重新检查，插件不会结束进程。", "actions": [{"id": "scan", "level": level, "label": "已退出，重新检查"}, more]}
    if record.get("incomplete_count"):
        return {"scenario": "incomplete", "message": "部分位置因权限或扫描上限未完成检查；这些位置不会被清理。", "actions": [{"id": "scan", "level": level, "label": "重新检查"}, more]}
    if level == "deep":
        return {"scenario": "exhausted", "message": "没有更多可自动清理的项目。请查看专用工具建议，或迁移文件到外置磁盘。", "actions": [{"id": "details", "label": "查看占用与建议"}, {"id": "done", "label": "结束"}]}
    return {"scenario": "nothing_found", "message": "这一层没有可清理项目，继续检查下一层。", "actions": [more, {"id": "done", "label": "结束"}]}
