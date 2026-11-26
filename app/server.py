"""CompressOS Studio: interfaz para escribir, editar y producir videos.

  python app/server.py            ->  http://127.0.0.1:8765  (solo en este equipo)
  python app/server.py --puerto 8080 --sin-navegador   (el puerto por defecto está en app/config.py)
"""
import argparse
import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import credenciales, marca_personal
from app.seguridad_local import SeguridadLocal
from app.subidas import LimiteSubidaPropios
from app import config  # noqa: E402
from app import licencia, preparar  # noqa: E402
from app import auth, cierre  # noqa: E402
from app.version import URL_APOYO, VERSION  # noqa: E402
from app import escribir as escritura  # noqa: E402
from app import narracion  # noqa: E402
from app import trabajos  # noqa: E402
from pipeline import guion as mod_guion  # noqa: E402
from pipeline import medios, vulkan
from pipeline import rutas  # noqa: E402,I001  (primero: fija HF_HOME en modo instalado)
from pipeline import imagenes, marca, musica, personajes, subtitulos, textos, voz, voz_local  # noqa: E402
from pipeline.producir import Opciones, carpeta  # noqa: E402
from pipeline.textos import Error, Texto, Unidos  # noqa: E402

# lo del usuario: en el repo (modo desarrollador) o en %LOCALAPPDATA%\DcryptoStudio (instalado); ver pipeline/rutas.py
GUIONES = rutas.GUIONES
SALIDA = rutas.SALIDA
MUSICA = rutas.MUSICA
ESTATICOS = Path(__file__).parent / "static"
LOGOS = RAIZ / "assets" / "logo"
AJUSTES = rutas.AJUSTES
LOGO_PERSONAL = rutas.DATOS / "marca" / "logo.png"
for d in (GUIONES, SALIDA, MUSICA):
    d.mkdir(parents=True, exist_ok=True)

AJUSTES_BASE = {
    "voz": "es-CO-GonzaloNeural",  # voz por defecto en español con edge-tts
    # motor de voz de los guiones nuevos: edge (en línea) | kokoro (local); cada guion guarda el suyo
    "motor_voz": "edge",
    "voz_kokoro": "em_alex",  # voz por defecto en español con Kokoro
    "formato": "short",
    "estilo_visual": "cinematic, dramatic lighting, 35mm film grain, muted teal and amber palette, photorealistic, high detail",
    "rate": "+0%",
    "calidad": "rapida",  # imágenes: rapida (SDXL-Turbo) o alta (SDXL base); ver pipeline/imagenes.py
    "subtitulos": True,
    "marca_agua": False,
    "portada": True,  # miniatura como primer cuadro (0,6 s), para elegirla en la app de YouTube
    "estilo_subtitulos": {},  # vacío = el estilo de siempre (ver pipeline/subtitulos.py)
    "frase_cierre": {"es": "", "en": ""},  # última frase de los Shorts (el lema del canal); vacía = sin frase fija
    "estilo_marca": {},  # tamaño, esquina y margen de la marca de agua; vacío = mediana, arriba a la derecha, 140 px
    "cierre_tarjeta": True,  # tarjeta de cierre (logo + nombre, 2,5 s) al final; cada guion puede cambiarlo ("cierre")
    "cierre_nombre": marca.CIERRE_TEXTO,  # el nombre bajo el logo en la tarjeta de cierre
    "demo": rutas.LINUX and os.environ.get("COMPRESSO_LOCAL_DEMO") == "1",
    # IA para "Generar guion" y "Traducir": claude_code | anthropic | openai | ninguno (ver app/escribir.py)
    "proveedor_ia": "gemini" if rutas.LINUX else "claude_code",
    "gemini_api_key": "", "gemini_modelo": "gemini-2.5-flash",
    "ollama_modelo": "qwen3:4b", "buscar_google": True,
    "anthropic_api_key": "", "anthropic_modelo": "claude-opus-5-5",
    "openai_api_key": "", "openai_modelo": "gpt-5",
}
CLAVES_API = ("anthropic_api_key", "openai_api_key", "gemini_api_key")  # nunca salen del servidor
VOCES_RESPALDO = ["es-CO-GonzaloNeural", "es-CO-SalomeNeural", "es-MX-JorgeNeural", "es-MX-DaliaNeural",
                  "en-US-AndrewNeural", "en-US-GuyNeural", "en-US-AriaNeural", "en-GB-RyanNeural"]
PRUEBA_SEG = 15.0
SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")



# ---------- idioma de la interfaz ----------
# La app manda su idioma en la cabecera X-Idioma. Los mensajes del servidor (errores, avisos, log, pasos)
# son textos.Texto: se traducen al responder, con el diccionario de app/static/i18n.js.

class Respuesta(JSONResponse):
    """JSON con los Texto traducidos al idioma de la petición."""

    def render(self, content) -> bytes:
        return super().render(textos.local(content))


