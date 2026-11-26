"""Verificación del Setup (la corre app/preparar.py en otro proceso): ffmpeg, una frase de voz y una imagen
de prueba pequeña, con los tiempos medidos en este equipo. Imprime una línea JSON al final.

Usa el mismo código que la app (pipeline/imagenes.py y pipeline/voz_local.py), así que si esto pasa,
generar un video también funciona.
"""
import json
import os
import shutil
from contextlib import nullcontext
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import rutas  # noqa: E402,F401  (fija HF_HOME, el PATH de ffmpeg y las librerías de IA)

res: dict = {"ok": True, "pasos": {}}
ultima_subetapa = None


def marcar(nombre: str) -> None:
    """Marcador fijo y sin datos del usuario; sobrevive a un fallo nativo."""
    global ultima_subetapa
    if nombre != ultima_subetapa:
        ultima_subetapa = nombre
        print("DCS_SETUP_ETAPA:" + nombre, file=sys.stderr, flush=True)


def observar(frame, evento, argumento):
    """Solo diagnóstico en este proceso: no sustituye funciones ni altera el motor."""
    if evento != "call":
        return
    nombre = frame.f_code.co_name
    if nombre not in ("_find_and_load", "_cargar", "_elegir_dispositivo", "__init__",
                      "__call__", "transcribe", "generate_segments", "run"):
        return
    modulo = frame.f_globals.get("__name__", "")
    if nombre == "_find_and_load" and modulo in ("importlib._bootstrap", "_frozen_importlib"):
        etapa = {"kokoro": "kokoro_importacion", "faster_whisper": "whisper_importacion",
                 "ctranslate2": "ctranslate2_importacion"}.get(frame.f_locals.get("name"))
        if etapa:
            marcar(etapa)
    elif modulo == "pipeline.voz_local" and nombre in ("_cargar", "_elegir_dispositivo"):
        marcar("kokoro_carga" if nombre == "_cargar" else "pytorch_cuda")
    elif modulo.startswith("kokoro."):
        if frame.f_code.co_qualname == "KModel.__init__":
            marcar("kokoro_carga")
        elif frame.f_code.co_qualname == "KPipeline.__call__":
            marcar("sintesis")
    elif modulo.startswith("faster_whisper."):
        if frame.f_code.co_qualname == "WhisperModel.__init__":
            marcar("whisper_carga")
        elif nombre in ("transcribe", "generate_segments"):
            marcar("transcripcion")
    elif modulo == "subprocess" and nombre == "run":
        args = frame.f_locals.get("popenargs", ())
        if args and isinstance(args[0], (list, tuple)) and args[0] and args[0][0] == "ffmpeg":
            marcar("ffmpeg_audio")


def paso(nombre: str, f) -> None:
    t = time.time()
    try:
        extra = f() or {}
        res["pasos"][nombre] = {"ok": True, "segundos": round(time.time() - t, 1), **extra}
    except Exception as e:  # noqa: BLE001
        res["ok"] = False
        res.setdefault("subetapa_fallo", ultima_subetapa)
        res["pasos"][nombre] = {"ok": False, "segundos": round(time.time() - t, 1), "error": f"{type(e).__name__}: {e}"}
        traceback.print_exc()


def ffmpeg():
    r = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True, check=True)
    return {"version": r.stdout.split()[2]}


