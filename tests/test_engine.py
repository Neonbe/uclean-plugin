import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/clean/scripts"))
from uclean.engine import Engine, GIB, next_step, process_busy
from uclean.fs import Rejected, measure


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uclean-tests-")
        self.home = Path(self.temp.name).resolve()
        (self.home / ".uclean-test-home").touch()
        self.engine = Engine(home=self.home, sandbox=True)

    def tearDown(self):
        self.temp.cleanup()

    def file(self, path, data=b"cache" * 4096):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def quick(self):
        return self.engine.scan()

    def plan(self):
        scan = self.quick()
        return self.engine.plan(scan["id"])


class EngineTests(Fixture):
    def test_read_only_scan_and_preview_then_exact_apply(self):
        cache = self.file(".npm/_cacache/中文 ; $(touch nope) 'file'")
        keep = self.file("Documents/important.txt")
        plan = self.plan()
        self.assertTrue(cache.exists())
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertFalse(cache.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["items"][0]["status"], "deleted")
        self.assertEqual(receipt["free_delta_bytes"], receipt["after"]["free_bytes"] - receipt["before"]["free_bytes"])
        self.assertEqual(self.engine.verify(plan["id"])["id"], receipt["id"])

    def test_wrong_confirmation_no_mutation(self):
        cache = self.file(".npm/_cacache/data")
        plan = self.plan()
        with self.assertRaisesRegex(Rejected, "confirmation"):
            self.engine.apply(plan["id"], "wrong")
        self.assertTrue(cache.exists())

    def test_plan_expiry(self):
        cache = self.file(".npm/_cacache/data")
        plan = self.plan()
        plan["expires_at"] = time.time()-1
        self.engine.store.save(plan)
        with self.assertRaisesRegex(Rejected, "expired"):
            self.engine.apply(plan["id"], plan["confirmation"])
        self.assertTrue(cache.exists())

    def test_plan_replay_rejected(self):
        self.file(".npm/_cacache/data")
        plan = self.plan()
        self.engine.apply(plan["id"], plan["confirmation"])
        with self.assertRaisesRegex(Rejected, "already_used"):
            self.engine.apply(plan["id"], plan["confirmation"])

    def test_nested_change_after_plan_skips(self):
        cache = self.file(".npm/_cacache/sub/data")
        plan = self.plan()
        cache.write_bytes(b"new contents")
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["items"][0]["reason"], "target_changed_rescan")
        self.assertTrue(cache.exists())

    def test_symlink_target_and_parent_never_followed(self):
        protected = self.file("Documents/important")
        (self.home / ".npm").mkdir()
        (self.home / ".npm/_cacache").symlink_to(protected.parent)
        scan = self.quick()
        self.assertEqual(scan["eligible_bytes"], 0)
        self.assertTrue(protected.exists())
        (self.home / ".npm/_cacache").unlink()
        (self.home / ".npm").rmdir()
        (self.home / ".npm").symlink_to(protected.parent)
        with self.assertRaises((Rejected, OSError)):
            self.engine.guard(self.home / ".npm/important")

    def test_symlink_swap_after_plan(self):
        cache = self.file(".npm/_cacache/data")
        keep = self.file("Documents/important")
        plan = self.plan()
        shutil.rmtree(cache.parent)
        cache.parent.symlink_to(keep.parent)
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["items"][0]["status"], "skipped")
        self.assertTrue(keep.exists())

    def test_nested_protected_content_blocks_entire_cache(self):
        self.file(".npm/_cacache/.git/objects/value")
        scan = self.quick()
        self.assertEqual(scan["eligible_bytes"], 0)
        self.assertFalse(scan["items"][0]["complete"])
        with self.assertRaises(Rejected):
            self.engine.plan(scan["id"], [scan["items"][0]["id"]])

    def test_protected_descendant_blocks_parent(self):
        cache = self.file(".npm/_cacache/keep/value")
        self.engine.protected.append(cache.parent)
        scan = self.quick()
        self.assertEqual(scan["eligible_bytes"], 0)

    def test_protection_normalizes_parent_segments_and_config(self):
        self.file(".npm/_cacache/keep/value")
        alias = self.home / ".npm/../.npm/_cacache/keep"
        engine = Engine(home=self.home, sandbox=True, protected=[alias])
        self.assertEqual(engine.scan()["eligible_bytes"], 0)
        (engine.state_path / "config.json").write_text(json.dumps({"protected_paths": [str(alias)]}))
        engine2 = Engine(home=self.home, sandbox=True)
        self.assertEqual(engine2.scan()["eligible_bytes"], 0)

    def test_protection_respects_case_insensitive_volume_identity(self):
        self.file(".npm/_cacache/keep/value")
        alias = self.home / ".NPM/_CACACHE/KEEP"
        if not alias.exists():
            self.skipTest("case-sensitive filesystem")
        engine = Engine(home=self.home, sandbox=True, protected=[alias])
        self.assertEqual(engine.scan()["eligible_bytes"], 0)

    def test_hard_links_not_counted_as_reclaimable(self):
        cache = self.file(".npm/_cacache/a")
        os.link(cache, cache.parent / "b")
        scan = self.quick()
        self.assertEqual(scan["eligible_bytes"], 0)
        self.assertEqual(scan["items"][0]["logical_bytes"], cache.stat().st_size)
        self.assertGreater(scan["items"][0]["shared_bytes"], 0)

    def test_sparse_file_counts_blocks_not_apparent_size(self):
        path = self.file(".npm/_cacache/sparse", b"")
        with path.open("wb") as f:
            f.seek(2*GIB)
            f.write(b"x")
        scan = self.quick()
        self.assertGreater(scan["items"][0]["logical_bytes"], GIB)
        self.assertLess(scan["eligible_bytes"], GIB)

    def test_incomplete_measurement_is_not_empty(self):
        cache = self.file(".npm/_cacache/a")
        result = measure(cache.parent, time.monotonic()-1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["issues"], ["scan_limit"])

    def test_permission_failure_is_not_zero_success(self):
        cache = self.file(".npm/_cacache/a")
        original = os.listdir
        def denied(fd):
            if isinstance(fd, int):
                raise PermissionError()
            return original(fd)
        with patch("uclean.fs.os.listdir", side_effect=denied):
            result = measure(cache.parent, time.monotonic()+1)
        self.assertFalse(result["complete"])
        self.assertIn("PermissionError", result["issues"])

    def test_record_tampering_rejected(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        path = self.engine.state_path / (plan["id"]+".json")
        value = json.loads(path.read_text())
        value["data"]["items"][0]["path"] = str(self.home / "Documents")
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(Rejected, "tampered"):
            self.engine.apply(plan["id"], plan["confirmation"])

    def test_id_traversal_rejected(self):
        with self.assertRaises(Rejected):
            self.engine.store.load("../../etc/passwd")

    def test_selection_is_exact_and_deduplicated(self):
        npm = self.file(".npm/_cacache/a")
        pip = self.file("Library/Caches/pip/b")
        scan = self.quick()
        item = next(i for i in scan["items"] if i["rule_id"] == "npm")
        with self.assertRaises(Rejected):
            self.engine.plan(scan["id"], [item["id"], item["id"]])
        plan = self.engine.plan(scan["id"], [item["id"]])
        self.engine.apply(plan["id"], plan["confirmation"])
        self.assertFalse(npm.exists())
        self.assertTrue(pip.exists())

    def test_higher_risk_not_preselected(self):
        cache = self.file("Library/Developer/Xcode/DerivedData/app/Index.noindex/result")
        scan = self.engine.scan("recommended")
        self.assertGreater(scan["eligible_bytes"], 0)
        with self.assertRaisesRegex(Rejected, "empty"):
            self.engine.plan(scan["id"])
        self.assertTrue(cache.exists())

    def test_nested_cache_link_removes_link_only(self):
        outside = self.file("Documents/keep/value")
        self.file(".npm/_cacache/cache")
        (self.home / ".npm/_cacache/outside-link").symlink_to(outside.parent)
        plan = self.plan()
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(outside.exists())

    def test_outer_git_repository_protection_with_nested_repository(self):
        self.file(".npm/_cacache/tracked-source")
        subprocess.run(["git", "init", "-q", str(self.home)], check=True)
        subprocess.run(["git", "-C", str(self.home), "add", ".npm/_cacache/tracked-source"], check=True)
        subprocess.run(["git", "init", "-q", str(self.home / ".npm")], check=True)
        scan = self.quick()
        self.assertFalse(scan["items"][0]["eligible"])
        self.assertEqual(scan["items"][0]["blocked"], "git_tracked_content")

    def test_running_app_blocks_plan(self):
        self.file(".npm/_cacache/a")
        self.engine.process_reader = lambda: ["/opt/homebrew/bin/node /opt/homebrew/lib/node_modules/npm/bin/npm-cli.js install"]
        scan = self.quick()
        self.assertEqual(scan["items"][0]["blocked"], "application_running")
        self.assertEqual(scan["next"]["scenario"], "app_running")

    def test_generic_runtime_and_homebrew_path_do_not_block_cache(self):
        self.assertFalse(process_busy(["brew", "ruby"], ["/opt/homebrew/bin/node server.js"]))
        self.assertFalse(process_busy(["npm", "node"], ["/opt/homebrew/bin/node server.js"]))
        self.assertTrue(process_busy(["brew"], ["/opt/homebrew/bin/brew cleanup"]))
        self.assertTrue(process_busy(["pip", "python"], ["python3 -m pip install something"]))

    def test_trash_and_restore_do_not_claim_freed_bytes(self):
        installer = self.file("Downloads/old 中文.dmg")
        old = time.time()-40*86400
        os.utime(installer, (old, old))
        scan = self.engine.scan("recommended", target_free_bytes=100000*GIB)
        plan = self.engine.plan(scan["id"], [scan["items"][0]["id"]])
        self.assertEqual(plan["delete_bytes"], 0)
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertFalse(installer.exists())
        self.assertEqual(receipt["deleted_allocated_bytes"], 0)
        self.assertEqual(receipt["next"]["scenario"], "trash_not_freed")
        self.engine.restore(receipt["id"], receipt["items"][0]["id"])
        self.assertTrue(installer.exists())

    def test_prune_retains_old_trash_restoration(self):
        installer = self.file("Downloads/old.dmg")
        old = time.time()-40*86400
        os.utime(installer, (old, old))
        scan = self.engine.scan("recommended")
        plan = self.engine.plan(scan["id"], [scan["items"][0]["id"]])
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        for record_id in (scan["id"], plan["id"], receipt["id"]):
            data = self.engine.store.load(record_id)
            data["created_at"] = old
            self.engine.store.save(data)
        self.engine.store.prune()
        self.engine.restore(receipt["id"], receipt["items"][0]["id"])
        self.assertTrue(installer.exists())

    def test_restore_does_not_overwrite(self):
        installer = self.file("Downloads/old.dmg")
        old = time.time()-40*86400
        os.utime(installer, (old, old))
        scan = self.engine.scan("recommended")
        plan = self.engine.plan(scan["id"], [scan["items"][0]["id"]])
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        installer.write_bytes(b"new file")
        with self.assertRaises(FileExistsError):
            self.engine.restore(receipt["id"], receipt["items"][0]["id"])
        self.assertEqual(installer.read_bytes(), b"new file")

    def test_cancel_stops_following_items(self):
        self.file(".npm/_cacache/a")
        self.file("Library/Caches/pip/b")
        plan = self.plan()
        def progress(_):
            self.engine.cancel.set()
        result = self.engine.apply(plan["id"], plan["confirmation"], progress)
        self.assertEqual([i["status"] for i in result["items"]], ["deleted", "cancelled"])

    def test_failed_delete_keeps_recovery_path_and_receipt(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        with patch("uclean.engine.remove_tree_at", side_effect=PermissionError()):
            receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["items"][0]["status"], "partial")
        self.assertTrue(Path(receipt["recovery_paths"][0]).is_dir())
        self.assertEqual(self.engine.verify(receipt["id"])["status"], "partial")

    def test_new_file_after_staging_is_not_deleted(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        original = os.rename
        def changed(*args, **kwargs):
            original(*args, **kwargs)
            staged = next((self.home / ".npm").glob(".uclean-pending-*"))
            (staged / "target/new-user-file").write_bytes(b"preserve")
        with patch("uclean.engine.os.rename", side_effect=changed):
            receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["items"][0]["status"], "partial")
        self.assertTrue((Path(receipt["recovery_paths"][0])/"target/new-user-file").exists())

    def test_known_cache_protects_git_tracked_files(self):
        cache = self.file(".npm/_cacache/source.py")
        subprocess.run(["git", "init", "-q", str(self.home / ".npm")], check=True)
        subprocess.run(["git", "-C", str(self.home / ".npm"), "add", "_cacache/source.py"], check=True)
        scan = self.quick()
        self.assertEqual(scan["items"][0]["blocked"], "git_tracked_content")
        self.assertFalse(scan["items"][0]["eligible"])
        self.assertTrue(cache.exists())

    def test_git_fsmonitor_hook_is_never_run(self):
        self.file("Projects/web/package.json", b"{}")
        self.file("Projects/web/.gitignore", b".next/\n")
        self.file("Projects/web/.next/cache/a")
        project = self.home / "Projects/web"
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        hook = self.file("probe-hook", b"#!/bin/sh\ntouch '" + str(self.home / "hook-ran").encode() + b"'\n")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(project), "config", "core.fsmonitor", str(hook)], check=True)
        scan = self.engine.scan("deep", budget=10)
        found = next(i for i in scan["items"] if i["rule_id"] == "project")
        self.engine.plan(scan["id"], [found["id"]])
        self.assertFalse((self.home / "hook-ran").exists())

    def test_file_replacement_before_leaf_detach_is_preserved(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        original = os.rename
        # Use original rename inside the replacement injection to avoid recursion.
        def injected(src, dst, **kwargs):
            if str(dst).startswith("leaf-"):
                fd = kwargs["src_dir_fd"]
                original(src, "saved-original", src_dir_fd=fd, dst_dir_fd=fd)
                handle = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                with os.fdopen(handle, "wb") as f:
                    f.write(b"preserve-new-user-data")
            original(src, dst, **kwargs)
        with patch("uclean.fs.os.rename", side_effect=injected):
            receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["items"][0]["status"], "partial")
        container = Path(receipt["recovery_paths"][0])
        self.assertTrue(any(p.read_bytes() == b"preserve-new-user-data" for p in container.glob("leaf-*")))

    def test_interrupted_plan_cannot_be_replayed(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        plan["status"] = "applying"
        self.engine.store.save(plan)
        with self.assertRaises(Rejected):
            self.engine.apply(plan["id"], plan["confirmation"])

    def test_git_ignored_build_output_requires_evidence(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.file("Projects/web/package.json", b"{}")
        self.file("Projects/web/.gitignore", b".next/\n")
        project = self.home / "Projects/web"
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        artifact = self.file("Projects/web/.next/cache/a")
        unrelated = self.file("Projects/no-evidence/target/user-file")
        scan = self.engine.scan("deep", budget=10)
        found = [i for i in scan["items"] if i["rule_id"] == "project"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["eligible"])
        plan = self.engine.plan(scan["id"], [found[0]["id"]])
        subprocess.run(["git", "-C", str(project), "add", "-f", ".next/cache/a"], check=True)
        result = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(result["items"][0]["reason"], "git_tracked_content")
        self.assertTrue(artifact.exists())
        self.assertTrue(unrelated.exists())

    def test_analysis_only_types_cannot_be_planned(self):
        self.file("Library/Containers/com.docker.docker/Data/data.raw")
        self.file("Library/Caches/unknown-tool/data")
        scan = self.engine.scan("deep", budget=10)
        self.assertTrue(scan["items"])
        self.assertTrue(all(i["method"] == "report" for i in scan["items"]))
        with self.assertRaisesRegex(Rejected, "not_eligible"):
            self.engine.plan(scan["id"], [scan["items"][0]["id"]])

    def test_concurrent_apply_is_rejected(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        with self.engine.store.lock():
            with self.assertRaisesRegex(Rejected, "another_cleanup"):
                self.engine.apply(plan["id"], plan["confirmation"])

    def test_sandbox_cannot_target_real_home(self):
        with self.assertRaises(Rejected):
            Engine(home=Path.home(), sandbox=True)

    def test_prune_preserves_recovery_records(self):
        old = time.time()-40*86400
        a = self.engine.store.save(dict(kind="receipt", created_at=old, status="partial", recovery_paths=["kept"]))
        b = self.engine.store.save(dict(kind="scan", created_at=old))
        self.assertEqual(self.engine.store.prune()["removed_records"], 1)
        self.assertTrue((self.engine.state_path/(a["id"]+".json")).exists())
        self.assertFalse((self.engine.state_path/(b["id"]+".json")).exists())

    def test_prune_keeps_config(self):
        config = self.engine.state_path / "config.json"
        config.write_text('{"protected_paths": []}')
        self.engine.store.prune()
        self.assertTrue(config.exists())

    def test_cache_root_must_be_a_directory(self):
        self.file(".npm/_cacache", b"personal file")
        scan = self.quick()
        self.assertFalse(scan["items"][0]["eligible"])
        self.assertEqual(scan["items"][0]["blocked"], "cache_root_must_be_directory")

    def test_late_open_file_check_preserves_staged_target(self):
        self.file(".npm/_cacache/a")
        plan = self.plan()
        def busy(item):
            if ".uclean-pending-" in item["path"]:
                raise Rejected("files_in_use")
        with patch.object(self.engine, "_busy", side_effect=busy):
            result = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(result["items"][0]["reason"], "files_in_use")
        self.assertTrue((Path(result["recovery_paths"][0])/"target/a").exists())

    def test_trash_replacement_is_partial_and_kept(self):
        installer = self.file("Downloads/old.dmg")
        old = time.time()-40*86400
        os.utime(installer, (old, old))
        scan = self.engine.scan("recommended")
        plan = self.engine.plan(scan["id"], [scan["items"][0]["id"]])
        original = os.rename
        def replace(src, dst, **kwargs):
            if src == "old.dmg":
                installer.write_bytes(b"new download")
            original(src, dst, **kwargs)
        with patch("uclean.engine.os.rename", side_effect=replace):
            receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.assertEqual(receipt["items"][0]["status"], "partial")
        self.assertEqual(Path(receipt["recovery_paths"][0]).read_bytes(), b"new download")

    def test_restore_never_unlinks_replacement_at_old_trash_path(self):
        installer = self.file("Downloads/old.dmg")
        old = time.time()-40*86400
        os.utime(installer, (old, old))
        scan = self.engine.scan("recommended")
        plan = self.engine.plan(scan["id"], [scan["items"][0]["id"]])
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        trash = Path(receipt["items"][0]["trash_path"])
        link = os.link
        def replace(*args, **kwargs):
            link(*args, **kwargs)
            trash.write_bytes(b"unrelated new data")
        with patch("uclean.engine.os.link", side_effect=replace):
            self.engine.restore(receipt["id"], receipt["items"][0]["id"])
        self.assertEqual(trash.read_bytes(), b"unrelated new data")
        self.assertTrue(installer.exists())

    def test_scenario_action_limit(self):
        records = [dict(kind="scan", level=level, items=[], eligible_bytes=0, trash_bytes=0, incomplete_count=n)
                   for level in ("quick", "recommended", "deep") for n in (0, 1)]
        records += [dict(kind="receipt", level=level, items=[], target_free_bytes=20, after={"free_bytes":free}, recovery_paths=[])
                    for level in ("quick", "recommended", "deep") for free in (10, 30)]
        for record in records:
            with self.subTest(record=record):
                self.assertIn(len(next_step(record)["actions"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
