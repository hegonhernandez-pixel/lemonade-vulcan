"""Orquestación completa: voz -> imágenes -> ensamblaje. La usan make_video.py y la app web."""
from dataclasses import dataclass, replace
from pathlib import Path

from . import ensamblar, imagenes, marca, miniatura, subtitulos, voz, voz_local
from .guion import Guion
from .textos import Texto, decir, error
from . import rutas

RAIZ = Path(__file__).parent.parent


@dataclass
class Opciones:
    demo: bool = False
    rate: str = "+0%"
    calidad: str = "rapida"  # de las imágenes: "rapida" (SDXL-Turbo) o "alta" (SDXL base); ver imagenes.CALIDADES
    modelo: str | None = None  # None = el de la calidad
    pasos: int | None = None
    subtitulos: bool = True  # quemarlos en el video
    estilo_subtitulos: dict | None = None  # ajustes globales; el campo "subtitulos" del guion manda sobre esto
    solo: str | None = None  # "voz", "imagenes" o "miniatura"
    marca_agua: bool = True
    estilo_marca: dict | None = None  # tamaño, esquina y margen (Ajustes); el campo "marca" del guion manda
    limite: float | None = None  # segundos: render parcial a prueba_mezcla.mp4 (solo las primeras escenas)
    portada: bool = False  # miniatura como primer cuadro (Ajustes); el campo "portada" del guion manda
    marca_prueba: bool = False  # modo prueba de la licencia (app/licencia.py): 'Dcrypto Studio TRIAL' en el centro
    logo_marca: Path | None = None  # solo la app proporciona el logo personalizado
    cierre: bool = True  # tarjeta de cierre al final (Ajustes); el campo "cierre" del guion manda
    cierre_nombre: str = marca.CIERRE_TEXTO  # el nombre bajo el logo en la tarjeta (Ajustes)


def con_portada(g: Guion, op: Opciones) -> bool:
    """La del guion si la fija; si no, la de Ajustes. Nunca en la prueba de mezcla."""
    return (g.portada if g.portada is not None else op.portada) and not op.limite


def tarjeta_cierre(g: Guion, op: Opciones, destino: Path) -> Path | None:
    """La tarjeta de cierre de este video, o None si va sin ella.
    "ninguno" = sin cierre; "propios/x.png" = la imagen subida; "global" (o sin el campo) = lo que diga
    Ajustes: la tarjeta con el logo y el nombre, o nada. Nunca en la prueba de mezcla."""
    if op.limite or g.cierre == "ninguno":
        return None
    if g.cierre != "global":
        propia = imagenes.ruta_propia(g.slug, g.cierre)
        if not propia.is_file():
            raise error("err.cierre_falta", ruta=g.cierre)
        return marca.tarjeta_propia(propia, g.tamano, destino)
    return marca.tarjeta_cierre(g.tamano, op.cierre_nombre) if op.cierre else None


def con_marca(g: Guion, op: Opciones) -> bool:
    """Marca de agua en este video: la de Ajustes, salvo que el guion diga "marca": false (Sin marca)."""
    return op.marca_agua and g.con_marca


def logo_miniatura(g: Guion, op: Opciones) -> dict:
    """La miniatura lleva logo solo si el video lleva marca de agua, y el mismo logo."""
    si = con_marca(g, op)
    return {"con_logo": si, "logo_fuente": op.logo_marca if si else None}


def carpeta(g: Guion) -> Path:
    return rutas.SALIDA / g.slug


def pasos(g: Guion, op: Opciones | None = None) -> list[tuple[str, Texto]]:
    """(id, nombre) de cada paso, en el orden en que avisa producir()."""
    final = [("ensamblar", Texto("paso.ensamblar")), ("miniatura", Texto("paso.miniatura"))]
    if op and con_portada(g, op):
        final.reverse()  # la miniatura hace falta antes: es el primer cuadro
    return (
        [(f"voz-{e.idx}", Texto("paso.voz", n=e.idx)) for e in g.escenas]
        + [(f"img-{e.idx}", Texto("paso.imagen", n=e.idx)) for e in g.escenas]
        + final
    )


