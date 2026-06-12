#!/usr/bin/env python3
"""editar.py — prepara vídeos de portfólio para scroll scrubbing.

Pipeline (um comando só):
    corte → ajuste de cor (escurecer + aquecer) → zoom progressivo (opcional)
    → extração de frames → otimização → relatório.

O trabalho pesado é todo do FFmpeg; este script é a "cola": valida a entrada,
monta o filtergraph dinamicamente conforme as flags e relata o resultado.

Uso:
    python editar.py --input video.mp4 --output hero --start 2 --duration 12 \\
        --brightness -0.06 --contrast 1.08 --temperature 5200 --vignette \\
        --fps 12 --width 1280 --quality 4
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────
# 1. CONFIG
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class Settings:
    """Todos os parâmetros do CLI, já com defaults sensatos."""

    input: Path
    output: str
    # corte (segundos)
    start: float = 0.0
    duration: float | None = None  # None = até o fim do vídeo
    # cor (filtro eq do FFmpeg)
    brightness: float = 0.0  # -1.0 .. 1.0   (negativo = escurece)
    contrast: float = 1.0  #  0.0 .. ~2.0  (1.0 = neutro)
    saturation: float = 1.0  #  0.0 .. ~3.0  (1.0 = neutro)
    # temperatura de cor (colortemperature). None = não aplica.
    temperature: int | None = None  # Kelvin; menor = mais quente
    vignette: bool = False
    # zoom progressivo (zoompan), usado no vídeo "Sobre"
    zoom: bool = False
    zoom_max: float = 2.0
    # extração
    fps: int = 12
    width: int = 1280
    quality: int = 4  # -q:v do mjpeg: 1 melhor … 31 pior (limitamos a 1..10)
    force: bool = False  # pula a confirmação de sobrescrita

    @property
    def out_dir(self) -> Path:
        return Path("public") / "frames" / self.output


# Códigos de cor ANSI para mensagens legíveis no terminal.
class C:
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RESET = "\033[0m"


def step(msg: str) -> None:
    """Mensagem de progresso (uma etapa do pipeline)."""
    print(f"{C.CYAN}▸{C.RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {C.DIM}{msg}{C.RESET}")


def warn(msg: str) -> None:
    print(f"{C.YELLOW}⚠{C.RESET}  {msg}")


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    """Encerra com erro legível — nunca um traceback cru."""
    print(f"{C.RED}✗ Erro:{C.RESET} {msg}", file=sys.stderr)
    sys.exit(code)


# ──────────────────────────────────────────────────────────────────────────
# 2. PROBE — lê metadados do vídeo de entrada com ffprobe
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int


def probe_video(path: Path) -> VideoInfo:
    """Roda ffprobe e devolve duração e resolução do vídeo."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("ffprobe não encontrado no PATH. Instale o FFmpeg (ex.: brew install ffmpeg).")

    if proc.returncode != 0:
        die(f"ffprobe falhou ao ler '{path}':\n{_tail(proc.stderr)}")

    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    if not streams:
        die(f"'{path}' não parece conter um stream de vídeo.")

    try:
        duration = float(data["format"]["duration"])
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (KeyError, ValueError, TypeError):
        die(f"Não consegui interpretar os metadados de '{path}'.")

    return VideoInfo(duration=duration, width=width, height=height)


# ──────────────────────────────────────────────────────────────────────────
# 3. VALIDATE — garante que start/duration cabem no vídeo real
# ──────────────────────────────────────────────────────────────────────────


