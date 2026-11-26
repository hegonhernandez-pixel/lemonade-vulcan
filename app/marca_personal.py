"""Logo del usuario: validación y escritura atómica, sin modificar ajustes."""
import io
import os
from pathlib import Path
import tempfile

LIMITE = 5 * 1024 * 1024


def guardar(entrada, destino: Path) -> None:
    from PIL import Image, UnidentifiedImageError

    contenido = entrada.read(LIMITE + 1)
    if not contenido or len(contenido) > LIMITE:
        raise ValueError("marca.archivo_invalido")
    try:
        with Image.open(io.BytesIO(contenido)) as original:
            if original.format not in ("PNG", "WEBP") or max(original.size) > 4096:
                raise ValueError("marca.archivo_invalido")
            imagen = original.convert("RGBA")
            if not imagen.getchannel("A").getbbox():
                raise ValueError("marca.archivo_invalido")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("marca.archivo_invalido") from None
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destino.parent, suffix=".png")
    os.close(fd)
    try:
        imagen.save(tmp, format="PNG")
        os.replace(tmp, destino)
    finally:
        Path(tmp).unlink(missing_ok=True)
