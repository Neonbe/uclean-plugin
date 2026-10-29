"""Private, authenticated records; plans are not arbitrary path input."""
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from .fs import Rejected, parent_fd


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with parent_fd(self.path / "probe") as (fd, _):
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise Rejected("state_directory_must_be_private_0700")
        key_path = self.path / "key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
        fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            if os.fstat(handle.fileno()).st_mode & 0o077:
                raise Rejected("state_key_must_be_private")
            self.key = handle.read()
        if len(self.key) != 32:
            raise Rejected("invalid_state_key")

    def save(self, data):
        data = dict(data)
        data.setdefault("id", secrets.token_hex(16))
        data.setdefault("created_at", time.time())
        self._id(data["id"])
        envelope = dict(data=data, signature=hmac.new(self.key, canonical(data), hashlib.sha256).hexdigest())
        temporary = self.path / ("." + secrets.token_hex(16))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical(envelope))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path / (data["id"] + ".json"))
        finally:
            temporary.unlink(missing_ok=True)
        return data

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise Rejected("invalid_record_id")

    def load(self, record_id, kind=None):
        self._id(record_id)
        fd = os.open(self.path / (record_id + ".json"), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as handle:
            envelope = json.load(handle)
        data = envelope["data"]
        signature = hmac.new(self.key, canonical(data), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, envelope["signature"]):
            raise Rejected("record_tampered")
        if data["id"] != record_id or (kind and data["kind"] != kind):
            raise Rejected("wrong_record_type")
        return data

    @contextmanager
    def lock(self):
        fd = os.open(self.path / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected("another_cleanup_is_running")
            yield
        finally:
            os.close(fd)

    def prune(self, days=30):
        """Only remove old signed records, never recovery state."""
        removed = 0
        with self.lock():
            records = [(p, self.load(p.stem)) for p in self.path.glob("*.json") if re.fullmatch(r"[0-9a-f]{32}", p.stem)]
            keep = set()
            for _, data in records:
                if data.get("kind") == "receipt" and any(i.get("status") == "trashed" for i in data.get("items", [])):
                    # Recovery receipts and their identity-bearing plans remain
                    # available even after the normal retention period.
                    keep.update((data["id"], data["plan_id"]))
                if data.get("status") in ("applying", "partial", "interrupted") or data.get("recovery_paths"):
                    keep.add(data["id"])
                    if data.get("plan_id"):
                        keep.add(data["plan_id"])
            for p, data in records:
                if data["id"] in keep:
                    continue
                if data.get("status") in ("applying", "partial", "interrupted") or data.get("recovery_paths"):
                    continue
                if data["created_at"] < time.time() - days * 86400:
                    p.unlink()
                    removed += 1
        return {"removed_records": removed, "retention_days": days}
