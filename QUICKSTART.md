# skip_copy — QUICKSTART

Copia arquivos de uma origem (mídia ruim, com erros de CRC/I-O) para um destino, **pulando rapidamente** o que falhar e **registrando logs persistentes** para múltiplas execuções.

## Filosofia

- **Sem retry**: arquivo deu erro, vai pro log e segue o jogo.
- **Logs por caminho absoluto da origem**: dá pra rodar várias vezes em sub-árvores diferentes que os logs continuam coerentes.
- **Checkpoint por diretório**: quando uma subárvore inteira é copiada com 100% de sucesso, o diretório vai para um log de checkpoint e na próxima execução é **pulado inteiro** sem nem ler a mídia.
- **Stat-averso**: a enumeração evita `stat()` em cada arquivo (o que faria `ls -l` travar em mídia ruim). Só usa `d_type` da `scandir`.
- **`scandir` em subprocesso com timeout** (`--scan-timeout`, default 30s): se um diretório danificado travar a listagem no kernel, o filho é abandonado e o resto da árvore continua. (Antes, isso prendia o processo principal até nem `Ctrl+C` resolver.)
- **Detecção de stall**: aborta se o destino parar de crescer (melhor que timeout fixo para arquivos grandes).
- **Kill não-bloqueante**: se o kernel deixar um filho em D-state por hardware travado, o pai não fica preso esperando — manda `SIGTERM`/`SIGKILL`, espera no máximo 1s e segue.
- **Status file** (`--status-file`): grava em tempo real o que o script está prestes a fazer. Em qualquer travamento, um `cat <status-file>` mostra exatamente onde parou.
- **Ctrl+C inteligente**: 1x aborta o arquivo atual e segue; 2x rápido (<2s) sai de vez.

## Sinais no progresso (stdout)

| símbolo | significado                                                  |
|---------|--------------------------------------------------------------|
| `.`     | copiou agora                                                 |
| `:`     | pulado pelo done-log (já copiou em rodada anterior)          |
| `=`     | destino já existia com mesmo tamanho (`--check-size`)        |
| `D`     | subárvore inteira pulada por checkpoint                      |
| `L`     | pulado por `--accept-loss` (perda aceita)                    |
| `x`     | pulado pelo err-log (`--skip-failed`)                        |
| `X`     | falha nesta rodada (foi pro err-log)                         |

Além disso, com a flag `--prescan`, o script faz uma **pré-varredura**
(só conta arquivos, sem `stat` por arquivo — usa `d_type` da `scandir`)
e passa a imprimir uma linha de progresso a cada **1%** dos arquivos
(ajustável via `--progress-pct`):

```
[ 42.0%] 4200/10000 ok=4180 fail=12 skip(done=8,fail=0,size=0) rate=87.3/s eta=66s
```

Por padrão a varredura é **preguiçosa** (lazy) e não há `[ x.x %]` —
mais resiliente em mídia ruim, sem custo de varrer tudo antes de
começar a copiar.

## Logs

- `--log` (default `/root/skip_copy.err.log`): falhas, formato `<abs_path>\t# motivo (tempo)`.
- `--done-log` (default `/root/skip_copy.done.log`): sucessos, formato `<abs_path>`.
- `--checkpoint-log` (default `/root/skip_copy.dirs.log`): diretórios cuja subárvore foi 100% copiada com sucesso. Em reruns, esses diretórios são pulados inteiros (`D`) sem nem ler a mídia. Para desabilitar use `--no-checkpoint`.
- `--accept-loss` (default `/root/accept_loss.txt`): **arquivo de entrada** que você preenche com caminhos absolutos (arquivos OU diretórios) cuja perda você aceita. Linhas começando com `#` são comentários. Falhas dentro desses caminhos não impedem que pais virem checkpoint. Itens listados nem são tentados (`L` no progresso).

## Instalação

```bash
# já está em /home/rec/skip_copy/skip_copy.py
sudo chmod +x /home/rec/skip_copy/skip_copy.py
```

## Exemplos

### 1) Uso básico

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec
```

O **status file** já vem ligado por padrão em `/tmp/skip_copy.status` (tmpfs, em RAM — não desgasta o SSD mesmo sendo regravado a cada arquivo). Para descobrir onde o script está num dado momento:

```bash
cat /tmp/skip_copy.status
# 2026-05-01T20:32:11   file:copy   /mnt/rec/foo/bar.bin
```

Fases possíveis: `prescan`, `dir:scan`, `file:check`, `file:copy`, `done`.

Para mudar de lugar use `--status-file /outro/caminho`. Para desligar, `--status-file ''`.

### 2) Workflow recomendado: copiar em partes, mais importante primeiro

Mesmos logs em todas as chamadas — o script pula automaticamente o que já foi copiado.

```bash
# (a) subdir mais importante de origema, primeiro
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/origema/subdir1  /dados_rec/origema/subdir1