def verificar(tmp):
    from pipeline import rutas  # noqa: F401 (rutas de la instalación, solo al ejecutar)
    marcar("ffmpeg")
    paso("ffmpeg", ffmpeg)

    try:
        marcar("pytorch_importacion")
        import torch  # noqa: F401
        con_ia = True
    except ImportError:
        con_ia = False

    if con_ia:
        # Dos veces cada cosa: la primera incluye cargar los modelos (y en el primer arranque, preparar CUDA); la
        # segunda es lo que tarda de verdad al trabajar, que es lo que se muestra.
        def voz():
            from pipeline import voz_local
            t = time.time()
            anterior = sys.getprofile()
            sys.setprofile(observar)
            try:
                marcar("sintesis")
                voz_local.sintetizar("Everything is ready.", "am_michael", tmp / "voz0.mp3")
                primera = time.time() - t
                t = time.time()
                marcar("sintesis")
                palabras = voz_local.sintetizar("Everything is ready. Let's tell a true story.", "am_michael", tmp / "voz.mp3")
            finally:
                sys.setprofile(anterior)
            return {"palabras": len(palabras), "dispositivo": voz_local._dispositivo, "carga_s": round(primera, 1),
                    "sintesis_s": round(time.time() - t, 1)}

        def imagen():
            import torch
            marcar("sdxl_importacion")
            from pipeline import imagenes, voz_local
            marcar("liberacion_memoria")
            voz_local.liberar()
            q, prompt = imagenes.CALIDADES["rapida"], "an old lighthouse on a cliff at dusk, cinematic, photorealistic"
            t = time.time()
            marcar("sdxl_carga")
            pipe = imagenes._cargar_pipe(q.modelo)
            marcar("imagen_generacion_1")
            imagenes._difundir(pipe, prompt, q, (512, 512), 7)
            carga = time.time() - t
            t = time.time()
            marcar("imagen_generacion_2")
            img = imagenes._difundir(pipe, prompt, q, (512, 512), 8)
            marcar("imagen_guardado")
            img.save(tmp / "prueba.jpg")
            return {"carga_s": round(carga, 1), "imagen_s": round(time.time() - t, 1), "cuda": torch.cuda.is_available(),
                    "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}

        paso("voz", voz)
        paso("imagen", imagen)
    else:
        def voz_edge():
            from pipeline import voz
            marcar("sintesis")
            info = voz._edge("Everything is ready.", "en-US-AndrewNeural", tmp / "voz.mp3", "+0%")
            return {"palabras": len(info), "motor": "edge-tts"}

        paso("voz", voz_edge)
        res["solo_voz"] = True

    if not res["ok"]:
        res["error"] = "; ".join(f"{k}: {v['error']}" for k, v in res["pasos"].items() if not v["ok"])


def ejecutar():
    """El temporal es exclusivo de esta verificación; no cambia sus motores."""
    antes = shutil.disk_usage(tempfile.gettempdir()).free
    disco = {'libre_antes_bytes': antes, 'temporales_previstos': ['dcrypto-verif-*'],
             'temporales_eliminados': []}
    print('DCS_SETUP_DISCO:' + json.dumps(disco), file=sys.stderr, flush=True)
    temporal = None
    control_padre = os.environ.get('DCRYPTO_VERIFY_TMP')
    contexto = nullcontext(control_padre) if control_padre else tempfile.TemporaryDirectory(prefix='dcrypto-verif-')
    try:
        with contexto as nombre:
            temporal = nombre
            if not Path(nombre).is_dir() or Path(nombre).is_symlink():
                raise OSError('Directorio de verificación no válido')
            disco['temporales_previstos'] = [nombre]
            print('DCS_SETUP_DISCO:' + json.dumps(disco), file=sys.stderr, flush=True)
            # También se preserva el comportamiento normal de la verificación.
            verificar(Path(nombre))
    finally:
        disco['libre_despues_bytes'] = shutil.disk_usage(tempfile.gettempdir()).free
        if temporal and control_padre:
            disco['limpieza_a_cargo_del_padre'] = True
        elif temporal and Path(temporal).exists():
            disco['residuo_temporal'] = temporal
        elif temporal:
            disco['temporales_eliminados'] = [temporal]
        print('DCS_SETUP_DISCO:' + json.dumps(disco), file=sys.stderr, flush=True)
    res['disco'] = disco
    print(json.dumps(res), flush=True)


if __name__ == '__main__':
    ejecutar()
