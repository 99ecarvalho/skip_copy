#!/usr/bin/env python3
"""
gen_testcase.py - generates a test tree for skip_copy.py.

Creates a source directory with:
  - small / medium / large files
  - nested subdirs (subdir1/deep, subdir2)
  - empty files
  - a symlink (should be ignored by skip_copy)
  - optionally: "bad" files that simulate slow I/O or read errors,
    by mounting a FUSE layer *if* `python3-fusepy` is installed
    and `/dev/fuse` is available. Otherwise, only the "healthy"
    files are generated and a warning is printed.

It also generates a small `run_demo.sh` script next to the tree with
skip_copy.py calls that reproduce the workflow from README.md.

Usage:
    python3 gen_testcase.py [--root /tmp/skip_copy_demo]
                            [--big-mb 8] [--n-files 20]
                            [--with-fuse]   # enable the error simulator
                            [--clean]       # delete and recreate

Without --with-fuse, no root is needed: only the healthy tree is generated.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import stat
import sys
import textwrap
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "skip_copy.py"


def write_random(path: Path, size: int, seed: int = 0) -> None:
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        # write in 64KB chunks
        remaining = size
        chunk = 64 * 1024
        while remaining > 0:
            n = min(chunk, remaining)
            f.write(rng.randbytes(n))
            remaining -= n


def build_tree(root: Path, big_mb: int, n_files: int) -> None:
    print(f"[gen] creating tree in {root}")
    (root / "sourcea" / "subdir1" / "deep").mkdir(parents=True, exist_ok=True)
    (root / "sourcea" / "subdir2").mkdir(parents=True, exist_ok=True)
    (root / "sourceb").mkdir(parents=True, exist_ok=True)

    # small files
    for i in range(n_files):
        write_random(root / "sourcea" / "subdir1" / f"small_{i:03d}.bin", 1024 * (i + 1), seed=i)
    for i in range(n_files // 2):
        write_random(root / "sourcea" / "subdir1" / "deep" / f"deep_{i:02d}.bin", 4096, seed=100 + i)
    for i in range(n_files):
        write_random(root / "sourcea" / "subdir2" / f"s2_{i:03d}.bin", 2048, seed=200 + i)
    for i in range(n_files):
        write_random(root / "sourceb" / f"b_{i:03d}.bin", 512 * (i + 1), seed=300 + i)

    # large files (several MB) -- good for testing --stall
    write_random(root / "sourcea" / "big_a.bin", big_mb * 1024 * 1024, seed=999)
    write_random(root / "sourceb" / "big_b.bin", big_mb * 1024 * 1024, seed=998)

    # empty file
    (root / "sourcea" / "empty.txt").write_bytes(b"")

    # file at the source root
    (root / "root_file.txt").write_text("hello at root\n")

    # symlink (skip_copy should ignore it)
    link = root / "sourcea" / "subdir1" / "link_to_root_file"
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(root / "root_file.txt")
    except OSError as e:
        print(f"[gen] warning: could not create symlink: {e}")

    print("[gen] tree created")


def write_runner(root: Path, dst: Path, errlog: Path, donelog: Path) -> Path:
    runner = root.parent / "run_demo.sh"
    runner.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Demo: calls skip_copy reproducing the workflow from README.md.
        # Use the same --log and --done-log in all 4 calls.
        set -u
        SRC={root}
        DST={dst}
        SC={SCRIPT}
        LOG={errlog}
        DONE={donelog}

        rm -f "$LOG" "$DONE"
        rm -rf "$DST"

        echo "==> (a) sourcea/subdir1"
        python3 "$SC" "$SRC/sourcea/subdir1" "$DST/sourcea/subdir1" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> (b) sourceb"
        python3 "$SC" "$SRC/sourceb" "$DST/sourceb" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> (c) sourcea (the rest; subdir1 should be skipped)"
        python3 "$SC" "$SRC/sourcea" "$DST/sourcea" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> (d) entire root (should skip everything)"
        python3 "$SC" "$SRC" "$DST" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> summary"
        echo "Successes: $(grep -cv '^#' "$DONE") in $DONE"
        echo "Failures:  $(grep -cv '^#' "$LOG") in $LOG"
        echo "Files in destination: $(find "$DST" -type f | wc -l)"
    """))
    runner.chmod(0o755)
    return runner


# ---------------------------------------------------------------------------
# optional FUSE layer, simulates read errors on some files
# ---------------------------------------------------------------------------

