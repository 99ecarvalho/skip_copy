#!/usr/bin/env python3
"""
skip_find.py - find files newer than a given date/time on damaged
media (bad CRC) that can hang kernel syscalls.

Functionally equivalent to:
    find <path> -newermt "2026-04-12" -type f
but resilient to kernel hangs in defective areas of the disk, and
INCREMENTAL (can be run many times; skips what was already scanned).

Logs (absolute paths; can be shared across runs):
- --list-log        FOUND:          <abs_path>  (mtime >= threshold)
- --err-log         FAILURES:       <abs_path>\t# <reason>
- --done-log        PROCESSED OK:   <abs_path>  (every file whose stat
                    succeeded, regardless of mtime)
- --checkpoint-log  DIRECTORIES OK: <abs_dir_path> (subtree 100%
                    processed without failures -- skipped entirely on
                    the next run)

Progress legend (stdout):
    .   file found (mtime >= threshold)
    _   older file (mtime < threshold)
    :   skipped via done-log (already processed in a previous run)
    D   whole subtree skipped via checkpoint
    x   skipped via err-log (--skip-failed)
    X   failure in this run (written to err-log)

Strategy for bad media (same as skip_copy.py):
- Enumeration (scandir) runs in a subprocess with a timeout
  (--scan-timeout). If the directory hangs in the kernel, the child is
  abandoned/killed and the scan continues with the other directories.
- stat() of each file is also done inside the scandir subprocess
  (batched per directory). If stat hangs, the directory timeout fires.
- For files in directories where scandir+stat worked but an individual
  stat failed, a fallback stat in a separate process with
  --stat-timeout is used.
- Status file (--status-file, default /tmp/skip_find.status on tmpfs):
  shows where the script is right now. If everything hangs, run
  'cat /tmp/skip_find.status' to find out.

Incremental use:
- Run on priority subdirectories first, then on broader directories --
  the logs are shared and whatever was already scanned is skipped
  automatically (done-log and checkpoint).
- --list-log ALWAYS appends (previous results are never lost).
- To force a rescan, use --no-checkpoint and/or delete the done-log.

Ctrl+C:
- once  -> skip the current directory/operation and continue.
- twice quickly (<2s) -> exit the program.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import signal
import sys
import time
from datetime import datetime


# ---------------------------------------------------------------------------
# global state for signal handling
# ---------------------------------------------------------------------------

class _Ctrl:
    abort_current = False
    last_int = 0.0
    int_count = 0

CTRL = _Ctrl()


def _install_sigint() -> None:
    def handler(signum, frame):
        now = time.monotonic()
        if now - CTRL.last_int < 2.0:
            CTRL.int_count += 1
        else:
            CTRL.int_count = 1
        CTRL.last_int = now
        if CTRL.int_count >= 2:
            sys.stderr.write("\n[interrupt] second Ctrl+C, exiting HARD.\n")
            os._exit(130)
        sys.stderr.write(
            "\n[interrupt] skipping current operation "
            "(Ctrl+C again within 2s to exit).\n")
        CTRL.abort_current = True
    signal.signal(signal.SIGINT, handler)


# ---------------------------------------------------------------------------
# non-blocking kill (child process stuck in D-state)
# ---------------------------------------------------------------------------

def _kill_nb(p: mp.Process) -> None:
    """Kill the child without blocking. If it is in D-state in the kernel,
    the kill will do nothing -- so we do NOT wait indefinitely."""
    try:
        if p.is_alive():
            p.terminate()
    except Exception:
        pass
    try:
        p.join(0.5)
    except Exception:
        pass
    try:
        if p.is_alive():
            p.kill()
    except Exception:
        pass
    try:
        p.join(0.5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# scandir + stat in a subprocess with timeout
# ---------------------------------------------------------------------------

def _scan_stat_worker(d: str, q) -> None:
    """Child: list the directory with scandir and stat each file.
    Returns a list of (name, kind, mtime_ns) via the queue.
    mtime_ns = -1 if stat failed for that file."""
    signal.signal(signal.SIGTERM, lambda *_: os._exit(2))
    signal.signal(signal.SIGINT, lambda *_: os._exit(2))
    try:
        result = []
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_symlink():
                        kind = "skip"
                        result.append((e.name, kind, -1))
                        continue
                    elif e.is_dir(follow_symlinks=False):
                        kind = "dir"
                        result.append((e.name, kind, -1))
                        continue
                    elif e.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        kind = "skip"
                        result.append((e.name, kind, -1))
                        continue
                except OSError:
                    kind = "unknown"
                    result.append((e.name, kind, -1))
                    continue

                # stat to get mtime
                try:
                    st = e.stat(follow_symlinks=False)
                    mtime_ns = st.st_mtime_ns
                except OSError:
                    mtime_ns = -1
                result.append((e.name, kind, mtime_ns))
        result.sort()
        q.put(("ok", result))
    except OSError as ex:
        q.put(("err", str(ex)))
    except Exception as ex:
        q.put(("err", repr(ex)))


def safe_scandir_stat(d: str, timeout: float):
    """List entries of `d` with stat in a subprocess.
    Returns (entries, error) where entries = [(name, kind, mtime_ns), ...] or None."""
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_scan_stat_worker, args=(d, q))
    p.daemon = True
    p.start()
    p.join(timeout)
    if p.is_alive():
        _kill_nb(p)
        return None, f"scan-timeout>{timeout:g}s"
    try:
        status, payload = q.get_nowait()
    except Exception:
        return None, "scan-no-result"
    if status == "ok":
        return payload, None
    return None, f"scan-error: {payload}"


# ---------------------------------------------------------------------------
# individual stat in a subprocess (fallback for files whose stat failed)
# ---------------------------------------------------------------------------

def _stat_worker(path: str, q) -> None:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(2))
    signal.signal(signal.SIGINT, lambda *_: os._exit(2))
    try:
        st = os.lstat(path)
        q.put(("ok", st.st_mtime_ns))
    except OSError as ex:
        q.put(("err", str(ex)))
    except Exception as ex:
        q.put(("err", repr(ex)))


def safe_stat_mtime(path: str, timeout: float):
    """Run stat in a subprocess. Returns (mtime_ns, error)."""
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_stat_worker, args=(path, q))
    p.daemon = True
    p.start()
    p.join(timeout)
    if p.is_alive():
        _kill_nb(p)
        return None, f"stat-timeout>{timeout:g}s"
    try:
        status, payload = q.get_nowait()
    except Exception:
        return None, "stat-no-result"
    if status == "ok":
        return payload, None
    return None, f"stat-error: {payload}"


# ---------------------------------------------------------------------------
# status file
# ---------------------------------------------------------------------------

class StatusFile:
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "w", buffering=1)

    def set(self, phase: str, path: str) -> None:
        try:
            self.f.seek(0)
            self.f.truncate()
            self.f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{phase}\t{path}\n")
            self.f.flush()
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.f.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# load skip-dir set
# ---------------------------------------------------------------------------

def load_path_set(path: str) -> set:
    s = set()
    if not path or not os.path.exists(path):
        return s
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                p = line.split("\t", 1)[0].strip()
                if p:
                    s.add(p)
    except OSError as e:
        print(f"warning: could not read {path}: {e}", file=sys.stderr)
    return s


# ---------------------------------------------------------------------------
# date/time parsing
# ---------------------------------------------------------------------------

def parse_newermt(s: str) -> float:
    """Convert a date or date+time string to a timestamp (epoch seconds).
    Accepted formats: YYYY-MM-DD, YYYY-MM-DD HH:MM, YYYY-MM-DD HH:MM:SS"""
    s = s.strip().strip('"').strip("'")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(
        f"invalid date format: '{s}'. "
        f"Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'")


# ---------------------------------------------------------------------------
# recursive walk with per-directory checkpoint
# ---------------------------------------------------------------------------

class FindContext:
    def __init__(self, args, src_root, threshold_ns, skip_dirs,
                 done, failed, checkpoint,
                 list_log, err_log, done_log, ckpt_log, status):
        self.args = args
        self.src_root = src_root
        self.threshold_ns = threshold_ns  # nanoseconds
        self.skip_dirs = skip_dirs
        self.done = done            # set of absolute paths already processed
        self.failed = failed        # set of paths that failed previously
        self.checkpoint = checkpoint  # set of directories that are 100% OK
        self.list_log = list_log
        self.err_log = err_log
        self.done_log = done_log
        self.ckpt_log = ckpt_log
        self.status = status
        # counters
        self.dirs_scanned = 0
        self.dirs_failed = 0
        self.dirs_skipped = 0
        self.skipped_ckpt_dirs = 0
        self.files_found = 0
        self.files_older = 0
        self.files_stat_failed = 0
        self.skipped_done = 0
        self.skipped_fail = 0
        # progress
        self.progress_t0 = time.monotonic()
        self.last_report = time.monotonic()


def _covers_skip(path: str, skip_dirs: set) -> bool:
    """True if path or any of its ancestors is in skip_dirs."""
    if not skip_dirs:
        return False
    p = path.rstrip("/") or "/"
    if p in skip_dirs:
        return True
    while True:
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent
        if p in skip_dirs:
            return True


def _print_status(ctx: FindContext, force: bool = False) -> None:
    now = time.monotonic()
    if not force and (now - ctx.last_report) < 10.0:
        return
    ctx.last_report = now
    elapsed = now - ctx.progress_t0
    sys.stdout.write(
        f"\n[{elapsed:.0f}s] dirs={ctx.dirs_scanned} "
        f"found={ctx.files_found} older={ctx.files_older} "
        f"skip(done={ctx.skipped_done},fail={ctx.skipped_fail},"
        f"ckpt={ctx.skipped_ckpt_dirs}) "
        f"err={ctx.files_stat_failed} dir-fail={ctx.dirs_failed} "
        f"dir-skip={ctx.dirs_skipped}\n")
    sys.stdout.flush()


def _process_file(full: str, ctx: FindContext) -> bool:
    """Process a file. Returns True if OK (for the parent's checkpoint),
    False if there was a failure that prevents the checkpoint, or None
    if the file still needs a stat."""
    args = ctx.args

    # 1) already in done-log: skip
    if full in ctx.done:
        ctx.skipped_done += 1
        sys.stdout.write(":"); sys.stdout.flush()
        return True

    # 2) already in err-log and --skip-failed: skip
    if args.skip_failed and full in ctx.failed:
        ctx.skipped_fail += 1
        sys.stdout.write("x"); sys.stdout.flush()
        return False  # previous failure prevents checkpoint

    return None  # needs stat


def _process_dir(d: str, ctx: FindContext) -> bool:
    """Process a directory recursively. Returns True if the whole subtree
    was 100% processed OK -- in that case it is written to the checkpoint."""
    args = ctx.args

    if CTRL.abort_current:
        CTRL.abort_current = False
        return False

    # checkpoint: subtree already marked as complete
    if not args.no_checkpoint and d in ctx.checkpoint:
        ctx.skipped_ckpt_dirs += 1
        sys.stdout.write("D"); sys.stdout.flush()
        return True

    # skip-dir: whole subtree skipped (does not count as checkpoint)
    if _covers_skip(d, ctx.skip_dirs):
        ctx.dirs_skipped += 1
        sys.stdout.write("D"); sys.stdout.flush()
        return True

    if ctx.status:
        ctx.status.set("dir:scan", d)

    entries, scan_err = safe_scandir_stat(d, args.scan_timeout)
    if scan_err is not None:
        ctx.dirs_failed += 1
        sys.stderr.write(f"\n[scandir-fail] {d}: {scan_err}\n")
        ctx.err_log.write(f"{d}\t# {scan_err}\n")
        ctx.err_log.flush()
        _print_status(ctx)
        return False

    ctx.dirs_scanned += 1
    all_good = True

    subdirs = []
    for name, kind, mtime_ns in entries:
        if CTRL.abort_current:
            CTRL.abort_current = False
            return False

        full = os.path.join(d, name)

        if kind == "unknown":
            ctx.files_stat_failed += 1
            ctx.err_log.write(f"{full}\t# type-unknown\n")
            sys.stdout.write("X"); sys.stdout.flush()
            all_good = False
            continue
        if kind == "skip":
            continue
        if kind == "dir":
            subdirs.append(full)
            continue

        # kind == "file"
        # check done/failed first (no stat needed)
        skip_result = _process_file(full, ctx)
        if skip_result is True:
            _print_status(ctx)
            continue
        if skip_result is False:
            all_good = False
            _print_status(ctx)
            continue

        # need stat -- check mtime_ns from batch
        if mtime_ns == -1:
            # stat failed in the batch, try an individual stat
            if ctx.status:
                ctx.status.set("file:stat", full)
            mtime_ns_retry, stat_err = safe_stat_mtime(full, args.stat_timeout)
            if stat_err is not None:
                ctx.files_stat_failed += 1
                ctx.err_log.write(f"{full}\t# {stat_err}\n")
                ctx.err_log.flush()
                sys.stdout.write("X"); sys.stdout.flush()
                all_good = False
                _print_status(ctx)
                continue
            mtime_ns = mtime_ns_retry

        # stat OK -- register in done-log
        ctx.done_log.write(full + "\n")
        ctx.done_log.flush()
        ctx.done.add(full)

        if mtime_ns >= ctx.threshold_ns:
            ctx.files_found += 1
            ctx.list_log.write(full + "\n")
            ctx.list_log.flush()
            sys.stdout.write("."); sys.stdout.flush()
        else:
            ctx.files_older += 1
            if args.verbose:
                sys.stdout.write("_"); sys.stdout.flush()

        _print_status(ctx)

    # recurse subdirs
    for sd in subdirs:
        sub_ok = _process_dir(sd, ctx)
        if not sub_ok:
            all_good = False

    # checkpoint: if everything is OK and not in no-checkpoint mode
    if all_good and not args.no_checkpoint:
        ctx.ckpt_log.write(d + "\n")
        ctx.ckpt_log.flush()
        ctx.checkpoint.add(d)
    return all_good


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Find files newer than a date on damaged media "
                    "(CRC errors) without hanging on syscalls. "
                    "Incremental: run it many times; it skips what was "
                    "already scanned.")
    ap.add_argument("src", help="root directory of the search")
    ap.add_argument("--newermt", required=True,
                    help="date/time threshold. Formats: "
                         "'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'. "
                         "Files with mtime >= this value are listed.")
    ap.add_argument("--list-log", default="/root/skip_find.list.log",
                    help="output: list of files found (append, "
                         "default: /root/skip_find.list.log)")
    ap.add_argument("--err-log", default="/root/skip_find.err.log",
                    help="output: errors/timeouts (append, "
                         "default: /root/skip_find.err.log)")
    ap.add_argument("--done-log", default="/root/skip_find.done.log",
                    help="log of files already processed (stat OK). "
                         "They are skipped on the next run. "
                         "(default: /root/skip_find.done.log)")
    ap.add_argument("--checkpoint-log", default="/root/skip_find.dirs.log",
                    help="log of 100%% processed directories. "
                         "They are skipped entirely on the next run. "
                         "(default: /root/skip_find.dirs.log)")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="disable the per-directory checkpoint "
                         "(revisits the whole tree)")
    ap.add_argument("--skip-failed", action="store_true",
                    help="skip files that are already in the err-log "
                         "(does not retry damaged areas)")
    ap.add_argument("--skip-dir", default="",
                    help="file with absolute paths of directories to skip "
                         "entirely (one per line). Use '' to disable.")
    ap.add_argument("--scan-timeout", type=float, default=30.0,
                    help="timeout for scandir+stat of ONE directory "
                         "(default 30s)")
    ap.add_argument("--stat-timeout", type=float, default=10.0,
                    help="timeout for the individual stat of ONE file "
                         "(fallback when the batch stat fails, default 10s)")
    ap.add_argument("--status-file", default="/tmp/skip_find.status",
                    help="status file (default: /tmp/skip_find.status). "
                         "Use '' to disable.")
    ap.add_argument("--verbose", action="store_true",
                    help="show '_' for each file older than the "
                         "threshold (default: silent)")
    args = ap.parse_args()

    # parse threshold
    try:
        threshold = parse_newermt(args.newermt)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    threshold_ns = int(threshold * 1_000_000_000)

    src_root = os.path.abspath(args.src.rstrip("/") or "/")
    if not os.path.isdir(src_root):
        print(f"error: not a directory: {src_root}", file=sys.stderr)
        return 2

    # skip-dirs (manual)
    skip_dirs = load_path_set(args.skip_dir) if args.skip_dir else set()

    # ensure log dirs exist
    for p in (args.list_log, args.err_log, args.done_log, args.checkpoint_log):
        d = os.path.dirname(p) or "."
        if d != ".":
            os.makedirs(d, exist_ok=True)

    # load previous logs
    done = load_path_set(args.done_log)
    failed = load_path_set(args.err_log) if args.skip_failed else set()
    checkpoint = set() if args.no_checkpoint else load_path_set(args.checkpoint_log)

    if done:
        print(f"done-log:       {len(done)} files previously processed")
    if failed:
        print(f"err-log:        {len(failed)} previously failed (--skip-failed)")
    if checkpoint:
        print(f"checkpoint-log: {len(checkpoint)} directories 100% complete")

    _install_sigint()

    started = datetime.now()

    status = StatusFile(args.status_file) if args.status_file else None
    if status:
        status.set("start", src_root)
        print(f"status-file: {args.status_file}")

    # all logs in append mode
    list_log = open(args.list_log, "a", buffering=1)
    err_log = open(args.err_log, "a", buffering=1)
    done_log = open(args.done_log, "a", buffering=1)
    ckpt_log = open(args.checkpoint_log, "a", buffering=1)

    hdr = (f"# skip_find started at {started.isoformat(timespec='seconds')}\n"
           f"# root={src_root} newermt={args.newermt} "
           f"(threshold={datetime.fromtimestamp(threshold).isoformat()}) "
           f"scan-timeout={args.scan_timeout}s stat-timeout={args.stat_timeout}s "
           f"skip_failed={args.skip_failed} no_checkpoint={args.no_checkpoint}\n")
    list_log.write(hdr)
    err_log.write(hdr)
    done_log.write(hdr)
    ckpt_log.write(hdr)

    print(f"root:      {src_root}")
    print(f"newermt:   {args.newermt} "
          f"(>= {datetime.fromtimestamp(threshold).isoformat()})")
    print(f"list-log:  {args.list_log}")
    print(f"err-log:   {args.err_log}")
    print(f"done-log:  {args.done_log}")
    print(f"checkpoint:{args.checkpoint_log}")
    if skip_dirs:
        print(f"skip-dirs: {len(skip_dirs)} directories to skip")
    print(flush=True)

    ctx = FindContext(args, src_root, threshold_ns, skip_dirs,
                      done, failed, checkpoint,
                      list_log, err_log, done_log, ckpt_log, status)

    try:
        _process_dir(src_root, ctx)
    finally:
        if status:
            status.set("done", src_root)
            status.close()
        ended = datetime.now()
        elapsed = (ended - started).total_seconds()
        sys.stdout.write("\n")
        summary = (f"dirs_scanned={ctx.dirs_scanned} "
                   f"dirs_failed={ctx.dirs_failed} "
                   f"dirs_skipped={ctx.dirs_skipped} "
                   f"ckpt_dirs={ctx.skipped_ckpt_dirs} "
                   f"files_found={ctx.files_found} "
                   f"files_older={ctx.files_older} "
                   f"skip(done={ctx.skipped_done},fail={ctx.skipped_fail}) "
                   f"stat_failed={ctx.files_stat_failed} "
                   f"elapsed={elapsed:.1f}s")
        tail = (f"# skip_find finished at "
                f"{ended.isoformat(timespec='seconds')}  {summary}\n")
        list_log.write(tail); list_log.close()
        err_log.write(tail); err_log.close()
        done_log.write(tail); done_log.close()
        ckpt_log.write(tail); ckpt_log.close()
        print(summary)
        print(f"Found:       {args.list_log} ({ctx.files_found} new in this run)")
        print(f"Processed:   {args.done_log}")
        print(f"Checkpoint:  {args.checkpoint_log}")
        print(f"Errors:      {args.err_log}")

    return 0 if ctx.dirs_failed == 0 and ctx.files_stat_failed == 0 else 1


if __name__ == "__main__":
    rc = main()
    os._exit(rc)  # hard exit: avoids a hang in multiprocessing atexit
