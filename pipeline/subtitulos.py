"""Subtítulos: grupos de palabras que respetan la puntuación, estilo configurable y ASS (con karaoke opcional).

Los tamaños van en unidades ASS sobre el lienzo 384x288 que ffmpeg usa por defecto al convertir SRT:
así el estilo por defecto reproduce píxel a píxel el de antes (SRT + force_style). En un Short,
1 unidad = 1920/288 = 6.67 px.
"""
import math
import os
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .guion import Guion
from .textos import ErrorEjecucion, Texto, error
from . import rutas

RAIZ = Path(__file__).parent.parent
FUENTES_DIR = RAIZ / "assets" / "fuentes"

# id: (nombre visible, archivo, familia interna, negrita). Bebas, Anton y Montserrat ExtraBold traen
# un solo peso: pedirles negrita haría que libass las engrose a la fuerza.
FUENTES = {
    "dejavu": ("DejaVu Sans Bold", "DejaVuSans-Bold.ttf", "DejaVu Sans", True),
    "montserrat": ("Montserrat ExtraBold", "Montserrat-ExtraBold.ttf", "Montserrat ExtraBold", False),
    "bebas": ("Bebas Neue", "BebasNeue-Regular.ttf", "Bebas Neue", False),
    "anton": ("Anton", "Anton-Regular.ttf", "Anton", False),
    "poppins": ("Poppins Bold", "Poppins-Bold.ttf", "Poppins", True),
    "oswald": ("Oswald Bold", "Oswald-Bold.ttf", "Oswald", True),
}
# unidades ASS en un Short; los largos usan 0.7x (14 era el tamaño histórico en 16:9)
TAMANOS = {"pequeno": 16, "medio": 20, "grande": 24, "muy_grande": 30}
ESCALA_LARGO = 0.7
# (alineación ASS, MarginV en Short, MarginV en largo). 2 = abajo centrado, 5 = centro.
POSICIONES = {"abajo": (2, 40, 20), "centro_bajo": (2, 70, 40), "centro": (5, 0, 0)}
PX_POR_UNIDAD = 1920 / 288
AMBAR = "#f2b84b"

# fin de frase: corta el grupo después de la palabra; apertura: corta antes
CIERRA = set(".,;:?!…)]}»”\"'")
ABRE = set("¿¡([{«“\"'—–")
# el punto de una abreviatura no termina la frase: "Dr. Bruce Moseley" va junto
ABREVIATURAS = {"dr.", "dra.", "sr.", "sra.", "srta.", "mr.", "mrs.", "ms.", "st.", "jr.", "prof.", "vs."}


@dataclass
class Estilo:
    fuente: str = "dejavu"
    tamano: str = "medio"
    posicion: str = "centro_bajo"
    mayusculas: bool = False
    color: str = "#ffffff"
    color_borde: str = "#000000"
    borde: int = 20  # px en 1080x1920 (20 px = Outline 3, el de siempre)
    sombra: bool | None = None  # None = la de siempre: no en Shorts, sí en largos
    karaoke: bool = False

    @classmethod
    def desde(cls, *capas: dict | None) -> "Estilo":
        """Combina capas (ajustes globales, luego el guion); ignora claves o valores inválidos."""
        e = cls()
        for capa in capas:
            for k, v in (capa or {}).items():
                if k == "fuente" and v in FUENTES or k == "tamano" and v in TAMANOS \
                        or k == "posicion" and v in POSICIONES:
                    setattr(e, k, v)
                elif k in ("mayusculas", "karaoke") and isinstance(v, bool):
                    setattr(e, k, v)
                elif k == "sombra" and (v is None or isinstance(v, bool)):
                    e.sombra = v
                elif k in ("color", "color_borde") and _hex_ok(v):
                    setattr(e, k, v.lower())
                elif k == "borde" and isinstance(v, (int, float)) and 0 <= v <= 60:
                    e.borde = round(v)
        return e

    def a_dict(self) -> dict:
        return asdict(self)


def _hex_ok(v) -> bool:
    return isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])


def _bgr(hexa: str) -> str:
    """#rrggbb -> BBGGRR (ASS guarda los colores al revés)."""
    return f"{hexa[5:7]}{hexa[3:5]}{hexa[1:3]}".upper()


def _ass_color(hexa: str) -> str:
    """Color de estilo ASS, opaco: &H00BBGGRR."""
    return f"&H00{_bgr(hexa)}"


def ajuste_global(clave: str, defecto=None):
    """Un valor guardado en Ajustes de la app (rutas.AJUSTES), para que el CLI salga igual."""
    import json

    try:
        return json.loads(rutas.AJUSTES.read_text(encoding="utf-8")).get(clave, defecto)
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def estilo_global() -> dict:
    """El estilo de subtítulos guardado en Ajustes de la app."""
    return ajuste_global("estilo_subtitulos") or {}


def marca_global() -> dict:
    """Tamaño, esquina y margen de la marca de agua guardados en Ajustes de la app."""
    return ajuste_global("estilo_marca") or {}


