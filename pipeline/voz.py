"""Voz por escena + tiempos por palabra para los subtítulos. Dos motores (el guion guarda cuál usa):
- "edge": edge-tts (gratis, voces neurales de Microsoft, necesita internet). Da los tiempos por palabra.
- "kokoro": Kokoro-82M local, sin internet (pipeline/voz_local.py); tiempos por alineación con whisper.
La voz se valida contra la lista del motor para el idioma del guion: si no existe, se usa la de por defecto."""
import asyncio
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import voz_local
from .guion import VOCES_DEFECTO, Escena, Guion, motor_de
from .textos import ErrorEjecucion, Texto, decir

# WordBoundary de edge-tts viene en unidades de 100 ns
_TICK = 10_000_000


def duracion(ruta: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(ruta)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# ---------- catálogo y validación ----------

_cat_edge: list[dict] | None = None
_edge_fallo = 0.0  # sin internet: no se reintenta en cada llamada
_avisadas: set[tuple[str, str]] = set()


def _correr(coro, timeout: float):
    """Corre una corrutina en un hilo propio: sirve también desde código que ya está en un event loop."""
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, asyncio.wait_for(coro, timeout)).result()


def voces_edge() -> list[dict] | None:
    """Las voces de edge-tts en español e inglés (de internet; se guardan en memoria). None si no hay conexión."""
    global _cat_edge, _edge_fallo
    if _cat_edge is None and time.time() - _edge_fallo > 60:
        import edge_tts

        try:
            todas = _correr(edge_tts.list_voices(), 10)
        except Exception:
            _edge_fallo = time.time()
            return None
        _cat_edge = sorted(({"id": v["ShortName"], "genero": v["Gender"], "locale": v["Locale"], "motor": "edge"}
                        for v in todas if v["Locale"][:3] in ("es-", "en-")), key=lambda v: v["id"])
    return _cat_edge


def voces_kokoro() -> list[dict]:
    return [{"id": k, "genero": g, "locale": loc, "motor": "kokoro"} for k, (loc, g) in sorted(voz_local.VOCES.items())]


def voz_valida(voz: str, idioma: str, motor: str) -> bool | None:
    """¿Existe esa voz en ese motor para ese idioma? None = no se sabe (edge-tts sin internet)."""
    if motor_de(voz) != motor:
        return False
    if motor == "kokoro":
        return voz in voz_local.VOCES and voz_local.VOCES[voz][0].startswith(idioma)
    lista = voces_edge()
    if lista is None:
        return None
    return any(v["id"] == voz and v["locale"].startswith(f"{idioma}-") for v in lista)


def corregir(voz: str, idioma: str, motor: str) -> tuple[str, Texto | None]:
    """(voz que se usará, aviso). Una voz que no existe para ese idioma y motor se cambia por la de por defecto."""
    if voz_valida(voz, idioma, motor) is False:
        nueva = VOCES_DEFECTO[motor][idioma]
        return nueva, Texto("aviso.voz_cambiada", voz=voz or "—", nueva=nueva, idioma=Texto(f"lang.{idioma}"))
    return voz, None


def efectiva(g: Guion, demo: bool = False, avisar: bool = False) -> Guion:
    """El guion con una voz que existe (la del guion si es válida). Con avisar, lo dice una vez por guion y voz."""
    if demo:
        return g
    v, aviso = corregir(g.voz, g.idioma, g.motor_voz)
    if aviso and avisar and (g.slug, g.voz) not in _avisadas:
        _avisadas.add((g.slug, g.voz))
        decir("aviso.voz_cambiada", voz=g.voz or "—", nueva=v, idioma=Texto(f"lang.{g.idioma}"))
    return replace(g, voz=v) if v != g.voz else g


# ---------- síntesis ----------

async def _sintetizar(texto: str, voz: str, mp3: Path, rate: str) -> list[dict]:
    import edge_tts

    # edge-tts >= 7 manda SentenceBoundary por defecto; los subtítulos necesitan palabra por palabra
    com = edge_tts.Communicate(texto, voz, rate=rate, boundary="WordBoundary")
    palabras = []
    with open(mp3, "wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                palabras.append({
                    "texto": chunk["text"],
                    "inicio": chunk["offset"] / _TICK,
                    "fin": (chunk["offset"] + chunk["duration"]) / _TICK,
                })
    return palabras


def _silencio(mp3: Path, segundos: float):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-t", f"{segundos:.2f}", "-q:a", "9", str(mp3)],
        check=True,
    )


def hash_voz(g: Guion, e: Escena, demo: bool = False, rate: str = "+0%") -> str:
    """Las voces de Kokoro y de edge-tts tienen nombres distintos: el motor ya va en g.voz."""
    return hashlib.md5(f"{e.texto}|{efectiva(g, demo).voz}|{rate}|{demo}".encode()).hexdigest()[:8]


