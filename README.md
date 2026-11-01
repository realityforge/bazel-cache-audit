# bazel-cache-audit

Find local Bazel cache directories that may be worth deleting. The script is read-only: it reports paths, evidence, and a safety level. It never deletes files, stops processes, or runs Bazel.

Requires Python 3.10 or newer. Uses only the standard library. Process detection supports Linux (`/proc`) and macOS (`ps`).

## Run

```sh
git clone https://github.com/realityforge/bazel-cache-audit.git
cd bazel-cache-audit
python3 bazel_cache_audit.py
```

Or download the single script to another host:

```sh
curl -fSLO https://raw.githubusercontent.com/realityforge/bazel-cache-audit/main/bazel_cache_audit.py
python3 bazel_cache_audit.py
```

The default report selects only level 1 candidates. All discovered entries remain visible so you can see why others are kept.

```sh
# Include inactive workspaces, with optional file-size estimates.
python3 bazel_cache_audit.py --level 2 --sizes

# Include shared repository caches; use JSON for further processing.
python3 bazel_cache_audit.py --level 3 --json

# Scan an explicit output root, output user root, or individual output base.
python3 bazel_cache_audit.py --base-dir /path/to/bazel-cache --level 2

# Scan multiple roots and a repository cache stored elsewhere.
python3 bazel_cache_audit.py \
  --base-dir /path/to/first-root \
  --base-dir /path/to/second-root \
  --repository-cache /path/to/shared-repository-cache \
  --level 3

# Inspect another local user's standard layout, subject to OS permissions.
python3 bazel_cache_audit.py --user example
```

Use `--help` for all options. `--base-dir` replaces the default roots. `--repository-cache` adds an explicitly identified repository cache; it does not replace root discovery. No hostnames, usernames, workspace paths, credentials, or Bazel configuration are built into the script.

## Levels

Higher levels include the lower ones. An active or uncertain entry is always kept, regardless of the selected level.

| Level | Candidate | Cost of removal |
| --- | --- | --- |
| 1 | Output base whose recorded workspace directory no longer exists, with no detected Bazel activity for that output base | Discards the removed workspace's build state. |
| 2 | Output base whose workspace still exists, with no detected Bazel activity for that output base | Loses incremental build state and fetched repositories for that workspace; the next build rebuilds/refetches. |
| 3 | Shared repository cache, with no detected Bazel process anywhere in the visible host process inventory | Loses cached downloads and fetched repository contents used across workspaces. Builds may need network access again. |

"No process" includes idle Bazel servers: a live server PID keeps its output base even when no build is running. A held Bazel client lock also keeps it. An unmapped Bazel process keeps all output bases because the script cannot establish which one it uses. Repository caches are kept whenever any Bazel process is detected, including processes outside the scanned directories and processes belonging to other users.

Missing workspace metadata, malformed PID files, conflicting metadata, permission errors, restricted process visibility, and symlinked cache directories or metadata prevent a candidate classification. A live PID is kept conservatively even if it might have been recycled for another process.

## Discovery

On Linux, discovery checks `~/.cache/bazel` and `$XDG_CACHE_HOME/bazel` when set. On macOS, it checks `~/Library/Caches/bazel`, `$XDG_CACHE_HOME/bazel` when set, and `/private/var/tmp/_bazel_USER` for Bazel 8 and earlier. `$TEST_TMPDIR`, when set, replaces these defaults. Another user's home comes from the local account database; the caller's cache environment overrides are ignored for `--user` when it names another user.

Under each root, the script checks `_bazel_USER`, immediate custom output bases, and immediate custom output user roots containing `install` or `cache/repos`. It does not recursively search the entire filesystem. Other `_bazel_*` users are skipped unless selected with `--user`. For custom locations or another user's XDG configuration, pass explicit paths.

Workspace paths come from Bazel's `DO_NOT_BUILD_HERE` marker, with `server/cmdline` as a fallback. Directory hashes alone cannot recover the original workspace path. Metadata-free hash directories are kept. Default repository caches are discovered at `cache/repos/v1` within each output user root. Custom repository caches need `--repository-cache`.

Yes, the repository cache is separate from per-workspace output bases. It normally lives at `<output_user_root>/cache/repos/v1`; newer Bazel versions can also share fetched directory contents under its `contents` subdirectory. That subtree is reported once as part of the repository cache. The `external` directory inside an output base belongs to that workspace's build state. Install bases, Bazelisk downloads, disk/action caches configured with `--disk_cache`, and remote caches are outside this tool's scope.

See Bazel's [output directory layout](https://bazel.build/remote/output-directories), [repository cache option](https://bazel.build/reference/command-line-reference#flag--repository_cache), and [repository contents cache option](https://bazel.build/reference/command-line-reference#flag--repo_contents_cache).

## Before removing anything

A candidate is evidence from one moment, not a guarantee against a concurrent build. Stop new builds, shut down the relevant Bazel servers, and rerun the audit immediately before removing reviewed paths. The script releases its temporary read lock after checking it; it does not reserve directories for deletion or generate removal commands.

Mount workspace volumes before auditing: an unmounted workspace can look deleted. For shared/network caches, containers, PID namespaces, or caches accessible from other hosts, check all users and hosts separately. Local process inspection cannot establish that a remote host is idle. Keep caches needed for offline work or dependencies that may no longer be downloadable.

`--sizes` sums regular file lengths, skips symlinks, and counts each hard link once per entry. It can be slow and is an estimate of file bytes, not space guaranteed to be reclaimed; sparse files, compression, hard links outside the directory, and open deleted files affect disk usage. Partial measurements are marked in the report.

Reports contain local paths and PIDs. Review them before sharing. Reports omit environment dumps and process command lines.

## JSON and exit status

Each entry includes `path`, `kind`, `state`, `level`, `candidate`, `workspace`, `pids`, `reason`, and optional size results. Filter on `candidate: true`, not on the level alone. `level` is null when activity or uncertainty prevents classification.

Exit status is `0` for a completed scan, `1` for scan/process warnings (a partial report is still printed), and `2` for invalid arguments. An entry marked `unknown` may appear in an otherwise completed scan. No discovered entries is a valid empty result; the report lists the roots it checked. Windows process detection is unsupported and cannot produce candidates.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary synthetic workspaces and caches, including real process liveness and POSIX file locks. CI runs on Linux and macOS.
