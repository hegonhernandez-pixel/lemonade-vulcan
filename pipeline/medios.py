"""Archivos locales de escenas: rutas privadas, subida acotada y validación FFmpeg."""
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .guion import EXT_CLIP, EXT_IMAGEN, media_valida
from .textos import error

MAX_BYTES = 250 * 1024 * 1024
MAX_DURACION = 600.0
SLUG = re.compile(r'^[a-z0-9][a-z0-9_-]{0,80}$')


def privada(raiz: Path, slug: str, media: str | None = None) -> Path:
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or (media is not None and not media_valida(media)):
        raise error('clip.ruta_invalida')
    carpeta = raiz / slug
    ruta = carpeta / media.replace('\\', '/').split('/')[-1] if media is not None else carpeta
    for p in (raiz, carpeta, ruta):
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise error('clip.ruta_invalida')
    if not ruta.resolve().is_relative_to(raiz.resolve()):
        raise error('clip.ruta_invalida')
    return ruta


@lru_cache(maxsize=128)
def _sondear(ruta: str, tamano: int, mtime: int) -> dict:
    try:
        r = subprocess.run([
            'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov',
            '-select_streams', 'v:0', '-show_entries',
            'stream=codec_name,width,height,duration:stream_disposition=attached_pic:format=format_name,duration',
            '-of', 'json', ruta,
        ], capture_output=True, text=True, timeout=15, check=False)
        if r.returncode:
            raise ValueError()
        info = json.loads(r.stdout)
        v = info['streams'][0]
        dur = float(v.get('duration') or info['format']['duration'])
        if (not {'mov', 'mp4'}.intersection(info['format']['format_name'].split(','))
                or v.get('disposition', {}).get('attached_pic') or not math.isfinite(dur) or dur <= 0
                or type(v.get('width')) is not int or type(v.get('height')) is not int
                or not 1 <= v['width'] <= 8192 or not 1 <= v['height'] <= 8192):
            raise ValueError()
        return {'duracion': dur, 'ancho': v['width'], 'alto': v['height'], 'codec': v['codec_name']}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        raise error('clip.invalido') from None


def info_clip(ruta: Path) -> dict:
    if not ruta.is_file() or ruta.is_symlink():
        raise error('clip.invalido')
    stat = ruta.stat()
    return dict(_sondear(str(ruta.resolve()), stat.st_size, stat.st_mtime_ns))


def validar_inicio(ruta: Path, inicio: float) -> dict:
    info = info_clip(ruta)
    if (isinstance(inicio, bool) or not isinstance(inicio, (int, float))
            or not math.isfinite(inicio) or not 0 <= inicio < info['duracion']):
        raise error('clip.inicio_invalido')
    return info


@lru_cache(maxsize=32)
def _ultimo_cuadro(ruta: str, tamano: int, mtime: int, duracion: float) -> float:
    """PTS del último cuadro: permite entrar en su intervalo, incluso en videos VFR."""
    try:
        r = subprocess.run([
            'ffprobe', '-v', 'error', '-threads', '2', '-protocol_whitelist', 'file,pipe',
            '-format_whitelist', 'mov', '-select_streams', 'v:0',
            '-read_intervals', f'{max(0, duracion - 2):.6f}%',
            '-show_entries', 'frame=best_effort_timestamp_time:format=start_time', '-of', 'json', ruta,
        ], capture_output=True, text=True, timeout=20, check=False)
        datos = json.loads(r.stdout)
        base = float(datos.get('format', {}).get('start_time') or 0)
        tiempos = [float(f['best_effort_timestamp_time']) - base for f in datos['frames']]
        if r.returncode or not tiempos or not all(math.isfinite(x) for x in tiempos):
            raise ValueError()
        return max(0, max(tiempos))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise error('clip.decodificacion') from None


def inicio_decodificable(ruta: Path, inicio: float, info: dict) -> float:
    # Un seek después del último PTS descarta todos los cuadros. Elegir ese último
    # PTS conserva exactamente el fotograma que cubre el segundo solicitado.
    if inicio:
        st = ruta.stat()
        return min(inicio, _ultimo_cuadro(str(ruta.resolve()), st.st_size, st.st_mtime_ns, info['duracion']))
    return inicio


def validar_clip(ruta: Path) -> dict:
    info = info_clip(ruta)
    if ruta.stat().st_size > MAX_BYTES:
        raise error('clip.tamano')
    if (info['codec'] != 'h264' or info['ancho'] * info['alto'] > 4096 * 4096
            or max(info['ancho'], info['alto']) > 4096 or info['duracion'] > MAX_DURACION):
        raise error('clip.compatible')
    try:
        # Decodificar toda la pista visual, sin transcodificar ni escribir otro video.
        r = subprocess.run([
            'ffmpeg', '-v', 'error', '-xerror', '-err_detect', 'explode', '-threads', '2',
            '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov', '-i', str(ruta),
            '-map', '0:v:0', '-an', '-sn', '-dn', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-',
        ], capture_output=True, text=True, timeout=90, check=False)
        frames = re.findall(r'^frame=(\d+)', r.stdout, re.M)
        if r.returncode or not frames or int(frames[-1]) <= 0:
            raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError):
        raise error('clip.decodificacion') from None
    return info


def guardar_subida(raiz: Path, slug: str, archivo, nombre: str, nombre_final: str | None = None) -> dict:
    """nombre_final (p. ej. "cierre"): solo imágenes, y se guarda como <nombre_final>.<ext>, reemplazando
    la anterior con esa extensión; si no, media_<huella>.<ext> (el mismo contenido no se duplica)."""
    ext = Path(nombre).suffix.lower()
    if ext not in (EXT_IMAGEN if nombre_final else EXT_IMAGEN + EXT_CLIP):
        raise error('srv.propio_ext', nombre=nombre)
    dest = privada(raiz, slug)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        h, total = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=dest, prefix='.subiendo-', suffix=ext, delete=False) as out:
            tmp = Path(out.name)
            while bloque := archivo.read(1 << 20):
                total += len(bloque)
                if total > MAX_BYTES:
                    raise error('clip.tamano')
                h.update(bloque)
                out.write(bloque)
        if not total:
            raise error('clip.invalido')
        if ext in EXT_CLIP:
            info = {'tipo': 'clip', **validar_clip(tmp)}
        else:
            from PIL import Image
            try:
                with Image.open(tmp) as im:
                    if im.format not in ('JPEG', 'PNG'):
                        raise ValueError()
                    im.verify()
            except Exception:
                raise error('srv.imagen_invalida', nombre=nombre) from None
            info = {'tipo': 'imagen'}
        # El nombre original no participa: volver a subir el mismo contenido no lo duplica.
        if nombre_final:
            final = privada(raiz, slug, f'propios/{nombre_final}{ext}')
            if final.exists() and not final.is_file():
                raise error('clip.ruta_invalida')
            os.replace(tmp, final)
            return {'media': f'propios/{final.name}', **info}
        digest = h.hexdigest()[:24]
        existentes = [p for p in dest.glob(f'media_{digest}.*') if p.suffix.lower() in EXT_IMAGEN + EXT_CLIP]
        final = existentes[0] if existentes else dest / f'media_{digest}{ext}'
        final = privada(raiz, slug, f'propios/{final.name}')
        if final.exists() and not final.is_file():
            raise error('clip.ruta_invalida')
        if not final.exists():
            os.replace(tmp, final)
        return {'media': f'propios/{final.name}', **info}
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
