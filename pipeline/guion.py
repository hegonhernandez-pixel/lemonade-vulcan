"""Carga y valida un guion JSON."""
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .textos import Error, Texto, Unidos, error

FORMATOS = {
    # ancho, alto
    "short": (1080, 1920),
    "largo": (1920, 1080),
}
IDIOMAS = {"es": "Español", "en": "English"}
# motor de voz: "edge" (Microsoft edge-tts, en línea) o "kokoro" (local, sin internet; ver pipeline/voz_local.py)
MOTORES = ("edge", "kokoro")
VOCES_DEFECTO = {
    "edge": {"es": "es-CO-GonzaloNeural", "en": "en-US-AndrewNeural"},
    "kokoro": {"es": "em_alex", "en": "am_michael"},
}
VOZ_DEFECTO = VOCES_DEFECTO["edge"]


def motor_de(voz: str) -> str:
    """El motor al que pertenece una voz por su nombre: las de Kokoro son "af_heart", "em_alex"."""
    import re
    return "kokoro" if re.fullmatch(r"[a-z]{2}_[a-z]+", voz or "") else "edge"
# lo que dice la voz al final de los Shorts, por idioma
EXT_IMAGEN = (".jpg", ".jpeg", ".png")
EXT_CLIP = (".mp4", ".mov")
PAUSA_MAX = 10.0
CIERRES = ("global", "ninguno")


def cierre_valido(c) -> bool:
    """'global', 'ninguno' o una imagen propia ("propios/cierre.png"); sin el campo = 'global'."""
    return c is None or c in CIERRES or (media_valida(c) and c.lower().endswith(EXT_IMAGEN))


@dataclass
class Escena:
    idx: int
    texto: str
    prompt: str
    semilla: int = 0  # se sube para pedir otra versión de la imagen con el mismo prompt
    calidad: str | None = None  # "alta" fuerza esa calidad solo en esta imagen; None = la de Ajustes
    media: str | None = None  # imagen o clip propio ("propios/x.mp4" = assets/propios/<slug>/x.mp4); reemplaza a la generada
    pausa: float = 0.0  # segundos de silencio después de la voz (para dejar respirar una frase)
    inicio_clip: float = 0.0  # segundo de entrada del video propio; no cambia la voz

    @property
    def es_clip(self) -> bool:
        return bool(self.media) and self.media.lower().endswith(EXT_CLIP)


@dataclass
class Guion:
    slug: str
    titulo: str
    formato: str
    voz: str
    estilo_visual: str
    escenas: list[Escena] = field(default_factory=list)
    musica: str | None = None  # ruta a un mp3, o un mood de assets/musica/ ("tension")
    volumen_musica: float = 0.15
    titulo_miniatura: str = ""  # máx. 6 palabras; *palabra* = resaltada en ámbar
    miniatura_escena: int = 1  # escena cuya imagen es la base de la miniatura (1-based)
    subtitulos: dict = field(default_factory=dict)  # estilo de subtítulos solo para este video
    marca: dict = field(default_factory=dict)  # tamaño, esquina y margen de la marca de agua solo para este video
    con_marca: bool = True  # "marca": false en el JSON = este video sin marca de agua (ni logo en la miniatura)
    idioma: str = "es"  # "es" o "en": idioma de la narración
    portada: bool | None = None  # miniatura como primer cuadro del video; None = lo que diga Ajustes
    motor_voz: str = "edge"  # se guarda en el guion: cambiar el ajuste global no cambia la voz de un video
    # tarjeta de cierre: "global" (lo que diga Ajustes), "ninguno" o "propios/cierre.png" (imagen subida)
    cierre: str = "global"

    @property
    def tamano(self) -> tuple[int, int]:
        return FORMATOS[self.formato]

    @property
    def vertical(self) -> bool:
        return self.formato == "short"


def cargar(ruta: str | Path, estricto: bool = True) -> Guion:
    ruta = Path(ruta)
    return desde_dict(json.loads(ruta.read_text(encoding="utf-8")), estricto)