def catalogo() -> dict:
    """Opciones para la interfaz."""
    return {
        "fuentes": [{"id": k, "nombre": v[0], "archivo": v[1]} for k, v in FUENTES.items()],
        "tamanos": [{"id": k, "px": round(u * PX_POR_UNIDAD)} for k, u in TAMANOS.items()],
        "posiciones": list(POSICIONES),
        "defecto": Estilo().a_dict(),
    }


# ---------- agrupación ----------

def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", s).lower() if c.isalnum())


def cortes(texto: str, palabras: list[dict]) -> set[int]:
    """Índices de palabra tras los que hay que cortar el grupo. edge-tts devuelve las palabras sin
    puntuación (y a veces junta varias: "diciembre de 1994"), así que se buscan en el texto original
    normalizado para recuperar la puntuación que las rodea."""
    toks = texto.split()
    flujo, dueno = "", []  # letras del texto sin puntuación, y a qué token pertenece cada una
    for ti, t in enumerate(toks):
        n = _norm(t)
        flujo += n
        dueno += [ti] * len(n)
    res, pos, previo = set(), 0, -1
    for i, p in enumerate(palabras):
        obj = _norm(p["texto"])
        k = flujo.find(obj, pos) if obj else -1
        if k < 0 or k - pos > 3:
            break  # no se pudo alinear: lo que queda se agrupa sin puntuación
        t_ini, t_fin = dueno[k], dueno[k + len(obj) - 1]
        empieza_token = k == 0 or dueno[k - 1] != t_ini
        termina_token = k + len(obj) == len(flujo) or dueno[k + len(obj)] != t_fin
        if i > 0 and empieza_token:
            sueltos = toks[previo + 1:t_ini]  # tokens sin letras entre palabras: "—", "..."
            if toks[t_ini][0] in ABRE or sueltos:
                res.add(i - 1)
        if termina_token and toks[t_fin][-1] in CIERRA and toks[t_fin].lower() not in ABREVIATURAS:
            res.add(i)
        pos, previo = k + len(obj), t_fin
    return res


def agrupar(n_palabras: int, cortes_: set[int], por_grupo: int) -> list[list[int]]:
    """Grupos de hasta por_grupo palabras sin cruzar un corte; dentro de cada frase, lo más parejos
    posible (4 palabras -> 2+2, no 3+1)."""
    frases, actual = [], []
    for i in range(n_palabras):
        actual.append(i)
        if i in cortes_:
            frases.append(actual)
            actual = []
    if actual:
        frases.append(actual)
    grupos = []
    for f in frases:
        k = math.ceil(len(f) / por_grupo)
        base, extra = divmod(len(f), k)
        ini = 0
        for gi in range(k):
            tam = base + (1 if gi < extra else 0)
            grupos.append(f[ini:ini + tam])
            ini += tam
    return grupos


def bloques(g: Guion, voces: list[dict], offset: float = 0.0) -> list[dict]:
    """[{inicio, fin, palabras: [{texto, inicio, fin}]}] con tiempos globales."""
    por_grupo = 3 if g.vertical else 6
    res, t0 = [], offset
    for e, v in zip(g.escenas, voces):
        pal = v["palabras"]
        grupos = agrupar(len(pal), cortes(e.texto, pal), por_grupo)
        for gi, grupo in enumerate(grupos):
            ini = t0 + pal[grupo[0]]["inicio"]
            # el subtítulo no desaparece antes de la siguiente palabra de la misma escena
            fin = t0 + (pal[grupos[gi + 1][0]]["inicio"] if gi + 1 < len(grupos) else pal[grupo[-1]]["fin"])
            res.append({"inicio": ini, "fin": fin, "palabras": [
                {"texto": pal[k]["texto"], "inicio": t0 + pal[k]["inicio"], "fin": t0 + pal[k]["fin"]} for k in grupo]})
        t0 += v["duracion"] + e.pausa  # la pausa es silencio: ahí no hay subtítulo
    return res


# ---------- escritura ----------

def _srt_t(t: float) -> str:
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def escribir_srt(bloques_: list[dict], ruta: Path):
    """Para subir a YouTube como subtítulos (CC)."""
    lineas = []
    for n, b in enumerate(bloques_, start=1):
        lineas += [str(n), f"{_srt_t(b['inicio'])} --> {_srt_t(b['fin'])}",
                   " ".join(p["texto"] for p in b["palabras"]), ""]
    ruta.write_text("\n".join(lineas), encoding="utf-8")


