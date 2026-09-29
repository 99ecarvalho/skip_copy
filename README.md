# skip_copy

Copies files from a source (bad media, with CRC/I-O errors) to a destination, **quickly skipping** whatever fails and **keeping persistent logs** across multiple runs.

## Philosophy

- **No retries**: if a file fails, it goes to the log and the run moves on.
- **Logs keyed by absolute source path**: you can run it several times on different subtrees and the logs stay consistent.
- **Per-directory checkpoint**: when an entire subtree is copied with 100% success, the directory is written to a checkpoint log and on the next run it is **skipped entirely** without even reading the media.
- **Stat-averse**: enumeration avoids calling `stat()` on each file (which is what makes `ls -l` hang on bad media). It only uses `d_type` from `scandir`.
- **`scandir` in a subprocess with a timeout** (`--scan-timeout`, default 30s): if a damaged directory hangs the listing in the kernel, the child is abandoned and the rest of the tree continues. (Previously this would lock up the main process to the point where not even `Ctrl+C` worked.)
- **Stall detection**: aborts if the destination stops growing (better than a fixed timeout for large files).
- **Non-blocking kill**: if the kernel leaves a child in D-state due to stuck hardware, the parent does not get stuck waiting — it sends `SIGTERM`/`SIGKILL`, waits at most 1s and moves on.
- **Status file** (`--status-file`): records in real time what the script is about to do. On any hang, `cat <status-file>` shows exactly where it stopped.
- **Smart Ctrl+C**: once aborts the current file and continues; twice quickly (<2s) exits for good.

## Progress symbols (stdout)

| symbol  | meaning                                                      |
|---------|--------------------------------------------------------------|
| `.`     | copied now                                                   |
| `:`     | skipped via done-log (already copied in a previous run)      |
| `=`     | destination already existed with the same size (`--check-size`) |
| `D`     | entire subtree skipped via checkpoint                        |
| `L`     | skipped via `--accept-loss` (accepted loss)                  |
| `x`     | skipped via err-log (`--skip-failed`)                        |
| `X`     | failed in this run (written to the err-log)                  |

In addition, with the `--prescan` flag the script performs a **pre-scan**
(it only counts files, no per-file `stat` — it uses `d_type` from `scandir`)
and then prints a progress line every **1%** of files
(adjustable via `--progress-pct`):

```
[ 42.0%] 4200/10000 ok=4180 fail=12 skip(done=8,fail=0,size=0) rate=87.3/s eta=66s
```

By default the scan is **lazy** and there is no `[ x.x %]` —
more resilient on bad media, with no cost of scanning everything before
starting to copy.

## Logs

- `--log` (default `/root/skip_copy.err.log`): failures, format `<abs_path>\t# reason (time)`.
- `--done-log` (default `/root/skip_copy.done.log`): successes, format `<abs_path>`.
- `--checkpoint-log` (default `/root/skip_copy.dirs.log`): directories whose subtree was 100% copied successfully. On reruns, these directories are skipped entirely (`D`) without even reading the media. To disable, use `--no-checkpoint`.
- `--accept-loss` (default `/root/accept_loss.txt`): an **input file** you fill in with absolute paths (files OR directories) whose loss you accept. Lines starting with `#` are comments. Failures inside these paths do not prevent parents from being checkpointed. Listed items are not even attempted (`L` in the progress output).

## Installation

```bash
# already located at /home/rec/skip_copy/skip_copy.py
sudo chmod +x /home/rec/skip_copy/skip_copy.py
```

## Examples

### 1) Basic usage

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec
```

The **status file** is enabled by default at `/tmp/skip_copy.status` (tmpfs, in RAM — it does not wear out the SSD even though it is rewritten for every file). To find out where the script is at any given moment:

```bash
cat /tmp/skip_copy.status
# 2026-05-01T20:32:11   file:copy   /mnt/rec/foo/bar.bin
```

Possible phases: `prescan`, `dir:scan`, `file:check`, `file:copy`, `done`.

To change its location use `--status-file /other/path`. To disable it, `--status-file ''`.

### 2) Recommended workflow: copy in parts, most important first

Use the same logs for every call — the script automatically skips what has already been copied.

```bash
# (a) most important subdir of sourcea, first
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/sourcea/subdir1  /dados_rec/sourcea/subdir1