class Idioma:
    """Middleware ASGI: fija textos.IDIOMA para toda la petición (también en los hilos de FastAPI)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            pedido = dict(scope.get("headers") or []).get(b"x-idioma", b"").decode("latin-1").strip().lower()
            textos.IDIOMA.set(pedido if pedido in textos.idiomas() else textos.DEFECTO)
        await self.app(scope, receive, send)


api = FastAPI(title="CompressOS Studio", default_response_class=Respuesta,
              docs_url=None, redoc_url=None, openapi_url=None)
api.add_middleware(Idioma)
api.add_middleware(LimiteSubidaPropios)
api.add_middleware(auth.Sesion)
api.add_middleware(SeguridadLocal)
api.include_router(auth.router)

from app.estudio import router as estudio_router
api.include_router(estudio_router)


@api.get("/api/salud")
def salud():
    return {"aplicacion": "CompressOS Studio" if rutas.LINUX else "Dcrypto Studio", "estado": "ok"}


@api.get("/api/credenciales/estado")
def estado_credenciales():
    try:
        campos = credenciales.afectadas(AJUSTES, CLAVES_API)
    except credenciales.ErrorCredenciales:
        raise HTTPException(503, "No se pudo comprobar la configuración de credenciales. No se modificó ningún dato.") from None
    return {"afectadas": [c.removesuffix("_api_key") for c in campos]}


@api.post("/api/credenciales/reemplazar")
def recuperar_credencial(cuerpo: dict = Body(...)):
    if set(cuerpo) != {"proveedor", "nueva_clave"} or cuerpo.get("proveedor") not in escritura.VAR_ENTORNO:
        raise HTTPException(400, "Solicitud de recuperación no válida.")
    try:
        credenciales.reemplazar_afectada(AJUSTES, cuerpo["proveedor"] + "_api_key", cuerpo["nueva_clave"], CLAVES_API)
    except credenciales.ErrorCredenciales:
        raise HTTPException(503, "No se pudo reemplazar la credencial. La configuración original se conservó; comprueba que la credencial siga afectada y que el cifrado esté configurado.") from None
    return {"estado": "reemplazada"}


@api.exception_handler(HTTPException)
async def error_http(_pedido, exc: HTTPException):
    return Respuesta({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@api.exception_handler(Exception)
async def error_inesperado(pedido, exc: Exception):
    """Cualquier excepción sin atrapar: un mensaje claro para la interfaz (en su idioma) en vez de un 500
    pelado, y el traceback completo en la consola del servidor."""
    print(f"\nERROR en {pedido.method} {pedido.url.path}:", file=sys.stderr)
    traceback.print_exception(exc, file=sys.stderr)
    idioma = pedido.headers.get("x-idioma", "").strip().lower()
    textos.IDIOMA.set(idioma if idioma in textos.idiomas() else textos.DEFECTO)  # este manejador va fuera del middleware
    if isinstance(exc, FileNotFoundError):
        detalle = Texto("srv.ya_no_existe", archivo=Path(exc.filename).name if exc.filename else exc)
    else:
        detalle = Texto("srv.error_interno", tipo=type(exc).__name__, error=textos.de(exc))
    return Respuesta({"detail": detalle}, status_code=500)


# ---------- utilidades ----------

def ajustes() -> dict:
    try:
        guardados = credenciales.leer(AJUSTES, CLAVES_API)
        # solo claves vigentes (los ajustes viejos traían "modelo" y "pasos")
        return {**AJUSTES_BASE, **{k: v for k, v in guardados.items() if k in AJUSTES_BASE}}
    except credenciales.ErrorCredenciales:
        raise HTTPException(503, "No se pudieron proteger o recuperar las credenciales del servidor. Los ajustes originales se conservaron.") from None
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(AJUSTES_BASE)


def opciones() -> Opciones:
    a = ajustes()
    return Opciones(demo=bool(a["demo"]), rate=a["rate"], calidad=a["calidad"],
                    subtitulos=bool(a["subtitulos"]), marca_agua=bool(a["marca_agua"]) and LOGO_PERSONAL.is_file(),
                    logo_marca=LOGO_PERSONAL if LOGO_PERSONAL.is_file() else None,
                    estilo_subtitulos=a["estilo_subtitulos"], estilo_marca=a["estilo_marca"], portada=bool(a["portada"]),
                    # ajustes de antes (sin estas claves): la tarjeta de siempre
                    cierre=bool(a.get("cierre_tarjeta", True)),
                    **({"cierre_nombre": str(a["cierre_nombre"] or "")} if "cierre_nombre" in a else {}))


api.state.opciones_render = opciones


def ruta_guion(slug: str) -> Path:
    if not SLUG_OK.match(slug):
        raise HTTPException(400, Texto("srv.slug_invalido"))
    return GUIONES / f"{slug}.json"


def leer(slug: str) -> dict:
    ruta = ruta_guion(slug)
    if not ruta.exists():
        raise HTTPException(404, Texto("srv.no_existe", slug=slug))
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(422, Texto("srv.json_invalido", slug=slug, error=e))


ORDEN = ["slug", "titulo", "titulo_miniatura", "miniatura_escena", "tema", "material", "formato", "idioma", "voz",
         "motor_voz", "estilo_visual", "musica", "volumen_musica", "portada", "subtitulos", "marca", "cierre", "escenas"]


def escribir(slug: str, data: dict) -> bool:
    """Guarda con un orden de claves estable (el archivo lo lee y edita Claude Code). True si cambió."""
    data = {**{k: data[k] for k in ORDEN if k in data}, **{k: v for k, v in data.items() if k not in ORDEN}}
    data["escenas"] = [
        {"texto": e.get("texto", ""), "prompt": e.get("prompt", ""),
         **({"semilla": int(e["semilla"])} if e.get("semilla") else {}),
         **{k: v for k, v in e.items() if k not in ("texto", "prompt", "semilla")}}
        for e in data.get("escenas", [])
    ]
    for e in data["escenas"]:  # opcionales: sin la clave cuando no aplican
        if not e.get("media"):
            e.pop("media", None)
        if not e.get("pausa"):
            e.pop("pausa", None)
    if data.get("portada") is None:
        data.pop("portada", None)
    if data.get("cierre") in (None, "", "global"):  # sin el campo = Ajustes globales (como los guiones de antes)
        data.pop("cierre", None)
    if data.get("idioma", "es") == "es":
        data.pop("idioma", None)
    if not data.get("musica"):
        data.pop("musica", None)
        data.pop("volumen_musica", None)
    if isinstance(data.get("subtitulos"), dict) and data["subtitulos"]:
        data["subtitulos"] = subtitulos.Estilo.desde(data["subtitulos"]).a_dict()
    else:
        data.pop("subtitulos", None)
    if data.get("marca") is False:  # "Sin marca": este video sin marca de agua ni logo en la miniatura
        pass
    elif isinstance(data.get("marca"), dict) and data["marca"]:
        data["marca"] = marca.EstiloMarca.desde(data["marca"]).a_dict()
    else:
        data.pop("marca", None)
    texto = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    ruta = ruta_guion(slug)
    if ruta.exists() and ruta.read_text(encoding="utf-8") == texto:
        return False
    tmp = ruta.with_suffix(".json.tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, ruta)
    return True


def url_musica(p: Path | None) -> str | None:
    if not p or not p.exists() or not p.resolve().is_relative_to(MUSICA.resolve()):
        return None
    return "/musica/" + p.resolve().relative_to(MUSICA.resolve()).as_posix()


def url(p: Path | None) -> str | None:
    if not p or not p.exists():
        return None
    if p.resolve().is_relative_to(imagenes.PROPIOS.resolve()):
        return "/propios/" + p.resolve().relative_to(imagenes.PROPIOS.resolve()).as_posix()
    return "/salida/" + p.relative_to(SALIDA).as_posix()


_duraciones: dict[tuple[str, float], float] = {}


def duracion_mp4(p: Path) -> float | None:
    clave = (str(p), p.stat().st_mtime)
    if clave not in _duraciones:
        try:
            _duraciones[clave] = voz.duracion(p)
        except (subprocess.CalledProcessError, ValueError):
            return None
    return _duraciones[clave]


def slugify(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if len(t) > 50:  # cortar en el último guion: palabras enteras, nunca "...-salvo-al"
        t = t[:51].rsplit("-", 1)[0]
    return t


def completo(data: dict) -> bool:
    esc = data.get("escenas") or []
    return bool(esc) and all((e.get("texto") or "").strip() and ((e.get("prompt") or "").strip() or e.get("media"))
                             for e in esc)


def estado_de(slug: str, data: dict, mp4: Path) -> str:
    if trabajos.activo(slug):
        return "generando"
    if mp4.exists():
        return "listo"
    return "guion" if completo(data) else "idea"


# ---------- guiones ----------

def detalle(slug: str) -> dict:
    data = leer(slug)
    ruta = ruta_guion(slug)
    a = ajustes()
    demo, rate, calidad = bool(a["demo"]), a["rate"], a["calidad"]
    escenas, error = [], None
    try:
        g = mod_guion.desde_dict({**data, "slug": data.get("slug") or slug, "titulo": data.get("titulo", "")},
                                 estricto=False)
    except ValueError as e:
        g, error = None, textos.de(e)
    dest = carpeta(g) if g else SALIDA / slug
    if g:
        for e in g.escenas:
            img, q = imagenes.vista(g, e, dest, demo, calidad)
            meta = voz.buscar(g, e, dest, demo, rate) if e.texto and dest.exists() else None
            propio = imagenes.ruta_propia(g.slug, e.media) if e.media else None
            clip_info = {}
            if e.es_clip and propio and propio.is_file():
                try:
                    clip_info = medios.info_clip(propio)
                except ValueError as exc:
                    clip_info = {"error": textos.de(exc)}
            escenas.append({
                "img": url(img),
                # archivo propio: {tipo: imagen|clip, url}; falta=True si el guion lo nombra pero no está
                "media": {"tipo": "clip" if e.es_clip else "imagen", "url": url(propio), "nombre": Path(e.media).name,
                          "falta": not propio.exists(), **clip_info} if propio else None,
                "calidad_img": q if img else None,  # la de la imagen que se ve (puede no ser la que toca)
                "calidad": imagenes.calidad_de(e, calidad),  # la que se usará al producir
                "presupuesto_visual": imagenes.presupuesto_visual(imagenes.prompt_final(g, e)) if e.prompt and not e.media else None,
                "voz": url(meta.with_suffix(".mp3")) if meta else None,
                "duracion": json.loads(meta.read_text(encoding="utf-8"))["duracion"] if meta else None,
            })
    mp4 = dest / f"{g.slug if g else slug}.mp4"
    video = None
    if mp4.exists():
        video = {
            "url": f"{url(mp4)}?v={int(mp4.stat().st_mtime)}",
            "duracion": duracion_mp4(mp4),
            "fecha": mp4.stat().st_mtime,
            "desactualizado": ruta.stat().st_mtime > mp4.stat().st_mtime,
        }
    mini = dest / "miniatura.jpg"
    prueba = dest / "prueba_mezcla.mp4"
    pista = musica.resolver(g.musica, g.slug) if g else None
    return {
        "slug": slug, "doc": data, "mtime": str(ruta.stat().st_mtime_ns), "escenas": escenas,
        "video": video, "estado": estado_de(slug, data, mp4), "error_guion": error,
        # la voz del guion no existe en su motor para su idioma: al producir se usa la de por defecto
        "aviso_voz": voz.corregir(g.voz, g.idioma, g.motor_voz)[1] if g and not demo else None,
        "musica": {"valor": g.musica if g else None, "pista": url_musica(pista), "nombre": pista.stem if pista and pista.exists() else None},
        "miniatura": {"url": f"{url(mini)}?v={int(mini.stat().st_mtime)}", "fecha": mini.stat().st_mtime}
        if mini.exists() else None,
        "prueba": {"url": f"{url(prueba)}?v={int(prueba.stat().st_mtime)}", "fecha": prueba.stat().st_mtime}
        if prueba.exists() else None,
        "carpeta": str(dest), "trabajos": [t.a_dict() for t in trabajos.listar(slug)][-15:],
    }


@api.get("/api/guiones")
def listar_guiones():
    """Una tarjeta por cada guiones/*.json que existe en este momento. No hay índice guardado: se recorre
    la carpeta en cada consulta. Un guion que desaparece a mitad de camino (movido a la papelera mientras
    la Biblioteca se refresca) se salta; uno que falla por otra cosa sale como tarjeta de error. Nunca
    tumba la lista entera."""
    a = ajustes()
    tarjetas = []
    for ruta in sorted(GUIONES.glob("*.json")):
        try:
            try:
                tarjeta = _tarjeta(ruta, a)
            except FileNotFoundError:
                if not ruta.exists():
                    continue  # ya no está: lo movieron a la papelera entre el glob y la lectura
                tarjeta = _tarjeta(ruta, a)  # el guion sigue: faltaba otro archivo (mp4, imagen), otro intento
        except FileNotFoundError:
            if not ruta.exists():
                continue
            raise
        except Exception as e:  # noqa: BLE001 - un guion raro no rompe la Biblioteca
            try:
                fecha = ruta.stat().st_mtime
            except OSError:
                continue
            tarjeta = {"slug": ruta.stem, "titulo": ruta.stem, "estado": "error", "error": textos.de(e), "fecha": fecha}
        if tarjeta:
            tarjetas.append(tarjeta)
    tarjetas.sort(key=lambda t: t["fecha"], reverse=True)
    return tarjetas


def _tarjeta(ruta: Path, a: dict) -> dict:
    slug = ruta.stem
    texto = ruta.read_text(encoding="utf-8")
    mtime = ruta.stat().st_mtime
    try:
        data = json.loads(texto)
        g = mod_guion.desde_dict({**data, "slug": data.get("slug") or slug, "titulo": data.get("titulo", "")},
                                 estricto=False)
    except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {"slug": slug, "titulo": slug, "estado": "error", "error": textos.de(e), "fecha": mtime}
    dest = carpeta(g)
    mp4 = dest / f"{g.slug}.mp4"
    mini = None
    if dest.exists():
        for e in g.escenas:
            if mini := imagenes.vista(g, e, dest, bool(a["demo"]), a["calidad"])[0]:
                break
    n = mod_guion.palabras(g)
    dur = duracion_mp4(mp4) if mp4.exists() else None
    try:
        mp4_mtime = mp4.stat().st_mtime
    except OSError:
        mp4_mtime = None
    return {
        "slug": slug, "titulo": g.titulo or data.get("tema") or slug, "tema": data.get("tema", ""),
        "formato": g.formato, "estado": estado_de(slug, data, mp4), "escenas": len(g.escenas),
        "palabras": n, "duracion": dur, "estimada": round(n / 2.4) if n else None,
        "fecha": max(mtime, mp4_mtime or 0),
        "miniatura": url(mini), "desactualizado": mp4_mtime is not None and mtime > mp4_mtime,
    }


@api.get("/api/guiones/{slug}")
def ver_guion(slug: str):
    return detalle(slug)


def voz_defecto(a: dict, motor: str, idioma: str) -> str:
    """La de Ajustes en español (una por motor); en inglés, la de por defecto del motor."""
    if idioma == "es":
        return a["voz_kokoro"] if motor == "kokoro" else a["voz"]
    return mod_guion.VOCES_DEFECTO[motor][idioma]


def ajustar_voz(doc: dict, a: dict) -> Texto | None:
    """Deja en el guion un motor y una voz que existan para su idioma. El motor es el del guion (o el de su voz,
    o el de Ajustes); una voz que no existe en ese motor para ese idioma se cambia por la de por defecto y se avisa."""
    motor = doc.get("motor_voz")
    if motor not in mod_guion.MOTORES:
        motor = mod_guion.motor_de(doc["voz"]) if doc.get("voz") else a["motor_voz"]
    idioma = doc.get("idioma") if doc.get("idioma") in mod_guion.IDIOMAS else "es"
    doc["motor_voz"] = motor
    if not doc.get("voz"):
        doc["voz"] = voz_defecto(a, motor, idioma)
        return None
    doc["voz"], aviso = voz.corregir(doc["voz"], idioma, motor)
    return aviso


@api.post("/api/guiones")
def crear_guion(datos: dict = Body(...)):
    a = ajustes()
    tema = (datos.get("tema") or "").strip()
    titulo = (datos.get("titulo") or "").strip()
    base = slugify(datos.get("slug") or titulo or tema) or time.strftime("video-%Y%m%d-%H%M")
    slug, n = base, 2
    while ruta_guion(slug).exists():
        slug, n = f"{base}-{n}", n + 1
    data = {
        "slug": slug, "titulo": titulo, "tema": tema, "material": (datos.get("material") or "").strip(),
        "formato": datos.get("formato") or a["formato"],
        "idioma": datos.get("idioma") if datos.get("idioma") in mod_guion.IDIOMAS else "es",
        "voz": datos.get("voz") or "",
        "motor_voz": datos.get("motor_voz") or a["motor_voz"],
        "portada": datos["portada"] if isinstance(datos.get("portada"), bool) else None,
        "estilo_visual": datos.get("estilo_visual") or a["estilo_visual"],
        "musica": datos.get("musica") or None,
        "volumen_musica": float(datos.get("volumen_musica") or 0.15),
        "subtitulos": datos.get("subtitulos") or {},
        "marca": False if datos.get("marca") is False else datos.get("marca") or {},
        # un guion nuevo aún no tiene carpeta de archivos propios: solo global o ninguno
        "cierre": "ninguno" if datos.get("cierre") == "ninguno" else "global",
        "escenas": datos.get("escenas") or [],
    }
    aviso = ajustar_voz(data, a)
    escribir(slug, data)
    return {**detalle(slug), "avisos": [aviso] if aviso else []}


@api.put("/api/guiones/{slug}")
def guardar_guion(slug: str, cuerpo: dict = Body(...)):
    data, base = cuerpo.get("doc"), cuerpo.get("mtime")
    if not isinstance(data, dict) or not isinstance(data.get("escenas"), list):
        raise HTTPException(400, Texto("srv.falta_doc"))
    ruta = ruta_guion(slug)
    if not ruta.exists():
        raise HTTPException(404, Texto("srv.no_existe", slug=slug))
    # string: los ns no caben exactos en un número de JavaScript
    if base and str(ruta.stat().st_mtime_ns) != str(base):
        # alguien más (Claude Code, un editor) cambió el archivo: no lo pisamos
        return Respuesta(status_code=409, content=detalle(slug))
    data["slug"] = slug
    try:
        g = mod_guion.desde_dict(data, estricto=False)
        for escena in g.escenas:
            if escena.es_clip:
                propio = imagenes.ruta_propia(slug, escena.media)
                if propio.is_file():
                    medios.validar_inicio(propio, escena.inicio_clip)
    except ValueError as e:
        raise HTTPException(400, textos.de(e))
    escribir(slug, data)
    return detalle(slug)


# ---------- crear guiones completos: pegados o escritos por Claude ----------

PARAMETROS = ("formato", "idioma", "voz", "motor_voz", "estilo_visual", "musica", "volumen_musica", "portada", "subtitulos", "marca", "cierre", "tema", "material")


def importar(data: dict, base: dict, reemplazar: bool = False, slug: str | None = None) -> tuple[str, list[Texto]]:
    """Guarda un guion completo. Lo que traiga 'data' manda; lo que falte sale de 'base' (la columna
    Idea) y luego de Ajustes. El slug sale del título. Devuelve (slug, avisos).
    ValueError con todos los errores si no es válido; FileExistsError(slug) si ya existe y no se
    pidió reemplazar: la app nunca pisa un guion sin preguntar."""
    errores = mod_guion.validar(data)
    if errores:
        raise Error(Unidos(errores))
    a = ajustes()
    slug = slug or slugify(data["titulo"]) or time.strftime("video-%Y%m%d-%H%M")
    if ruta_guion(slug).exists() and not reemplazar:
        raise FileExistsError(slug)
    defecto = {"formato": a["formato"], "estilo_visual": a["estilo_visual"], "motor_voz": a["motor_voz"]}
    doc = {**defecto, **{k: v for k, v in base.items() if k in PARAMETROS and v not in (None, "", {})},
           **{k: v for k, v in data.items() if k not in ("slug", "escenas")}}
    doc["slug"] = slug
    doc["titulo"] = data["titulo"].strip()
    doc["escenas"] = [{"texto": e["texto"].strip(), "prompt": e["prompt"].strip(),
                       **({"semilla": e["semilla"]} if e.get("semilla") else {}),
                       **({"prompt_sugerido": True} if e.get("prompt_sugerido") is True else {})}
                      for e in data["escenas"]]
    if data.get("voz") and not data.get("motor_voz"):
        doc.pop("motor_voz", None)  # una voz explícita sin motor: el motor sale de la voz
    aviso_voz = ajustar_voz(doc, a)
    escribir(slug, doc)
    avisos = mod_guion.avisos(doc) + ([aviso_voz] if aviso_voz else [])
    if any(e.get("prompt_sugerido") for e in doc["escenas"]):
        avisos.insert(0, Texto("aviso.prompts_sugeridos"))
    return slug, avisos


def _libre(slug: str) -> str:
    base, n = slug, 2
    while ruta_guion(slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


@api.post("/api/guiones/importar")
def importar_guion(cuerpo: dict = Body(...)):
    """Opción "Pegar guion": {texto} o {guion} ya parseado. El texto puede ser un guion en JSON (también
    envuelto en ```json) o la narración en texto normal, que se parte en escenas con prompts sugeridos
    (app/narracion.py). reemplazar=true pisa uno existente; copia=true lo guarda con otro slug libre."""
    base = cuerpo.get("base") or {}
    texto = cuerpo.get("texto") or ""
    try:
        if isinstance(cuerpo.get("guion"), dict):
            data = cuerpo["guion"]
        elif narracion.parece_json(texto):
            data = mod_guion.desde_texto(texto)
        elif texto.strip():
            data = narracion.guion(texto, base)
        else:
            raise textos.error("srv.pegar_vacio")
    except ValueError as e:
        return Respuesta(status_code=422, content={"errores": [textos.de(e)]})
    slug = None
    if cuerpo.get("copia") and isinstance(data, dict) and isinstance(data.get("titulo"), str):
        slug = _libre(slugify(data["titulo"]) or "video")
    try:
        slug, avisos = importar(data, base, bool(cuerpo.get("reemplazar")), slug)
        if data.get("idioma") and data["idioma"] != (base.get("idioma") or "es"):  # texto pegado en otro idioma
            avisos.insert(0, Texto("aviso.idioma_detectado", idioma=Texto(f"lang.{data['idioma']}"), voz=data.get("voz", "")))
    except ValueError as e:
        return Respuesta(status_code=422, content={"errores": textos.lineas(e)})
    except FileExistsError as e:
        existente = leer(str(e))
        return Respuesta(status_code=409, content={"existe": str(e), "titulo": existente.get("titulo", "")})
    return {"slug": slug, "avisos": avisos, "guion": detalle(slug)}


def pedido_escritura(cuerpo: dict):
    """(slug, base, datos, actual, nota) de "Generar guion" / "Reescribir": lo mismo con o sin IA integrada."""
    slug = cuerpo.get("slug")
    actual = None
    if slug:
        previo = leer(slug)
        actual = {k: previo.get(k) for k in ("titulo", "titulo_miniatura", "escenas")}
        base = {**{k: previo.get(k) for k in PARAMETROS}, **{k: v for k, v in cuerpo.items() if k in PARAMETROS}}
    else:
        base = {k: v for k, v in cuerpo.items() if k in PARAMETROS}
    if not (base.get("tema") or "").strip() and not (base.get("material") or "").strip():
        raise HTTPException(400, Texto("srv.falta_tema"))
    a = ajustes()
    datos = {"formato": a["formato"], "estilo_visual": a["estilo_visual"], **base}
    nota = (cuerpo.get("nota") or "").strip() or None
    return slug, base, datos, actual, nota


@api.post("/api/escribir/prompt")
def prompt_escritura(cuerpo: dict = Body(...)):
    """Sin IA integrada: el prompt completo de "Generar guion" (el mismo de app/escribir.py, con el tema y el
    material del formulario) para copiarlo a otra IA. La respuesta se pega con "Pegar guion"."""
    _, _, datos, actual, nota = pedido_escritura(cuerpo)
    return {"prompt": escritura.construir_prompt(datos, nota, actual)}


@api.post("/api/escribir")
def escribir_con_claude(cuerpo: dict = Body(...)):
    """Opción "Generar guion con IA" / "Reescribir": con el proveedor de Ajustes, en segundo plano.
    Con 'slug' es una reescritura de ese guion (la app ya preguntó antes de reemplazarlo)."""
    exigir_ia()
    slug, base, datos, actual, nota = pedido_escritura(cuerpo)

    def trabajo(t):
        llamada = escritura.nueva_llamada(ajustes())
        t.cancelar = llamada.cancelar
        try:
            guion = escritura.escribir_guion(datos, llamada, t.log.append, nota=nota, actual=actual)
        except escritura.SinFormato as e:
            e.resultado = {"crudo": e.crudo}  # la interfaz lo abre en "Pegar guion" para arreglarlo a mano
            raise
        t.log.append(Texto("log.guion_recibido", titulo=guion["titulo"], n=len(guion["escenas"])))
        try:
            nuevo, avisos = importar(guion, base, reemplazar=bool(slug), slug=slug)
        except FileExistsError as e:
            # no se pisa: la interfaz pregunta y luego llama a /api/guiones/importar
            return {"existe": str(e), "titulo": leer(str(e)).get("titulo", ""), "guion": guion, "base": base}
        return {"slug": nuevo, "avisos": avisos}

    t = trabajos.lanzar(slug or "_nuevo", "escribir", trabajo)
    return t.a_dict()


@api.get("/api/trabajos/{id_}")
def ver_trabajo(id_: int):
    t = trabajos.buscar(id_)
    if not t:
        raise HTTPException(404, Texto("srv.trabajo_no_existe"))
    return t.a_dict()


@api.post("/api/trabajos/{id_}/cancelar")
def cancelar_trabajo(id_: int):
    return {"cancelado": trabajos.cancelar(id_)}


@api.post("/api/guiones/{slug}/generar")
def generar(slug: str, cuerpo: dict = Body(...)):
    tipo, escena = cuerpo.get("tipo", "video"), cuerpo.get("escena")
    ruta = ruta_guion(slug)
    if tipo == "video":
        try:
            mod_guion.cargar(ruta)
        except ValueError as e:
            raise HTTPException(400, textos.de(e))
        escena = None
    elif tipo in ("prueba", "miniatura"):
        escena = None
    elif tipo in ("voz", "imagen"):
        if not isinstance(escena, int) or escena < 1:
            raise HTTPException(400, Texto("srv.falta_escena"))
    else:
        raise HTTPException(400, Texto("srv.tipo_desconocido", tipo=tipo))
    op = opciones()
    if (tipo == "video" or (tipo == "prueba" and licencia.comercial())) and not licencia.puede_exportar(slug):
        raise HTTPException(402, Texto("srv.licencia_requerida") if licencia.comercial()
                            else Texto("srv.prueba_agotada", n=licencia.PRUEBA_VIDEOS))
    op.marca_prueba = licencia.en_prueba()  # también en "probar mezcla"
    if tipo == "prueba":
        op.limite = PRUEBA_SEG
    if tipo == "miniatura":
        op.solo = "miniatura"
    trabajos.encolar(slug, tipo, escena, op)
    return detalle(slug)


# ---------- imagen o clip propio por escena ----------

EXT_PROPIOS = mod_guion.EXT_IMAGEN + mod_guion.EXT_CLIP


@api.post("/api/guiones/{slug}/propios")
def subir_propio(slug: str, archivo: UploadFile = File(...), destino: str | None = None):
    """Guarda una imagen (jpg, png) o un clip (mp4, mov) en assets/propios/<slug>/ y devuelve su ruta
    relativa ("propios/x.mp4"). La app la anota en la escena ("media") al guardar el guion: así hay
    un solo escritor del JSON y no se pisan ediciones.
    destino=cierre: la tarjeta de cierre propia (solo png o jpg), guardada como cierre.<ext>; la app la
    anota en el campo "cierre" del guion."""
    data = leer(slug)
    if destino not in (None, "cierre"):
        raise HTTPException(400, Texto("srv.tipo_desconocido", tipo=destino))
    try:
        return medios.guardar_subida(imagenes.PROPIOS, data.get("slug") or slug,
                                     archivo.file, archivo.filename or "archivo", nombre_final=destino)
    except ValueError as exc:
        clave = getattr(getattr(exc, "texto", None), "clave", "")
        raise HTTPException(413 if clave == "clip.tamano" else 400, textos.de(exc)) from None
    finally:
        archivo.file.close()



# ---------- traducir ----------

def copiar_salida(origen: str, destino: str):
    """Las imágenes generadas y los archivos propios del guion original: el hash de una imagen no depende
    del slug, así que en la copia salen de caché en vez de volver a pasar por la GPU."""
    copiar_propios(origen, destino)
    src, dst = SALIDA / origen, SALIDA / destino
    if not src.is_dir():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for img in src.glob("img_*.jpg"):
        if not (dst / img.name).exists():
            shutil.copy2(img, dst / img.name)
            n += 1
    return n


def copiar_propios(origen: str, destino: str):
    """La copia de un guion nombra los mismos "propios/<archivo>": necesita los archivos en su carpeta."""
    src = imagenes.carpeta_propios(origen)
    if src.is_dir():
        shutil.copytree(src, imagenes.carpeta_propios(destino), dirs_exist_ok=True)


@api.post("/api/guiones/{slug}/traducir")
def traducir_guion(slug: str, cuerpo: dict = Body(default={})):
    """Copia el guion como <slug>-en con los textos, el título y el título de miniatura en inglés
    (con el proveedor de IA de Ajustes, sin búsqueda web). Los prompts de imagen ya están en inglés y no se tocan."""
    exigir_ia()
    data, nuevo, existe = preparar_traduccion(slug, cuerpo)
    if existe:
        return existe

    def trabajo(t):
        llamada = escritura.nueva_llamada(ajustes())
        t.cancelar = llamada.cancelar
        tr = escritura.traducir_guion(data, llamada, t.log.append)
        return guardar_traduccion(slug, data, nuevo, tr, t.log.append)

    return trabajos.lanzar(nuevo, "traducir", trabajo).a_dict()


def preparar_traduccion(slug: str, cuerpo: dict):
    """(guion original, slug de la copia en inglés, respuesta 409 si ya existe y no se pidió reemplazarla)."""
    data = leer(slug)
    if data.get("idioma", "es") == "en":
        raise HTTPException(400, Texto("srv.ya_ingles"))
    if not data.get("escenas") or not all((e.get("texto") or "").strip() for e in data["escenas"]):
        raise HTTPException(400, Texto("srv.traducir_textos"))
    nuevo = f"{slug[:78]}-en"
    if ruta_guion(nuevo).exists():
        if trabajos.activo(nuevo):
            raise HTTPException(409, Texto("srv.generandose", slug=nuevo))
        if not cuerpo.get("reemplazar"):
            return data, nuevo, Respuesta(status_code=409, content={"existe": nuevo, "titulo": leer(nuevo).get("titulo", "")})
    return data, nuevo, None


def guardar_traduccion(slug: str, data: dict, nuevo: str, tr: dict, log) -> dict:
    """Guarda <slug>-en: copia del original (prompts, semillas, calidad, archivos propios, pausas e imágenes ya
    generadas) con idioma "en", la voz inglesa por defecto de su motor y los textos traducidos, escena por escena.
    tr = {titulo, titulo_miniatura, escenas: [texto, ...]}, de la IA o pegado a mano."""
    doc = json.loads(json.dumps(data))  # copia profunda: escenas con prompt, semilla, calidad, media, pausa
    # el mismo motor que el original, con su voz inglesa por defecto (validada contra la lista del motor)
    motor = data.get("motor_voz") if data.get("motor_voz") in mod_guion.MOTORES else mod_guion.motor_de(data.get("voz", ""))
    doc.update(slug=nuevo, idioma="en", motor_voz=motor, voz=mod_guion.VOCES_DEFECTO[motor]["en"],
               titulo=tr["titulo"], titulo_miniatura=tr["titulo_miniatura"])
    aviso_voz = ajustar_voz(doc, ajustes())
    for e, texto in zip(doc["escenas"], tr["escenas"]):
        e["texto"] = texto
    escribir(nuevo, doc)
    n = copiar_salida(data.get("slug") or slug, nuevo)
    log(Texto.plural("log.traduccion_guardada", n, slug=nuevo))
    if aviso_voz:
        log(aviso_voz)
    return {"slug": nuevo, "avisos": mod_guion.avisos(doc) + ([aviso_voz] if aviso_voz else [])}


@api.post("/api/guiones/{slug}/traducir/manual")
def traducir_manual(slug: str, cuerpo: dict = Body(...)):
    """Traducir sin IA integrada: {texto, reemplazar?}. El texto es la traducción que el usuario hizo fuera
    (Claude, ChatGPT, DeepL...) del guion copiado como texto; las escenas se emparejan por su número.
    422 con los errores y las escenas que faltan, sobran o se repiten si no cuadra con el original."""
    data, nuevo, existe = preparar_traduccion(slug, cuerpo)
    if existe:
        return existe
    try:
        tr = escritura.leer_traduccion_manual(cuerpo.get("texto") or "", len(data["escenas"]))
    except escritura.TraduccionIncompleta as e:
        return Respuesta(status_code=422, content={"errores": e.errores, "faltan": e.faltan, "sobran": e.sobran,
                                                   "repetidas": e.repetidas})
    return Respuesta(content=guardar_traduccion(slug, data, nuevo, tr, lambda _: None))


# ---------- personajes ----------

def _personaje_dict(p: "personajes.Personaje") -> dict:
    def u(f: Path):
        return f"/personajes/{p.nombre}/{f.name}?v={int(f.stat().st_mtime)}"
    prueba = p.carpeta / "prueba.jpg"
    ts = [t for t in trabajos.listar(f"@{p.nombre}") if t.tipo == "personaje"]
    return {
        "nombre": p.nombre, "descripcion": p.descripcion, "fuerza": p.fuerza, "modalidad": p.modalidad,
        "fotos": [{"nombre": f.name, "url": u(f)} for f in p.fotos],
        # la prueba se queda vieja si cambian las fotos, la descripción o la fuerza después de hacerla
        "prueba": {"url": u(prueba), "vieja": (p.carpeta / "prueba.huella").exists()
                   and (p.carpeta / "prueba.huella").read_text() != p.huella()} if prueba.exists() else None,
        "trabajo": ts[-1].a_dict() if ts else None,
    }


@api.get("/api/personajes")
def listar_personajes():
    return {"personajes": [_personaje_dict(p) for p in personajes.listar()],
            "solo_alta": not imagenes.TURBO_CON_PERSONAJE, "segundos_alta": None if vulkan.activo() else imagenes.SEG_ALTA_PERSONAJE,
            "fuerza": {"min": personajes.FUERZA_MIN, "max": personajes.FUERZA_MAX, "defecto": personajes.FUERZA_DEFECTO},
            "max_fotos": personajes.MAX_FOTOS}


def _guardar_fotos(carpeta: Path, fotos: list[UploadFile]) -> list[Path]:
    """Cada foto como JPEG RGB de 1024 px como máximo, con un hash del contenido en el nombre."""
    import hashlib
    import io

    from PIL import Image, ImageOps

    nuevas = []
    for f in fotos:
        crudo = f.file.read()
        try:
            with Image.open(io.BytesIO(crudo)) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1024, 1024))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=94)
        except Exception:
            raise HTTPException(400, Texto("srv.imagen_invalida", nombre=f.filename or "?"))
        datos = buf.getvalue()
        nuevas.append((f"ref_{hashlib.md5(datos).hexdigest()[:10]}.jpg", datos))
    rutas = []
    for nombre, datos in nuevas:
        (carpeta / nombre).write_bytes(datos)
        rutas.append(carpeta / nombre)
    return rutas


def _datos_personaje(descripcion: str, fuerza: float, modalidad: str = "completo", total: int = 1) -> dict:
    if modalidad not in ("foto", "completo"):
        raise HTTPException(400, Texto("pers.modalidad_invalida"))
    if modalidad == "foto" and total != 1:
        raise HTTPException(400, Texto("pers.una_foto_error"))
    if not personajes.FUERZA_MIN <= fuerza <= personajes.FUERZA_MAX:
        raise HTTPException(400, Texto("srv.personaje_fuerza", min=personajes.FUERZA_MIN, max=personajes.FUERZA_MAX))
    return {"descripcion": (descripcion or "").strip()[:300], "fuerza": round(fuerza, 2), "modalidad": modalidad}


@api.post("/api/personajes")
def crear_personaje(nombre: str = Form(...), descripcion: str = Form(""), fuerza: float = Form(personajes.FUERZA_DEFECTO),
                    fotos: list[UploadFile] = File(...), modalidad: str = Form("completo")):
    nombre = nombre.strip().lower()
    if not personajes.nombre_valido(nombre):
        raise HTTPException(400, Texto("srv.personaje_nombre"))
    carpeta = personajes.CARPETA / nombre
    if carpeta.exists():
        raise HTTPException(409, Texto("srv.personaje_existe", nombre=nombre))
    if not 1 <= len(fotos) <= personajes.MAX_FOTOS:
        raise HTTPException(400, Texto("srv.personaje_fotos", max=personajes.MAX_FOTOS))
    datos = _datos_personaje(descripcion, fuerza, modalidad, len(fotos))
    carpeta.mkdir(parents=True)
    try:
        _guardar_fotos(carpeta, fotos)
    except HTTPException:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise
    (carpeta / "personaje.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    return _personaje_dict(personajes.cargar(nombre))


@api.put("/api/personajes/{nombre}")
def editar_personaje(nombre: str, descripcion: str = Form(""), fuerza: float = Form(personajes.FUERZA_DEFECTO),
                     conservar: str = Form("[]"), fotos: list[UploadFile] = File(default=[]), modalidad: str | None = Form(None)):
    """El nombre no se cambia: los guiones lo llaman con @nombre. 'conservar' = JSON con los nombres de las
    fotos que se quedan; las demás se borran. Las nuevas se agregan."""
    p = personajes.cargar(nombre)
    if trabajos.activo(f"@{p.nombre}", "personaje"):
        raise HTTPException(409, Texto("srv.personaje_ocupado", nombre=p.nombre))
    try:
        quedan = {n for n in json.loads(conservar) if isinstance(n, str)}
    except json.JSONDecodeError:
        quedan = set()
    viejas = [f for f in p.fotos if f.name in quedan]
    if not 1 <= len(viejas) + len(fotos) <= personajes.MAX_FOTOS:
        raise HTTPException(400, Texto("srv.personaje_fotos", max=personajes.MAX_FOTOS))
    datos = _datos_personaje(descripcion, fuerza, modalidad if modalidad is not None else p.modalidad, len(viejas) + len(fotos))
    nuevas = _guardar_fotos(p.carpeta, fotos)
    for f in p.fotos:
        if f not in viejas and f not in nuevas:
            f.unlink(missing_ok=True)
    (p.carpeta / "personaje.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    return _personaje_dict(personajes.cargar(p.nombre))


@api.delete("/api/personajes/{nombre}")
def borrar_personaje(nombre: str):
    """A la papelera (papelera/<fecha>-personaje-<nombre>/), como los guiones."""
    p = personajes.cargar(nombre)
    if trabajos.activo(f"@{p.nombre}", "personaje"):
        raise HTTPException(409, Texto("srv.personaje_ocupado", nombre=p.nombre))
    caja = PAPELERA / f"{time.strftime('%Y%m%d-%H%M%S')}-personaje-{p.nombre}"
    caja.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(p.carpeta, caja)
    except OSError as e:
        raise HTTPException(409, Texto("srv.papelera_error", error=e))
    trabajos.olvidar(f"@{p.nombre}")
    return {"ok": True, "papelera": caja.name}


@api.post("/api/personajes/{nombre}/probar")
def probar_personaje(nombre: str):
    p = personajes.cargar(nombre)
    if p.modalidad == "foto":
        raise HTTPException(400, Texto("pers.foto_sin_prueba"))
    trabajos.encolar(f"@{p.nombre}", "personaje", None, opciones())
    return _personaje_dict(p)


# ---------- borrar (a la papelera) y duplicar ----------

PAPELERA = rutas.PAPELERA


@api.post("/api/guiones/{slug}/borrar")
def borrar_guion(slug: str):
    """No elimina nada: mueve el guion y su carpeta de salida a papelera/<fecha>-<slug>/.
    Devuelve el id para deshacerlo con /api/papelera/{id}/restaurar."""
    data = leer(slug)
    if trabajos.activo(slug):
        raise HTTPException(409, Texto("srv.borrar_generando"))
    dest = SALIDA / (data.get("slug") or slug)
    id_ = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    while (PAPELERA / id_).exists():  # el mismo slug borrado dos veces en el mismo segundo
        id_ += "-b"
    caja = PAPELERA / id_
    caja.mkdir(parents=True)
    try:
        if dest.exists():
            shutil.move(dest, caja / "salida")  # primero la carpeta: es lo que puede estar en uso
        shutil.move(ruta_guion(slug), caja / f"{slug}.json")
    except OSError as e:
        if (caja / "salida").exists() and not dest.exists():
            shutil.move(caja / "salida", dest)
        shutil.rmtree(caja, ignore_errors=True)
        raise HTTPException(409, Texto("srv.papelera_error", error=e))
    olvidar(slug, dest)
    return {"papelera": id_, "slug": slug}


def olvidar(slug: str, dest: Path):
    """Lo que el servidor recuerda de un guion que ya no está en guiones/: sus trabajos en cola (fallarían
    al no encontrar el archivo) y las duraciones de mp4 en caché. No hay índice ni lista de recientes en
    disco: la Biblioteca recorre guiones/ en cada consulta."""
    trabajos.olvidar(slug)
    for clave in [k for k in _duraciones if Path(k[0]).parent == dest]:
        _duraciones.pop(clave, None)


@api.post("/api/papelera/{id_}/restaurar")
def restaurar_guion(id_: str):
    caja = PAPELERA / id_
    if not SLUG_OK.match(id_) or not caja.is_dir():
        raise HTTPException(404, Texto("srv.no_en_papelera"))
    archivo = next(caja.glob("*.json"), None)
    if not archivo:
        raise HTTPException(404, Texto("srv.papelera_sin_guion"))
    slug = archivo.stem
    data = json.loads(archivo.read_text(encoding="utf-8"))
    dest = SALIDA / (data.get("slug") or slug)
    if ruta_guion(slug).exists() or ((caja / "salida").exists() and dest.exists()):
        raise HTTPException(409, Texto("srv.ya_hay", slug=slug))
    if (caja / "salida").exists():
        shutil.move(caja / "salida", dest)
    shutil.move(archivo, ruta_guion(slug))
    shutil.rmtree(caja, ignore_errors=True)
    return {"slug": slug}


@api.post("/api/guiones/{slug}/duplicar")
def duplicar_guion(slug: str):
    """Copia solo el guion (no la salida) con el primer slug libre: <slug>-2, -3..."""
    data = leer(slug)
    n = 2
    while ruta_guion(nuevo := f"{slug[:76]}-{n}").exists():
        n += 1
    escribir(nuevo, {**data, "slug": nuevo})
    copiar_propios(slug, nuevo)
    return {"slug": nuevo}


# ---------- espacio en disco ----------

# lo que "Limpiar" deja en salida/<slug>/ de un video terminado. Las imágenes se quedan: pesan ~3 MB por
# video y regenerarlas cuesta minutos de GPU si luego se edita una escena.
SE_QUEDA = ("miniatura.jpg", "subtitulos.srt")


def _peso(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    # sin seguir enlaces y sin repetir archivos: la caché de Hugging Face enlaza cada modelo a hub/blobs
    total, vistos = 0, set()
    for raiz, _, archivos in os.walk(p):
        for a in archivos:
            try:
                st = os.stat(os.path.join(raiz, a), follow_symlinks=False)
            except OSError:
                continue
            if not os.path.islink(os.path.join(raiz, a)) and (st.st_dev, st.st_ino) not in vistos:
                vistos.add((st.st_dev, st.st_ino))
                total += st.st_size
    return total


def _sobrantes() -> list[Path]:
    """Clips, audios y demás intermedios de los videos que ya tienen su mp4 final."""
    res = []
    for d in SALIDA.iterdir() if SALIDA.exists() else []:
        if not d.is_dir() or not (d / f"{d.name}.mp4").exists():
            continue
        quedan = {f"{d.name}.mp4", *SE_QUEDA}
        # propios/ nunca (archivos subidos, no regenerables; los de ahora ya viven en assets/propios/)
        res += [f for f in d.iterdir() if f.name not in quedan and f.name != "propios"
                and not (f.is_file() and f.name.startswith("img_"))]
    return res


@api.get("/api/espacio")
def espacio():
    modelos = rutas.MODELOS
    externos = set()
    if vulkan.activo():
        externos = {p for k in vulkan.PERFILES if (p := vulkan.ruta_modelo(k))
                    and p.is_file() and not p.is_relative_to(modelos)}
    else:
        from huggingface_hub.constants import HF_HUB_CACHE

        modelos = Path(HF_HUB_CACHE)
    return {
        "salida": _peso(SALIDA), "papelera": _peso(PAPELERA) if PAPELERA.exists() else 0,
        "modelos": _peso(modelos) + sum(_peso(p) for p in externos), "ruta_modelos": str(modelos),
        "liberable": sum(_peso(f) for f in _sobrantes()) + (_peso(PAPELERA) if PAPELERA.exists() else 0),
    }


@api.post("/api/espacio/limpiar")
def limpiar():
    """Vacía papelera/ y deja los videos terminados solo con mp4, miniatura, .srt e imágenes."""
    if any(t.estado in trabajos.ACTIVOS and t.tipo != "escribir" for t in trabajos.listar()):
        raise HTTPException(409, Texto("srv.cola_limpiar"))
    imagenes.migrar_propios(SALIDA)
    antes, errores = espacio()["liberable"], []
    for f in _sobrantes() + ([PAPELERA] if PAPELERA.exists() else []):
        try:
            shutil.rmtree(f) if f.is_dir() else f.unlink()
        except OSError as e:
            errores.append(f"{f.relative_to(rutas.DATOS)}: {e.strerror}")
    return {"liberado": antes - espacio()["liberable"], "errores": errores, **espacio()}


@api.post("/api/guiones/{slug}/abrir")
def abrir_carpeta(slug: str):
    data = leer(slug)
    dest = SALIDA / (data.get("slug") or slug)
    if not hasattr(os, "startfile"):
        raise HTTPException(400, Texto("srv.solo_windows"))
    carpeta_local = dest if dest.exists() else GUIONES
    if rutas.LINUX:
        if not shutil.which("xdg-open"):
            raise HTTPException(503, "xdg-open no está disponible.")
        subprocess.Popen(["xdg-open", str(carpeta_local)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.startfile(carpeta_local)
    return {"ok": True, "carpeta": str(dest)}


# ---------- catálogos, ajustes, sistema ----------

@api.get("/api/voces")
async def voces():
    """Las voces de los dos motores; cada una con su "motor". Sin internet, las de edge-tts son una lista mínima."""
    edge = await asyncio.to_thread(voz.voces_edge)
    if edge is None:
        edge = [{"id": v, "genero": "", "locale": v[:5], "motor": "edge"} for v in VOCES_RESPALDO]
    return edge + voz.voces_kokoro()


def catalogo_musica() -> dict:
    pista = lambda p: {"nombre": p.stem, "ruta": p.relative_to(rutas.DATOS).as_posix(), "url": url_musica(p)}
    return {
        "moods": [{"id": m, "pistas": [pista(p) for p in musica.pistas(m)]} for m in musica.MOODS],
        # mp3 sueltos en assets/musica/ (de antes de las carpetas por mood)
        "sueltas": [pista(p) for p in sorted(MUSICA.iterdir()) if p.suffix.lower() in musica.EXTENSIONES],
    }


@api.get("/api/voz/prueba")
def probar_voz(id: str, rate: str = "+0%"):
    if not re.fullmatch(r"(es|en)-[A-Z]{2}-[A-Za-z0-9]+Neural", id) or not re.fullmatch(r"[+-]\d{1,2}%", rate):
        raise HTTPException(400, "Selecciona una voz Edge TTS y una velocidad válida.")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as archivo:
        ruta = Path(archivo.name)
    try:
        texto = ("Esta es una prueba de voz para tu próximo video." if id.startswith("es-") else
                 "This is a voice preview for your next video.")
        voz._edge(texto, id, ruta, rate)
        return Response(ruta.read_bytes(), media_type="audio/mpeg")
    except Exception as exc:
        logging.getLogger(__name__).info("Falló el monitor Edge TTS: %s", type(exc).__name__)
        raise HTTPException(503, "Edge TTS no pudo sintetizar la prueba.") from None
    finally:
        ruta.unlink(missing_ok=True)


@api.get("/api/marca/cierre")
def vista_cierre(formato: str = "short", nombre: str = ""):
    from pipeline.miniatura import TAMANO

    if formato not in TAMANO:
        raise HTTPException(400, "Formato inválido.")
    return FileResponse(marca.tarjeta_cierre(TAMANO[formato], nombre), media_type="image/png")


@api.get("/api/musica")
def ver_musica():
    return catalogo_musica()


@api.post("/api/musica/subir")
def subir_musica(mood: str = Form(...), archivos: list[UploadFile] = File(...)):
    guardadas, errores = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for f in archivos:
            origen = Path(tmp) / f"subida{Path(f.filename or '').suffix.lower()}"
            with open(origen, "wb") as out:
                shutil.copyfileobj(f.file, out)
            try:
                guardadas.append(musica.guardar(origen, f.filename or "pista", mood).relative_to(rutas.DATOS).as_posix())
            except ValueError as e:
                errores.append(textos.de(e))
    if not guardadas and errores:
        raise HTTPException(400, Unidos(errores, "; "))
    return {"guardadas": guardadas, "errores": errores, **catalogo_musica()}


@api.get("/api/subtitulos")
def catalogo_subtitulos():
    return {**subtitulos.catalogo(), "marca": marca.catalogo_marca()}


@api.post("/api/subtitulos/preview")
def preview_subtitulos(cuerpo: dict = Body(...)):
    """Un cuadro de la escena 1 con el estilo pedido (sin guardar nada). Sin slug, usa el guion más
    reciente que ya tenga la imagen de la escena 1."""
    a = ajustes()
    demo, rate = bool(a["demo"]), a["rate"]
    candidatos = [cuerpo["slug"]] if cuerpo.get("slug") else [t["slug"] for t in listar_guiones()]
    for slug in candidatos:
        try:
            g = mod_guion.desde_dict(leer(slug), estricto=False)
        except (HTTPException, ValueError, KeyError):
            continue
        if not g.escenas:
            continue
        dest, e = carpeta(g), g.escenas[0]
        img = imagenes.vista(g, e, dest, demo, a["calidad"])[0]
        if not img:
            continue
        meta = voz.buscar(g, e, dest, demo, rate) if e.texto else None
        estilo = subtitulos.Estilo.desde(
            cuerpo["estilo_global"] if "estilo_global" in cuerpo else a["estilo_subtitulos"],
            cuerpo.get("estilo_guion") if "estilo_guion" in cuerpo else g.subtitulos,
        )
        # la marca de agua, como saldría en el video (si está activada)
        marca_guion = cuerpo.get("marca_guion") if "marca_guion" in cuerpo else (g.marca if g.con_marca else False)
        con_marca = (cuerpo["marca_agua"] if isinstance(cuerpo.get("marca_agua"), bool) else bool(a["marca_agua"])) \
            and marca_guion is not False  # "Sin marca" en este video
        em = marca.EstiloMarca.desde(
            cuerpo["marca_global"] if "marca_global" in cuerpo else a["estilo_marca"],
            marca_guion or {},
        ) if con_marca and LOGO_PERSONAL.is_file() else None
        t0 = time.time()
        try:
            jpg = subtitulos.previsualizar(g, img, json.loads(meta.read_text(encoding="utf-8")) if meta else None,
                                           dest, estilo, em, logo_marca=LOGO_PERSONAL if em else None)
        except (RuntimeError, ValueError) as err:
            raise HTTPException(400, textos.de(err))
        return {"url": f"{url(jpg)}?v={time.time_ns()}", "slug": slug, "segundos": round(time.time() - t0, 2),
                "formato": g.formato}
    raise HTTPException(400, Texto("srv.preview_sin_imagen"))


def exigir_ia():
    if not escritura.disponible(ajustes()):
        raise HTTPException(400, Texto("srv.sin_ia"))


def ajustes_publicos(a: dict) -> dict:
    """Los ajustes para la interfaz: las API keys nunca salen del servidor (se ve desde el celular por la
    red local), solo sus últimos 4 caracteres. Más el estado del proveedor de IA."""
    def enmascarar(k: str) -> str:
        return f"…{k[-4:]}" if k else ""
    pub = {k: v for k, v in a.items() if k not in CLAVES_API}
    pub["logo_marca"] = f"/api/marca/logo?v={LOGO_PERSONAL.stat().st_mtime_ns}" if LOGO_PERSONAL.is_file() else None
    pub["marca_agua"] = bool(a["marca_agua"]) and bool(pub["logo_marca"])
    pub["kokoro_instalado"] = voz_local.disponible()
    pub["version"] = VERSION
    pub["url_apoyo"] = "" if rutas.LINUX else URL_APOYO
    pub["instalado"] = rutas.INSTALADO
    pub["linux"] = rutas.LINUX
    pub["auth"] = auth.activa()
    url = config.FEEDBACK_URL.strip()
    pub["feedback_url"] = url if re.match(r"^https?://", url) else ""  # solo lectura: el PUT no la guarda
    pub["ia"] = {
        "disponible": escritura.disponible(a),
        "claude_instalado": escritura.claude_instalado(),
        "claves": {p: enmascarar(escritura.clave_api(a, p)) for p in escritura.VAR_ENTORNO},
        "de_entorno": {p: not a.get(f"{p}_api_key") and bool(escritura.clave_api(a, p)) for p in escritura.VAR_ENTORNO},
    }
    return pub


@api.get("/api/ajustes")
def ver_ajustes():
    return ajustes_publicos(ajustes())


@api.get("/api/marca/logo")
def ver_logo_personal():
    if not LOGO_PERSONAL.is_file():
        raise HTTPException(404, Texto("marca.sin_logo"))
    return FileResponse(LOGO_PERSONAL, media_type="image/png")


@api.post("/api/marca/logo")
def subir_logo_personal(archivo: UploadFile = File(...)):
    try:
        marca_personal.guardar(archivo.file, LOGO_PERSONAL)
    except ValueError:
        raise HTTPException(400, Texto("marca.archivo_invalido")) from None
    except OSError:
        raise HTTPException(503, Texto("marca.error_guardar")) from None
    return {"url": f"/api/marca/logo?v={LOGO_PERSONAL.stat().st_mtime_ns}"}


@api.put("/api/ajustes")
def guardar_ajustes(nuevos: dict = Body(...)):
    """Una API key solo cambia si llega con texto (o vacía, para quitarla); si no viene, se queda la guardada."""
    nuevos = {k: (v.strip() if isinstance(v, str) else v) for k, v in nuevos.items()}
    a = {**ajustes(), **{k: v for k, v in nuevos.items() if k in AJUSTES_BASE}}
    if a["proveedor_ia"] not in escritura.PROVEEDORES:
        raise HTTPException(400, Texto("srv.proveedor_ia", valor=a["proveedor_ia"]))
    if a["motor_voz"] not in mod_guion.MOTORES:
        raise HTTPException(400, Texto("srv.motor_voz", valor=a["motor_voz"]))
    if a["motor_voz"] == "kokoro" and not voz_local.disponible():
        raise HTTPException(400, Texto("err.kokoro_no_instalado"))
    if not voz.voz_valida(a["voz_kokoro"], "es", "kokoro"):
        a["voz_kokoro"] = mod_guion.VOCES_DEFECTO["kokoro"]["es"]
    if not re.match(r"^[+-]\d{1,2}%$", str(a["rate"])):
        raise HTTPException(400, Texto("srv.rate"))
    if a["calidad"] not in imagenes.CALIDADES:
        raise HTTPException(400, Texto("err.calidad", calidad=a["calidad"], opciones=" / ".join(imagenes.CALIDADES)))
    a["estilo_subtitulos"] = subtitulos.Estilo.desde(a["estilo_subtitulos"]).a_dict()
    a["estilo_marca"] = marca.EstiloMarca.desde(a["estilo_marca"]).a_dict()
    a["cierre_tarjeta"] = bool(a["cierre_tarjeta"])
    a["cierre_nombre"] = " ".join(str(a["cierre_nombre"] or "").split())[:marca.CIERRE_NOMBRE_MAX]
    fc = a["frase_cierre"] if isinstance(a.get("frase_cierre"), dict) else {}
    a["frase_cierre"] = {k: str(fc.get(k) or "").strip()[:150] for k in ("es", "en")}
    try:
        credenciales.guardar(AJUSTES, a, CLAVES_API)
    except credenciales.ErrorCredenciales:
        raise HTTPException(503, "No se pudieron proteger las credenciales del servidor. No se guardaron cambios.") from None
    return ajustes_publicos(a)


def _gpu() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
        nombre, usada, total = (x.strip() for x in out.split(","))
        return {"nombre": nombre, "vram_usada": int(usada), "vram_total": int(total)}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


# ---------- setup de la primera ejecución (app/preparar.py) ----------

@api.get("/api/setup")
def ver_setup(fresco: bool = False):
    if rutas.LINUX:
        raise HTTPException(404, "Consulta el estado del equipo en Ajustes.")
    if fresco:
        preparar.equipo(fresco=True)
    return preparar.PREPARADOR.estado()


@api.post("/api/setup/iniciar")
def iniciar_setup(cuerpo: dict = Body(...)):
    if rutas.LINUX:
        raise HTTPException(404)
    perfil = cuerpo.get("perfil", "completo")
    if perfil not in ("voz", "esencial", "completo"):
        raise HTTPException(400, Texto("srv.setup_perfil"))
    try:
        preparar.PREPARADOR.iniciar(perfil)
    except ValueError as exc:
        raise HTTPException(400, Texto(str(exc)) if str(exc).startswith("srv.setup_") else Texto("srv.setup_perfil")) from None
    return preparar.PREPARADOR.estado()


@api.post("/api/setup/pausar")
def pausar_setup():
    if rutas.LINUX:
        raise HTTPException(404)
    preparar.PREPARADOR.pausar()
    return {"ok": True}


@api.post("/api/setup/verificar")
def verificar_setup():
    """En segundo plano: carga los modelos (unos minutos la primera vez). La interfaz sigue el estado."""
    if rutas.LINUX:
        raise HTTPException(404)
    p = preparar.PREPARADOR
    if p.fase in ("descargando", "verificando"):
        raise HTTPException(409, Texto("srv.setup_ocupado"))
    if licencia.comercial() and not p.componentes_completos():
        raise HTTPException(409, Texto("srv.setup_incompleto"))
    p.fase = "verificando"
    threading.Thread(target=p.verificar, daemon=True).start()
    return p.estado()


@api.post("/api/setup/visto")
def setup_visto():
    if rutas.LINUX:
        raise HTTPException(404)
    if licencia.comercial() and not preparar.PREPARADOR.completo():
        raise HTTPException(409, Texto("srv.setup_incompleto"))
    preparar.PREPARADOR.guardar(visto=True)
    return {"ok": True}


@api.get("/licencias/ffmpeg")
def licencia_ffmpeg():
    """La licencia de ffmpeg que bajó el Setup (GPL v3). Si aún no se bajó, el texto de la GPL v3 en gnu.org."""
    lic = rutas.FFMPEG / "LICENSE.txt"
    if lic.exists():
        return FileResponse(lic, media_type="text/plain; charset=utf-8")
    return RedirectResponse("https://www.gnu.org/licenses/gpl-3.0.txt")


@api.get("/licencias/espeak-ng")
def licencia_espeak():
    """espeak-ng (GPL v3) viene en espeakng_loader, que instala el Setup: Kokoro lo usa para el español.
    El texto de la licencia va en licenses\\ del paquete (build/licencias/); en desarrollo, en build/."""
    for lic in (rutas.APP / "licenses" / "espeak-ng-GPL-3.0.txt", rutas.APP / "build" / "licencias" / "espeak-ng-GPL-3.0.txt"):
        if lic.is_file():
            return FileResponse(lic, media_type="text/plain; charset=utf-8")
    return RedirectResponse("https://www.gnu.org/licenses/gpl-3.0.txt")


@api.post("/api/apoyar")
def apoyar():
    """"Apoyar el proyecto ☕" (Ajustes): abre URL_APOYO en el navegador del sistema. URL fija, sin parámetros."""
    webbrowser.open(URL_APOYO)
    return {"ok": True}


# ---------- licencia (app/licencia.py) ----------
# Versión gratuita: solo responden si licencia.LICENCIA_REQUERIDA está encendida (versión Pro).

def _solo_con_licencia():
    if not licencia.LICENCIA_REQUERIDA:
        raise HTTPException(404)


@api.get("/api/licencia")
def ver_licencia():
    if not licencia.LICENCIA_REQUERIDA:
        return licencia.estado()
    return licencia.estado_equipo()


@api.post("/api/licencia")
def activar_licencia(cuerpo: dict = Body(...)):
    _solo_con_licencia()
    try:
        licencia.activar(str(cuerpo.get("clave") or ""))
    except ValueError as e:
        raise HTTPException(400, Texto(f"srv.licencia_{e}" if str(e) in ("formato", "firma", "version", "equipo", "dispositivo") else "srv.licencia_formato"))
    return licencia.estado()


@api.post("/api/licencia/quitar")
def quitar_licencia():
    _solo_con_licencia()
    licencia.quitar()
    return licencia.estado()


@api.post("/api/licencia/visto")
def licencia_visto():
    _solo_con_licencia()
    licencia.marcar_visto()
    return licencia.estado()


@api.get("/api/sistema")
def sistema():
    return {
        "vulkan": vulkan.estado() if vulkan.activo() else None,
        "gpu": None if vulkan.activo() else _gpu(),
        "torch": importlib.util.find_spec("torch") is not None,
        "modelo_cargado": imagenes.modelo_cargado(),
        "descarga": imagenes.descarga,  # {modelo, gb, total} mientras baja un modelo
        "ip_lan": None,  # acceso exclusivamente local
        "puerto": PUERTO,
        "cola": [t.a_dict() for t in trabajos.listar() if t.estado in trabajos.ACTIVOS],
        "terminados": terminados(),
        "licencia": licencia.estado(),
    }


def terminados(minutos: float = 15) -> list[dict]:
    """Videos y pruebas de personaje que acabaron hace poco: la app avisa aunque estés en otra pestaña."""
    res = []
    for t in trabajos.listar():
        if t.tipo not in ("video", "personaje") or t.estado not in ("listo", "error") or not t.fin                 or time.time() - t.fin > minutos * 60:
            continue
        titulo = t.slug.removeprefix("@")
        if t.tipo == "video":
            try:
                titulo = json.loads(ruta_guion(t.slug).read_text(encoding="utf-8")).get("titulo") or t.slug
            except (OSError, ValueError):
                pass
        res.append({"id": t.id, "tipo": t.tipo, "slug": t.slug, "estado": t.estado, "titulo": titulo,
                    "error": t.error})
    return res


@api.post("/api/sistema/liberar")
def liberar_gpu():
    trabajos.encolar("_sistema", "liberar")
    return {"ok": True}


@api.post("/api/prompt/mejorar")
def mejorar_prompt(cuerpo: dict = Body(...)):
    exigir_ia()
    texto = cuerpo.get("texto")
    if not isinstance(texto, str) or not 1 <= len(texto.strip()) <= 2000:
        raise HTTPException(400, "Escribe un prompt de entre 1 y 2000 caracteres.")
    instruccion = ("Improve this prompt for Stable Diffusion. Preserve the subject and setting, describe the composition, "
                   "lighting and style concretely, use English, and keep it under 65 words. "
                   'Return only a JSON object with one string field named "prompt". Input: ')
    try:
        respuesta = escritura.nueva_llamada(ajustes()).correr(instruccion + json.dumps(texto), buscar=False)
        mejorado = mod_guion.desde_texto(respuesta)["prompt"]
        if not isinstance(mejorado, str) or not mejorado.strip() or len(mejorado) > 2000:
            raise ValueError("prompt")
    except Exception:
        raise HTTPException(502, "El proveedor no devolvió un prompt válido.") from None
    return {"prompt": mejorado.strip()}


@api.post("/api/prompt/clip")
def prompt_clip(cuerpo: dict = Body(...)):
    """Estudio de imágenes: ¿el prompt pasa del límite de CLIP? {"aviso": null} si cabe."""
    texto = cuerpo.get("texto") if isinstance(cuerpo, dict) else None
    if not isinstance(texto, str) or len(texto) > 4000:
        raise HTTPException(400, Texto("est.err_prompt"))
    return imagenes.presupuesto_visual(texto)


@api.post("/api/cerrar")
def cerrar_app(cuerpo: dict = Body(default={})):
    """"Cerrar Dcrypto Studio". Con trabajos corriendo o en cola, sin {"forzar": true}, responde 409 con la
    lista para que la interfaz pida confirmación. Si no: cancela la cola, suelta los modelos y cierra el
    servidor (y, con el lanzador, el lanzador y ffmpeg) un instante después de responder."""
    pendientes = trabajos.pendientes()
    if pendientes and not (isinstance(cuerpo, dict) and cuerpo.get("forzar") is True):
        return Respuesta(status_code=409, content={
            "detail": Texto("srv.cerrar_pendientes", n=len(pendientes)),
            "pendientes": [{"id": t.id, "tipo": t.tipo, "slug": t.slug, "estado": t.estado} for t in pendientes]})
    return cierre.cerrar()


# ---------- archivos ----------

rutas.crear_estructura()
musica.preparar_catalogo()
api.mount("/salida", StaticFiles(directory=SALIDA), name="salida")
if n := imagenes.migrar_propios(SALIDA):
    print(f"propios: {n} archivos movidos de salida/<slug>/propios/ a assets/propios/<slug>/", flush=True)
imagenes.PROPIOS.mkdir(parents=True, exist_ok=True)
api.mount("/propios", StaticFiles(directory=imagenes.PROPIOS), name="propios")
api.mount("/musica", StaticFiles(directory=MUSICA), name="musica")
api.mount("/fuentes", StaticFiles(directory=subtitulos.FUENTES_DIR), name="fuentes")
api.mount("/logo", StaticFiles(directory=LOGOS), name="logo")  # icono de la app (logo_dcrypto_studio*)
api.mount("/help", StaticFiles(directory=ESTATICOS / "help"), name="help")  # capturas de la pestaña Ayuda
personajes.CARPETA.mkdir(parents=True, exist_ok=True)
api.mount("/personajes", StaticFiles(directory=personajes.CARPETA), name="personajes")

SIN_CACHE = {"Cache-Control": "no-cache"}


@api.get("/")
def inicio():
    # la versión va escrita en la página: el pie la muestra desde el primer pintado (antes parpadeaba "v ·")
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", VERSION, 1)
    return HTMLResponse(html, headers=SIN_CACHE)


@api.get("/i18n.js")
def diccionario():
    return FileResponse(ESTATICOS / "i18n.js", media_type="text/javascript", headers=SIN_CACHE)


@api.get("/favicon.ico")
def favicon():
    return FileResponse(LOGOS / "compresso.png", media_type="image/png")


@api.get("/estudio.js")
def javascript_estudio():
    return FileResponse(ESTATICOS / "estudio.js", media_type="text/javascript", headers=SIN_CACHE)


PUERTO = config.PUERTO


class SinConexionCortada(logging.Filter):
    """En Windows, cuando el navegador cierra una conexión (recargar, cambiar de video, cortar un <video>),
    asyncio registra "ConnectionResetError [WinError 10054]" con traceback. Es normal y tapa los errores
    reales: se descarta."""

    def filter(self, registro: logging.LogRecord) -> bool:
        exc = registro.exc_info[1] if registro.exc_info else None
        if isinstance(exc, ConnectionResetError) or (isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10054):
            return False
        return "WinError 10054" not in registro.getMessage()


def silenciar_conexiones_cortadas():
    for nombre in ("asyncio", "uvicorn.error"):
        logging.getLogger(nombre).addFilter(SinConexionCortada())


def ocupado(puerto: int) -> bool:
    """Otro proceso escucha en el puerto (en 127.0.0.1 o en todas las interfaces)."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", puerto)) == 0:
            return True
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", puerto))
        except OSError:
            return True
    return False


def main():
    global PUERTO
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--puerto", type=int, default=config.PUERTO)
    ap.add_argument("--sin-navegador", action="store_true")
    a = ap.parse_args()
    PUERTO = a.puerto
    if ocupado(PUERTO):
        # lo normal: la app instalada (que también usa 8765) está abierta. No abrir el navegador en su puerto.
        sys.exit(f"\nEl puerto {PUERTO} ya está en uso (¿hay otra instancia abierta?).\n"
                 f"Arranca el de desarrollo en otro:  python app/server.py --puerto 8780\n")

    trabajos.iniciar(ruta_guion)
    silenciar_conexiones_cortadas()
    print(f"\nCompressOS Studio\n  en esta laptop:  http://127.0.0.1:{PUERTO}")
    print("  Ctrl+C para cerrar\n")
    if not a.sin_navegador:
        threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{PUERTO}"]).start()
    uvicorn.run(api, host="127.0.0.1", port=PUERTO, log_level="warning")


if __name__ == "__main__":
    main()