def validate(s: Settings, v: VideoInfo) -> float:
    """Valida o recorte. Devolve a duração efetiva do corte (segundos)."""
    if s.start < 0:
        die("--start não pode ser negativo.")
    if s.start >= v.duration:
        die(
            f"--start {s.start}s está além do fim do vídeo "
            f"(duração real {v.duration:.2f}s)."
        )

    available = v.duration - s.start
    if s.duration is None:
        effective = available
    else:
        if s.duration <= 0:
            die("--duration deve ser maior que zero.")
        if s.duration > available + 0.05:  # margem pra arredondamento
            die(
                f"--start {s.start}s + --duration {s.duration}s = "
                f"{s.start + s.duration:.2f}s ultrapassa a duração real "
                f"({v.duration:.2f}s). Sobram apenas {available:.2f}s a partir de --start."
            )
        effective = s.duration

    if s.width > v.width:
        warn(
            f"--width {s.width}px é maior que a largura original ({v.width}px); "
            "o vídeo será ampliado (upscale) e pode perder nitidez."
        )

    return effective


# ──────────────────────────────────────────────────────────────────────────
# 4. FILTER — monta a string -vf dinamicamente (o coração da ferramenta)
# ──────────────────────────────────────────────────────────────────────────


def scaled_dims(s: Settings, v: VideoInfo) -> tuple[int, int]:
    """Calcula (largura, altura) após scale=width:-2, altura par."""
    height = round(v.height * s.width / v.width)
    if height % 2:  # filtros/codecs exigem dimensões pares
        height += 1
    return s.width, height


def build_zoompan(s: Settings, dims: tuple[int, int], total_frames: int) -> str:
    """Zoom progressivo e centralizado ao longo de todo o clipe.

    zoompan incrementa o zoom POR FRAME DE SAÍDA (não por segundo). Para um
    zoom linear de 1.0 → zoom_max, o incremento por frame é:
        inc = (zoom_max - 1) / total_frames

    Setamos a própria fps do zoompan (fps={s.fps}) para não precisar de um
    filtro fps extra depois — assim evitamos reamostragem dupla. O 's=WxH' é
    OBRIGATÓRIO: sem ele, o zoompan força 1280x720 (hd720) e distorce o aspecto.
    """
    w, h = dims
    inc = (s.zoom_max - 1.0) / max(total_frames, 1)
    return (
        f"zoompan=z='min(zoom+{inc:.6f},{s.zoom_max})'"
        ":x='iw/2-(iw/zoom/2)'"  # mantém o zoom centralizado
        ":y='ih/2-(ih/zoom/2)'"
        ":d=1"
        f":s={w}x{h}"
        f":fps={s.fps}"
    )


def build_filter(s: Settings, v: VideoInfo, total_frames: int) -> str:
    """Junta os estágios condicionais num filtergraph linear (separado por vírgula).

    Ordem deliberada:
      1. scale      — reduz pixels primeiro (acelera todo o resto)
      2. eq         — brightness/contrast/saturation num filtro só
      3. colortemp  — aquece/esfria depois da correção base
      4. zoompan    — recorta antes da vinheta, pra vinheta emoldurar o final
      5. vignette   — escurece bordas da imagem já corrigida
      6. fps        — amostragem por último (só quando NÃO há zoom)
    """
    dims = scaled_dims(s, v)
    stages: list[str] = [f"scale={s.width}:-2"]

    if s.brightness != 0.0 or s.contrast != 1.0 or s.saturation != 1.0:
        stages.append(
            f"eq=brightness={s.brightness}"
            f":contrast={s.contrast}"
            f":saturation={s.saturation}"
        )

    if s.temperature is not None:
        stages.append(f"colortemperature=temperature={s.temperature}")

    if s.zoom:
        # zoompan já cuida da fps internamente.
        stages.append(build_zoompan(s, dims, total_frames))

    if s.vignette:
        stages.append("vignette=PI/5")  # ângulo suave (PI/4 já fica forte)

    if not s.zoom:
        stages.append(f"fps={s.fps}")

    return ",".join(stages)


# ──────────────────────────────────────────────────────────────────────────
# 5. EXTRACT — chama o FFmpeg
# ──────────────────────────────────────────────────────────────────────────


