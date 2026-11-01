#!/usr/bin/env python3
"""Identify local Bazel cache deletion candidates; optionally delete them."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
import errno
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


@dataclass
class Process:
    pid: int
    output_base: str | None


@dataclass
class Processes:
    bazel: list[Process] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Entry:
    path: str
    kind: str
    state: str = "unknown"
    level: int | None = None
    candidate: bool = False
    workspace: str | None = None
    pids: list[int] = field(default_factory=list)
    reason: str = ""
    size_bytes: int | None = None
    size_complete: bool | None = None
    deleted: bool = False
    delete_error: str | None = None


def absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def option(argv: list[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def is_bazel(argv: list[str]) -> bool:
    if not argv:
        return False
    name = Path(argv[0]).name.lower()
    return (
        name in {"bazel", "bazelisk", "blaze"}
        or name.startswith(("bazel(", "blaze(", "bazel-", "bazelisk-"))
        or "--product_name=Bazel" in argv
        or any(Path(arg).name.lower() in {"a-server.jar", "bazelserver_deploy.jar"}
               for arg in argv)
    )


def processes() -> Processes:
    result = Processes()
    if sys.platform.startswith("linux"):
        try:
            paths = list(Path("/proc").iterdir())
        except OSError as exc:
            result.errors.append(f"cannot enumerate /proc: {exc.strerror}")
            return result
        if not any(path.name == str(os.getpid()) for path in paths):
            result.errors.append("procfs does not include the auditor's own PID")
        for path in paths:
            if not path.name.isdecimal() or int(path.name) == os.getpid():
                continue
            try:
                argv = [os.fsdecode(arg) for arg in (path / "cmdline").read_bytes().split(b"\0")
                        if arg]
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as exc:
                result.errors.append(f"cannot inspect PID {path.name}: {exc.strerror}")
                continue
            if is_bazel(argv):
                result.bazel.append(Process(int(path.name), option(argv, "--output_base")))
        # A restricted procfs can omit processes entirely, without read errors.
        try:
            mounts = Path("/proc/mounts").read_text()
            if any("hidepid=" in line and not re.search(r"hidepid=(0|off)(,|\s)", line)
                   for line in mounts.splitlines() if " /proc " in line):
                result.errors.append("procfs uses hidepid; process visibility is restricted")
        except OSError:
            result.errors.append("cannot check procfs visibility")
    elif sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["ps", "-axww", "-o", "pid=,args="], check=True,
                capture_output=True, text=True, timeout=15,
            ).stdout
            if not re.search(rf"^\s*{os.getpid()}\s", output, re.MULTILINE):
                result.errors.append("ps does not include the auditor's own PID")
            for line in output.splitlines():
                match = re.match(r"\s*(\d+)\s+(.*)", line)
                if not match:
                    result.errors.append("cannot parse a ps row")
                    continue
                pid, command = match.groups()
                if not command.strip():
                    result.errors.append(f"empty command for PID {pid}")
                    continue
                if int(pid) == os.getpid():
                    continue
                if is_bazel([command.split()[0]]) or re.search(r"(?:^|\s)(?:bazel\(|--product_name=Bazel\b|\S*/A-server.jar\b)", command):
                    base = re.search(r"(?:^|\s)--output_base[= ](.*?)(?=\s--[a-zA-Z_]|$)", command)
                    result.bazel.append(Process(int(pid), base.group(1) if base else None))
        except (OSError, subprocess.SubprocessError) as exc:
            result.errors.append(f"cannot run ps: {exc}")
    else:
        result.errors.append("process detection supports Linux and macOS only")
    return result


def default_roots(user: str) -> list[Path]:
    current = user == getpass.getuser()
    if current:
        home = Path.home()
    else:
        import pwd
        home = Path(pwd.getpwnam(user).pw_dir)
    if current and os.environ.get("TEST_TMPDIR"):
        return [absolute(os.environ["TEST_TMPDIR"])]
    roots = []
    if current and os.environ.get("XDG_CACHE_HOME"):
        roots.append(absolute(os.environ["XDG_CACHE_HOME"]) / "bazel")
    if sys.platform == "darwin":
        roots.extend([home / "Library/Caches/bazel", Path("/private/var/tmp") / f"_bazel_{user}"])
    elif os.name == "nt":
        roots.append(home)
    else:
        roots.append(home / ".cache/bazel")
    return list(dict.fromkeys(roots))


def present(path: Path) -> bool:
    # Unlike Path.exists(), permission errors must not look like missing files.
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def discover(roots: list[Path], user: str, repositories: list[Path]) -> tuple[list[Entry], list[str]]:
    found: dict[str, Entry] = {}
    warnings = []

    def add(path: Path, kind: str) -> None:
        found.setdefault(str(path), Entry(str(path), kind))

    def scan(root: Path, depth: int = 0) -> None:
        try:
            if not present(root):
                return
            if root.is_symlink():
                add(root, "unrecognized")
                return
            if not root.is_dir():
                warnings.append(f"not a directory: {root}")
                return
            if present(root / "DO_NOT_BUILD_HERE") or present(root / "server"):
                add(root, "output-base")
                return
            repo = root / "cache/repos/v1"
            if present(root / "cache"):
                if (root / "cache").is_symlink() or (root / "cache/repos").is_symlink():
                    add(root / "cache", "unrecognized")
                elif present(repo):
                    add(repo, "repository-cache")
            for child in sorted(root.iterdir()):
                if child.name in {"cache", "install"}:
                    continue
                if child.name.startswith("_bazel_") and child.name != f"_bazel_{user}":
                    continue
                if not child.is_dir() and not child.is_symlink():
                    continue
                if child.is_symlink():
                    add(child, "unrecognized")
                elif present(child / "DO_NOT_BUILD_HERE") or present(child / "server"):
                    add(child, "output-base")
                elif depth < 1 and (child.name == f"_bazel_{user}"
                                   or present(child / "cache/repos")
                                   or present(child / "install")):
                    scan(child, depth + 1)
                elif re.fullmatch(r"[0-9a-fA-F]{32}", child.name):
                    add(child, "output-base")
                elif depth > 0:
                    add(child, "unrecognized")
        except OSError as exc:
            warnings.append(f"cannot scan {root}: {exc.strerror}")

    for root in roots:
        scan(root)
    for repo in repositories:
        try:
            if present(repo):
                add(repo, "repository-cache")
            else:
                warnings.append(f"repository cache does not exist: {repo}")
        except OSError as exc:
            warnings.append(f"cannot inspect {repo}: {exc.strerror}")
    return sorted(found.values(), key=lambda entry: entry.path), warnings


def read_metadata(path: Path) -> bytes:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"symlinked metadata: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError(f"oversized metadata: {path.name}")
    return data


def workspace_for(base: Path) -> Path:
    try:
        name = os.fsdecode(read_metadata(base / "DO_NOT_BUILD_HERE"))
    except FileNotFoundError:
        name = None
    try:
        argv = [os.fsdecode(arg) for arg in read_metadata(base / "server/cmdline").split(b"\0")]
    except FileNotFoundError:
        argv = []
    recorded = option(argv, "--workspace_directory")
    if name is None:
        name = recorded
    elif recorded and recorded != name:
        raise ValueError("workspace marker disagrees with server metadata")
    if not name or "\0" in name or not Path(name).is_absolute():
        raise ValueError("workspace metadata is missing or not an absolute path")
    return Path(name)


def lock_state(base: Path) -> str:
    try:
        import fcntl
        if (base / "lock").is_symlink():
            return "unknown"
        # A nonblocking read lock conflicts with Bazel's exclusive client lock.
        # Open an existing file read-only; never create or rewrite it.
        with (base / "lock").open("rb") as stream:
            try:
                fcntl.lockf(stream, fcntl.LOCK_SH | fcntl.LOCK_NB, 1)
            except OSError as exc:
                return "held" if exc.errno in {errno.EACCES, errno.EAGAIN} else "unknown"
            fcntl.lockf(stream, fcntl.LOCK_UN, 1)
        return "free"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    except ImportError:
        return "unknown"


def classify(entry: Entry, snapshot: Processes, level: int, *, locked: bool = False) -> None:
    base = Path(entry.path)
    try:
        if base.is_symlink() or not base.is_dir():
            entry.reason = "symlink or unavailable directory; not followed"
            return
        if entry.kind == "unrecognized":
            entry.reason = "unrecognized directory; not a deletion candidate"
            return
        if entry.kind == "repository-cache":
            if snapshot.bazel:
                entry.state = "active"
                entry.pids = [process.pid for process in snapshot.bazel]
                entry.reason = "shared cache: Bazel processes exist on this host"
            elif snapshot.errors:
                entry.reason = "shared cache: process inventory is incomplete"
            else:
                entry.state, entry.level = "shared", 3
                entry.reason = "no Bazel process detected; clearing this shared cache requires downloads/fetches again"
        else:
            entry.pids = [process.pid for process in snapshot.bazel
                          if process.output_base and Path(process.output_base).is_absolute()
                          and Path(process.output_base).resolve() == base.resolve()]
            try:
                raw_pid = read_metadata(base / "server/server.pid.txt").strip()
                if not raw_pid.isdigit() or len(raw_pid) > 10:
                    raise ValueError("invalid server PID")
                pid = int(raw_pid)
                if pid <= 0 or pid > 2**31 - 1:
                    raise ValueError("invalid server PID")
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    entry.pids.append(pid)
                else:
                    entry.pids.append(pid)
            except FileNotFoundError:
                pass
            # Opening/closing another descriptor would release our POSIX lock.
            lock = "free" if locked else lock_state(base)
            if entry.pids or lock == "held":
                entry.state = "active"
                entry.pids = sorted(set(entry.pids))
                entry.reason = "live PID or Bazel client lock; keep (idle servers also count)"
                return
            if lock == "unknown" or snapshot.errors:
                entry.reason = "cannot establish inactivity: lock or process inspection is incomplete"
                return
            if any(not process.output_base or not Path(process.output_base).is_absolute()
                   for process in snapshot.bazel):
                entry.reason = "Bazel process has no identifiable output base; activity is uncertain"
                return
            workspace = workspace_for(base)
            entry.workspace = str(workspace)
            try:
                info = workspace.stat()
            except FileNotFoundError:
                entry.state, entry.level = "orphaned", 1
                entry.reason = "recorded workspace is missing and no Bazel activity was detected"
            else:
                if not stat.S_ISDIR(info.st_mode):
                    entry.reason = "recorded workspace is not a directory"
                    return
                entry.state, entry.level = "inactive", 2
                entry.reason = "workspace exists and no Bazel activity was detected; next build will rebuild/refetch"
        entry.candidate = entry.level is not None and entry.level <= level
    except (OSError, ValueError) as exc:
        entry.reason = f"cannot verify metadata or workspace: {exc}"


def measure(path: Path) -> tuple[int, bool]:
    seen = set()
    size, complete = 0, True
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.extend(current.iterdir())
            elif stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                size += info.st_size
        except OSError:
            complete = False
    return size, complete


def delete_entry(entry: Entry, level: int) -> Entry:
    if not entry.candidate:
        return entry
    checked = Entry(entry.path, entry.kind, size_bytes=entry.size_bytes,
                    size_complete=entry.size_complete)
    base = Path(entry.path)
    holding = None
    moved = None
    try:
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("this platform cannot safely remove directory trees")
        real = base.resolve()
        if any(path.resolve().is_relative_to(real) for path in (Path.home(), Path.cwd())):
            raise ValueError("refusing to remove a home directory, current directory, or its ancestor")
        if any(present(base / name) for name in (".git", "MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel")):
            raise ValueError("refusing to remove a source checkout")
        if entry.kind not in {"output-base", "repository-cache"}:
            raise ValueError("unrecognized cache kind")
        with ExitStack() as stack:
            directory = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            original = os.fstat(directory)
            locked = entry.kind == "output-base"
            if locked:
                import fcntl
                lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                               dir_fd=directory)
                stack.callback(os.close, lock)
                fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
                if not os.path.samestat(os.fstat(lock), os.stat("lock", dir_fd=directory, follow_symlinks=False)):
                    raise ValueError("client lock changed during inspection")
            classify(checked, processes(), level, locked=locked)
            if not checked.candidate:
                raise ValueError(f"no longer a selected candidate: {checked.reason}")
            if checked.workspace and Path(checked.workspace).resolve().is_relative_to(real):
                raise ValueError("refusing to remove a directory containing its workspace")
            if not os.path.samestat(original, base.lstat()):
                raise ValueError("cache directory changed during inspection")
            # Detach the old tree so a new build cannot populate it while we remove it.
            holding = Path(tempfile.mkdtemp(prefix=".bazel-cache-audit-", dir=base.parent))
            moved = holding / "cache"
            base.rename(moved)
            if not os.path.samestat(original, moved.lstat()):
                raise ValueError("cache directory changed before it was moved; no contents removed")
            if locked:
                # Waiting Bazel clients must reopen the original path, not this old inode.
                (moved / "lock").unlink()

            def walk_error(exc: OSError) -> None:
                raise exc

            # Bazel makes some repository directories read-only. Change only real
            # directories reached through file descriptors, never symlink targets.
            for _, _, _, descriptor in os.fwalk(".", dir_fd=directory, follow_symlinks=False,
                                               onerror=walk_error):
                info = os.fstat(descriptor)
                if info.st_dev != original.st_dev:
                    raise ValueError("cache contains another filesystem; refusing to traverse it")
                if not info.st_mode & stat.S_IWUSR:
                    os.fchmod(descriptor, stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
            shutil.rmtree(moved)
            moved = None
            checked.deleted = True
            checked.candidate = False
    except (OSError, ValueError, ImportError) as exc:
        checked.delete_error = str(exc)
        if moved is not None:
            checked.delete_error += f"; remaining data may be at {moved}"
    finally:
        if holding is not None:
            try:
                holding.rmdir()
            except OSError:
                pass  # Preserve remaining data after a failed removal.
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", action="append", default=[], metavar="PATH",
                        help="output root, output user root, or output base; repeatable; replaces defaults")
    parser.add_argument("--user", default=getpass.getuser(), help="username for default paths and _bazel_USER selection")
    parser.add_argument("--repository-cache", action="append", default=[], metavar="PATH",
                        help="additional shared repository cache; repeatable")
    parser.add_argument("--level", type=int, choices=(1, 2, 3), default=1,
                        help="include candidates through this level (default: 1)")
    parser.add_argument("--sizes", action="store_true", help="sum regular file sizes without following symlinks (can be slow)")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument("--delete", action="store_true",
                        help="delete selected candidates after rechecking them; default is read-only")
    args = parser.parse_args(argv)
    try:
        roots = [absolute(path) for path in args.base_dir] if args.base_dir else default_roots(args.user)
    except (ImportError, KeyError) as exc:
        parser.error(f"cannot find home directory for {args.user!r}; use --base-dir: {exc}")
    entries, warnings = discover(roots, args.user, [absolute(path) for path in args.repository_cache])
    if args.base_dir:
        for root in roots:
            try:
                if not present(root):
                    warnings.append(f"base directory does not exist: {root}")
            except OSError:
                pass  # Discovery already reported this error.
    snapshot = processes()
    warnings.extend(snapshot.errors)
    for index, entry in enumerate(entries):
        classify(entry, snapshot, args.level)
        if args.sizes and entry.kind != "unrecognized" and not Path(entry.path).is_symlink():
            entry.size_bytes, entry.size_complete = measure(Path(entry.path))
        if args.delete and entry.candidate:
            if warnings:
                entry.delete_error = "scan warnings prevent deletion; resolve them and rerun"
            else:
                entries[index] = delete_entry(entry, args.level)
    report = {
        "level": args.level, "delete_requested": args.delete, "roots": [str(root) for root in roots],
        "warnings": warnings, "entries": [asdict(entry) for entry in entries],
        "notice": ("Deletion requested; selected candidates were rechecked." if args.delete else "Read-only snapshot. Stop builds before removal; rerun immediately before acting.")
                  + " Shared/network caches and other PID namespaces require checks on every host/namespace.",
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(report["notice"])
        print("Roots: " + ", ".join(json.dumps(str(root), ensure_ascii=True) for root in roots))
        for entry in entries:
            label = f"L{entry.level}" if entry.level else entry.state.upper()
            choice = "deleted" if entry.deleted else ("candidate" if entry.candidate else "keep")
            print(f"\n[{label} {choice}] {json.dumps(entry.path, ensure_ascii=True)}")
            if entry.workspace:
                print(f"  Workspace: {json.dumps(entry.workspace, ensure_ascii=True)}")
            print(f"  {entry.reason}")
            if entry.delete_error:
                print(f"  Deletion skipped or failed: {entry.delete_error}")
            if entry.pids:
                print(f"  PIDs: {', '.join(map(str, entry.pids))}")
            if entry.size_bytes is not None:
                print(f"  File bytes: {entry.size_bytes:,}" + (" (partial)" if not entry.size_complete else ""))
        print(f"\n{sum(entry.candidate for entry in entries)} candidate(s), "
              f"{sum(entry.deleted for entry in entries)} deleted, {len(entries)} entries; level {args.level}.")
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    return 1 if warnings or any(entry.delete_error for entry in entries) else 0


if __name__ == "__main__":
    sys.exit(main())