# (b) all of sourceb
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/sourceb  /dados_rec/sourceb

# (c) the rest of sourcea (subdir1 is already done -> skipped via done-log)
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/sourcea  /dados_rec/sourcea

# (d) final sweep over all of /mnt/rec -- skips everything already done
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec
```

> **Why it works**: the logs store **absolute source paths** (`/mnt/rec/sourcea/subdir1/foo.jpg`). When you later run with `/mnt/rec` as the source, the same absolute path shows up and is detected in the done-log.

### 3) Don't retry what has already failed

Useful when you already know that region of the disk is lost and you don't want to waste time (and wear the media further) retrying:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --skip-failed
```

### 4) Very slow disk (USB 2.0, optical): increase the stall

If the good disk copies at only a few MB/s, 15s without progress may just be slowness. Raise it to 60s:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stall 60
```

### 5) Fast disk with isolated bad sectors: aggressive stall

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stall 5
```

### 6) Absolute limit on top of the stall (for small files)

Useful for tiny files that fail before even writing any bytes to the destination (the stall never triggers):

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stall 15 --timeout 60
```

### 7) Custom log locations

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  \
    --log     /home/rec/skip_copy/errors.log \
    --done-log /home/rec/skip_copy/successes.log
```

### 8) Dry-run: list what would be copied

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/sourcea  /dados_rec/sourcea  --dry-run | head
```

### 9) Size verification on reruns (`--check-size`)

By default the script does **not** call `stat()` on the source (so it doesn't hang on bad areas). With `--check-size`, it confirms the source size before skipping a file that is already in the done-log. Use it only if the source is responding well:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --check-size
```

### 10) Filesystem that doesn't return `d_type` (rare): tune the stat-timeout

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stat-timeout 5
```

### 11) Percentage progress (with pre-scan)

By default the script is lazy and shows no %. To see `[ 42.0% ] ... eta=...`,
enable the pre-scan:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --prescan
```

To print fewer lines (every 5% instead of 1%):

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --prescan --progress-pct 5
```

### 12) Lazy (default) — starts immediately, no %

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec
```

### 13) Accept losses (keep going despite failures in known areas)

Create `/root/accept_loss.txt` with absolute paths (files or directories). Everything listed there is considered expendable — failures do not break the parent directory's checkpoint, and listed paths are skipped without being attempted (`L`):

```text
# /root/accept_loss.txt
# entire expendable directories:
/mnt/rec/home/hex/.cache
/mnt/rec/home/hex/snap/firefox/common/.cache
# specific expendable file:
/mnt/rec/home/hex/Downloads/corrupted_iso.iso
```

Usage (the default file is read automatically):

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec
```

Using a custom file or disabling it:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec \
    --accept-loss /home/rec/losses.txt
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec \
    --accept-loss ''
```

## Inspecting the logs

```bash
# how many successes / failures
grep -cv '^#' /root/skip_copy.done.log
grep -cv '^#' /root/skip_copy.err.log

# view the failures
grep -v '^#' /root/skip_copy.err.log

# failures by reason
grep -v '^#' /root/skip_copy.err.log | awk -F'#' '{print $2}' | sort | uniq -c | sort -rn
```

## Reset / start over

```bash
sudo rm -f /root/skip_copy.done.log /root/skip_copy.err.log /root/skip_copy.dirs.log
```

---

# skip_find — find files by date on bad media

Equivalent to `find <path> -newermt "2026-04-12" -type f`, but with the same protection against hung syscalls on media with bad CRC **and incremental** (it can run many times, skipping what it has already scanned).

## Philosophy (same as skip_copy)

- **Incremental**: run it on the most important subtrees first, then on broader directories. Logs are shared and whatever has already been scanned is skipped automatically.
- **Per-directory checkpoint**: when an entire subtree is processed without failures, it goes to the checkpoint-log and on the next run it is **skipped entirely** (`D`).
- **Done-log**: every file whose stat succeeded (regardless of mtime) is recorded. On the next run it is skipped (`:`) without even touching the media.
- **scandir + stat in a subprocess** with a timeout (`--scan-timeout`, default 30s): if a directory hangs in the kernel, the child is abandoned and the scan continues.
- **Individual stat in a subprocess** (`--stat-timeout`, default 10s): fallback for files where the batch stat failed.
- **Non-blocking kill**: a child stuck in D-state is abandoned.
- **Status file** (`/tmp/skip_find.status`): shows where the script is right now.
- **Smart Ctrl+C**: once skips the current directory; twice quickly (<2s) exits.