def desde_texto(texto: str) -> dict:
    """El JSON de un guion copiado de un chat: acepta ```json ... ``` y texto antes o después."""
    t = texto.strip()
    if "```" in t:
        bloques = t.split("```")
        # el bloque de código más largo que parezca JSON
        candidatos = [b.removeprefix("json").removeprefix("JSON").strip() for b in bloques[1::2]]
        t = max((c for c in candidatos if c.startswith("{")), key=len, default=t)
    ini, fin = t.find("{"), t.rfind("}")
    if ini < 0 or fin < ini:
        raise error("err.sin_json")
    t = t[ini:fin + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        linea = t.splitlines()[e.lineno - 1] if e.lineno - 1 < len(t.splitlines()) else ""
        raise error("err.json_mal", linea=e.lineno, columna=e.colno, error=e.msg, texto=linea.strip()[:120])


def validar(data) -> list[Texto]:
    """Todos los errores que impiden usar el guion (no solo el primero), diciendo escena y campo.
    Lista vacía = válido. No mira el slug: la app lo genera del título."""
    if not isinstance(data, dict):
        return [Texto("err.no_objeto")]
    errores = []
    if not isinstance(data.get("titulo"), str) or not data["titulo"].strip():
        errores.append(Texto("err.falta_titulo"))
    if data.get("formato", "short") not in FORMATOS:
        errores.append(Texto("err.formato", valor=repr(data["formato"])))
    if not musica_valida(data.get("musica")):
        errores.append(Texto("err.musica", tipo=type(data["musica"]).__name__))
    if "volumen_musica" in data and not isinstance(data["volumen_musica"], (int, float)):
        errores.append(Texto("err.volumen"))
    if data.get("idioma", "es") not in IDIOMAS:
        errores.append(Texto("err.idioma", valor=repr(data["idioma"])))
    if data.get("portada") is not None and not isinstance(data["portada"], bool):
        errores.append(Texto("err.portada"))
    if not cierre_valido(data.get("cierre")):
        errores.append(Texto("err.cierre", valor=repr(data["cierre"])))
    escenas = data.get("escenas")
    if not isinstance(escenas, list) or not escenas:
        errores.append(Texto("err.falta_escenas"))
        return errores
    for i, e in enumerate(escenas, start=1):
        if not isinstance(e, dict):
            errores.append(Texto("err.escena_objeto", n=i))
            continue
        # con imagen o clip propio el prompt sobra
        for campo in ("texto",) if e.get("media") else ("texto", "prompt"):
            v = e.get(campo)
            if not isinstance(v, str) or not v.strip():
                errores.append(Texto("err.escena_falta" if v in (None, "") else "err.escena_no_texto", n=i, campo=campo))
        errores += _errores_extra(i, e)
    return errores


def _errores_extra(i: int, e: dict) -> list[Texto]:
    """'media' y 'pausa' de una escena (opcionales)."""
    res = []
    m = e.get("media")
    if m is not None and not media_valida(m):
        res.append(Texto("err.media", n=i, valor=repr(m)))
    p = e.get("pausa")
    if p is not None and (isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= PAUSA_MAX):
        res.append(Texto("err.pausa", n=i, max=f"{PAUSA_MAX:g}"))
    inicio = e.get('inicio_clip', 0)
    if (isinstance(inicio, bool) or not isinstance(inicio, (int, float))
            or not math.isfinite(inicio) or inicio < 0
            or (inicio and not (isinstance(m, str) and m.lower().endswith(EXT_CLIP)))):
        res.append(Texto('clip.inicio_invalido'))
    return res


def musica_valida(m) -> bool:
    """'musica' es un mood o una ruta (texto), o no está. Un objeto o una lista no se pueden usar."""
    return m is None or isinstance(m, str)


def media_valida(m) -> bool:
    """Solo "propios/<archivo>" (assets/propios/<slug>/<archivo>): nada de rutas absolutas ni '..'."""
    if not isinstance(m, str):
        return False
    partes = m.replace("\\", "/").split("/")
    return (len(partes) == 2 and partes[0] == "propios" and partes[1] not in ("", ".", "..")
            and not re.search(r'[\x00-\x1f<>:"|?*]', partes[1])
            and m.lower().endswith(EXT_IMAGEN + EXT_CLIP))


# palabras (mín, máx) y escenas (mín, máx) por formato
LIMITES = {"short": (100, 140, 6, 9), "largo": (1200, 1800, 25, 40)}
MAX_PALABRAS_ESCENA = 40


def avisos(data: dict) -> list[Texto]:
    """Lo que no impide guardar el guion pero conviene revisar. Supone un guion ya validado."""
    formato = data.get("formato", "short")
    pmin, pmax, emin, emax = LIMITES[formato]
    escenas = data["escenas"]
    total = sum(len(e["texto"].split()) for e in escenas)
    res, fmt = [], Texto(f"fmt.{formato}")
    if not pmin <= total <= pmax:
        res.append(Texto("aviso.palabras", total=total, formato=fmt, min=pmin, max=pmax))
    if not emin <= len(escenas) <= emax:
        res.append(Texto("aviso.escenas", total=len(escenas), formato=fmt, min=emin, max=emax))
    for i, e in enumerate(escenas, start=1):
        n = len(e["texto"].split())
        if n > MAX_PALABRAS_ESCENA:
            res.append(Texto("aviso.escena_larga", n=i, total=n, max=MAX_PALABRAS_ESCENA))
        if "// VERIFICAR" in e["texto"]:
            res.append(Texto("aviso.verificar", n=i))
    tm = (data.get("titulo_miniatura") or "").replace("*", " ").split()
    if not tm:
        res.append(Texto("aviso.falta_miniatura"))
    elif len(tm) > 6:
        res.append(Texto("aviso.miniatura_larga", total=len(tm)))
    return res


def desde_dict(data: dict, estricto: bool = True) -> Guion:
    """estricto=False acepta guiones a medio escribir (escenas vacías, // VERIFICAR): lo usa el editor web."""
    for k in ("slug", "titulo", "escenas"):
        if k not in data:
            raise error("err.campo_obligatorio", campo=k)
    formato = data.get("formato", "short")
    if formato not in FORMATOS:
        raise error("err.formato_corto", opciones=list(FORMATOS), valor=formato)
    if data.get("idioma", "es") not in IDIOMAS:
        raise error("err.idioma_corto", opciones=list(IDIOMAS), valor=data["idioma"])
    if not cierre_valido(data.get("cierre")):
        raise error("err.cierre", valor=repr(data["cierre"]))
    if not musica_valida(data.get("musica")):  # también en el editor: si no, un objeto llega a Path() y da 500
        raise error("err.musica", tipo=type(data["musica"]).__name__)
    if estricto:
        errores = validar(data) + [
            Texto("err.verificar_producir", n=i)
            for i, e in enumerate(data["escenas"], start=1) if "// VERIFICAR" in (e.get("texto") or "")]
        if errores:
            raise Error(Unidos(errores))

    escenas = []
    for i, e in enumerate(data["escenas"], start=1):
        texto = (e.get("texto") or "").strip()
        prompt = (e.get("prompt") or "").strip()
        extra = _errores_extra(i, e)
        if extra:
            raise Error(Unidos(extra))
        escenas.append(Escena(idx=i, texto=texto, prompt=prompt, semilla=int(e.get("semilla", 0)),
                              calidad=e.get("calidad") or None, media=e.get("media") or None,
                              pausa=round(float(e.get("pausa") or 0), 2),
                              inicio_clip=round(float(e.get('inicio_clip') or 0), 3)))

    return Guion(
        slug=data["slug"],
        titulo=data["titulo"],
        formato=formato,
        voz=data.get("voz", "es-CO-GonzaloNeural"),
        estilo_visual=data.get("estilo_visual", "cinematic, dramatic lighting, photorealistic, 35mm film"),
        escenas=escenas,
        musica=data.get("musica") or None,
        volumen_musica=float(data.get("volumen_musica", 0.15)),
        titulo_miniatura=(data.get("titulo_miniatura") or "").strip(),
        miniatura_escena=int(data.get("miniatura_escena") or 1),
        subtitulos=data["subtitulos"] if isinstance(data.get("subtitulos"), dict) else {},
        marca=data["marca"] if isinstance(data.get("marca"), dict) else {},
        con_marca=data.get("marca") is not False,
        idioma=data.get("idioma") or "es",
        portada=data["portada"] if isinstance(data.get("portada"), bool) else None,
        motor_voz=data["motor_voz"] if data.get("motor_voz") in MOTORES else motor_de(data.get("voz", "")),
        cierre=data.get("cierre") or "global",
    )


def palabras(g: Guion) -> int:
    return sum(len(e.texto.split()) for e in g.escenas)
