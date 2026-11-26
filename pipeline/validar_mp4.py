"""Inspección de metadatos, sin decodificar, publicar ni eliminar el MP4."""
import json
import math
from pathlib import Path
import subprocess


class ErrorValidacionMP4(ValueError):
    """El archivo no satisface las condiciones del render esperado."""


def _positivo(valor):
    if isinstance(valor, bool):
        raise ValueError("valor booleano")
    numero = float(valor)
    if not math.isfinite(numero) or numero <= 0:
        raise ValueError("valor no positivo o no finito")
    return numero


def validar_mp4(ruta: Path, tamano: tuple[int, int], duracion_esperada: float,
                *, tolerancia: float = 0.5, timeout: float = 15.0) -> dict:
    """Exige video y audio, dimensiones y duración de contenedor y pistas.

    Usar ensamblar.duracion_prevista con las voces y opciones del render.
    0,5 s cubren cuantización a 30 fps y paquetes AAC; es configurable.
    Devuelve los metadatos comprobados. Cualquier fallo conserva el archivo.
    """
    try:
        esperada = _positivo(duracion_esperada)
        espera = _positivo(timeout)
        if isinstance(tolerancia, bool) or not math.isfinite(tolerancia) or tolerancia < 0:
            raise ValueError("tolerancia inválida")
        if len(tamano) != 2 or any(type(n) is not int or n <= 0 for n in tamano):
            raise ValueError("dimensiones inválidas")
        ruta = Path(ruta)
        if not ruta.is_file() or ruta.stat().st_size <= 0:
            raise ValueError("archivo inexistente, no regular o vacío")
        resultado = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_type,width,height,duration:stream_disposition=attached_pic",
             "-of", "json", str(ruta.resolve())],
            capture_output=True, text=True, timeout=espera, check=False,
        )
        if resultado.returncode != 0:
            raise ValueError("ffprobe terminó con error")
        datos = json.loads(resultado.stdout)
        formato = datos["format"]
        if "mp4" not in formato["format_name"].split(","):
            raise ValueError("contenedor incompatible con MP4")
        pistas = datos["streams"]
        videos = [p for p in pistas if p.get("codec_type") == "video"
                  and not p.get("disposition", {}).get("attached_pic", 0)]
        audios = [p for p in pistas if p.get("codec_type") == "audio"]
        if not videos or not audios:
            raise ValueError("faltan pistas de video o audio")
        for video in videos:
            if (video.get("width"), video.get("height")) != tuple(tamano):
                raise ValueError("dimensiones inesperadas")
        # No aceptar una pista truncada aunque el contenedor indique el total correcto.
        for elemento in [formato, *videos, *audios]:
            duracion = _positivo(elemento["duration"])
            if abs(duracion - esperada) > tolerancia:
                raise ValueError("duración fuera de tolerancia")
        return datos
    except (OSError, subprocess.SubprocessError, ValueError, TypeError,
            KeyError, AttributeError, OverflowError) as exc:
        raise ErrorValidacionMP4(f"MP4 no válido: {exc}") from exc
