#!/usr/bin/env python3
"""
skip_find.py - localiza arquivos mais novos que uma data/hora dada,
em midia com defeito (CRC ruim) que pode travar syscalls do kernel.

Equivalente funcional a:
    find <path> -newermt "2026-04-12" -type f
mas robusto a travamentos de kernel em areas defeituosas do disco
e INCREMENTAL (pode rodar varias vezes, pula o que ja varreu).

Logs (caminhos absolutos; compartilhaveis entre execucoes):
- --list-log        ENCONTRADOS:    <abs_path>  (mtime >= threshold)
- --err-log         FALHAS:         <abs_path>\t# <motivo>
- --done-log        PROCESSADOS OK: <abs_path>  (todos os arquivos onde
                    o stat funcionou, independente do mtime)
- --checkpoint-log  DIRETORIOS OK:  <abs_dir_path> (subarvore 100%%
                    processada sem falhas -- sera pulada inteira na
                    proxima execucao)

Legenda do progresso (stdout):
    .   arquivo encontrado (mtime >= threshold)
    _   arquivo mais antigo (mtime < threshold)
    :   pulado pelo done-log (ja processado em rodada anterior)
    D   subarvore inteira pulada por checkpoint
    x   pulado pelo err-log (--skip-failed)
    X   falha nesta rodada (foi pro err-log)

Estrategia para midia ruim (mesma do skip_copy.py):
- Enumeracao (scandir) em subprocesso com timeout (--scan-timeout).
  Se o diretorio travar no kernel, o filho e abandonado/morto e a
  varredura continua nos demais diretorios.
- stat() de cada arquivo tambem e feito dentro do subprocesso do scandir
  (batch por diretorio). Se o stat travar, o timeout do diretorio dispara.
- Para arquivos em diretorios onde o scandir+stat funcionou mas com
  stat individual falhando, um fallback com stat em processo separado
  com --stat-timeout pode ser usado.
- Status file (--status-file, default /tmp/skip_find.status em tmpfs):
  mostra onde o script esta agora. Em caso de travamento total:
  'cat /tmp/skip_find.status' para descobrir.

Uso incremental:
- Rode em subdiretorios prioritarios primeiro, depois em diretorios
  mais amplos -- os logs sao compartilhados e o que ja foi varrido
  e automaticamente pulado (done-log e checkpoint).
- O --list-log SEMPRE faz append (nunca perde resultado anterior).
- Para forcar re-varredura, use --no-checkpoint e/ou apague o done-log.

Ctrl+C:
- 1x  -> pula o diretorio/operacao atual e segue.
- 2x rapido (<2s) -> sai do programa.
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
# estado global para sinais
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
            sys.stderr.write("\n[interrupt] segundo Ctrl+C, saindo HARD.\n")
            os._exit(130)
        sys.stderr.write(
            "\n[interrupt] pulando operacao atual "
            "(Ctrl+C de novo em <2s para sair).\n")
        CTRL.abort_current = True
    signal.signal(signal.SIGINT, handler)


# ---------------------------------------------------------------------------
# kill nao-bloqueante (processo filho preso em D-state)
# ---------------------------------------------------------------------------

def _kill_nb(p: mp.Process) -> None:
    """Mata o filho sem bloquear. Se estiver em D-state no kernel, o kill
    nao fara nada -- entao NAO esperamos indefinidamente."""
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
# scandir + stat em subprocesso com timeout
# ---------------------------------------------------------------------------

def _scan_stat_worker(d: str, q) -> None:
    """Filho: lista diretorio com scandir e faz stat em cada arquivo.
    Retorna lista de (name, kind, mtime_ns) via queue.
    mtime_ns = -1 se stat falhou para aquele arquivo."""
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

                # stat para obter mtime
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
    """Lista entradas de `d` com stat num subprocesso.
    Retorna (entries, error) onde entries = [(name, kind, mtime_ns), ...] ou None."""
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
# stat individual em subprocesso (fallback para arquivos com stat falhado)
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
    """Faz stat num subprocesso. Retorna (mtime_ns, error)."""
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
        print(f"aviso: nao consegui ler {path}: {e}", file=sys.stderr)
    return s


# ---------------------------------------------------------------------------
# parse de data/hora
# ---------------------------------------------------------------------------

def parse_newermt(s: str) -> float:
    """Converte string de data ou data+hora para timestamp (epoch seconds).
    Aceita formatos: YYYY-MM-DD, YYYY-MM-DD HH:MM, YYYY-MM-DD HH:MM:SS"""
    s = s.strip().strip('"').strip("'")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(
        f"formato de data invalido: '{s}'. "
        f"Use YYYY-MM-DD ou 'YYYY-MM-DD HH:MM:SS'")


# ---------------------------------------------------------------------------
# walk recursivo com checkpoint por diretorio
# ---------------------------------------------------------------------------

class FindContext:
    def __init__(self, args, src_root, threshold_ns, skip_dirs,
                 done, failed, checkpoint,
                 list_log, err_log, done_log, ckpt_log, status):
        self.args = args
        self.src_root = src_root
        self.threshold_ns = threshold_ns  # nanoseconds
        self.skip_dirs = skip_dirs
        self.done = done            # set de caminhos absolutos ja processados
        self.failed = failed        # set de caminhos com falha anterior
        self.checkpoint = checkpoint  # set de diretorios 100% OK
        self.list_log = list_log
        self.err_log = err_log
        self.done_log = done_log
        self.ckpt_log = ckpt_log
        self.status = status
        # contadores
        self.dirs_scanned = 0
        self.dirs_failed = 0
        self.dirs_skipped = 0
        self.skipped_ckpt_dirs = 0
        self.files_found = 0
        self.files_older = 0
        self.files_stat_failed = 0
        self.skipped_done = 0
        self.skipped_fail = 0
        # progresso
        self.progress_t0 = time.monotonic()
        self.last_report = time.monotonic()


def _covers_skip(path: str, skip_dirs: set) -> bool:
    """True se path ou algum ancestral esta em skip_dirs."""
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
    """Processa um arquivo. Retorna True se OK (para checkpoint do pai),
    False se houve falha que impede checkpoint."""
    args = ctx.args

    # 1) ja em done-log: pula
    if full in ctx.done:
        ctx.skipped_done += 1
        sys.stdout.write(":"); sys.stdout.flush()
        return True

    # 2) ja em err-log e --skip-failed: pula
    if args.skip_failed and full in ctx.failed:
        ctx.skipped_fail += 1
        sys.stdout.write("x"); sys.stdout.flush()
        return False  # falha anterior impede checkpoint

    return None  # precisa stat


def _process_dir(d: str, ctx: FindContext) -> bool:
    """Processa um diretorio recursivamente. Retorna True se a subarvore
    inteira foi 100% processada OK -- nesse caso grava no checkpoint."""
    args = ctx.args

    if CTRL.abort_current:
        CTRL.abort_current = False
        return False

    # checkpoint: subarvore ja marcada como completa
    if not args.no_checkpoint and d in ctx.checkpoint:
        ctx.skipped_ckpt_dirs += 1
        sys.stdout.write("D"); sys.stdout.flush()
        return True

    # skip-dir: subarvore inteira pulada (nao conta como checkpoint)
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
            # stat falhou no batch, tenta stat individual
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

    # checkpoint: se tudo OK e nao esta no modo no-checkpoint
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
        description="Localiza arquivos mais novos que uma data em midia "
                    "com defeito (CRC), sem travar em syscalls. "
                    "Incremental: rode varias vezes, pula o que ja varreu.")
    ap.add_argument("src", help="diretorio raiz da busca")
    ap.add_argument("--newermt", required=True,
                    help="threshold de data/hora. Formatos: "
                         "'YYYY-MM-DD' ou 'YYYY-MM-DD HH:MM:SS'. "
                         "Arquivos com mtime >= este valor sao listados.")
    ap.add_argument("--list-log", default="/root/skip_find.list.log",
                    help="saida: lista de arquivos encontrados (append, "
                         "default: /root/skip_find.list.log)")
    ap.add_argument("--err-log", default="/root/skip_find.err.log",
                    help="saida: erros/timeouts (append, "
                         "default: /root/skip_find.err.log)")
    ap.add_argument("--done-log", default="/root/skip_find.done.log",
                    help="log de arquivos ja processados (stat OK). "
                         "Na proxima execucao esses sao pulados. "
                         "(default: /root/skip_find.done.log)")
    ap.add_argument("--checkpoint-log", default="/root/skip_find.dirs.log",
                    help="log de diretorios 100%% processados. "
                         "Na proxima execucao sao pulados inteiros. "
                         "(default: /root/skip_find.dirs.log)")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="desliga o checkpoint por diretorio "
                         "(re-visita toda a arvore)")
    ap.add_argument("--skip-failed", action="store_true",
                    help="pula arquivos que ja estao no err-log "
                         "(nao re-tenta areas danificadas)")
    ap.add_argument("--skip-dir", default="",
                    help="arquivo com caminhos absolutos de diretorios a "
                         "pular inteiros (um por linha). Use '' para desligar.")
    ap.add_argument("--scan-timeout", type=float, default=30.0,
                    help="timeout para scandir+stat de UM diretorio "
                         "(default 30s)")
    ap.add_argument("--stat-timeout", type=float, default=10.0,
                    help="timeout para stat individual de UM arquivo "
                         "(fallback quando o stat no batch falha, default 10s)")
    ap.add_argument("--status-file", default="/tmp/skip_find.status",
                    help="arquivo de status (default: /tmp/skip_find.status). "
                         "Use '' para desligar.")
    ap.add_argument("--verbose", action="store_true",
                    help="mostra '_' para cada arquivo mais antigo que o "
                         "threshold (default: silencioso)")
    args = ap.parse_args()

    # parse threshold
    try:
        threshold = parse_newermt(args.newermt)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    threshold_ns = int(threshold * 1_000_000_000)

    src_root = os.path.abspath(args.src.rstrip("/") or "/")
    if not os.path.isdir(src_root):
        print(f"erro: nao e diretorio: {src_root}", file=sys.stderr)
        return 2

    # skip-dirs (manual)
    skip_dirs = load_path_set(args.skip_dir) if args.skip_dir else set()

    # ensure log dirs exist
    for p in (args.list_log, args.err_log, args.done_log, args.checkpoint_log):
        d = os.path.dirname(p) or "."
        if d != ".":
            os.makedirs(d, exist_ok=True)

    # carregar logs anteriores
    done = load_path_set(args.done_log)
    failed = load_path_set(args.err_log) if args.skip_failed else set()
    checkpoint = set() if args.no_checkpoint else load_path_set(args.checkpoint_log)

    if done:
        print(f"done-log:       {len(done)} arquivos previamente processados")
    if failed:
        print(f"err-log:        {len(failed)} previamente com falha (--skip-failed)")
    if checkpoint:
        print(f"checkpoint-log: {len(checkpoint)} diretorios 100%% concluidos")

    _install_sigint()

    started = datetime.now()

    status = StatusFile(args.status_file) if args.status_file else None
    if status:
        status.set("start", src_root)
        print(f"status-file: {args.status_file}")

    # todos os logs em append
    list_log = open(args.list_log, "a", buffering=1)
    err_log = open(args.err_log, "a", buffering=1)
    done_log = open(args.done_log, "a", buffering=1)
    ckpt_log = open(args.checkpoint_log, "a", buffering=1)

    hdr = (f"# skip_find iniciado em {started.isoformat(timespec='seconds')}\n"
           f"# raiz={src_root} newermt={args.newermt} "
           f"(threshold={datetime.fromtimestamp(threshold).isoformat()}) "
           f"scan-timeout={args.scan_timeout}s stat-timeout={args.stat_timeout}s "
           f"skip_failed={args.skip_failed} no_checkpoint={args.no_checkpoint}\n")
    list_log.write(hdr)
    err_log.write(hdr)
    done_log.write(hdr)
    ckpt_log.write(hdr)

    print(f"raiz:      {src_root}")
    print(f"newermt:   {args.newermt} "
          f"(>= {datetime.fromtimestamp(threshold).isoformat()})")
    print(f"list-log:  {args.list_log}")
    print(f"err-log:   {args.err_log}")
    print(f"done-log:  {args.done_log}")
    print(f"checkpoint:{args.checkpoint_log}")
    if skip_dirs:
        print(f"skip-dirs: {len(skip_dirs)} diretorios a pular")
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
        tail = (f"# skip_find terminado em "
                f"{ended.isoformat(timespec='seconds')}  {summary}\n")
        list_log.write(tail); list_log.close()
        err_log.write(tail); err_log.close()
        done_log.write(tail); done_log.close()
        ckpt_log.write(tail); ckpt_log.close()
        print(summary)
        print(f"Encontrados: {args.list_log} ({ctx.files_found} novos nesta rodada)")
        print(f"Processados: {args.done_log}")
        print(f"Checkpoint:  {args.checkpoint_log}")
        print(f"Erros:       {args.err_log}")

    return 0 if ctx.dirs_failed == 0 and ctx.files_stat_failed == 0 else 1


if __name__ == "__main__":
    rc = main()
    os._exit(rc)  # hard exit: evita hang no atexit do multiprocessing
