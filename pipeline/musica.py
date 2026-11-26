"""Música de fondo: pistas por mood, mezcla con ducking (baja cuando habla la voz) y conversión a mp3."""
import hashlib
import re
import subprocess
import unicodedata
from pathlib import Path

from .textos import ErrorEjecucion, Texto, error
from . import rutas

RAIZ = Path(__file__).parent.parent
CARPETA = rutas.MUSICA
MOODS = ["tension", "misterio", "epico", "triste", "neutro"]
EXTENSIONES = (".mp3", ".m4a", ".wav")

FADE_IN = 1.5
FADE_OUT = 2.0
# Compresor con la voz como señal de control. Con la voz de edge-tts (~-20 dBFS RMS) esto baja
# la música unos 10 dB mientras se habla y la devuelve en ~0.5 s en los silencios.
DUCKING = "threshold=0.03:ratio=4:attack=15:release=450:knee=4:link=maximum:detection=rms"


def pistas(mood: str) -> list[Path]:
    d = CARPETA / mood
    return sorted(p for p in d.iterdir() if p.suffix.lower() in EXTENSIONES) if d.is_dir() else []


def resolver(musica: str | None, slug: str) -> Path | None:
    """'musica' puede ser un mood ("tension") o una ruta. Para un mood se elige una pista de forma
    determinista por slug: la de mayor hash(slug + nombre). Así, mismo guion = misma pista, y
    añadir pistas nuevas a la carpeta casi nunca cambia la de un guion ya hecho."""
    if not musica:
        return None
    if musica in MOODS:
        lista = pistas(musica)
        if not lista:
            return None
        return max(lista, key=lambda p: hashlib.md5(f"{slug}|{p.name}".encode()).hexdigest())
    ruta = Path(musica)
    return ruta if ruta.is_absolute() else rutas.DATOS / ruta


def mezclar(voz: Path, musica: Path, salida: Path, volumen: float, duracion: float):
    """Voz + música en loop, con fade in/out y ducking. 'duracion' puede ser mayor que la voz
    (cola de cierre): la música sigue sonando y hace el fade out al final."""
    fin = max(0.0, duracion - FADE_OUT)
    filtro = (
        # todo a 44.1 kHz estéreo: sidechaincompress y amix lo necesitan igual en ambas entradas
        "[0:a]aresample=44100,aformat=channel_layouts=stereo,apad,atrim=0:{d:.3f},asplit=2[voz][control];"
        "[1:a]aresample=44100,aformat=channel_layouts=stereo,atrim=0:{d:.3f},volume={v},"
        "afade=t=in:d={fi},afade=t=out:st={fin:.3f}:d={fo}[mus];"
        "[mus][control]sidechaincompress={duck}[bajada];"
        "[voz][bajada]amix=inputs=2:duration=first:normalize=0[a]"
    ).format(d=duracion, v=volumen, fi=FADE_IN, fin=fin, fo=FADE_OUT, duck=DUCKING)
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(voz), "-stream_loop", "-1", "-i", str(musica),
         "-filter_complex", filtro, "-map", "[a]", "-t", f"{duracion:.3f}",
         "-c:a", "aac", "-b:a", "192k", str(salida)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise ErrorEjecucion(Texto("err.ffmpeg_musica", detalle=r.stderr[-3000:]))


def nombre_seguro(nombre: str) -> str:
    base = unicodedata.normalize("NFKD", Path(nombre).stem).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:60] or "pista"


def guardar(origen: Path, nombre: str, mood: str) -> Path:
    """Copia una pista subida a assets/musica/<mood>/ como mp3 (convierte wav/m4a con ffmpeg)."""
    if mood not in MOODS:
        raise error("err.mood", mood=mood)
    if Path(nombre).suffix.lower() not in EXTENSIONES:
        raise error("err.musica_ext", nombre=nombre)
    destino_dir = CARPETA / mood
    destino_dir.mkdir(parents=True, exist_ok=True)
    base = nombre_seguro(nombre)
    destino, n = destino_dir / f"{base}.mp3", 2
    while destino.exists():
        destino, n = destino_dir / f"{base}-{n}.mp3", n + 1
    if Path(nombre).suffix.lower() == ".mp3":
        args = ["-c:a", "copy"]
    else:
        args = ["-c:a", "libmp3lame", "-q:a", "2"]
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(origen), "-vn", *args, str(destino)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        destino.unlink(missing_ok=True)
        raise error("err.audio_invalido", nombre=nombre)
    return destino
