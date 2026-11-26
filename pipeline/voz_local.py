"""Motor de voz local: Kokoro-82M (Apache 2.0), sin internet, en GPU si hay sitio y si no en CPU.

Kokoro no da tiempos por palabra como edge-tts, así que los subtítulos (y el karaoke) salen de una
alineación con faster-whisper (word_timestamps): se transcribe el audio generado y cada palabra del
guion toma los tiempos de la palabra reconocida que le corresponde (las que no casan se interpolan).

Los modelos se quedan cargados entre escenas; liberar() los suelta (antes de cargar SDXL).
La primera vez descarga ~330 MB (Kokoro) y ~460 MB (whisper "small") a la caché de Hugging Face.
"""
import difflib
import os
import re
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

from .textos import decir

REPO = "hexgrad/Kokoro-82M"
WHISPER = "small"
SR = 24000
VRAM_MIN_GB = 1.5  # libre en la GPU para usarla (con SDXL cargado no la hay: va a CPU)

# id -> (locale, género). Las de "e" son las únicas en español que trae Kokoro.
VOCES = {
    **{v: ("en-US", "Female") for v in ("af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
                                        "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky")},
    **{v: ("en-US", "Male") for v in ("am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
                                      "am_onyx", "am_puck", "am_santa")},
    **{v: ("en-GB", "Female") for v in ("bf_alice", "bf_emma", "bf_isabella", "bf_lily")},
    **{v: ("en-GB", "Male") for v in ("bm_daniel", "bm_fable", "bm_george", "bm_lewis")},
    "ef_dora": ("es", "Female"), "em_alex": ("es", "Male"), "em_santa": ("es", "Male"),
}
# El español de Kokoro sale de espeak-ng; "es" es castellano (ceceo), "es-419" es latinoamericano (seseo).
ESPEAK_ES = "es-419"

_lock = threading.Lock()
_modelo = None  # KModel
_pipes: dict[str, object] = {}  # lang_code -> KPipeline
_whisper = None
_dispositivo: str | None = None


def disponible() -> bool:
    try:
        import kokoro  # noqa: F401
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _elegir_dispositivo() -> str:
    import torch

    if not torch.cuda.is_available():
        return "cpu"
    libre, _ = torch.cuda.mem_get_info()
    return "cuda" if libre / 2**30 >= VRAM_MIN_GB else "cpu"


def _cargar():
    """Kokoro + whisper en el mismo dispositivo. Devuelve el dispositivo."""
    global _modelo, _whisper, _dispositivo
    if _modelo is not None:
        return _dispositivo
    import torch
    from kokoro import KModel

    _dispositivo = _elegir_dispositivo()
    t0 = time.time()
    _modelo = KModel(repo_id=REPO).to(_dispositivo).eval()
    if _dispositivo == "cuda":
        # ctranslate2 (faster-whisper) usa las DLL de CUDA/cuDNN que ya trae torch
        os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__), "lib"))
    from faster_whisper import WhisperModel

    _whisper = WhisperModel(WHISPER, device=_dispositivo,
                            compute_type="float16" if _dispositivo == "cuda" else "int8")
    decir("log.kokoro_cargado", dispositivo="GPU" if _dispositivo == "cuda" else "CPU", s=f"{time.time() - t0:.0f}")
    return _dispositivo


def _pipe(voz: str):
    from kokoro import KPipeline

    lang = voz[0]
    if lang not in _pipes:
        p = KPipeline(lang_code=lang, repo_id=REPO, model=_modelo)
        if lang == "e":
            from misaki import espeak

            p.g2p = espeak.EspeakG2P(language=ESPEAK_ES)
        _pipes[lang] = p
    return _pipes[lang]


def cargado() -> bool:
    """Kokoro está en memoria (la app lo suelta tras un rato sin uso: app/trabajos.py)."""
    return _modelo is not None


def liberar():
    global _modelo, _whisper, _dispositivo
    with _lock:
        if _modelo is None:
            return
        _modelo = _whisper = _dispositivo = None
        _pipes.clear()
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def velocidad(rate: str) -> float:
    """El rate de edge-tts ("+10%") como speed de Kokoro (1.1)."""
    m = re.fullmatch(r"\s*([+-]?\d+)\s*%\s*", rate or "")
    return max(0.5, min(2.0, 1 + int(m.group(1)) / 100)) if m else 1.0


