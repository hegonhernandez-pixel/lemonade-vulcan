"""Miniatura automática: imagen de una escena + degradado oscuro + título grande + logo del canal."""
from pathlib import Path

from . import imagenes
from .guion import Guion
from .marca import FUENTE, logo
from .textos import decir, error

TAMANO = {"short": (1080, 1920), "largo": (1280, 720)}
AMBAR = (242, 184, 75)  # #f2b84b
BLANCO = (255, 255, 255)
MAX_PALABRAS = 6


def palabras_titulo(texto: str) -> list[tuple[str, bool]]:
    """'el hombre que *desapareció*' -> [("EL", False), ..., ("DESAPARECIÓ", True)].
    Los asteriscos pueden abarcar varias palabras: '*la realidad* no existe'."""
    res, resaltar = [], False
    for tok in texto.split():
        if tok.startswith("*"):
            resaltar = True
        limpio = tok.strip("*")
        if limpio:
            res.append((limpio.upper(), resaltar))
        if tok.endswith("*") and (len(tok) > 1 or not tok.startswith("*")):
            resaltar = False
    return res


def _dos_lineas(palabras: list[tuple[str, bool]]) -> list[list[tuple[str, bool]]]:
    """Corta en dos líneas lo más parejas posible (por cantidad de letras)."""
    if len(palabras) < 2:
        return [palabras]
    largo = lambda ps: sum(len(p) for p, _ in ps) + len(ps) - 1
    k = min(range(1, len(palabras)), key=lambda k: max(largo(palabras[:k]), largo(palabras[k:])))
    return [palabras[:k], palabras[k:]]


def render(base: Path, texto: str, formato: str, salida: Path, con_logo: bool = True,
           logo_fuente: Path | None = None) -> Path:
    """con_logo: el logo arriba a la derecha, la misma decisión que la marca de agua del video.
    logo_fuente: el logo de la marca de agua (Ajustes); None = el del canal."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    W, H = TAMANO[formato]
    vertical = formato == "short"
    img = ImageOps.fit(Image.open(base).convert("RGB"), (W, H), Image.LANCZOS, centering=(0.5, 0.4))

    # degradado oscuro en la parte baja, para que el texto se lea sobre cualquier imagen
    desde = int(H * (0.42 if vertical else 0.30))
    mascara = Image.new("L", (1, H), 0)
    for y in range(desde, H):
        mascara.putpixel((0, y), round(235 * ((y - desde) / (H - desde)) ** 1.3))
    img.paste(Image.new("RGB", (W, H), (0, 0, 0)), mask=mascara.resize((W, H)))

    margen = round(W * (0.06 if vertical else 0.045))
    lineas = _dos_lineas(palabras_titulo(texto))
    if lineas[0]:
        d = ImageDraw.Draw(img)
        # el tamaño más grande con el que la línea más ancha cabe entre márgenes
        tam = round(W * 0.15) if vertical else round(H * 0.19)
        while True:
            fuente = ImageFont.truetype(str(FUENTE), tam)
            ancho = lambda ps: fuente.getlength(" ".join(p for p, _ in ps))
            if max(ancho(l) for l in lineas) <= W - 2 * margen or tam <= 30:
                break
            tam -= 4
        borde = max(3, round(tam * 0.085))
        alto_linea = round(tam * 1.12)
        fondo = round(H * 0.83) if vertical else H - round(margen * 1.2)
        y = fondo - alto_linea * len(lineas)
        espacio = fuente.getlength(" ")
        for linea in lineas:
            x = (W - ancho(linea)) / 2 if vertical else margen
            for palabra, resaltada in linea:
                d.text((x, y), palabra, font=fuente, fill=AMBAR if resaltada else BLANCO,
                       stroke_width=borde, stroke_fill=(0, 0, 0))
                x += fuente.getlength(palabra) + espacio
            y += alto_linea

    if con_logo:
        marca = logo(round(H * 0.07) if vertical else round(H * 0.13), fuente=logo_fuente)
        m = round(W * 0.045)
        img.paste(marca, (W - marca.width - m, m), marca)
    img.save(salida, quality=92)
    return salida


def texto_de(g: Guion) -> str:
    if g.titulo_miniatura.strip():
        return g.titulo_miniatura
    # sin título de miniatura: las primeras palabras del título, para no dejarla vacía
    return " ".join(g.titulo.split()[:MAX_PALABRAS])


def generar(g: Guion, carpeta: Path, demo: bool = False, calidad: str = "rapida", modelo: str | None = None,
            pasos: int | None = None, prueba: bool = False, con_logo: bool = True,
            logo_fuente: Path | None = None) -> Path:
    """salida/<slug>/miniatura.jpg. Si la imagen de la escena base no existe aún, la genera.
    con_logo / logo_fuente: como la marca de agua del video (sin marca de agua, sin logo).
    prueba: modo prueba de la licencia: 'Dcrypto Studio TRIAL' en el centro, como en el video."""
    if not g.escenas:
        raise error("err.miniatura_sin_escenas")
    n = min(max(1, g.miniatura_escena), len(g.escenas))
    e = g.escenas[n - 1]
    if e.media:  # imagen propia, o un cuadro del clip propio
        base = imagenes.imagen_base(g, e, carpeta)
    elif not e.prompt:
        raise error("err.miniatura_sin_prompt", n=n)
    else:
        base, _ = imagenes.generar_escena(g, e, carpeta, demo, calidad, modelo, pasos)
    texto = texto_de(g)
    if len(palabras_titulo(texto)) > MAX_PALABRAS:
        decir("log.miniatura_larga", max=MAX_PALABRAS)
    salida = render(base, texto, g.formato, carpeta / "miniatura.jpg", con_logo, logo_fuente)
    if prueba:
        from PIL import Image
        from . import marca
        img = Image.open(salida).convert("RGBA")
        img.alpha_composite(Image.open(marca.marca_prueba(img.size)))
        img.convert("RGB").save(salida, quality=92)
    decir("log.miniatura", n=n, texto=texto, ruta=salida)
    return salida