## Progress symbols (stdout)

| symbol  | meaning                                        |
|---------|------------------------------------------------|
| `.`     | file found (mtime >= threshold)                |
| `_`     | older file (only with `--verbose`)             |
| `:`     | skipped via done-log (already processed)       |
| `D`     | entire subtree skipped via checkpoint          |
| `x`     | skipped via err-log (`--skip-failed`)          |
| `X`     | failure/timeout in this run (sent to err-log)  |

Every ~10s it prints a summary line with counters.

## Logs

- `--list-log` (default `/root/skip_find.list.log`): absolute paths of the files found (mtime >= threshold). Always appended.
- `--err-log` (default `/root/skip_find.err.log`): failures/timeouts, format `<abs_path>\t# reason`. Always appended.
- `--done-log` (default `/root/skip_find.done.log`): files already processed (stat OK). On the next run they are skipped (`:`) without touching the media.
- `--checkpoint-log` (default `/root/skip_find.dirs.log`): directories whose subtree was 100% processed. On the next run they are skipped entirely (`D`). To disable, use `--no-checkpoint`.

## Examples

### 1) Basic usage — files newer than 2026-04-12

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12"
```

### 2) Precise date and time

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12 14:30:00"
```

### 3) Incremental workflow: scan by priority

```bash
# (a) most important subdir first
sudo python3 skip_find.py /mnt/rec/projects --newermt "2026-04-12"

# (b) another area
sudo python3 skip_find.py /mnt/rec/documents --newermt "2026-04-12"

# (c) everything (already scanned subtrees are skipped via checkpoint)
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12"
```

> **Why it works**: the logs store **absolute paths**. When you later run with `/mnt/rec` as the root, the directories `/mnt/rec/projects` and `/mnt/rec/documents` are already in the checkpoint-log and are skipped entirely (`D`).

### 4) Don't retry what has already failed

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" --skip-failed
```

### 5) Custom log locations

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-01" \
    --list-log /root/found.log \
    --err-log  /root/find_errors.log \
    --done-log /root/find_done.log \
    --checkpoint-log /root/find_dirs.log
```

### 6) Skip directories known to be bad

Create a file with absolute paths (one per line):

```text
# /root/skip_dirs.txt
/mnt/rec/home/hex/.cache
/mnt/rec/System Volume Information
```

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" \
    --skip-dir /root/skip_dirs.txt
```

### 7) Badly hanging disk: aggressive timeouts

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" \
    --scan-timeout 10 --stat-timeout 5
```

### 8) Force a rescan (ignore checkpoints)

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" --no-checkpoint
```

### 9) Check the status from another terminal

```bash
cat /tmp/skip_find.status
# 2026-05-01T20:32:11   dir:scan   /mnt/rec/home/hex/Documents
```

Possible phases: `start`, `dir:scan`, `file:stat`, `done`.

### 10) Reset / start over

```bash
sudo rm -f /root/skip_find.{list,err,done,dirs}.log
```

## Inspecting the logs

```bash
# how many found
grep -cv '^#' /root/skip_find.list.log

# how many failures
grep -cv '^#' /root/skip_find.err.log

# failures by reason
grep -v '^#' /root/skip_find.err.log | awk -F'#' '{print $2}' | sort | uniq -c | sort -rn
```

---

## Known limitations (both scripts)

- If the **kernel** hangs in a syscall in D-state (uninterruptible) because of stuck SATA/USB hardware, not even `SIGKILL` kills it right away. In those cases:
  - reduce `--stall` / `--scan-timeout`,
  - or make an image with `ddrescue` first (and copy from the image),
  - or unmount/remount with tolerant options (e.g. NTFS via `ntfs-3g`).
- Symlinks are **ignored** (neither followed nor copied as links).
- Extended attributes (xattr/ACL) are not preserved — only `mode` and `timestamps`.