# (b) origemb inteiro
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/origemb  /dados_rec/origemb

# (c) restante de origema (subdir1 ja foi -> sera pulado pelo done-log)
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/origema  /dados_rec/origema

# (d) varredura final em /mnt/rec inteiro -- pula tudo que ja foi feito
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec
```

> **Por que funciona**: os logs guardam **caminhos absolutos da origem** (`/mnt/rec/origema/subdir1/foo.jpg`). Quando você roda depois com origem `/mnt/rec`, o mesmo caminho absoluto aparece e é detectado no done-log.

### 3) Não tentar de novo o que já falhou

Útil quando você já sabe que aquela região do disco está perdida e não quer perder tempo (e desgastar mais a mídia) re-tentando:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --skip-failed
```

### 4) Disco muito lento (USB 2.0, ópticos): aumenta o stall

Se o disco bom copia em poucos MB/s, 15s sem progresso pode ser só lentidão. Suba para 60s:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stall 60
```

### 5) Disco rápido com setores ruins isolados: stall agressivo

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stall 5
```

### 6) Limite absoluto além do stall (pra arquivos pequenos)

Útil pra arquivos minúsculos que falham antes mesmo de criar bytes no destino (o stall não dispara):

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stall 15 --timeout 60
```

### 7) Logs em local custom

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  \
    --log     /home/rec/skip_copy/erros.log \
    --done-log /home/rec/skip_copy/sucessos.log
```

### 8) Dry-run: lista o que seria copiado

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec/origema  /dados_rec/origema  --dry-run | head
```

### 9) Verificação de tamanho ao re-rodar (`--check-size`)

Por padrão, o script **não** chama `stat()` na origem (pra não travar em áreas ruins). Com `--check-size`, ele confirma o tamanho da origem antes de pular um arquivo já no done-log. Use só se a origem está respondendo bem:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --check-size
```

### 10) Filesystem que não retorna `d_type` (raro): ajuste o stat-timeout

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --stat-timeout 5
```

### 11) Progresso em % (com pré-varredura)

Por padrão o script é lazy e não mostra %. Para ver `[ 42.0% ] ... eta=...`,
ative a pré-varredura:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --prescan
```

Para imprimir menos linhas (a cada 5% em vez de 1%):

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec  --prescan --progress-pct 5
```

### 12) Lazy (default) — começa imediato, sem %

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py \
    /mnt/rec  /dados_rec
```

### 13) Aceitar perdas (continuar mesmo com falhas em áreas conhecidas)

Crie `/root/accept_loss.txt` com caminhos absolutos (arquivos ou diretórios). Tudo lá é considerado perdível — falhas não quebram o checkpoint do diretório pai, e os caminhos listados são pulados sem tentativa (`L`):

```text
# /root/accept_loss.txt
# diretorios inteiros perdiveis:
/mnt/rec/home/hex/.cache
/mnt/rec/home/hex/snap/firefox/common/.cache
# arquivo especifico perdivel:
/mnt/rec/home/hex/Downloads/iso_corrompido.iso
```

Uso (file padrão é lido automaticamente):

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec
```

Usando arquivo custom ou desligando:

```bash
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec \
    --accept-loss /home/rec/perdas.txt
sudo python3 /home/rec/skip_copy/skip_copy.py /mnt/rec /dados_rec \
    --accept-loss ''
```

## Inspecionando os logs

```bash
# quantos sucessos / falhas
grep -cv '^#' /root/skip_copy.done.log
grep -cv '^#' /root/skip_copy.err.log

# ver as falhas
grep -v '^#' /root/skip_copy.err.log

