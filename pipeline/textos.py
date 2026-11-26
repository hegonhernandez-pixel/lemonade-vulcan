"""Mensajes que pueden llegar a la pantalla de la app (errores, avisos, log), en varios idiomas.

El diccionario es app/static/i18n.js, el mismo de la interfaz web: un solo archivo con todos los idiomas.
Un Texto es un str en español (así lo ven la consola y make_video.py) que además recuerda su clave y sus
parámetros, para que la app lo muestre en el idioma de la interfaz de quien pregunta (IDIOMA).
"""
import contextvars
import json
import re
import sys
import threading
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "app" / "static" / "i18n.js"
DEFECTO = "es"
# idioma de la interfaz de la petición en curso (la app lo fija con la cabecera X-Idioma)
IDIOMA: contextvars.ContextVar[str] = contextvars.ContextVar("idioma_ui", default=DEFECTO)

_cache: dict = {"mtime": None, "dic": {}}
_lock = threading.Lock()


def diccionario() -> dict:
    """{idioma: {clave: plantilla}}, releído si i18n.js cambió."""
    try:
        mtime = I18N.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] != mtime:
            s = I18N.read_text(encoding="utf-8")
            ini, fin = s.index("/*JSON*/") + len("/*JSON*/"), s.index("/*FIN-JSON*/")
            _cache["dic"], _cache["mtime"] = json.loads(s[ini:fin]), mtime
        return _cache["dic"]


def idiomas() -> list[str]:
    return list(diccionario()) or [DEFECTO]


def traducir(clave: str, idioma: str | None = None, params: dict | None = None) -> str:
    idioma = idioma or IDIOMA.get()
    dic = diccionario()
    plantilla = dic.get(idioma, {}).get(clave) or dic.get(DEFECTO, {}).get(clave) or clave
    params = params or {}

    def poner(m):
        if m.group(1) not in params:
            return m.group(0)
        v = params[m.group(1)]
        return v.en(idioma) if isinstance(v, Texto) else str(v)

    return re.sub(r"\{(\w+)\}", poner, plantilla)


class Texto(str):
    """Texto("err.mood", mood="x"): el mensaje en español, que se puede pedir en otro idioma con .en()."""

    def __new__(cls, clave: str, **params):
        t = super().__new__(cls, traducir(clave, DEFECTO, params))
        t.clave, t.params = clave, params
        return t

    @classmethod
    def plural(cls, clave: str, n: int, **params) -> "Texto":
        return cls(f"{clave}.{'uno' if n == 1 else 'otros'}", n=n, **params)

    def en(self, idioma: str | None = None) -> str:
        return traducir(self.clave, idioma, self.params)


class Unidos(Texto):
    """Varios Textos unidos con un separador (una lista de errores en un solo mensaje)."""

    def __new__(cls, partes, sep: str = "\n"):
        partes = list(partes)
        t = str.__new__(cls, sep.join(str(p) for p in partes))
        t.clave, t.params, t.partes, t.sep = "", {}, partes, sep
        return t

    def en(self, idioma: str | None = None) -> str:
        return self.sep.join(local(p, idioma) for p in self.partes)


def local(x, idioma: str | None = None):
    """Texto -> str en el idioma pedido (por defecto, el de la petición en curso). Recorre dicts y listas;
    lo demás lo deja igual."""
    if isinstance(x, Texto):
        return x.en(idioma)
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return {k: local(v, idioma) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [local(v, idioma) for v in x]
    return x


class Error(ValueError):
    """Un error con mensaje traducible: Error(Texto(...)). str(e) es el español."""

    def __init__(self, texto: Texto):
        super().__init__(texto)
        self.texto = texto


class ErrorEjecucion(RuntimeError):
    """Como Error, para fallos de ejecución (ffmpeg, claude -p)."""

    def __init__(self, texto: Texto):
        super().__init__(texto)
        self.texto = texto


def error(clave: str, **params) -> Error:
    return Error(Texto(clave, **params))


def de(e: BaseException):
    """El mensaje de una excepción: su Texto si es nuestra; si no, su str."""
    t = getattr(e, "texto", None)
    return t if isinstance(t, Texto) else (str(e) or type(e).__name__)


def lineas(e: BaseException) -> list:
    """Los mensajes de una excepción por separado (una lista de errores de validación)."""
    t = de(e)
    if isinstance(t, Unidos) and t.sep == "\n":
        return t.partes
    return [t] if isinstance(t, Texto) else str(t).split("\n")


def decir(clave: str, sangria: int = 1, **params):
    """print() traducible: en la consola sale en español; en la app va al log del trabajo con su clave."""
    t = Texto(clave, **params)
    out = sys.stdout
    if hasattr(out, "texto"):
        out.texto(t, "  " * sangria)
    else:
        print("  " * sangria + t)