def _edge(texto: str, voz: str, mp3: Path, rate: str) -> list[dict]:
    import time

    import edge_tts

    # el servicio de Microsoft a veces corta la primera conexión ("No audio was received") aunque la voz
    # y la red estén bien: hasta 3 intentos antes de dar el error (que entonces sí es voz inválida o sin red)
    for intento in range(3):
        try:
            return asyncio.run(_sintetizar(texto, voz, mp3, rate))
        except edge_tts.exceptions.NoAudioReceived:
            mp3.unlink(missing_ok=True)
            if intento < 2:
                time.sleep(1.5 * (intento + 1))
    raise ErrorEjecucion(Texto("err.voz_sin_audio", voz=voz))


def _kokoro(texto: str, voz: str, mp3: Path, rate: str) -> list[dict]:
    if not voz_local.disponible():
        raise ErrorEjecucion(Texto("err.kokoro_no_instalado"))
    return voz_local.sintetizar(texto, voz, mp3, rate)


def buscar(g: Guion, e: Escena, carpeta: Path, demo: bool = False, rate: str = "+0%") -> Path | None:
    """El .json de la voz ya generada para este texto, aunque sea de otra posición (escenas reordenadas)."""
    h = hash_voz(g, e, demo, rate)
    propio = carpeta / f"voz_{e.idx:02d}_{h}.json"
    for meta in [propio, *carpeta.glob(f"voz_*_{h}.json")]:
        if meta.exists() and meta.with_suffix(".mp3").exists():
            return meta
    return None


def generar_escena(g: Guion, e: Escena, carpeta: Path, demo: bool = False, rate: str = "+0%") -> tuple[dict, bool]:
    """Voz de una escena. Devuelve (info, venia_de_cache).
    El nombre lleva un hash de texto+voz+velocidad: si cambia algo, se regenera solo esta escena."""
    carpeta.mkdir(parents=True, exist_ok=True)
    g = efectiva(g, demo, avisar=True)
    h = hash_voz(g, e, demo, rate)
    mp3 = carpeta / f"voz_{e.idx:02d}_{h}.mp3"
    meta = carpeta / f"voz_{e.idx:02d}_{h}.json"

    previo = buscar(g, e, carpeta, demo, rate)
    if previo == meta:
        return json.loads(meta.read_text(encoding="utf-8")), True
    if previo:
        shutil.copyfile(previo.with_suffix(".mp3"), mp3)
        info = json.loads(previo.read_text(encoding="utf-8"))
        info.update(idx=e.idx, mp3=mp3.name)
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        return info, True

    if demo:
        # ~2.6 palabras por segundo, repartidas uniformemente
        n = len(e.texto.split())
        dur = max(2.0, n / 2.6)
        _silencio(mp3, dur)
        paso = dur / n
        palabras = [
            {"texto": w, "inicio": i * paso, "fin": (i + 1) * paso}
            for i, w in enumerate(e.texto.split())
        ]
    else:
        t0 = time.time()
        palabras = (_kokoro if g.motor_voz == "kokoro" else _edge)(e.texto, g.voz, mp3, rate)
        segundos = round(time.time() - t0, 2)
    info = {"idx": e.idx, "hash": h, "mp3": mp3.name, "duracion": duracion(mp3), "palabras": palabras,
            "motor": g.motor_voz, "voz": g.voz, **({} if demo else {"segundos": segundos})}
    meta.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info, False


def generar(g: Guion, carpeta: Path, demo: bool = False, rate: str = "+0%", avisar=None) -> list[dict]:
    """Genera un mp3 por escena. Devuelve lista de escenas con duración y palabras.
    avisar(paso, estado) es opcional: la app web lo usa para mostrar el progreso."""
    resultado = []
    for e in g.escenas:
        if avisar:
            avisar(f"voz-{e.idx}", "corriendo")
        info, cache = generar_escena(g, e, carpeta, demo, rate)
        resultado.append(info)
        decir("log.voz_escena_cache" if cache else "log.voz_escena", n=f"{e.idx:02d}", s=f"{info['duracion']:.1f}")
        if avisar:
            avisar(f"voz-{e.idx}", "cache" if cache else "ok")
    return resultado


async def listar_voces(idioma: str = "es"):
    import edge_tts

    print("edge-tts (\"motor_voz\": \"edge\", en línea):")
    voces = await edge_tts.list_voices()
    for v in sorted(voces, key=lambda x: x["ShortName"]):
        if v["Locale"].startswith(f"{idioma}-"):
            print(f"  {v['ShortName']:28s} {v['Gender']}")
    print("\nKokoro (\"motor_voz\": \"kokoro\", local)" + ("" if voz_local.disponible() else " — no instalado") + ":")
    for v in voces_kokoro():
        if v["locale"].startswith(idioma):
            print(f"  {v['id']:28s} {v['genero']} {v['locale']}")