def producir(g: Guion, op: Opciones, avisar=None, *, destino_mp4: Path | None = None,
             al_renderizar=None) -> Path | None:
    """Corre el pipeline. avisar(paso, estado): corriendo | ok | cache.

    destino_mp4 permite renderizar en privado sin publicar ni validar automáticamente.
    Si se omite, conserva la salida normal y la salida de prueba de mezcla.
    al_renderizar(duracion, tamano) informa el objetivo real antes del ensamblaje.
    """
    destino = carpeta(g)
    destino.mkdir(parents=True, exist_ok=True)
    if op.solo == "miniatura":
        return miniatura.generar(g, destino, op.demo, op.calidad, op.modelo, op.pasos, prueba=op.marca_prueba,
                                 **logo_miniatura(g, op))
    if op.limite:
        g = _primeras(g, destino, op)
        decir("log.prueba", sangria=0, s=f"{op.limite:.0f}", n=len(g.escenas))

    portada = con_portada(g, op)
    print()
    decir("log.fase_voz", sangria=0)
    voces = voz.generar(g, destino, demo=op.demo, rate=op.rate, avisar=avisar)
    voz_local.liberar()  # si hubo Kokoro: suelta su VRAM antes de las imágenes
    ass = subtitulos.escribir(g, voces, destino, subtitulos.Estilo.desde(op.estilo_subtitulos, g.subtitulos),
                              offset=ensamblar.PORTADA_SEG if portada else 0.0)
    total = sum(v["duracion"] + e.pausa for e, v in zip(g.escenas, voces))
    decir("log.duracion_total", s=f"{total:.1f}", ass=ass)
    if op.solo == "voz":
        return None

    print()
    decir("log.fase_imagenes", sangria=0)
    imgs = imagenes.generar(g, destino, demo=op.demo, calidad=op.calidad, modelo=op.modelo, pasos=op.pasos,
                            avisar=avisar)
    if op.solo == "imagenes":
        return None

    def hacer_miniatura():
        if avisar:
            avisar("miniatura", "corriendo")
        ruta = miniatura.generar(g, destino, op.demo, op.calidad, op.modelo, op.pasos, prueba=op.marca_prueba,
                                 **logo_miniatura(g, op))
        if avisar:
            avisar("miniatura", "ok")
        return ruta

    print()
    decir("log.fase_ensamblaje", sangria=0)
    mini = hacer_miniatura() if portada else None  # la portada es la miniatura: va primero
    if avisar:
        avisar("ensamblar", "corriendo")
    tarjeta = tarjeta_cierre(g, op, destino)
    if al_renderizar:
        al_renderizar(ensamblar.duracion_prevista(g, voces, op.limite, bool(tarjeta), bool(mini)), g.tamano)
    final = ensamblar.ensamblar(g, destino, imgs, voces, ass, subtitulos=op.subtitulos,
                                destino=(destino_mp4 if destino_mp4 is not None else
                                         destino / "prueba_mezcla.mp4" if op.limite else None),
                                limite=op.limite, marca_agua=con_marca(g, op), cierre=bool(tarjeta), tarjeta=tarjeta, portada=mini,
                                estilo_marca=marca.EstiloMarca.desde(op.estilo_marca, g.marca), prueba=op.marca_prueba,
                                logo_marca=op.logo_marca)
    if avisar:
        avisar("ensamblar", "ok")
    if not op.limite and not portada:
        hacer_miniatura()
    return final


def _primeras(g: Guion, destino: Path, op: Opciones) -> Guion:
    """El guion recortado a las escenas que cubren los primeros op.limite segundos de voz."""
    acumulado, n = 0.0, 0
    for e in g.escenas:
        info, _ = voz.generar_escena(g, e, destino, op.demo, op.rate)
        acumulado, n = acumulado + info["duracion"] + e.pausa, n + 1
        if acumulado >= op.limite:
            break
    return replace(g, escenas=g.escenas[:n])
