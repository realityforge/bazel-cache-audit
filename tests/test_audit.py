import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bazel_cache_audit as audit


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / "cache/output with spaces"
        self.base.mkdir(parents=True)
        self.workspace = self.root / "source/worktree"
        (self.base / "DO_NOT_BUILD_HERE").write_bytes(os.fsencode(self.workspace))

    def entry(self, snapshot=None, level=3):
        entry = audit.Entry(str(self.base), "output-base")
        audit.classify(entry, snapshot or audit.Processes(), level)
        return entry

    def server_file(self, name, content):
        server = self.base / "server"
        server.mkdir(exist_ok=True)
        (server / name).write_bytes(content)

    def test_missing_workspace_is_level_one(self):
        entry = self.entry(level=1)
        self.assertEqual((entry.state, entry.level, entry.candidate), ("orphaned", 1, True))
        self.assertEqual(entry.workspace, str(self.workspace))

    def test_existing_workspace_needs_level_two(self):
        self.workspace.mkdir(parents=True)
        self.assertFalse(self.entry(level=1).candidate)
        self.assertEqual((self.entry(level=2).state, self.entry(level=2).level), ("inactive", 2))
        self.assertTrue(self.entry(level=2).candidate)

    def test_live_pid_keeps_even_a_missing_workspace(self):
        self.server_file("server.pid.txt", str(os.getpid()).encode())
        self.assertEqual(self.entry().state, "active")
        self.assertFalse(self.entry().candidate)

    def test_exited_pid_does_not_keep_an_orphan(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.server_file("server.pid.txt", str(child.pid).encode())
        self.assertTrue(self.entry().candidate)

    def test_malformed_pid_is_unknown(self):
        for value in [b"not a pid", b"0", b"-1", b"99999999999999999999999"]:
            with self.subTest(value=value):
                self.server_file("server.pid.txt", value)
                self.assertEqual(self.entry().state, "unknown")
                self.assertFalse(self.entry().candidate)

    def test_process_keeps_base_even_without_pid_file(self):
        snapshot = audit.Processes([audit.Process(123, str(self.base))])
        self.assertEqual(self.entry(snapshot).state, "active")

    def test_unmapped_bazel_process_prevents_candidates(self):
        snapshot = audit.Processes([audit.Process(123, None)])
        self.assertFalse(self.entry(snapshot).candidate)

    def test_relative_process_output_base_is_uncertain(self):
        snapshot = audit.Processes([audit.Process(123, "relative/cache")])
        self.assertFalse(self.entry(snapshot).candidate)

    def test_unrelated_output_base_does_not_block(self):
        snapshot = audit.Processes([audit.Process(123, str(self.root / "elsewhere"))])
        self.assertTrue(self.entry(snapshot).candidate)

    def test_incomplete_process_inventory_prevents_candidates(self):
        self.assertFalse(self.entry(audit.Processes(errors=["permission denied"])).candidate)

    def test_workspace_metadata_is_required(self):
        (self.base / "DO_NOT_BUILD_HERE").unlink()
        self.assertFalse(self.entry().candidate)

    def test_nul_separated_server_metadata_is_a_fallback(self):
        (self.base / "DO_NOT_BUILD_HERE").unlink()
        self.server_file("cmdline", b"bazel(test)\0--workspace_directory=" + os.fsencode(self.workspace) + b"\0")
        self.assertTrue(self.entry().candidate)

    def test_disagreeing_metadata_is_unknown(self):
        self.server_file("cmdline", b"bazel(test)\0--workspace_directory=/different/worktree\0")
        self.assertFalse(self.entry().candidate)

    def test_relative_empty_and_nul_workspace_paths_are_unknown(self):
        for value in [b"relative/path", b"", b"/invalid\0path"]:
            with self.subTest(value=value):
                (self.base / "DO_NOT_BUILD_HERE").write_bytes(value)
                self.assertFalse(self.entry().candidate)

    def test_unicode_workspace_is_not_mistaken_for_orphan(self):
        workspace = self.root / "source/caf\u00e9 \U0001f980"
        workspace.mkdir(parents=True)
        (self.base / "DO_NOT_BUILD_HERE").write_bytes(os.fsencode(workspace))
        self.assertEqual(self.entry().state, "inactive")

    def test_workspace_path_ending_in_newline_is_not_trimmed(self):
        workspace = self.root / "source/newline\n"
        workspace.mkdir(parents=True)
        (self.base / "DO_NOT_BUILD_HERE").write_bytes(os.fsencode(workspace))
        self.assertEqual(self.entry().state, "inactive")

    def test_file_instead_of_workspace_is_unknown(self):
        self.workspace.parent.mkdir(parents=True)
        self.workspace.write_text("not a workspace")
        self.assertFalse(self.entry().candidate)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires unprivileged POSIX permissions")
    def test_unreadable_workspace_parent_is_not_orphaned(self):
        self.workspace.parent.mkdir(parents=True)
        self.workspace.parent.chmod(0)
        try:
            self.assertFalse(self.entry().candidate)
            self.assertEqual(self.entry().state, "unknown")
        finally:
            self.workspace.parent.chmod(0o700)

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_symlinked_output_and_metadata_are_not_candidates(self):
        alias = self.root / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        entry = audit.Entry(str(alias), "output-base")
        audit.classify(entry, audit.Processes(), 3)
        self.assertFalse(entry.candidate)
        marker = self.base / "DO_NOT_BUILD_HERE"
        marker.unlink()
        marker.symlink_to(self.root / "absent")
        self.assertFalse(self.entry().candidate)

    @unittest.skipUnless(os.name == "posix", "requires POSIX locks")
    def test_real_client_lock_is_detected_without_changing_file(self):
        lock = self.base / "lock"
        lock.write_text("original lock contents")
        script = (
            "import fcntl,sys; f=open(sys.argv[1],'r+'); "
            "fcntl.lockf(f,fcntl.LOCK_EX,1); print('ready',flush=True); sys.stdin.read()"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(lock)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(audit.lock_state(self.base), "held")
            self.assertEqual(self.entry().state, "active")
        finally:
            child.communicate(timeout=10)
        self.assertEqual(audit.lock_state(self.base), "free")
        self.assertEqual(lock.read_text(), "original lock contents")

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires unprivileged POSIX permissions")
    def test_unreadable_lock_is_unknown(self):
        lock = self.base / "lock"
        lock.touch(mode=0)
        try:
            self.assertEqual(audit.lock_state(self.base), "unknown")
            self.assertFalse(self.entry().candidate)
        finally:
            lock.chmod(0o600)

    def test_shared_cache_requires_level_three_and_no_processes(self):
        repo = self.root / "repos/v1"
        repo.mkdir(parents=True)
        for snapshot, level, expected in [
            (audit.Processes(), 2, False),
            (audit.Processes(), 3, True),
            (audit.Processes([audit.Process(123, "/outside/scanned/roots")]), 3, False),
            (audit.Processes(errors=["incomplete"]), 3, False),
        ]:
            with self.subTest(level=level, snapshot=snapshot):
                entry = audit.Entry(str(repo), "repository-cache")
                audit.classify(entry, snapshot, level)
                self.assertEqual(entry.candidate, expected)

    def test_discovery_supports_standard_and_custom_names(self):
        userroot = self.root / "cache/_bazel_example"
        hashed = userroot / ("a" * 32)
        hashed.mkdir(parents=True)
        (hashed / "DO_NOT_BUILD_HERE").write_bytes(os.fsencode(self.workspace))
        (userroot / "cache/repos/v1/contents").mkdir(parents=True)
        other = self.root / "cache/_bazel_other"
        other.mkdir()
        (other / "DO_NOT_BUILD_HERE").write_bytes(b"/not/this/user")
        entries, warnings = audit.discover([self.root / "cache"], "example", [])
        self.assertFalse(warnings)
        self.assertEqual({e.path for e in entries}, {
            str(self.base), str(hashed), str(userroot / "cache/repos/v1"),
        })

    def test_direct_output_base_and_repository_override(self):
        repo = self.root / "shared/repositories"
        repo.mkdir(parents=True)
        entries, warnings = audit.discover([self.base], "example", [repo, repo])
        self.assertFalse(warnings)
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.kind for e in entries}, {"output-base", "repository-cache"})

    def test_hash_without_metadata_is_kept(self):
        base = self.root / "cache" / ("a" * 32)
        base.mkdir()
        entries, _ = audit.discover([base.parent], "example", [])
        entry = next(e for e in entries if e.path == str(base))
        audit.classify(entry, audit.Processes(), 3)
        self.assertFalse(entry.candidate)

    def test_custom_user_root_is_discovered(self):
        root = self.root / "cache/custom-version"
        (root / "cache/repos/v1").mkdir(parents=True)
        entries, warnings = audit.discover([root.parent], "example", [])
        self.assertFalse(warnings)
        self.assertIn(str(root / "cache/repos/v1"), {e.path for e in entries})

    @unittest.skipUnless(os.name == "posix", "requires POSIX links")
    def test_sizes_ignore_symlinks_and_count_hardlinks_once(self):
        file = self.base / "artifact"
        file.write_bytes(b"12345")
        (self.base / "alias").symlink_to(self.root, target_is_directory=True)
        os.link(file, self.base / "hardlink")
        self.assertEqual(audit.measure(self.base), (5 + len(os.fsencode(self.workspace)), True))

    def test_json_cli_does_not_change_fixture(self):
        def tree():
            return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                    p.read_bytes() if p.is_file() else None) for p in self.root.rglob("*")}
        before = tree()
        output = io.StringIO()
        with patch.object(audit, "processes", return_value=audit.Processes()), contextlib.redirect_stdout(output):
            self.assertEqual(audit.main(["--base-dir", str(self.base), "--json", "--sizes"]), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["entries"][0]["candidate"])
        self.assertEqual(before, tree())

    def test_process_warning_is_json_and_exit_one(self):
        output = io.StringIO()
        with patch.object(audit, "processes", return_value=audit.Processes(errors=["incomplete"])), contextlib.redirect_stdout(output):
            code = audit.main(["--base-dir", str(self.base), "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["entries"][0]["candidate"])

    @unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "darwin", "requires supported process detection")
    def test_finds_a_live_process_with_spaces_in_base(self):
        child = subprocess.Popen([
            sys.executable, "-c", "import sys; print('ready',flush=True); sys.stdin.read()",
            "--product_name=Bazel", f"--output_base={self.base}",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            matches = [p for p in audit.processes().bazel if p.pid == child.pid]
            self.assertEqual(matches, [audit.Process(child.pid, str(self.base))])
        finally:
            child.communicate(timeout=10)

    def test_macos_process_parser_keeps_spaces(self):
        output = f" {os.getpid()} python3 audit.py\n 123 bazel(project) -jar /install/A-server.jar --output_base={self.base} --workspace_directory=/source\n"
        with patch.object(audit.sys, "platform", "darwin"), patch.object(audit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
            result = audit.processes()
            self.assertFalse(result.errors)
            self.assertEqual(result.bazel, [audit.Process(123, str(self.base))])

    def test_empty_ps_output_is_incomplete(self):
        with patch.object(audit.sys, "platform", "darwin"), patch.object(audit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")):
            self.assertTrue(audit.processes().errors)

    def test_empty_ps_command_is_incomplete(self):
        output = f"{os.getpid()} python3 audit.py\n 123 \n"
        with patch.object(audit.sys, "platform", "darwin"), patch.object(audit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
            self.assertTrue(audit.processes().errors)

    def test_missing_explicit_base_directory_is_a_warning(self):
        output = io.StringIO()
        with patch.object(audit, "processes", return_value=audit.Processes()), contextlib.redirect_stdout(output):
            code = audit.main(["--base-dir", str(self.root / "missing"), "--json"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output.getvalue())["warnings"])

    def test_ps_failure_is_incomplete(self):
        with patch.object(audit.sys, "platform", "darwin"), patch.object(audit.subprocess, "run", side_effect=FileNotFoundError("ps")):
            self.assertTrue(audit.processes().errors)

    def test_defaults_respect_environment(self):
        with patch.dict(os.environ, {"TEST_TMPDIR": str(self.root)}, clear=True):
            self.assertEqual(audit.default_roots(audit.getpass.getuser()), [self.root])
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root)}, clear=True), patch.object(audit.sys, "platform", "linux"):
            self.assertIn(self.root / "bazel", audit.default_roots(audit.getpass.getuser()))

    def test_macos_defaults_include_both_layouts(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(audit.sys, "platform", "darwin"):
            roots = audit.default_roots(audit.getpass.getuser())
            self.assertIn(Path.home() / "Library/Caches/bazel", roots)
            self.assertIn(Path("/private/var/tmp") / f"_bazel_{audit.getpass.getuser()}", roots)

    def test_unsupported_process_backend_is_incomplete(self):
        with patch.object(audit.sys, "platform", "win32"):
            self.assertTrue(audit.processes().errors)

    def delete(self, entry=None, level=1, snapshot=None):
        entry = entry or self.entry(level=level)
        with patch.object(audit, "processes", return_value=snapshot or audit.Processes()):
            return audit.delete_entry(entry, level)

    def test_delete_orphan_removes_only_cache(self):
        self.workspace.parent.mkdir(parents=True)
        sentinel = self.workspace.parent / "keep"
        sentinel.write_text("source")
        result = self.delete()
        self.assertTrue(result.deleted)
        self.assertFalse(result.candidate)
        self.assertIsNone(result.delete_error)
        self.assertFalse(self.base.exists())
        self.assertEqual(sentinel.read_text(), "source")
        self.assertFalse(list(self.base.parent.glob(".bazel-cache-audit-*")))

    def test_delete_level_one_keeps_existing_workspace_cache(self):
        self.workspace.mkdir(parents=True)
        self.assertFalse(self.delete().deleted)
        self.assertTrue(self.base.is_dir())
        self.assertTrue(self.delete(level=2).deleted)
        self.assertTrue(self.workspace.is_dir())

    def test_delete_rechecks_new_live_pid(self):
        planned = self.entry(level=1)
        self.server_file("server.pid.txt", str(os.getpid()).encode())
        result = self.delete(planned)
        self.assertFalse(result.deleted)
        self.assertEqual(result.state, "active")
        self.assertIsNotNone(result.delete_error)
        self.assertTrue(self.base.is_dir())

    def test_delete_rechecks_workspace_appearance(self):
        planned = self.entry(level=1)
        self.workspace.mkdir(parents=True)
        result = self.delete(planned)
        self.assertFalse(result.deleted)
        self.assertEqual(result.level, 2)
        self.assertTrue(self.base.is_dir())

    def test_delete_rechecks_incomplete_process_inventory(self):
        result = self.delete(snapshot=audit.Processes(errors=["incomplete"]))
        self.assertFalse(result.deleted)
        self.assertIsNotNone(result.delete_error)
        self.assertTrue(self.base.is_dir())

    @unittest.skipUnless(os.name == "posix", "requires POSIX locks")
    def test_delete_blocks_new_clients_during_recheck(self):
        planned = self.entry(level=1)

        def inspect():
            script = (
                "import fcntl,sys; f=open(sys.argv[1],'r+'); "
                "\ntry: fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB,1)"
                "\nexcept BlockingIOError: sys.exit(0)"
                "\nelse: sys.exit(1)"
            )
            result = subprocess.run([sys.executable, "-c", script, str(self.base / "lock")], timeout=10)
            self.assertEqual(result.returncode, 0)
            return audit.Processes()

        with patch.object(audit, "processes", side_effect=inspect):
            self.assertTrue(audit.delete_entry(planned, 1).deleted)

    @unittest.skipUnless(os.name == "posix", "requires POSIX locks")
    def test_delete_keeps_cache_if_client_takes_lock_after_audit(self):
        planned = self.entry(level=1)
        lock = self.base / "lock"
        lock.touch()
        script = (
            "import fcntl,sys; f=open(sys.argv[1],'r+'); "
            "fcntl.lockf(f,fcntl.LOCK_EX,1); print('ready',flush=True); sys.stdin.read()"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(lock)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            result = self.delete(planned)
            self.assertFalse(result.deleted)
            self.assertIsNotNone(result.delete_error)
            self.assertTrue(self.base.is_dir())
        finally:
            child.communicate(timeout=10)

    @unittest.skipUnless(os.name == "posix", "requires POSIX links")
    def test_delete_handles_readonly_directories_without_changing_symlink_targets(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("source")
        (self.base / "external-link").symlink_to(outside, target_is_directory=True)
        nested = self.base / "external/repository"
        nested.mkdir(parents=True)
        (nested / "artifact").write_text("generated")
        nested.chmod(0o555)
        outside.chmod(0o555)
        try:
            result = self.delete()
            self.assertTrue(result.deleted, result.delete_error)
            self.assertEqual(sentinel.read_text(), "source")
            self.assertEqual(outside.stat().st_mode & 0o777, 0o555)
        finally:
            outside.chmod(0o755)

    @unittest.skipUnless(os.name == "posix", "requires POSIX links")
    def test_delete_rejects_root_replaced_with_symlink(self):
        planned = self.entry(level=1)
        original = self.base.with_name("original")
        self.base.rename(original)
        self.base.symlink_to(original, target_is_directory=True)
        result = self.delete(planned)
        self.assertFalse(result.deleted)
        self.assertTrue((original / "DO_NOT_BUILD_HERE").is_file())

    def test_delete_rejects_source_checkout(self):
        (self.base / ".git").mkdir()
        result = self.delete()
        self.assertFalse(result.deleted)
        self.assertIn("source checkout", result.delete_error)
        self.assertFalse((self.base / "lock").exists())

    def test_delete_preserves_replacement_directory_if_rename_races(self):
        planned = self.entry(level=1)
        source = self.root / "source-checkout"
        source.mkdir()
        (source / "keep").write_text("source")
        original = self.base.with_name("original")
        rename = Path.rename

        def replace(path, target):
            if path == self.base:
                rename(path, original)
                rename(source, path)
            return rename(path, target)

        with patch.object(audit.Path, "rename", replace):
            result = self.delete(planned)
        self.assertFalse(result.deleted)
        self.assertIn("no contents removed", result.delete_error)
        preserved = next(self.base.parent.glob(".bazel-cache-audit-*/cache/keep"))
        self.assertEqual(preserved.read_text(), "source")
        self.assertTrue((original / "DO_NOT_BUILD_HERE").is_file())

    def test_delete_rejects_home_directory(self):
        planned = self.entry(level=1)
        with patch.object(audit.Path, "home", return_value=self.base):
            result = self.delete(planned)
        self.assertFalse(result.deleted)
        self.assertTrue(self.base.is_dir())

    def test_delete_rejects_cache_containing_workspace(self):
        workspace = self.base / "source"
        workspace.mkdir()
        (self.base / "DO_NOT_BUILD_HERE").write_bytes(os.fsencode(workspace))
        result = self.delete(level=2)
        self.assertFalse(result.deleted)
        self.assertIn("containing its workspace", result.delete_error)
        self.assertTrue(workspace.is_dir())

    def test_delete_shared_cache_rechecks_all_bazel_processes(self):
        repo = self.root / "repository-cache"
        repo.mkdir()
        planned = audit.Entry(str(repo), "repository-cache")
        audit.classify(planned, audit.Processes(), 3)
        result = self.delete(planned, level=3, snapshot=audit.Processes([audit.Process(123, "/elsewhere")]))
        self.assertFalse(result.deleted)
        self.assertTrue(repo.is_dir())
        self.assertTrue(self.delete(planned, level=3).deleted)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires unprivileged POSIX permissions")
    def test_delete_failure_preserves_remaining_data_and_reports_location(self):
        locked = self.base / "unreadable"
        locked.mkdir()
        (locked / "keep").write_text("data")
        locked.chmod(0)
        result = self.delete()
        self.assertFalse(result.deleted)
        self.assertIn("remaining data may be at", result.delete_error)
        moved = next(self.base.parent.glob(".bazel-cache-audit-*/cache"))
        (moved / "unreadable").chmod(0o700)
        self.assertEqual((moved / "unreadable/keep").read_text(), "data")

    def test_delete_cli_reports_deleted(self):
        output = io.StringIO()
        with patch.object(audit, "processes", return_value=audit.Processes()), contextlib.redirect_stdout(output):
            code = audit.main(["--base-dir", str(self.base), "--delete", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(report["delete_requested"])
        self.assertTrue(report["entries"][0]["deleted"])
        self.assertFalse(self.base.exists())

    def test_delete_cli_aborts_on_scan_warning(self):
        output = io.StringIO()
        with patch.object(audit, "processes", return_value=audit.Processes()), contextlib.redirect_stdout(output):
            code = audit.main(["--base-dir", str(self.base), "--base-dir", str(self.root / "missing"), "--delete", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["entries"][0]["deleted"])
        self.assertTrue(self.base.is_dir())


if __name__ == "__main__":
    unittest.main()