def _ass_t(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _estilo_ass(e: Estilo, vertical: bool) -> str:
    _, _, familia, negrita = FUENTES[e.fuente]
    tam = TAMANOS[e.tamano] * (1 if vertical else ESCALA_LARGO)
    borde = e.borde / PX_POR_UNIDAD * (1 if vertical else 2 / 3)
    sombra = e.sombra if e.sombra is not None else not vertical
    # las sombras de siempre: semitransparente en Shorts, negra opaca y fina en largos
    fondo, prof = ("&H80000000", 2) if vertical else ("&H0", 1)
    alin, mv_v, mv_h = POSICIONES[e.posicion]
    return (f"Style: Default,{familia},{_num(tam)},{_ass_color(e.color)},&Hffffff,{_ass_color(e.color_borde)},"
            f"{fondo},{1 if negrita else 0},0,0,0,100,100,0,0,1,{_num(borde)},{prof if sombra else 0},"
            f"{alin},10,10,{mv_v if vertical else mv_h},1")


def _texto(t: str, e: Estilo) -> str:
    t = t.replace("\\", "").replace("{", "(").replace("}", ")")
    return t.upper() if e.mayusculas else t


def escribir_ass(bloques_: list[dict], ruta: Path, e: Estilo, vertical: bool):
    eventos = []
    for b in bloques_:
        pal = b["palabras"]
        if not e.karaoke:
            eventos.append((b["inicio"], b["fin"], " ".join(_texto(p["texto"], e) for p in pal)))
            continue
        # karaoke: un evento por palabra con la línea completa, solo cambia cuál va en ámbar
        for k, p in enumerate(pal):
            ini = b["inicio"] if k == 0 else p["inicio"]
            fin = pal[k + 1]["inicio"] if k + 1 < len(pal) else b["fin"]
            texto = " ".join(
                f"{{\\c&H{_bgr(AMBAR)}&}}{_texto(q['texto'], e)}{{\\r}}" if q is p else _texto(q["texto"], e)
                for q in pal)
            eventos.append((ini, fin, texto))
    lineas = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 384", "PlayResY: 288",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: None", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _estilo_ass(e, vertical), "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lineas += [f"Dialogue: 0,{_ass_t(i)},{_ass_t(f)},Default,,0,0,0,,{t}" for i, f, t in eventos if f > 0]
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def escribir(g: Guion, voces: list[dict], carpeta: Path, e: Estilo, offset: float = 0.0) -> Path:
    """subtitulos.ass (el que se quema en el video) y subtitulos.srt (para subir a YouTube).
    offset: segundos antes de la primera voz (la portada)."""
    b = bloques(g, voces, offset)
    escribir_srt(b, carpeta / "subtitulos.srt")
    ass = carpeta / "subtitulos.ass"
    escribir_ass(b, ass, e, g.vertical)
    return ass


def filtro(ass: Path, carpeta: Path) -> str:
    """Filtro de ffmpeg. Rutas relativas a 'carpeta' (cwd del ffmpeg): el "C:" rompe el parser."""
    fuentes = Path(os.path.relpath(FUENTES_DIR, carpeta)).as_posix()
    return f"subtitles={ass.name}:fontsdir={fuentes}"


def previsualizar(g: Guion, imagen: Path, voz_info: dict | None, carpeta: Path, e: Estilo, marca=None,
                 logo_marca: Path | None = None) -> Path:
    """Un cuadro de la escena 1 con el estilo aplicado, en ~1 s, para comparar sin renderizar.
    Muestra el primer grupo de palabras; con karaoke, con la segunda palabra resaltada.
    marca: un marca.EstiloMarca para poner también la marca de agua como en el video (None = sin marca)."""
    esc = g.escenas[0]
    if voz_info is None:  # sin voz todavía: tiempos inventados, solo importa el aspecto
        voz_info = {"duracion": 9.0, "palabras": [{"texto": w, "inicio": i * 0.35, "fin": i * 0.35 + 0.3}
                                                  for i, w in enumerate(esc.texto.split())]}
    b = bloques(replace(g, escenas=[esc]), [voz_info])
    if not b:
        raise error("err.preview_sin_texto")
    pal = b[0]["palabras"]
    p = pal[1] if len(pal) > 1 else pal[0]
    t = (p["inicio"] + p["fin"]) / 2
    for x in b:  # desplazado para que el instante elegido quede en t=0
        x["inicio"] -= t
        x["fin"] -= t
        for q in x["palabras"]:
            q["inicio"] -= t
            q["fin"] -= t
    ass = carpeta / "preview_subtitulos.ass"
    escribir_ass(b, ass, e, g.vertical)
    w, h = g.tamano
    salida = carpeta / "preview_subtitulos.jpg"
    cuadro = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},{filtro(ass, carpeta)}"
    if marca is None:
        entradas, filtros = ["-i", str(imagen)], ["-vf", cuadro]
    else:
        from . import marca as mod_marca

        entradas = ["-i", str(imagen), "-i", str(mod_marca.marca_agua(h, marca.tamano, logo_marca))]
        filtros = ["-filter_complex", f"[0:v]{cuadro}[s];[s][1:v]overlay={marca.overlay(h)}"]
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *entradas, *filtros, "-frames:v", "1", "-q:v", "3", salida.name],
        cwd=carpeta, capture_output=True, text=True)
    if r.returncode != 0:
        raise ErrorEjecucion(Texto("err.ffmpeg_preview", detalle=r.stderr[-1500:]))
    return salida