# falhas por motivo
grep -v '^#' /root/skip_copy.err.log | awk -F'#' '{print $2}' | sort | uniq -c | sort -rn
```

## Reset / recomeçar do zero

```bash
sudo rm -f /root/skip_copy.done.log /root/skip_copy.err.log /root/skip_copy.dirs.log
```

---

# skip_find — busca de arquivos por data em mídia ruim

Equivalente a `find <path> -newermt "2026-04-12" -type f`, mas com a mesma proteção contra syscalls travadas em mídia com CRC ruim **e incremental** (pode rodar várias vezes, pula o que já varreu).

## Filosofia (mesma do skip_copy)

- **Incremental**: rode nas subárvores mais importantes primeiro, depois em diretórios mais amplos. Os logs são compartilhados e o que já foi varrido é pulado automaticamente.
- **Checkpoint por diretório**: quando uma subárvore inteira é processada sem falhas, vai para o checkpoint-log e na próxima execução é **pulada inteira** (`D`).
- **Done-log**: cada arquivo onde o stat funcionou (independente do mtime) é registrado. Na próxima execução é pulado (`:`) sem nem tocar na mídia.
- **scandir + stat em subprocesso** com timeout (`--scan-timeout`, default 30s): se um diretório travar no kernel, o filho é abandonado e a varredura continua.
- **stat individual em subprocesso** (`--stat-timeout`, default 10s): fallback para arquivos onde o stat em batch falhou.
- **Kill não-bloqueante**: filho preso em D-state é abandonado.
- **Status file** (`/tmp/skip_find.status`): mostra onde o script está agora.
- **Ctrl+C inteligente**: 1x pula o diretório atual; 2x rápido (<2s) sai.

## Sinais no progresso (stdout)

| símbolo | significado                                    |
|---------|------------------------------------------------|
| `.`     | arquivo encontrado (mtime >= threshold)        |
| `_`     | arquivo mais antigo (só com `--verbose`)       |
| `:`     | pulado pelo done-log (já processado antes)     |
| `D`     | subárvore inteira pulada por checkpoint        |
| `x`     | pulado pelo err-log (`--skip-failed`)          |
| `X`     | falha/timeout nesta rodada (foi pro err-log)   |

A cada ~10s imprime uma linha de resumo com contadores.

## Logs

- `--list-log` (default `/root/skip_find.list.log`): caminhos absolutos dos arquivos encontrados (mtime >= threshold). Sempre append.
- `--err-log` (default `/root/skip_find.err.log`): falhas/timeouts, formato `<abs_path>\t# motivo`. Sempre append.
- `--done-log` (default `/root/skip_find.done.log`): arquivos já processados (stat OK). Na próxima execução são pulados (`:`) sem tocar na mídia.
- `--checkpoint-log` (default `/root/skip_find.dirs.log`): diretórios cuja subárvore foi 100% processada. Na próxima execução são pulados inteiros (`D`). Para desabilitar use `--no-checkpoint`.

## Exemplos

### 1) Uso básico — arquivos mais novos que 12/abr/2026

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12"
```

### 2) Data+hora precisa

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12 14:30:00"
```

### 3) Workflow incremental: varrer por prioridade

```bash
# (a) subdir mais importante primeiro
sudo python3 skip_find.py /mnt/rec/projetos --newermt "2026-04-12"

# (b) outra area
sudo python3 skip_find.py /mnt/rec/documentos --newermt "2026-04-12"

# (c) tudo (subarvores já varridas sao puladas via checkpoint)
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12"
```

> **Por que funciona**: os logs guardam **caminhos absolutos**. Quando você roda depois com raiz `/mnt/rec`, os diretórios `/mnt/rec/projetos` e `/mnt/rec/documentos` já estão no checkpoint-log e são pulados inteiros (`D`).

### 4) Não re-tentar o que já falhou

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" --skip-failed
```

### 5) Logs em local custom

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-01" \
    --list-log /root/encontrados.log \
    --err-log  /root/find_erros.log \
    --done-log /root/find_done.log \
    --checkpoint-log /root/find_dirs.log
```

### 6) Pular diretórios conhecidos como ruins

Crie um arquivo com caminhos absolutos (um por linha):

```text
# /root/skip_dirs.txt
/mnt/rec/home/hex/.cache
/mnt/rec/System Volume Information
```

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" \
    --skip-dir /root/skip_dirs.txt
```

### 7) Disco muito travado: timeout agressivo

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" \
    --scan-timeout 10 --stat-timeout 5
```

### 8) Forçar re-varredura (ignorar checkpoints)

```bash
sudo python3 skip_find.py /mnt/rec --newermt "2026-04-12" --no-checkpoint
```

### 9) Inspecionar status em outro terminal

```bash
cat /tmp/skip_find.status
# 2026-05-01T20:32:11   dir:scan   /mnt/rec/home/hex/Documents
```

Fases possíveis: `start`, `dir:scan`, `file:stat`, `done`.

### 10) Reset / recomeçar do zero

```bash
sudo rm -f /root/skip_find.{list,err,done,dirs}.log
```

## Inspecionando os logs

```bash
# quantos encontrados
grep -cv '^#' /root/skip_find.list.log

# quantas falhas
grep -cv '^#' /root/skip_find.err.log

# falhas por motivo
grep -v '^#' /root/skip_find.err.log | awk -F'#' '{print $2}' | sort | uniq -c | sort -rn
```

---

## Limitações conhecidas (ambos os scripts)

- Se o **kernel** travar num syscall em estado D (uninterruptible) por causa de hardware travado no SATA/USB, nem `SIGKILL` mata na hora. Nesses casos:
  - reduza `--stall` / `--scan-timeout`,
  - ou faça uma imagem com `ddrescue` antes (e copie da imagem),
  - ou desmonte/remonte com opções tolerantes (ex.: NTFS via `ntfs-3g`).
- Symlinks são **ignorados** (não seguidos nem copiados como link).
- Atributos estendidos (xattr/ACL) não são preservados — só `mode` e `timestamps`.
