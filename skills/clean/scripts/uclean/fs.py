"""Metadata-only accounting and descriptor-relative filesystem operations."""
import hashlib
import os
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from .rules import PROTECTED_NAMES, PROTECTED_SUFFIXES


class Rejected(RuntimeError):
    pass


def identity(st):
    return [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def protected_name(name):
    return name.lower() in PROTECTED_NAMES or name.lower().endswith(PROTECTED_SUFFIXES) or name.startswith(".env.")


@contextmanager
def parent_fd(path):
    """Open every component without following symlinks, including intermediate ones."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.name in ("", ".", ".."):
        raise Rejected("invalid_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd, path.name
    finally:
        os.close(fd)


def measure(path, deadline, max_entries=400000, cancel=None, guard_names=True, root_identity=None, manifest=False):
    """One metadata traversal; hard links excluded from reclaimable allocation.

    No content reads, no symlink traversal, no crossing devices. Incomplete
    measurements can be reported but cannot be planned for deletion.
    """
    result = dict(logical_bytes=0, allocated_bytes=0, shared_bytes=0, entries=0,
                  complete=True, issues=[], fingerprint=None, identity=None)
    digest = hashlib.sha256()
    seen = set()
    objects = {}

    def visit(fd, name, relative, device):
        if time.monotonic() >= deadline or result["entries"] >= max_entries or (cancel and cancel.is_set()):
            raise Rejected("scan_limit")
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        result["entries"] += 1
        if st.st_dev != device:
            raise Rejected("different_volume")
        digest.update(repr((relative, root_identity if relative == "" and root_identity else identity(st), st.st_nlink)).encode("utf-8", "surrogateescape"))
        if manifest:
            objects[relative] = identity(st)
        if guard_names and protected_name(name):
            raise Rejected("protected_content")
        if stat.S_ISLNK(st.st_mode):
            if relative == "":
                raise Rejected("symlink_target")
            digest.update(os.readlink(name, dir_fd=fd).encode("utf-8", "surrogateescape"))
            return  # Never open, account for, or walk the link's referent.
        if stat.S_ISDIR(st.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if identity(os.fstat(child)) != identity(st):
                    raise Rejected("changed_during_scan")
                for entry in sorted(os.listdir(child)):
                    visit(child, entry, relative + "/" + entry, device)
                if identity(os.fstat(child)) != identity(st):
                    raise Rejected("changed_during_scan")
            finally:
                os.close(child)
        elif stat.S_ISREG(st.st_mode):
            inode = (st.st_dev, st.st_ino)
            if inode not in seen:
                seen.add(inode)
                result["logical_bytes"] += st.st_size
                allocated = st.st_blocks * 512
                if st.st_nlink > 1:
                    result["shared_bytes"] += allocated
                else:
                    result["allocated_bytes"] += allocated
        else:
            raise Rejected("special_file")

    try:
        with parent_fd(path) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            result["identity"] = identity(st)
            visit(fd, name, "", st.st_dev)
        result["fingerprint"] = digest.hexdigest()
    except (OSError, Rejected) as exc:
        result["complete"] = False
        result["issues"].append(str(exc) if isinstance(exc, Rejected) else exc.__class__.__name__)
    if manifest:
        result["objects"] = objects
    return result


def remove_tree_at(fd, name, device, objects, relative="", cancel=None, quarantine_fd=None):
    """No-follow deletion after staging. Never follows links or mount points."""
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if cancel and cancel.is_set():
        raise Rejected("user_cancelled")
    if identity(st) != objects.get(relative):
        raise Rejected("target_changed_during_delete")
    if st.st_dev != device or (stat.S_ISLNK(st.st_mode) and relative == "") or protected_name(name):
        raise Rejected("target_changed_during_delete")
    if stat.S_ISDIR(st.st_mode):
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if os.fstat(child).st_ino != st.st_ino:
                raise Rejected("target_changed_during_delete")
            for entry in os.listdir(child):
                remove_tree_at(child, entry, device, objects, relative + "/" + entry, cancel, quarantine_fd)
            now = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if now.st_ino != st.st_ino or now.st_dev != st.st_dev:
                raise Rejected("target_changed_during_delete")
            os.rmdir(name, dir_fd=fd)
        finally:
            os.close(child)
    elif stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        # Detach each leaf from the old directory's namespace before rechecking.
        # A process holding an old directory fd can replace its names, but cannot
        # accidentally replace this unpredictable name in the private container.
        held = "leaf-" + secrets.token_hex(16)
        os.rename(name, held, src_dir_fd=fd, dst_dir_fd=quarantine_fd)
        current = os.stat(held, dir_fd=quarantine_fd, follow_symlinks=False)
        if identity(current)[:5] != objects[relative][:5]:
            raise Rejected("target_changed_during_delete")
        os.unlink(held, dir_fd=quarantine_fd)
    else:
        raise Rejected("special_file")
