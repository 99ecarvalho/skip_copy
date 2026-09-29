#!/usr/bin/env python3
"""
skip_copy.py - copies files from SOURCE to DESTINATION, quickly skipping
files that raise I/O errors (CRC errors on bad media) and logging them.

Logs (absolute source paths; can be shared across runs):
- --log              FAILURES:      <abs_path>\\t# <reason> (<dt>s)
- --done-log         SUCCESSES:     <abs_path>
- --checkpoint-log   DIRS OK:       <abs_dir_path> (subtree 100% copied
                     without failures; skipped entirely on the next run)
- --accept-loss      ACCEPTED LOSSES (input): absolute paths -- files
                     OR directories -- that you accept losing. Failures
                     inside them do not prevent parent directories from
                     being checkpointed. Default: /root/accept_loss.txt

Progress legend (stdout):
    .   copied now
    :   skipped via done-log (already copied in a previous run)
    =   destination already existed with the same size (--check-size)
    D   entire subtree skipped via checkpoint
    L   skipped via --accept-loss (accepted loss)
    x   skipped via err-log (--skip-failed)
    X   failed in this run (written to err-log)

Strategy for bad media:
- Enumeration via os.scandir WITHOUT a per-file stat (uses d_type), and
  on top of that inside a subprocess with a timeout (--scan-timeout) for
  directories in damaged areas that hang the listing in the kernel.
- Copying happens in a child process monitored by the parent:
    * --stall N   : abort if the destination does not grow for N seconds.
    * --timeout N : optional absolute cap per file.
    * no retry.
- Killing a stuck child is NON-BLOCKING (in case the kernel leaves the
  process in D-state due to hung hardware). The parent moves on and the
  child becomes an orphan/zombie -- the rest of the copy continues.
- Status file (--status-file, default /tmp/skip_copy.status on tmpfs):
  records what is being done right now; if things hang, just 'cat' the
  file to find out where it stopped.

Ctrl+C:
- 1x  -> abort the current file and continue.
- 2x quickly (<2s) -> exit the program.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import shutil
import signal
import stat as _stat
import sys
import time
from datetime import datetime


# ---------------------------------------------------------------------------
# global state for signal handling
# ---------------------------------------------------------------------------

class _Ctrl:
    abort_current = False   # tells copy_one to give up on the current file
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
            "\n[interrupt] skipping current file "
            "(Ctrl+C again within 2s to exit).\n")
        CTRL.abort_current = True
    signal.signal(signal.SIGINT, handler)


# ---------------------------------------------------------------------------
# copy worker
# ---------------------------------------------------------------------------

def _copy_worker(src: str, dst: str, chunk: int, preserve: bool) -> None:
    """Child: exits 0 on success, !=0 on failure. No retry."""
    signal.signal(signal.SIGTERM, lambda *_: os._exit(2))
    signal.signal(signal.SIGINT, lambda *_: os._exit(2))
    try:
        src_fd = os.open(src, os.O_RDONLY)
        try:
            dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                while True:
                    buf = os.read(src_fd, chunk)
                    if not buf:
                        break
                    mv = memoryview(buf)
                    while mv:
                        n = os.write(dst_fd, mv)
                        mv = mv[n:]
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)

        if preserve:
            try:
                shutil.copystat(src, dst, follow_symlinks=False)
            except OSError:
                pass
        os._exit(0)
    except OSError as e:
        sys.stderr.write(f"[worker] {src}: {e}\n")
        os._exit(1)
    except Exception as e:
        sys.stderr.write(f"[worker] {src}: {e!r}\n")
        os._exit(3)


def _kill_nb(p: mp.Process) -> None:
    """Kill the child without blocking. If it is stuck in D-state in the kernel,
    not even SIGKILL will do anything -- so we do NOT wait. The rest of the
    run continues and the child becomes an orphan/zombie."""
    try:
        if p.is_alive():
            p.terminate()
    except Exception:
        pass
    # Give it a very short chance to exit on SIGTERM.
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
    # If it is still alive, abandon it. Never call join() without a timeout.


def copy_one(src: str, dst: str, stall: float, max_timeout: float,
             chunk: int, preserve: bool, poll: float = 1.0) -> tuple:
    """Copy one file in a monitored child. Returns (ok, reason)."""
    ctx = mp.get_context("fork")
    p = ctx.Process(target=_copy_worker, args=(src, dst, chunk, preserve))
    p.start()

    start = time.monotonic()
    last_size = -1
    last_progress = start

    while True:
        # Ctrl+C from the user?
        if CTRL.abort_current:
            CTRL.abort_current = False
            _kill_nb(p)
            _safe_unlink(dst)
            return False, "user-abort (ctrl+c)"

        try:
            p.join(poll)
        except KeyboardInterrupt:
            # our handler already set abort_current; loop again
            continue

        now = time.monotonic()

        if not p.is_alive():
            break

        try:
            sz = os.path.getsize(dst)
        except OSError:
            sz = -1

        if sz >= 0 and sz != last_size:
            last_size = sz
            last_progress = now

        if (now - last_progress) > stall:
            _kill_nb(p)
            _safe_unlink(dst)
            return False, f"stall>{stall:g}s @ {last_size}B"

        if max_timeout > 0 and (now - start) > max_timeout:
            _kill_nb(p)
            _safe_unlink(dst)
            return False, f"timeout>{max_timeout:g}s @ {last_size}B"

    if p.exitcode == 0:
        return True, "ok"
    _safe_unlink(dst)
    return False, f"exitcode={p.exitcode}"


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# bad-media-tolerant scandir (in a subprocess, with a timeout)
# ---------------------------------------------------------------------------
#
# scandir() can hang in the kernel when the directory lives in a damaged
# area of the disk. That blocks the entire PARENT process -- not even Ctrl+C
# works because the syscall is stuck in D-state. That is why we run scandir
# in a monitored child and abandon the child if it exceeds the timeout.

def _scan_worker(d: str, q) -> None:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(2))
    signal.signal(signal.SIGINT, lambda *_: os._exit(2))
    try:
        result = []
        with os.scandir(d) as it:
            for e in it:
                # classify using d_type (no extra stat)
                try:
                    if e.is_symlink():
                        kind = "skip"
                    elif e.is_dir(follow_symlinks=False):
                        kind = "dir"
                    elif e.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        kind = "skip"
                except OSError:
                    kind = "unknown"
                result.append((e.name, kind))
        result.sort()
        q.put(("ok", result))
    except OSError as ex:
        q.put(("err", str(ex)))
    except Exception as ex:
        q.put(("err", repr(ex)))


def safe_scandir(d: str, timeout: float):
    """List the entries of `d` in a subprocess. Returns (entries, error)
    where entries is a list of (name, kind), or None on error."""
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_scan_worker, args=(d, q))
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
# logs
# ---------------------------------------------------------------------------

def load_path_set(path: str) -> set:
    s = set()
    if not os.path.exists(path):
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


class AcceptLoss:
    """Set of absolute paths whose failures the user accepts losing.
    Each entry can be a file (exact match) or a directory (matches the
    directory itself and any descendant)."""

    def __init__(self, paths: set):
        # normalize by stripping the trailing slash, except for root '/'
        norm = set()
        for p in paths:
            if not p:
                continue
            np = p.rstrip("/") or "/"
            norm.add(np)
        self._paths = norm

    def __bool__(self) -> bool:
        return bool(self._paths)

    def __len__(self) -> int:
        return len(self._paths)

    def covers(self, path: str) -> bool:
        """True if `path` (or any ancestor) is in the list."""
        if not self._paths:
            return False
        p = path.rstrip("/") or "/"
        if p in self._paths:
            return True
        # check ancestors
        while True:
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent
            if p in self._paths:
                return True


# ---------------------------------------------------------------------------
# recursive walk with per-directory checkpoint
# ---------------------------------------------------------------------------

class WalkContext:
    def __init__(self, args, src_root, dst_root, done, failed,
                 checkpoint, accept_loss, err_log, done_log, ckpt_log, status):
        self.args = args
        self.src_root = src_root
        self.dst_root = dst_root
        self.done = done
        self.failed = failed
        self.checkpoint = checkpoint
        self.accept_loss = accept_loss  # AcceptLoss
        self.err_log = err_log
        self.done_log = done_log
        self.ckpt_log = ckpt_log
        self.status = status  # StatusFile or None
        # counters
        self.total = 0
        self.ok = 0
        self.fail = 0
        self.skipped_done = 0
        self.skipped_fail = 0
        self.skipped_size = 0
        self.skipped_ckpt_dirs = 0
        self.skipped_accept = 0
        # progress
        self.total_planned = 0
        self.progress_step = 0
        self.next_progress = 0
        self.progress_t0 = time.monotonic()


class StatusFile:
    """Writes the current operation to a file. If things hang, just 'cat'
    the file to find out where the script got stuck."""
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "w", buffering=1)

    def set(self, phase: str, path: str) -> None:
        try:
            self.f.seek(0)
            self.f.truncate()
            self.f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{phase}\t{path}\n")
            self.f.flush()
            # We deliberately do NOT fsync: the default is /tmp (tmpfs/RAM),
            # and even on a real disk a per-file fsync would wear out the SSD
            # for no benefit (the status is disposable).
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.f.close()
        except OSError:
            pass


def _print_progress(ctx: WalkContext, force: bool = False) -> None:
    if ctx.progress_step <= 0 or ctx.total_planned <= 0:
        return
    if not force and ctx.total < ctx.next_progress:
        return
    elapsed = time.monotonic() - ctx.progress_t0
    pct = 100.0 * ctx.total / ctx.total_planned
    done_count = ctx.ok + ctx.fail + ctx.skipped_done + ctx.skipped_fail
    rate = done_count / elapsed if elapsed > 0 else 0.0
    remaining = max(0, ctx.total_planned - ctx.total)
    eta = remaining / rate if rate > 0 else float("inf")
    eta_s = f"{eta:.0f}s" if eta != float("inf") else "?"
    sys.stdout.write(
        f"\n[{pct:5.1f}%] {ctx.total}/{ctx.total_planned} "
        f"ok={ctx.ok} fail={ctx.fail} "
        f"skip(done={ctx.skipped_done},fail={ctx.skipped_fail},"
        f"size={ctx.skipped_size},ckpt-dirs={ctx.skipped_ckpt_dirs},"
        f"accept={ctx.skipped_accept}) "
        f"rate={rate:.1f}/s eta={eta_s}\n")
    sys.stdout.flush()
    while ctx.total >= ctx.next_progress:
        ctx.next_progress += ctx.progress_step


def _process_file(full: str, ctx: WalkContext) -> bool:
    """Returns True if the parent directory may consider this file OK
    for checkpoint purposes (success, skipped due to a previous success, or
    accepted loss via --accept-loss). Returns False if there was a failure
    that prevents the parent from being checkpointed.
    """
    args = ctx.args
    ctx.total += 1
    rel = os.path.relpath(full, ctx.src_root)
    out = os.path.join(ctx.dst_root, rel)

    # 0) accepted loss: do not try to copy, do not block the checkpoint
    if ctx.accept_loss.covers(full):
        ctx.skipped_accept += 1
        sys.stdout.write("L"); sys.stdout.flush()
        _print_progress(ctx)
        return True

    if ctx.status:
        ctx.status.set("file:check", full)

    # 1) already in done-log
    if full in ctx.done:
        if args.check_size:
            try:
                d_sz = os.path.getsize(out)
                s_sz = os.path.getsize(full)
                if d_sz == s_sz:
                    ctx.skipped_done += 1
                    sys.stdout.write(":"); sys.stdout.flush()
                    _print_progress(ctx)
                    return True
            except OSError:
                pass
        else:
            ctx.skipped_done += 1
            sys.stdout.write(":"); sys.stdout.flush()
            _print_progress(ctx)
            return True

    # 2) already in err-log and --skip-failed
    if args.skip_failed and full in ctx.failed:
        ctx.skipped_fail += 1
        sys.stdout.write("x"); sys.stdout.flush()
        _print_progress(ctx)
        # if under an accepted-loss area, do not block the parent's checkpoint
        return ctx.accept_loss.covers(full)

    if args.dry_run:
        print(f"COPY {full} -> {out}")
        return True

    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
    except OSError as e:
        ctx.fail += 1
        ctx.err_log.write(f"{full}\t# mkdir: {e}\n")
        sys.stdout.write("X"); sys.stdout.flush()
        _print_progress(ctx)
        return False

    # 3) destination already exists with the same size? (only with --check-size)
    if args.check_size:
        try:
            d_sz = os.path.getsize(out)
            if d_sz > 0 and os.path.getsize(full) == d_sz:
                ctx.ok += 1
                ctx.skipped_size += 1
                if full not in ctx.done:
                    ctx.done_log.write(full + "\n")
                    ctx.done.add(full)
                sys.stdout.write("="); sys.stdout.flush()
                _print_progress(ctx)
                return True
        except OSError:
            pass

    # 4) copy
    if ctx.status:
        ctx.status.set("file:copy", full)
    t0 = time.monotonic()
    success, reason = copy_one(full, out, args.stall, args.timeout,
                               args.chunk, args.no_preserve is False, args.poll)
    dt = time.monotonic() - t0
    if success:
        ctx.ok += 1
        ctx.done_log.write(full + "\n")
        ctx.done.add(full)
        sys.stdout.write("."); sys.stdout.flush()
        _print_progress(ctx)
        return True
    else:
        ctx.fail += 1
        ctx.err_log.write(f"{full}\t# {reason} ({dt:.1f}s)\n")
        sys.stdout.write("X"); sys.stdout.flush()
        _print_progress(ctx)
        # if the failure is inside an accepted-loss area, do not
        # block the parent directory's checkpoint
        return ctx.accept_loss.covers(full)


def _process_dir(d: str, ctx: WalkContext) -> bool:
    """Process a directory recursively. Returns True if the entire subtree
    was 100% copied/considered OK -- in that case the directory is
    written to the checkpoint."""
    args = ctx.args

    # checkpoint: subtree already marked as complete
    if not args.no_checkpoint and d in ctx.checkpoint:
        ctx.skipped_ckpt_dirs += 1
        sys.stdout.write("D"); sys.stdout.flush()
        return True

    # whole directory listed in --accept-loss: do not scan, consider it OK
    if ctx.accept_loss.covers(d):
        ctx.skipped_accept += 1
        sys.stdout.write("L"); sys.stdout.flush()
        return True

    if ctx.status:
        ctx.status.set("dir:scan", d)

    entries, scan_err = safe_scandir(d, args.scan_timeout)
    if scan_err is not None:
        sys.stderr.write(f"\n[scandir-fail] {d}: {scan_err}\n")
        if ctx.err_log:
            ctx.err_log.write(f"{d}\t# {scan_err}\n")
        # if this directory is under an accepted-loss area, do not block the parent
        return ctx.accept_loss.covers(d)

    all_good = True
    for name, kind in entries:
        full = os.path.join(d, name)
        if kind == "unknown":
            sys.stderr.write(f"\n[type-unknown] {full} -- skipping\n")
            if ctx.err_log:
                ctx.err_log.write(f"{full}\t# type-unknown\n")
            all_good = False
            continue
        if kind == "skip":
            continue
        if kind == "dir":
            sub_ok = _process_dir(full, ctx)
            if not sub_ok:
                all_good = False
        elif kind == "file":
            f_ok = _process_file(full, ctx)
            if not f_ok:
                all_good = False

    if all_good and not args.no_checkpoint and not args.dry_run:
        ctx.ckpt_log.write(d + "\n")
        ctx.checkpoint.add(d)
    return all_good


def _prescan(src_root: str, checkpoint: set, no_checkpoint: bool,
             scan_timeout: float, status, accept_loss: "AcceptLoss") -> int:
    """Count files under src_root, skipping subtrees that are checkpointed
    or in accept-loss. Uses safe_scandir to avoid hanging on bad directories."""
    count = 0
    stack = [src_root]
    while stack:
        d = stack.pop()
        if not no_checkpoint and d in checkpoint:
            continue
        if accept_loss.covers(d):
            continue
        if status:
            status.set("prescan", d)
        entries, err = safe_scandir(d, scan_timeout)
        if err is not None:
            sys.stderr.write(f"\n[prescan] {d}: {err}\n")
            continue
        for name, kind in entries:
            full = os.path.join(d, name)
            if accept_loss.covers(full):
                continue
            if kind == "dir":
                stack.append(full)
            elif kind == "file":
                count += 1
                if count % 5000 == 0:
                    print(f"[prescan] {count} files...", flush=True)
    return count


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Copy files, skipping those with I/O (CRC) errors on bad media.")
    ap.add_argument("src", help="source directory")
    ap.add_argument("dst", help="destination directory")
    ap.add_argument("--log", default="/root/skip_copy.err.log",
                    help="log of FAILURES (absolute paths)")
    ap.add_argument("--done-log", default="/root/skip_copy.done.log",
                    help="log of SUCCESSES (absolute paths)")
    ap.add_argument("--checkpoint-log", default="/root/skip_copy.dirs.log",
                    help="log of 100%% copied DIRECTORIES (entire subtree "
                         "skipped on the next run)")
    ap.add_argument("--accept-loss", default="/root/accept_loss.txt",
                    help="input file with absolute paths (files "
                         "OR directories) whose loss you accept. Failures inside "
                         "them do not prevent parents from being checkpointed. "
                         "Use '' to disable. Lines starting with # are comments.")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="disable the per-directory checkpoint "
                         "(revisits the whole tree)")
    ap.add_argument("--skip-failed", action="store_true",
                    help="skip files already in the failure log "
                         "(useful to avoid retrying damaged areas; "
                         "directories with previous failures are not checkpointed "
                         "unless you pass --skip-failed)")
    ap.add_argument("--stall", type=float, default=15.0,
                    help="abort if the destination does not grow for N seconds (default 15)")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="absolute timeout per file (0=disabled, default)")
    ap.add_argument("--scan-timeout", type=float, default=30.0,
                    help="timeout for listing ONE directory (default 30s). "
                         "Directories in damaged areas that hang scandir "
                         "are abandoned and recorded in the err-log.")
    ap.add_argument("--status-file", default="/tmp/skip_copy.status",
                    help="file where the current operation is written (overwritten "
                         "at every step). Default: /tmp/skip_copy.status (tmpfs, "
                         "in RAM, does not wear out the SSD). Use '' to disable. "
                         "To inspect from another terminal: cat <status-file>")
    ap.add_argument("--chunk", type=int, default=1024 * 1024,
                    help="read block size (default 1MiB)")
    ap.add_argument("--poll", type=float, default=1.0,
                    help="monitoring interval in seconds (default 1)")
    ap.add_argument("--no-preserve", action="store_true",
                    help="do not preserve mode/timestamps")
    ap.add_argument("--check-size", action="store_true",
                    help="on re-runs, check the destination size before "
                         "skipping (safer, but stats the source)")
    ap.add_argument("--progress-pct", type=float, default=1.0,
                    help="if --prescan is enabled, print a progress "
                         "line every N%% of the files (default 1)")
    ap.add_argument("--prescan", action="store_true",
                    help="pre-scan to count files (enables %% and ETA)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only list what would be done")
    args = ap.parse_args()

    src_root = os.path.abspath(args.src.rstrip("/") or "/")
    dst_root = os.path.abspath(args.dst.rstrip("/") or "/")

    if not os.path.isdir(src_root):
        print(f"error: source is not a directory: {src_root}", file=sys.stderr)
        return 2
    if not args.dry_run:
        os.makedirs(dst_root, exist_ok=True)

    for p in (args.log, args.done_log, args.checkpoint_log):
        d = os.path.dirname(p) or "."
        os.makedirs(d, exist_ok=True)

    done = load_path_set(args.done_log)
    failed = load_path_set(args.log) if args.skip_failed else set()
    checkpoint = set() if args.no_checkpoint else load_path_set(args.checkpoint_log)
    accept_loss = AcceptLoss(load_path_set(args.accept_loss) if args.accept_loss else set())
    if done:
        print(f"done-log:       {len(done)} files previously OK")
    if failed:
        print(f"err-log:        {len(failed)} previously failed (--skip-failed)")
    if checkpoint:
        print(f"checkpoint-log: {len(checkpoint)} directories 100% complete")
    if accept_loss:
        print(f"accept-loss:    {len(accept_loss)} paths with accepted loss")

    _install_sigint()

    started = datetime.now()

    status = StatusFile(args.status_file) if args.status_file else None
    if status:
        status.set("start", src_root)
        print(f"status-file: {args.status_file}")

    if args.dry_run:
        err_log = done_log = ckpt_log = None
    else:
        err_log = open(args.log, "a", buffering=1)
        done_log = open(args.done_log, "a", buffering=1)
        ckpt_log = open(args.checkpoint_log, "a", buffering=1)
        hdr = (f"# skip_copy started at {started.isoformat(timespec='seconds')}\n"
               f"# source={src_root} destination={dst_root} "
               f"stall={args.stall}s timeout={args.timeout}s "
               f"skip_failed={args.skip_failed} no_checkpoint={args.no_checkpoint}\n")
        err_log.write(hdr)
        done_log.write(hdr)
        ckpt_log.write(hdr)

    ctx = WalkContext(args, src_root, dst_root, done, failed, checkpoint,
                      accept_loss, err_log, done_log, ckpt_log, status)

    # optional prescan
    if args.prescan and args.progress_pct > 0:
        print("[prescan] enumerating files...", flush=True)
        t0 = time.monotonic()
        ctx.total_planned = _prescan(src_root, checkpoint, args.no_checkpoint,
                                      args.scan_timeout, status, accept_loss)
        print(f"[prescan] {ctx.total_planned} files in "
              f"{time.monotonic() - t0:.1f}s", flush=True)
        if ctx.total_planned > 0 and args.progress_pct > 0:
            ctx.progress_step = max(1, int(ctx.total_planned * args.progress_pct / 100.0))
            ctx.next_progress = ctx.progress_step

    try:
        _process_dir(src_root, ctx)
    finally:
        if status:
            status.set("done", src_root)
            status.close()
        ended = datetime.now()
        sys.stdout.write("\n")
        summary = (f"total={ctx.total} ok={ctx.ok} "
                   f"(done-log={ctx.skipped_done}, "
                   f"same-size={ctx.skipped_size}, "
                   f"ckpt-dirs={ctx.skipped_ckpt_dirs}, "
                   f"accept-loss={ctx.skipped_accept}) "
                   f"failures={ctx.fail} (skip-failed={ctx.skipped_fail})")
        if err_log:
            tail = (f"# skip_copy finished at "
                    f"{ended.isoformat(timespec='seconds')}  {summary}\n")
            err_log.write(tail); err_log.close()
            done_log.write(tail); done_log.close()
            ckpt_log.write(tail); ckpt_log.close()
        print(summary)
        print(f"Failures:    {args.log}")
        print(f"Successes:   {args.done_log}")
        print(f"Checkpoint:  {args.checkpoint_log}")

    return 0 if ctx.fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
