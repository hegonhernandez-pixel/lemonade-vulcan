"""Identidad del canal: logo (miniaturas, marca de agua) y tarjeta de cierre."""
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

from .textos import error

RAIZ = Path(__file__).parent.parent
LOGO = RAIZ / "assets" / "logo" / "logo_dcrypto_transparente_1024.png"
FUENTE = RAIZ / "assets" / "fuentes" / "DejaVuSans-Bold.ttf"


def logo(alto: int, opacidad: float = 1.0, fuente: Path | bytes | None = None):
    """El logo recortado a su contenido (el png trae mucho margen transparente) y escalado a 'alto' px."""
    from PIL import Image

    if fuente is None and not LOGO.exists():
        raise error("err.logo", ruta=LOGO.relative_to(RAIZ))
    im = Image.open(BytesIO(fuente) if isinstance(fuente, bytes) else fuente or LOGO).convert("RGBA")
    im = im.crop(im.getchannel("A").getbbox())
    im = im.resize((max(1, round(im.width * alto / im.height)), alto), Image.LANCZOS)
    if opacidad < 1:
        a = im.getchannel("A").point(lambda v: round(v * opacidad))
        im.putalpha(a)
    return im


CIERRE_DIR = RAIZ / "assets" / "cierre"
CIERRE_SEG = 2.5
CIERRE_FONDO = (10, 13, 19)  # #0a0d13
CIERRE_TEXTO = "Dcrypto Historias"
MARCA_OPACIDAD = 0.55


def _cacheado(nombre: str, crear) -> Path:
    """PNG en assets/cierre/. Se rehace solo si falta o si el logo es más nuevo."""
    CIERRE_DIR.mkdir(parents=True, exist_ok=True)
    ruta = CIERRE_DIR / nombre
    if not ruta.exists() or ruta.stat().st_mtime < LOGO.stat().st_mtime:
        crear().save(ruta)
    return ruta


# Marca de agua: alto del logo y margen en px de un Short (1080x1920); en 16:9 escalan con el alto del video.
MARCA_TAMANOS = {"pequeno": 70, "medio": 90, "grande": 120}
MARCA_POSICIONES = ("arriba_der", "arriba_izq", "abajo_der", "abajo_izq")
MARGEN_MIN, MARGEN_MAX = 40, 200


@dataclass
class EstiloMarca:
    """Tamaño, esquina y margen de la marca de agua. Ajustes globales ("estilo_marca") y, encima, el campo
    "marca" del guion. Margen 140 por defecto: YouTube e Instagram tapan las esquinas con sus botones."""
    tamano: str = "medio"
    posicion: str = "arriba_der"
    margen: int = 140

    @classmethod
    def desde(cls, *capas: dict | None) -> "EstiloMarca":
        """Combina capas (ajustes globales, luego el guion); ignora claves o valores inválidos."""
        e = cls()
        for capa in capas:
            for k, v in (capa or {}).items():
                if k == "tamano" and v in MARCA_TAMANOS or k == "posicion" and v in MARCA_POSICIONES:
                    setattr(e, k, v)
                elif k == "margen" and isinstance(v, (int, float)) and not isinstance(v, bool)                         and MARGEN_MIN <= v <= MARGEN_MAX:
                    e.margen = round(v)
        return e

    def a_dict(self) -> dict:
        return asdict(self)

    def overlay(self, alto_video: int) -> str:
        """Posición x:y para el filtro overlay de ffmpeg (W/H = video, w/h = logo)."""
        m = round(alto_video * self.margen / 1920)
        x = f"W-w-{m}" if self.posicion.endswith("der") else str(m)
        y = str(m) if self.posicion.startswith("arriba") else f"H-h-{m}"
        return f"{x}:{y}"


def catalogo_marca() -> dict:
    """Opciones para la interfaz."""
    return {"tamanos": [{"id": k, "px": v} for k, v in MARCA_TAMANOS.items()], "posiciones": list(MARCA_POSICIONES),
            "margen": [MARGEN_MIN, MARGEN_MAX], "defecto": EstiloMarca().a_dict()}


def marca_agua(alto_video: int, tamano: str = "medio", fuente: Path | None = None) -> Path:
    """Logo al 55 % para la esquina del video: 70 / 90 / 120 px de alto en un Short (1920)."""
    alto = round(alto_video * MARCA_TAMANOS.get(tamano, MARCA_TAMANOS["medio"]) / 1920)
    if fuente is not None:
        import hashlib
        import os
        import tempfile

        # Cache privada por contenido; no altera los recursos de marca del programa.
        # Una sola lectura: una subida posterior no cambia los píxeles asociados a esta huella.
        contenido = fuente.read_bytes()
        huella = hashlib.sha256(contenido).hexdigest()
        ruta = fuente.parent / f"marca_{huella}_{alto}.png"
        if not ruta.exists():
            fd, tmp = tempfile.mkstemp(dir=fuente.parent, suffix=".png")
            os.close(fd)
            try:
                logo(alto, MARCA_OPACIDAD, contenido).save(tmp)
                os.replace(tmp, ruta)
            finally:
                Path(tmp).unlink(missing_ok=True)
        return ruta
    return _cacheado(f"marca_agua_{alto}.png", lambda: logo(alto, MARCA_OPACIDAD))