def sintetizar(texto: str, voz: str, mp3: Path, rate: str = "+0%") -> list[dict]:
    """Escribe el mp3 y devuelve las palabras del texto con sus tiempos [{texto, inicio, fin}]."""
    import soundfile as sf
    import torch

    with _lock:
        _cargar()
        idioma = "es" if voz[0] == "e" else "en"
        with torch.inference_mode():
            partes = [r.audio for r in _pipe(voz)(texto, voice=voz, speed=velocidad(rate)) if r.audio is not None]
        if not partes:
            raise RuntimeError(f"Kokoro no generó audio para: {texto[:60]}")
        audio = torch.cat(partes).cpu().numpy()
        wav = mp3.with_suffix(".wav")
        sf.write(wav, audio, SR)
        try:
            segs, _ = _whisper.transcribe(str(wav), language=idioma, word_timestamps=True, initial_prompt=texto,
                                          beam_size=5, vad_filter=False, condition_on_previous_text=False)
            reconocidas = [(w.word, float(w.start), float(w.end)) for s in segs for w in (s.words or [])]
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame", "-q:a", "2", str(mp3)],
                           check=True)
        finally:
            wav.unlink(missing_ok=True)
    return alinear(texto, reconocidas, _inicio_voz(audio), len(audio) / SR)


def _inicio_voz(audio, umbral: float = 0.02) -> float:
    """Segundo en que empieza a sonar algo (whisper suele poner la primera palabra en 0.0)."""
    import numpy as np

    sobre = np.flatnonzero(np.abs(audio) > umbral)
    return float(sobre[0]) / SR if len(sobre) else 0.0


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if c.isalnum())


def _limpia(tok: str) -> str:
    """La palabra como la muestra el subtítulo: sin la puntuación de los bordes (igual que edge-tts)."""
    return re.sub(r"^[^\w¿¡]+|[^\w%]+$", "", tok).lstrip("¿¡") or tok


def alinear(texto: str, reconocidas: list[tuple[str, float, float]], inicio: float, total: float) -> list[dict]:
    """Las palabras del guion con los tiempos de las reconocidas por whisper. Se emparejan con difflib
    sobre las formas normalizadas; las que no casan ("1994" leído como "nineteen ninety-four" y escrito
    distinto, nombres raros) se reparten entre sus vecinas según su número de letras."""
    toks = [t for t in texto.split() if _norm(t)]
    pal = [{"texto": _limpia(t), "inicio": None, "fin": None} for t in toks]
    if not toks:
        return []
    a, b = [_norm(t) for t in toks], [_norm(w) for w, _, _ in reconocidas]
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            _, ini, fin = reconocidas[blk.b + k]
            pal[blk.a + k].update(inicio=ini, fin=fin)
    # huecos: interpolar entre el fin de la anterior conocida y el inicio de la siguiente
    i = 0
    while i < len(pal):
        if pal[i]["inicio"] is not None:
            i += 1
            continue
        j = i
        while j < len(pal) and pal[j]["inicio"] is None:
            j += 1
        desde = pal[i - 1]["fin"] if i > 0 else inicio
        hasta = pal[j]["inicio"] if j < len(pal) else total
        if hasta <= desde:
            hasta = desde + 0.25 * (j - i)
        letras = [max(1, len(a[k])) for k in range(i, j)]
        t, paso = desde, (hasta - desde) / sum(letras)
        for k, n in zip(range(i, j), letras):
            pal[k].update(inicio=t, fin=t + n * paso)
            t += n * paso
        i = j
    pal[0]["inicio"] = max(pal[0]["inicio"], min(inicio, pal[0]["fin"] - 0.05))
    for p, sig in zip(pal, pal[1:]):  # monótonas y sin solaparse
        sig["inicio"] = max(sig["inicio"], p["inicio"])
        p["fin"] = max(p["inicio"], min(p["fin"], sig["inicio"]))
    return [{"texto": p["texto"], "inicio": round(p["inicio"], 3), "fin": round(p["fin"], 3)} for p in pal]