def confirm_overwrite(s: Settings) -> None:
    """Se a pasta já tem frames, pede confirmação (a menos que --force)."""
    existing = list(s.out_dir.glob("frame-*.jpg"))
    if not existing:
        return
    if s.force:
        info(f"Sobrescrevendo {len(existing)} frame(s) existente(s) (--force).")
    else:
        warn(f"{s.out_dir} já contém {len(existing)} frame(s).")
        resp = input(f"  Sobrescrever? [s/N] ").strip().lower()
        if resp not in ("s", "sim", "y", "yes"):
            die("Cancelado pelo usuário.", code=0)
    for f in existing:
        f.unlink()


def run_ffmpeg(s: Settings, vf: str) -> None:
    """Monta e executa o comando do FFmpeg que extrai os frames."""
    s.out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(s.out_dir / "frame-%03d.jpg")

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    # -ss antes de -i: seek rápido e (no FFmpeg moderno) preciso o suficiente.
    if s.start > 0:
        cmd += ["-ss", str(s.start)]
    cmd += ["-i", str(s.input)]
    if s.duration is not None:
        cmd += ["-t", str(s.duration)]
    cmd += ["-vf", vf, "-q:v", str(s.quality), pattern]

    info(f"filtro: {vf}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("ffmpeg não encontrado no PATH. Instale o FFmpeg (ex.: brew install ffmpeg).")

    if proc.returncode != 0:
        die(f"o FFmpeg falhou ao processar o vídeo:\n{_tail(proc.stderr)}")


# ──────────────────────────────────────────────────────────────────────────
# 6. OPTIMIZE — jpegoptim, se disponível
# ──────────────────────────────────────────────────────────────────────────


def optimize_jpegs(s: Settings) -> None:
    """Otimiza os JPEGs com jpegoptim. Se não houver, avisa mas não quebra."""
    if shutil.which("jpegoptim") is None:
        warn(
            "jpegoptim não encontrado — pulando otimização.\n"
            "  Para otimizar (reduz ~10-30% do tamanho): brew install jpegoptim"
        )
        return

    frames = sorted(s.out_dir.glob("frame-*.jpg"))
    if not frames:
        return
    step("Otimizando JPEGs (jpegoptim)…")
    cmd = ["jpegoptim", "--strip-all", "--quiet", *[str(f) for f in frames]]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        warn(f"jpegoptim retornou erro (frames mantidos):\n{_tail(proc.stderr)}")


# ──────────────────────────────────────────────────────────────────────────
# 7. REPORT
# ──────────────────────────────────────────────────────────────────────────


def _human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def report(s: Settings) -> int:
    """Relatório final. Devolve a contagem de frames."""
    frames = sorted(s.out_dir.glob("frame-*.jpg"))
    count = len(frames)
    total = sum(f.stat().st_size for f in frames)

    print()
    print(f"{C.GREEN}{C.BOLD}✓ Concluído.{C.RESET}")
    print(f"  Frames gerados : {C.BOLD}{count}{C.RESET}")
    print(f"  Tamanho total  : {_human_size(total)}")
    print(f"  Pasta          : {s.out_dir}/")
    print(f"  Padrão         : frame-001.jpg … frame-{count:03d}.jpg")

    if count == 0:
        warn("Nenhum frame foi gerado — confira os parâmetros.")
    elif count > 999:
        warn(
            f"{count} frames excedem o padrão frame-%03d (max 999). "
            "A numeração reinicia/estoura — reduza --fps ou --duration."
        )
    elif not 90 <= count <= 150:
        info(
            f"Dica: a meta é 90-150 frames para o scroll. "
            f"Ajuste --fps/--duration se quiser chegar mais perto."
        )
    return count


# ──────────────────────────────────────────────────────────────────────────
# Util
# ──────────────────────────────────────────────────────────────────────────


def _tail(text: str, lines: int = 15) -> str:
    """Últimas N linhas de um stderr — onde o FFmpeg põe o erro real."""
    rows = [r for r in (text or "").strip().splitlines() if r.strip()]
    snippet = "\n".join(rows[-lines:]) if rows else "(sem detalhes)"
    return f"{C.DIM}{snippet}{C.RESET}"


# ──────────────────────────────────────────────────────────────────────────
# 8. CLI
# ──────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> Settings:
    p = argparse.ArgumentParser(
        prog="editar.py",
        description="Edita um vídeo e extrai frames prontos para scroll scrubbing.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input", required=True, type=Path, help="Caminho do vídeo bruto.")
    p.add_argument(
        "--output",
        required=True,
        help='Nome base da saída. Ex.: "hero" → public/frames/hero/',
    )
    # corte
    p.add_argument("--start", type=float, default=0.0, help="Início do corte (segundos).")
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duração do corte (segundos). Padrão: até o fim do vídeo.",
    )
    # cor
    p.add_argument(
        "--brightness",
        type=float,
        default=0.0,
        help="Brilho (eq): -1.0 a 1.0. Negativo escurece.",
    )
    p.add_argument(
        "--contrast", type=float, default=1.0, help="Contraste (eq): 1.0 = neutro."
    )
    p.add_argument(
        "--saturation", type=float, default=1.0, help="Saturação (eq): 1.0 = neutro."
    )
    p.add_argument(
        "--temperature",
        type=int,
        default=None,
        help="Temperatura de cor em Kelvin (colortemperature). Menor = mais quente. "
        "Padrão: não aplica.",
    )
    p.add_argument(
        "--vignette", action="store_true", help="Aplica uma vinheta suave nas bordas."
    )
    # zoom
    p.add_argument(
        "--zoom", action="store_true", help="Ativa zoom progressivo (vídeo 'Sobre')."
    )
    p.add_argument(
        "--zoom-max", type=float, default=2.0, help="Zoom máximo ao fim do clipe."
    )
    # extração
    p.add_argument(
        "--fps",
        type=int,
        default=12,
        help="Frames por segundo a extrair (mire em 90-150 frames totais).",
    )
    p.add_argument(
        "--width", type=int, default=1280, help="Largura de saída (altura automática)."
    )
    p.add_argument(
        "--quality",
        type=int,
        default=4,
        choices=range(1, 11),
        metavar="{1-10}",
        help="Qualidade JPEG: 1 melhor … 10 pior.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Sobrescreve frames existentes sem perguntar.",
    )

    ns = p.parse_args(argv)
    return Settings(
        input=ns.input,
        output=ns.output,
        start=ns.start,
        duration=ns.duration,
        brightness=ns.brightness,
        contrast=ns.contrast,
        saturation=ns.saturation,
        temperature=ns.temperature,
        vignette=ns.vignette,
        zoom=ns.zoom,
        zoom_max=ns.zoom_max,
        fps=ns.fps,
        width=ns.width,
        quality=ns.quality,
        force=ns.force,
    )


def main(argv: list[str] | None = None) -> int:
    s = parse_args(argv)

    if not s.input.exists():
        die(f"vídeo de entrada não encontrado: {s.input}")

    step(f"Lendo metadados de {s.input.name}…")
    v = probe_video(s.input)
    info(f"duração {v.duration:.2f}s · resolução {v.width}x{v.height}")

    effective = validate(s, v)
    total_frames = round(s.fps * effective)
    info(f"corte efetivo {effective:.2f}s · ~{total_frames} frames previstos")

    confirm_overwrite(s)

    pieces = ["cortando", "ajustando cor"]
    if s.zoom:
        pieces.append("zoom")
    if s.vignette:
        pieces.append("vinheta")
    pieces.append("extraindo frames")
    step(" → ".join(pieces) + "…")

    vf = build_filter(s, v, total_frames)
    run_ffmpeg(s, vf)

    optimize_jpegs(s)
    report(s)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        die("Interrompido.", code=130)
