"""Dónde vive cada cosa.

- APP: el código y los assets del producto (logo, fuentes, tarjeta de cierre, capturas de Ayuda).
- DATOS: lo del usuario (ajustes, guiones, salida, personajes, propios, música, papelera, modelos).

Modo desarrollador (solo checkout local sin marca comercial): DATOS es la carpeta del repo, con la misma estructura de siempre
(guiones/, salida/, assets/musica/…, app/ajustes.json). Modo instalado: el lanzador (build/launcher.py)
define DCRYPTO_DATA = %LOCALAPPDATA%\\DcryptoStudio y todo lo del usuario va ahí, con la misma estructura
relativa (así una ruta guardada en un guion, como "assets/musica/tension/x.mp3", vale en los dos modos).
Sin el lanzador, un paquete comercial usa igualmente los datos de LocalAppData.
"""
import os
from pathlib import Path

APP = Path(__file__).resolve().parent.parent

LINUX = os.environ.get("COMPRESSO_LINUX") == "1"
_datos = os.environ.get("DCRYPTO_DATA", "").strip()
# Solo un checkout local sin marca comercial conserva el modo desarrollador.
# El paquete queda cerrado incluso al iniciar server.py sin el lanzador o sin DCRYPTO_DATA.
COMERCIAL = not LINUX and (bool(_datos) or (APP / ".comercial").is_file() or not (APP / ".git").exists())
INSTALADO = COMERCIAL
DATOS = (Path(os.environ.get("COMPRESSO_DATA_DIR") or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "compresso-studio").expanduser().resolve() if LINUX else
         Path(_datos) if _datos else
         Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "DcryptoStudio"
         if COMERCIAL else APP)

GUIONES = DATOS / "guiones"
SALIDA = DATOS / "salida"
PAPELERA = DATOS / "papelera"
MUSICA = DATOS / "assets" / "musica"
PERSONAJES = DATOS / "assets" / "personajes"
PROPIOS = DATOS / "assets" / "propios"
AJUSTES = DATOS / "ajustes.json" if INSTALADO or LINUX else APP / "app" / "ajustes.json"
MODELOS = DATOS / "modelos"  # caché de Hugging Face y de torch (solo en modo instalado)
LOGS = DATOS / "logs"
# Lo que baja el Setup (app/preparar.py), solo en modo instalado; en desarrollo, el venv y el ffmpeg del PATH:
LIBS = DATOS / "python"  # torch, diffusers, kokoro, faster-whisper… (pip --target): sobrevive a reinstalar la app
FFMPEG = DATOS / "ffmpeg"  # ffmpeg.exe y ffprobe.exe (gyan.dev, GPL): no van dentro del paquete
DESCARGAS = DATOS / "descargas"  # archivos a medias (.part) y paquetes antes de instalarse

MOODS = ("tension", "misterio", "epico", "triste", "neutro")

if INSTALADO:
    import site

    # antes de que se importe huggingface_hub o torch: los modelos van a los datos del usuario
    os.environ.setdefault("HF_HOME", str(MODELOS / "huggingface"))
    os.environ.setdefault("TORCH_HOME", str(MODELOS / "torch"))
    # ffmpeg descargado: el pipeline llama a "ffmpeg" por el PATH, también si se descarga con la app abierta
    os.environ["PATH"] = str(FFMPEG) + os.pathsep + os.environ.get("PATH", "")
    # las librerías de IA: después de las del runtime (addsitedir procesa sus .pth)
    LIBS.mkdir(parents=True, exist_ok=True)
    site.addsitedir(str(LIBS))


def crear_estructura() -> None:
    """La primera vez (y en cada arranque, por si se borró algo): las carpetas del usuario."""
    for d in (GUIONES, SALIDA, PERSONAJES, PROPIOS, *(MUSICA / m for m in MOODS)):
        d.mkdir(parents=True, exist_ok=True)
    if LINUX:
        for d in (MODELOS, LOGS):
            d.mkdir(parents=True, exist_ok=True)
    if INSTALADO:
        for d in (MODELOS, LOGS, LIBS, DESCARGAS):
            d.mkdir(parents=True, exist_ok=True)