FUSE_SCRIPT_TEMPLATE = r'''#!/usr/bin/env python3
"""
bad_fuse.py - mounts {mountpoint} mirroring {backing}, but with simulated
I/O failures on files whose name contains "BAD" and slow reads on
files whose name contains "SLOW".
"""
import errno, os, stat, sys, time
from fuse import FUSE, Operations, FuseOSError

BACKING = {backing!r}

class BadFS(Operations):
    def _full(self, path):
        return BACKING + path
    def getattr(self, path, fh=None):
        st = os.lstat(self._full(path))
        return {{k: getattr(st, k) for k in (
            "st_atime","st_ctime","st_gid","st_mode","st_mtime",
            "st_nlink","st_size","st_uid")}}
    def readdir(self, path, fh):
        yield "."; yield ".."
        for e in os.listdir(self._full(path)):
            yield e
    def open(self, path, flags):
        return os.open(self._full(path), flags)
    def read(self, path, size, offset, fh):
        name = os.path.basename(path)
        if "BAD" in name and offset > 0:
            # first chunk succeeds, then EIO -> simulates a CRC error midway
            raise FuseOSError(errno.EIO)
        if "SLOW" in name:
            time.sleep(60)  # hangs well beyond --stall
        os.lseek(fh, offset, 0)
        return os.read(fh, size)
    def release(self, path, fh):
        return os.close(fh)
    def readlink(self, path):
        return os.readlink(self._full(path))

if __name__ == "__main__":
    mp = {mountpoint!r}
    os.makedirs(mp, exist_ok=True)
    FUSE(BadFS(), mp, foreground=True, nothreads=True, allow_other=False)
'''


def setup_fuse_layer(root: Path, mount: Path) -> Path | None:
    """Try to create bad_fuse.py. Return the script path or None."""
    try:
        import fuse  # noqa: F401
    except Exception:
        print("[gen] warning: package 'fusepy' not found; skipping FUSE layer.", file=sys.stderr)
        print("       install with: pip install fusepy   (and: sudo apt install fuse3)", file=sys.stderr)
        return None
    if not os.path.exists("/dev/fuse"):
        print("[gen] warning: /dev/fuse missing; skipping FUSE layer.", file=sys.stderr)
        return None

    fuse_script = root.parent / "bad_fuse.py"
    fuse_script.write_text(FUSE_SCRIPT_TEMPLATE.format(
        backing=str(root), mountpoint=str(mount)))
    fuse_script.chmod(0o755)

    # create some files with special names to trigger BAD/SLOW
    for sub in (root / "sourcea" / "subdir1", root / "sourceb"):
        sub.mkdir(parents=True, exist_ok=True)
        write_random(sub / "BAD_crc_demo.bin", 256 * 1024, seed=42)
        write_random(sub / "SLOW_huge.bin",    256 * 1024, seed=43)

    print(f"[gen] FUSE layer at {fuse_script}")
    print(f"[gen] to use it:")
    print(f"      python3 {fuse_script}    # in another terminal")
    print(f"      then run skip_copy reading from {mount} instead of {root}")
    return fuse_script


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/skip_copy_demo",
                    help="base directory of the test case (default /tmp/skip_copy_demo)")
    ap.add_argument("--big-mb", type=int, default=8, help="size of the large files in MiB")
    ap.add_argument("--n-files", type=int, default=12, help="number of small files per directory")
    ap.add_argument("--with-fuse", action="store_true",
                    help="also generate bad_fuse.py (requires fusepy)")
    ap.add_argument("--clean", action="store_true", help="remove everything before generating")
    args = ap.parse_args()

    base = Path(args.root).resolve()
    src = base / "source"
    dst = base / "dest"
    mount = base / "source_via_fuse"
    errlog = base / "logs" / "skip_copy.err.log"
    donelog = base / "logs" / "skip_copy.done.log"

    if args.clean and base.exists():
        print(f"[gen] removing {base}")
        shutil.rmtree(base)

    base.mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    src.mkdir(parents=True, exist_ok=True)

    build_tree(src, args.big_mb, args.n_files)

    runner = write_runner(src, dst, errlog, donelog)
    print(f"[gen] runner: {runner}")

    if args.with_fuse:
        setup_fuse_layer(src, mount)

    print()
    print(f"Source:      {src}")
    print(f"Destination: {dst}")
    print(f"Logs:        {errlog} | {donelog}")
    print()
    print(f"To run the demo:  bash {runner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
