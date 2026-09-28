#!/usr/bin/env python3
"""
gen_testcase.py - gera uma arvore de teste para o skip_copy.py.

Cria um diretorio de origem com:
  - arquivos pequenos / medios / grandes
  - subdirs aninhados (subdir1/deep, subdir2)
  - arquivos vazios
  - um symlink (deve ser ignorado pelo skip_copy)
  - opcionalmente: arquivos "ruins" que simulam I/O lento ou erro,
    montando uma camada FUSE *se* `python3-fusepy` estiver instalado
    e `/dev/fuse` disponivel. Caso contrario, gera apenas os arquivos
    "saudaveis" e avisa.

E gera tambem um pequeno script `run_demo.sh` ao lado da arvore com
chamadas de skip_copy.py reproduzindo o workflow do QUICKSTART.

Uso:
    python3 gen_testcase.py [--root /tmp/skip_copy_demo]
                            [--big-mb 8] [--n-files 20]
                            [--with-fuse]   # liga simulador de erros
                            [--clean]       # apaga e recria

Sem --with-fuse, nao precisa de root: gera so a arvore boa.
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
        # escreve em chunks de 64KB
        remaining = size
        chunk = 64 * 1024
        while remaining > 0:
            n = min(chunk, remaining)
            f.write(rng.randbytes(n))
            remaining -= n


def build_tree(root: Path, big_mb: int, n_files: int) -> None:
    print(f"[gen] criando arvore em {root}")
    (root / "origema" / "subdir1" / "deep").mkdir(parents=True, exist_ok=True)
    (root / "origema" / "subdir2").mkdir(parents=True, exist_ok=True)
    (root / "origemb").mkdir(parents=True, exist_ok=True)

    # arquivos pequenos
    for i in range(n_files):
        write_random(root / "origema" / "subdir1" / f"small_{i:03d}.bin", 1024 * (i + 1), seed=i)
    for i in range(n_files // 2):
        write_random(root / "origema" / "subdir1" / "deep" / f"deep_{i:02d}.bin", 4096, seed=100 + i)
    for i in range(n_files):
        write_random(root / "origema" / "subdir2" / f"s2_{i:03d}.bin", 2048, seed=200 + i)
    for i in range(n_files):
        write_random(root / "origemb" / f"b_{i:03d}.bin", 512 * (i + 1), seed=300 + i)

    # arquivos grandes (varios MB) -- bom pra testar --stall
    write_random(root / "origema" / "big_a.bin", big_mb * 1024 * 1024, seed=999)
    write_random(root / "origemb" / "big_b.bin", big_mb * 1024 * 1024, seed=998)

    # arquivo vazio
    (root / "origema" / "empty.txt").write_bytes(b"")

    # arquivo na raiz da origem
    (root / "root_file.txt").write_text("hello at root\n")

    # symlink (skip_copy deve ignorar)
    link = root / "origema" / "subdir1" / "link_to_root_file"
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(root / "root_file.txt")
    except OSError as e:
        print(f"[gen] aviso: nao consegui criar symlink: {e}")

    print("[gen] arvore criada")


def write_runner(root: Path, dst: Path, errlog: Path, donelog: Path) -> Path:
    runner = root.parent / "run_demo.sh"
    runner.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Demo: chama skip_copy reproduzindo o workflow do QUICKSTART.
        # Use os mesmos --log e --done-log nas 4 chamadas.
        set -u
        SRC={root}
        DST={dst}
        SC={SCRIPT}
        LOG={errlog}
        DONE={donelog}

        rm -f "$LOG" "$DONE"
        rm -rf "$DST"

        echo "==> (a) origema/subdir1"
        python3 "$SC" "$SRC/origema/subdir1" "$DST/origema/subdir1" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> (b) origemb"
        python3 "$SC" "$SRC/origemb" "$DST/origemb" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> (c) origema (restante; subdir1 deve ser pulado)"
        python3 "$SC" "$SRC/origema" "$DST/origema" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> (d) raiz inteira (deve pular tudo)"
        python3 "$SC" "$SRC" "$DST" \\
            --log "$LOG" --done-log "$DONE" --stall 10

        echo
        echo "==> resumo"
        echo "Sucessos: $(grep -cv '^#' "$DONE") em $DONE"
        echo "Falhas:   $(grep -cv '^#' "$LOG") em $LOG"
        echo "Arquivos no destino: $(find "$DST" -type f | wc -l)"
    """))
    runner.chmod(0o755)
    return runner


# ---------------------------------------------------------------------------
# camada FUSE opcional, simula erros de leitura em alguns arquivos
# ---------------------------------------------------------------------------

FUSE_SCRIPT_TEMPLATE = r'''#!/usr/bin/env python3
"""
bad_fuse.py - monta {mountpoint} espelhando {backing} mas com falhas
simuladas de I/O em arquivos cujo nome contem "BAD" e leitura lenta
em arquivos cujo nome contem "SLOW".
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
            # primeiro chunk passa, depois EIO -> simula CRC no meio
            raise FuseOSError(errno.EIO)
        if "SLOW" in name:
            time.sleep(60)  # trava muito alem do --stall
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
    """Tenta criar bad_fuse.py. Retorna o path do script ou None."""
    try:
        import fuse  # noqa: F401
    except Exception:
        print("[gen] aviso: pacote 'fusepy' nao encontrado; pulando camada FUSE.", file=sys.stderr)
        print("       instale com: pip install fusepy   (e: sudo apt install fuse3)", file=sys.stderr)
        return None
    if not os.path.exists("/dev/fuse"):
        print("[gen] aviso: /dev/fuse ausente; pulando camada FUSE.", file=sys.stderr)
        return None

    fuse_script = root.parent / "bad_fuse.py"
    fuse_script.write_text(FUSE_SCRIPT_TEMPLATE.format(
        backing=str(root), mountpoint=str(mount)))
    fuse_script.chmod(0o755)

    # cria alguns arquivos com nomes especiais para acionar BAD/SLOW
    for sub in (root / "origema" / "subdir1", root / "origemb"):
        sub.mkdir(parents=True, exist_ok=True)
        write_random(sub / "BAD_crc_demo.bin", 256 * 1024, seed=42)
        write_random(sub / "SLOW_huge.bin",    256 * 1024, seed=43)

    print(f"[gen] camada FUSE em {fuse_script}")
    print(f"[gen] para usar:")
    print(f"      python3 {fuse_script}    # em outro terminal")
    print(f"      depois rode skip_copy lendo de {mount} em vez de {root}")
    return fuse_script


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/skip_copy_demo",
                    help="diretorio base do testcase (default /tmp/skip_copy_demo)")
    ap.add_argument("--big-mb", type=int, default=8, help="tamanho dos arquivos grandes em MiB")
    ap.add_argument("--n-files", type=int, default=12, help="quantidade de arquivos pequenos por diretorio")
    ap.add_argument("--with-fuse", action="store_true",
                    help="tambem gera bad_fuse.py (precisa de fusepy)")
    ap.add_argument("--clean", action="store_true", help="remove tudo antes de gerar")
    args = ap.parse_args()

    base = Path(args.root).resolve()
    src = base / "origem"
    dst = base / "destino"
    mount = base / "origem_via_fuse"
    errlog = base / "logs" / "skip_copy.err.log"
    donelog = base / "logs" / "skip_copy.done.log"

    if args.clean and base.exists():
        print(f"[gen] removendo {base}")
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
    print(f"Origem:  {src}")
    print(f"Destino: {dst}")
    print(f"Logs:    {errlog} | {donelog}")
    print()
    print(f"Para rodar a demo:  bash {runner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
