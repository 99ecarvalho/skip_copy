#!/usr/bin/env python3
"""
skip_copy.py - copia arquivos de ORIGEM para DESTINO, pulando rapidamente
arquivos que dao erro de I/O (CRC em midia ruim) e registrando-os.

Logs (caminhos absolutos da origem; compartilhaveis entre execucoes):
- --log              FALHAS:        <abs_path>\\t# <motivo> (<dt>s)
- --done-log         SUCESSOS:      <abs_path>
- --checkpoint-log   DIRETORIOS OK: <abs_dir_path> (subarvore 100% copiada
                     sem falhas, sera pulada inteira na proxima execucao)
- --accept-loss      PERDAS ACEITAS (entrada): caminhos absolutos -- arquivos
                     OU diretorios -- que voce assume como perdiveis. Falhas
                     dentro deles nao impedem que diretorios pais virem
                     checkpoint. Default: /root/accept_loss.txt

Legenda do progresso (stdout):
    .   copiou agora
    :   pulado pelo done-log (ja copiou em rodada anterior)
    =   destino ja existia com mesmo tamanho (--check-size)
    D   subarvore inteira pulada por checkpoint
    L   pulado por --accept-loss (perda aceita)
    x   pulado pelo err-log (--skip-failed)
    X   falha nesta rodada (foi pro err-log)

Estrategia para midia ruim:
- Enumeracao via os.scandir SEM stat por arquivo (usa d_type), e ainda
  por cima dentro de um subprocesso com timeout (--scan-timeout) para
  diretorios em area defeituosa que travam a listagem no kernel.
- Copia em processo filho monitorado pelo pai:
    * --stall N   : aborta se o destino nao crescer por N segundos.
    * --timeout N : teto absoluto opcional por arquivo.
    * sem retry.
- Kill do filho preso e NAO-BLOQUEANTE (caso o kernel deixe o processo
  em D-state por hardware travado). O parent move adiante e o filho
  vira orfao/zumbi -- o resto da copia continua.
- Status file (--status-file, default /tmp/skip_copy.status em tmpfs):
  grava o que esta sendo feito agora; em caso de travamento, basta
  'cat' do arquivo para descobrir onde parou.

Ctrl+C:
- 1x  -> aborta o arquivo atual e segue.
- 2x rapido (<2s) -> sai do programa.
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
# estado global para sinais
# ---------------------------------------------------------------------------

class _Ctrl:
    abort_current = False   # sinaliza copy_one a desistir do arquivo atual
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
            "\n[interrupt] pulando arquivo atual "
            "(Ctrl+C de novo em <2s para sair).\n")
        CTRL.abort_current = True
    signal.signal(signal.SIGINT, handler)


# ---------------------------------------------------------------------------
# worker de copia
# ---------------------------------------------------------------------------

def _copy_worker(src: str, dst: str, chunk: int, preserve: bool) -> None:
    """Filho: sai 0 em sucesso, !=0 em falha. Sem retry."""
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
    """Mata o filho sem bloquear. Se ele estiver preso em D-state no kernel,
    nem SIGKILL fara nada -- entao a gente NAO espera. O resto da execucao
    continua e o filho vira orfao/zumbi."""
    try:
        if p.is_alive():
            p.terminate()
    except Exception:
        pass
    # Da uma chance bem curta para sair com SIGTERM.
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
    # Se ainda esta vivo, abandonamos. Nao chamamos join() sem timeout.


def copy_one(src: str, dst: str, stall: float, max_timeout: float,
             chunk: int, preserve: bool, poll: float = 1.0) -> tuple:
    """Copia 1 arquivo num filho monitorado. Retorna (ok, motivo)."""
    ctx = mp.get_context("fork")
    p = ctx.Process(target=_copy_worker, args=(src, dst, chunk, preserve))
    p.start()

    start = time.monotonic()
    last_size = -1
    last_progress = start

    while True:
        # Ctrl+C pelo usuario?
        if CTRL.abort_current:
            CTRL.abort_current = False
            _kill_nb(p)
            _safe_unlink(dst)
            return False, "user-abort (ctrl+c)"

        try:
            p.join(poll)
        except KeyboardInterrupt:
            # nosso handler ja setou abort_current, repete o loop
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
# scandir tolerante a midia ruim (em subprocesso, com timeout)
# ---------------------------------------------------------------------------
#
# scandir() pode travar no kernel quando o diretorio esta numa area com
# defeito do disco. Isso bloqueia o processo PAI inteiro -- nem Ctrl+C
# funciona porque o syscall fica em D-state. Por isso fazemos scandir
# num filho monitorado e abandonamos o filho se ele exceder o timeout.

def _scan_worker(d: str, q) -> None:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(2))
    signal.signal(signal.SIGINT, lambda *_: os._exit(2))
    try:
        result = []
        with os.scandir(d) as it:
            for e in it:
                # classifica usando d_type (sem stat extra)
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
    """Lista entradas de `d` num subprocesso. Retorna (entries, error)
    onde entries e uma lista de (name, kind) ou None em caso de erro."""
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
        print(f"aviso: nao consegui ler {path}: {e}", file=sys.stderr)
    return s


class AcceptLoss:
    """Conjunto de caminhos absolutos cujas falhas o usuario aceita perder.
    Cada entrada pode ser um arquivo (match exato) ou um diretorio (match
    no proprio diretorio e em qualquer descendente)."""

    def __init__(self, paths: set):
        # normaliza removendo barra final, exceto na raiz '/'
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
        """True se `path` (ou algum ancestral) esta na lista."""
        if not self._paths:
            return False
        p = path.rstrip("/") or "/"
        if p in self._paths:
            return True
        # checa ancestrais
        while True:
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent
            if p in self._paths:
                return True


# ---------------------------------------------------------------------------
# walk recursivo com checkpoint por diretorio
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
        self.status = status  # StatusFile ou None
        # contadores
        self.total = 0
        self.ok = 0
        self.fail = 0
        self.skipped_done = 0
        self.skipped_fail = 0
        self.skipped_size = 0
        self.skipped_ckpt_dirs = 0
        self.skipped_accept = 0
        # progresso
        self.total_planned = 0
        self.progress_step = 0
        self.next_progress = 0
        self.progress_t0 = time.monotonic()


class StatusFile:
    """Escreve a operacao atual num arquivo. Em caso de travamento, basta
    'cat' do arquivo para descobrir onde o script estava preso."""
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "w", buffering=1)

    def set(self, phase: str, path: str) -> None:
        try:
            self.f.seek(0)
            self.f.truncate()
            self.f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{phase}\t{path}\n")
            self.f.flush()
            # NAO chamamos fsync de proposito: o default e /tmp (tmpfs/RAM),
            # e mesmo em disco real fsync por arquivo desgastaria o SSD
            # sem nenhum beneficio (o status e descartavel).
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
    """Retorna True se o diretorio pai pode considerar este arquivo OK
    para fins de checkpoint (sucesso, skip por sucesso anterior, ou perda
    aceita via --accept-loss). Retorna False se houve falha que impede o
    checkpoint do pai.
    """
    args = ctx.args
    ctx.total += 1
    rel = os.path.relpath(full, ctx.src_root)
    out = os.path.join(ctx.dst_root, rel)

    # 0) perda aceita: nao tenta copiar, nao bloqueia checkpoint
    if ctx.accept_loss.covers(full):
        ctx.skipped_accept += 1
        sys.stdout.write("L"); sys.stdout.flush()
        _print_progress(ctx)
        return True

    if ctx.status:
        ctx.status.set("file:check", full)

    # 1) ja em done-log
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

    # 2) ja em err-log e --skip-failed
    if args.skip_failed and full in ctx.failed:
        ctx.skipped_fail += 1
        sys.stdout.write("x"); sys.stdout.flush()
        _print_progress(ctx)
        # se esta sob area de perda aceita, nao bloqueia checkpoint do pai
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

    # 3) destino ja existe com mesmo tamanho? (so com --check-size)
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

    # 4) copia
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
        # se a falha esta dentro de uma area de perdas aceitas, nao
        # bloqueia o checkpoint do diretorio pai
        return ctx.accept_loss.covers(full)


def _process_dir(d: str, ctx: WalkContext) -> bool:
    """Processa um diretorio recursivamente. Retorna True se a subarvore
    inteira foi 100% copiada/considerada OK -- nesse caso o diretorio e
    gravado no checkpoint."""
    args = ctx.args

    # checkpoint: subarvore ja marcada como completa
    if not args.no_checkpoint and d in ctx.checkpoint:
        ctx.skipped_ckpt_dirs += 1
        sys.stdout.write("D"); sys.stdout.flush()
        return True

    # diretorio inteiro listado em --accept-loss: nao varre, considera OK
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
        # se este diretorio esta sob area de perda aceita, nao bloqueia pai
        return ctx.accept_loss.covers(d)

    all_good = True
    for name, kind in entries:
        full = os.path.join(d, name)
        if kind == "unknown":
            sys.stderr.write(f"\n[type-unknown] {full} -- pulando\n")
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
    """Conta arquivos sob src_root, pulando subarvores em checkpoint
    e em accept-loss. Usa safe_scandir para nao travar em diretorios ruins."""
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
                    print(f"[prescan] {count} arquivos...", flush=True)
    return count


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Copia pulando arquivos com erro de I/O (CRC) em midia ruim.")
    ap.add_argument("src", help="diretorio de origem")
    ap.add_argument("dst", help="diretorio de destino")
    ap.add_argument("--log", default="/root/skip_copy.err.log",
                    help="log de FALHAS (caminhos absolutos)")
    ap.add_argument("--done-log", default="/root/skip_copy.done.log",
                    help="log de SUCESSOS (caminhos absolutos)")
    ap.add_argument("--checkpoint-log", default="/root/skip_copy.dirs.log",
                    help="log de DIRETORIOS 100%% copiados (subarvore "
                         "inteira pulada na proxima execucao)")
    ap.add_argument("--accept-loss", default="/root/accept_loss.txt",
                    help="arquivo de entrada com caminhos absolutos (arquivos "
                         "OU diretorios) cuja perda voce aceita. Falhas dentro "
                         "deles nao impedem que pais virem checkpoint. "
                         "Use '' para desligar. Linhas com # sao comentarios.")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="desliga o checkpoint por diretorio "
                         "(re-visita toda a arvore)")
    ap.add_argument("--skip-failed", action="store_true",
                    help="pula arquivos que ja estao no log de falhas "
                         "(util pra nao re-tentar areas danificadas; "
                         "diretorios com falhas anteriores nao viram checkpoint "
                         "a menos que voce passe --skip-failed)")
    ap.add_argument("--stall", type=float, default=15.0,
                    help="aborta se destino nao crescer por N segundos (default 15)")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="timeout absoluto por arquivo (0=desligado, default)")
    ap.add_argument("--scan-timeout", type=float, default=30.0,
                    help="timeout para listar UM diretorio (default 30s). "
                         "Diretorios em area defeituosa que travam o scandir "
                         "sao abandonados e marcados no err-log.")
    ap.add_argument("--status-file", default="/tmp/skip_copy.status",
                    help="arquivo onde a operacao atual e gravada (sobrescrito "
                         "a cada passo). Default: /tmp/skip_copy.status (tmpfs, "
                         "em RAM, nao desgasta SSD). Use '' para desligar. "
                         "Para inspecionar em outro terminal: cat <status-file>")
    ap.add_argument("--chunk", type=int, default=1024 * 1024,
                    help="tamanho do bloco de leitura (default 1MiB)")
    ap.add_argument("--poll", type=float, default=1.0,
                    help="intervalo de monitoramento em segundos (default 1)")
    ap.add_argument("--no-preserve", action="store_true",
                    help="nao preservar mode/timestamps")
    ap.add_argument("--check-size", action="store_true",
                    help="ao re-rodar, verifica tamanho do destino antes de "
                         "pular (mais seguro porem faz stat na origem)")
    ap.add_argument("--progress-pct", type=float, default=1.0,
                    help="se --prescan estiver ligado, imprime linha de "
                         "progresso a cada N%% dos arquivos (default 1)")
    ap.add_argument("--prescan", action="store_true",
                    help="pre-varre para contar arquivos (habilita %% e ETA)")
    ap.add_argument("--dry-run", action="store_true",
                    help="apenas lista o que faria")
    args = ap.parse_args()

    src_root = os.path.abspath(args.src.rstrip("/") or "/")
    dst_root = os.path.abspath(args.dst.rstrip("/") or "/")

    if not os.path.isdir(src_root):
        print(f"erro: origem nao e diretorio: {src_root}", file=sys.stderr)
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
        print(f"done-log:       {len(done)} arquivos previamente OK")
    if failed:
        print(f"err-log:        {len(failed)} previamente com falha (--skip-failed)")
    if checkpoint:
        print(f"checkpoint-log: {len(checkpoint)} diretorios 100%% concluidos")
    if accept_loss:
        print(f"accept-loss:    {len(accept_loss)} caminhos com perda aceita")

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
        hdr = (f"# skip_copy iniciado em {started.isoformat(timespec='seconds')}\n"
               f"# origem={src_root} destino={dst_root} "
               f"stall={args.stall}s timeout={args.timeout}s "
               f"skip_failed={args.skip_failed} no_checkpoint={args.no_checkpoint}\n")
        err_log.write(hdr)
        done_log.write(hdr)
        ckpt_log.write(hdr)

    ctx = WalkContext(args, src_root, dst_root, done, failed, checkpoint,
                      accept_loss, err_log, done_log, ckpt_log, status)

    # prescan opcional
    if args.prescan and args.progress_pct > 0:
        print("[prescan] enumerando arquivos...", flush=True)
        t0 = time.monotonic()
        ctx.total_planned = _prescan(src_root, checkpoint, args.no_checkpoint,
                                      args.scan_timeout, status, accept_loss)
        print(f"[prescan] {ctx.total_planned} arquivos em "
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
                   f"mesmo-tamanho={ctx.skipped_size}, "
                   f"ckpt-dirs={ctx.skipped_ckpt_dirs}, "
                   f"accept-loss={ctx.skipped_accept}) "
                   f"falhas={ctx.fail} (skip-failed={ctx.skipped_fail})")
        if err_log:
            tail = (f"# skip_copy terminado em "
                    f"{ended.isoformat(timespec='seconds')}  {summary}\n")
            err_log.write(tail); err_log.close()
            done_log.write(tail); done_log.close()
            ckpt_log.write(tail); ckpt_log.close()
        print(summary)
        print(f"Falhas:      {args.log}")
        print(f"Sucessos:    {args.done_log}")
        print(f"Checkpoint:  {args.checkpoint_log}")

    return 0 if ctx.fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