def marca_prueba(size: tuple[int, int]) -> Path:
    """'Dcrypto Studio TRIAL' grande en el centro, semitransparente (modo prueba de la licencia, app/licencia.py).
    PNG del tamaño del video, transparente salvo el texto."""
    def crear():
        from PIL import Image, ImageDraw, ImageFont

        w, h = size
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        lineas = [("Dcrypto Studio", 0.075), ("TRIAL", 0.16)]  # alto de letra relativo al ancho
        fuentes = [ImageFont.truetype(str(FUENTE), round(w * t)) for _, t in lineas]
        cajas = [f.getbbox(txt) for (txt, _), f in zip(lineas, fuentes)]
        alto = sum(c[3] - c[1] for c in cajas) + round(w * 0.02)
        y = (h - alto) // 2
        for (txt, _), f, c in zip(lineas, fuentes, cajas):
            x = (w - (c[2] - c[0])) // 2 - c[0]
            d.text((x, y - c[1]), txt, font=f, fill=(255, 255, 255, 120),
                   stroke_width=max(2, round(w * 0.004)), stroke_fill=(0, 0, 0, 110))
            y += c[3] - c[1] + round(w * 0.02)
        return img

    return _cacheado(f"prueba_{size[0]}x{size[1]}.png", crear)


CIERRE_NOMBRE_MAX = 60


def tarjeta_cierre(size: tuple[int, int], texto: str = CIERRE_TEXTO) -> Path:
    """Fondo #0a0d13, logo centrado y 'texto' debajo (Ajustes → Nombre en la tarjeta), del tamaño del video.
    Texto vacío = solo el logo."""
    texto = (texto or "").strip()[:CIERRE_NOMBRE_MAX]

    def crear():
        from PIL import Image, ImageDraw, ImageFont

        w, h = size
        img = Image.new("RGB", size, CIERRE_FONDO)
        vertical = h > w
        lg = logo(round(h * (0.16 if vertical else 0.30)))
        alto_letra = round(h * (0.032 if vertical else 0.062))
        fuente = ImageFont.truetype(str(FUENTE), alto_letra)
        # un nombre largo se achica para caber en el 90 % del ancho
        while texto and fuente.getlength(texto) > w * 0.9 and alto_letra > 12:
            alto_letra -= 2
            fuente = ImageFont.truetype(str(FUENTE), alto_letra)
        caja = fuente.getbbox(texto) if texto else (0, 0, 0, 0)
        alto_texto = caja[3] - caja[1]
        espacio = round(lg.height * 0.22) if texto else 0
        y = (h - (lg.height + espacio + alto_texto)) // 2
        img.paste(lg, ((w - lg.width) // 2, y), lg)
        if texto:
            d = ImageDraw.Draw(img)
            d.text(((w - fuente.getlength(texto)) / 2, y + lg.height + espacio - caja[1]),
                   texto, font=fuente, fill=(231, 233, 238))
        return img

    if texto == CIERRE_TEXTO:  # el mismo archivo de siempre
        return _cacheado(f"cierre_{size[0]}x{size[1]}.png", crear)
    import hashlib
    huella = hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12]
    return _cacheado(f"cierre_{size[0]}x{size[1]}_{huella}.png", crear)


def tarjeta_propia(fuente: Path, size: tuple[int, int], carpeta: Path) -> Path:
    """La imagen que subió el usuario como tarjeta de cierre: entera (sin recortar), centrada sobre el fondo
    #0a0d13 y del tamaño del video. Se guarda en 'carpeta' (la salida del video) con la huella del contenido:
    subir otra imagen con el mismo nombre la rehace."""
    import hashlib
    import os
    import tempfile

    from PIL import Image, ImageOps

    contenido = fuente.read_bytes()
    huella = hashlib.sha256(contenido).hexdigest()[:16]
    ruta = carpeta / f"cierre_propio_{huella}_{size[0]}x{size[1]}.png"
    if ruta.exists():
        return ruta
    carpeta.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(contenido)) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
        im = ImageOps.contain(im, size, Image.LANCZOS)
    img = Image.new("RGB", size, CIERRE_FONDO)
    img.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2), im)
    fd, tmp = tempfile.mkstemp(dir=carpeta, suffix=".png")
    os.close(fd)
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, ruta)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return ruta
