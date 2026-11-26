"""Imágenes por escena en la GPU local (cabe en 6 GB con offload). Dos calidades:
  rapida: SDXL-Turbo, 4 pasos (por defecto)
  alta:   SDXL base 1.0, 25 pasos con guidance; mucho más lenta
Con --demo genera placeholders sin GPU."""
import hashlib
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import personajes
from .guion import Escena, Guion
from .textos import decir, error
from . import rutas, vulkan

NEGATIVO = "text, watermark, logo, letters, caption, blurry, deformed, extra fingers, low quality"

# Tamaño de generación (múltiplos de 64). Luego ffmpeg escala al tamaño final.
GEN = {"short": (640, 1152), "largo": (1152, 640)}


@dataclass(frozen=True)
class Calidad:
    nombre: str
    modelo: str
    pasos: int
    guidance: float


CALIDADES = {
    "rapida": Calidad("rápida", "stabilityai/sdxl-turbo", 4, 0.0),  # turbo no usa guidance
    "alta": Calidad("alta", "stabilityai/stable-diffusion-xl-base-1.0", 25, 7.0),
}
if vulkan.activo():
    CALIDADES = {k: Calidad(v[0], k, v[2], v[3]) for k, v in vulkan.PERFILES.items()}

DESCARGA_GB = 7.0  # lo que baja la primera vez cada modelo (pesos fp16, ~6.9 GB)
ADAPTER_GB = 3.3  # IP-Adapter plus-face + codificador de imagen ViT-H (fp32), la primera vez
# IP-Adapter con SDXL-Turbo: ver medidas en README (sección Personajes). Sin cabida en 6 GB -> solo Alta.
TURBO_CON_PERSONAJE = False
SEG_ALTA_PERSONAJE = 80  # medido en la RTX 3050 6 GB, 640x1152: para que la app avise cuánto tarda

# progreso de una descarga en curso, para la app: {"modelo", "gb", "total"} o None
descarga: dict | None = None


def calidad_de(e: Escena, calidad: str = "rapida") -> str:
    """La calidad de la escena manda sobre la global ("regenerar en alta" la fija en el guion)."""
    q = e.calidad or calidad
    if q not in CALIDADES:
        raise error("err.calidad", calidad=q, opciones=" / ".join(CALIDADES))
    if (q != "alta" if vulkan.activo() else q == "rapida" and not TURBO_CON_PERSONAJE) and personajes.menciones(e.prompt):
        return "alta"  # IP-Adapter solo cabe con el offload secuencial del modo Alta
    return q


def _placeholder(ruta: Path, texto: str, size: tuple[int, int], seed: int):
    from PIL import Image, ImageDraw, ImageFont

    w, h = size
    r = (seed * 37) % 120 + 20
    g = (seed * 91) % 120 + 20
    b = (seed * 53) % 120 + 40
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        k = y / h
        for x in range(0, w):
            px[x, y] = (int(r * (1 - k) + 10 * k), int(g * (1 - k) + 10 * k), int(b * (1 - k) + 30 * k))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(str(Path(__file__).parent.parent / "assets/fuentes/DejaVuSans-Bold.ttf"), 28)
    except OSError:
        font = ImageFont.load_default()
    # prompt envuelto en varias líneas
    palabras, lineas, actual = texto.split(), [], ""
    for p in palabras:
        if len(actual) + len(p) > 34:
            lineas.append(actual)
            actual = p
        else:
            actual = (actual + " " + p).strip()
    lineas.append(actual)
    y0 = h // 2 - 20 * len(lineas)
    for i, l in enumerate(lineas[:12]):
        d.text((40, y0 + i * 40), l, fill=(230, 230, 230), font=font)
    img.save(ruta, quality=92)


_pipe = None
_modelo_cargado = None
_con_adapter = False  # IP-Adapter (personajes) cargado en _pipe


def _vigilar_descarga(modelo: str, listo: threading.Event, total_gb: float = DESCARGA_GB):
    """Mientras baja el modelo, publica el avance en 'descarga' (y en el log). Se cuentan los bytes
    recibidos por la red: el tamaño de los archivos no sirve porque Windows los reserva enteros al empezar."""
    global descarga
    import psutil

    inicio, ultimo = psutil.net_io_counters().bytes_recv, 0.0
    while not listo.wait(2):
        gb = min((psutil.net_io_counters().bytes_recv - inicio) / 1e9, total_gb * 0.99)
        descarga = {"modelo": modelo, "gb": round(gb, 2), "total": total_gb}
        if time.time() - ultimo > 20:
            decir("log.descargando", modelo=modelo, gb=f"{gb:.1f}", total=f"{total_gb:.0f}")
            ultimo = time.time()


def _cargar_pipe(modelo: str, adapter: bool = False, *, solo_local: bool = False):
    """El pipeline del modelo, con o sin IP-Adapter. Cambiar el adapter no recarga el modelo: se quitan los
    hooks del offload, se carga o descarga el adapter y se vuelve a repartir (unos segundos)."""
    global _con_adapter
    pipe = _cargar_modelo(modelo, solo_local=solo_local)
    if adapter != _con_adapter:
        pipe.remove_all_hooks()
        if adapter:
            _cargar_adapter(pipe)
        else:
            pipe.unload_ip_adapter()
        _con_adapter = adapter
        _offload(pipe, modelo)
    return pipe


def _cargar_adapter(pipe):
    a = personajes.ADAPTER
    kw = {"subfolder": a["subfolder"], "weight_name": a["weight_name"], "image_encoder_folder": a["image_encoder_folder"]}
    decir("log.cargando_adapter")
    try:
        pipe.load_ip_adapter(a["repo"], local_files_only=True, **kw)
    except (OSError, ValueError):
        global descarga
        decir("log.descargando_adapter", total=f"{ADAPTER_GB:.1f}")
        listo = threading.Event()
        threading.Thread(target=_vigilar_descarga, args=("IP-Adapter", listo, ADAPTER_GB), daemon=True).start()
        try:
            pipe.load_ip_adapter(a["repo"], **kw)
        finally:
            listo.set()
            descarga = None


def _offload(pipe, modelo: str):
    import torch

    if not torch.cuda.is_available():
        return
    if modelo == CALIDADES["rapida"].modelo and not _con_adapter:
        # offload por modelo: la UNet entera en la GPU (pico ~5.6 GB); sin guidance cabe en 6 GB
        pipe.enable_model_cpu_offload()
    else:
        # Con guidance (lote x2) la UNet de SDXL no cabe junto a lo que usa Windows: el driver
        # desborda a la memoria compartida y cada paso tarda ~20 s (medido en la RTX 3050 6 GB,
        # también con atención en rebanadas o a 512x896). El offload secuencial sube capa por capa:
        # pico ~2.8 GB y ~2.7 s por paso a resolución completa. Con IP-Adapter, igual (ver README).
        pipe.enable_sequential_cpu_offload()


def _cargar_modelo(modelo: str, *, solo_local: bool = False):
    global _pipe, _modelo_cargado, descarga, _con_adapter
    if _pipe is not None and _modelo_cargado == modelo:
        return _pipe
    liberar()  # un solo modelo a la vez: al cambiar de calidad se suelta el anterior
    from . import voz_local
    voz_local.liberar()  # Kokoro + whisper: la VRAM es para SDXL
    import torch
    from diffusers import AutoPipelineForText2Image

    kw = {"torch_dtype": torch.float16, "variant": "fp16"}
    try:
        _pipe = AutoPipelineForText2Image.from_pretrained(modelo, local_files_only=True, **kw)
    except (OSError, ValueError):
        if solo_local:
            raise error("est.err_modelo_local") from None
        decir("log.descargando_modelo", modelo=modelo, total=f"{DESCARGA_GB:.0f}")
        listo = threading.Event()
        threading.Thread(target=_vigilar_descarga, args=(modelo, listo), daemon=True).start()
        try:
            _pipe = AutoPipelineForText2Image.from_pretrained(modelo, **kw)
        finally:
            listo.set()
            descarga = None
        decir("log.descarga_completa")
    decir("log.modelo_cargado", modelo=modelo)
    _modelo_cargado, _con_adapter = modelo, False
    if torch.cuda.is_available():
        _offload(_pipe, modelo)
        _pipe.vae.enable_slicing()  # diffusers >= 0.40 ya no lo expone en el pipeline
    else:
        decir("log.sin_cuda")
    return _pipe


def liberar():
    """Suelta el modelo de la RAM y la VRAM (la app web lo mantiene cargado entre trabajos)."""
    global _pipe, _modelo_cargado, _con_adapter
    if _pipe is None:
        return
    try:
        _pipe.remove_all_hooks()  # los hooks del offload guardan referencias a los pesos
    except AttributeError:
        pass
    _pipe, _modelo_cargado, _con_adapter = None, None, False
    import gc

    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def modelo_cargado() -> str | None:
    return _modelo_cargado


# ---------- límite de CLIP ----------
# Los dos codificadores de texto de SDXL (CLIP) leen 77 tokens, dos de ellos de control: lo que pasa de
# CLIP_MAX tokens se corta en silencio y no llega a la imagen (por eso se avisa).
CLIP_MAX = 75
MAX_PALABRAS_VISUALES = 40
PALABRA_VISUAL = re.compile(r"\b\w+(?:[-’']\w+)*\b")
REINTENTO_TOKENIZER_S = 60
_tokenizador = None  # CLIPTokenizer, o False si no se pudo cargar (mientras tanto se estima)
_tokenizador_fallo = 0.0
_tokenizador_lock = threading.Lock()


def _tokenizer():
    """El tokenizer de SDXL-Turbo (local). Si falla (sin transformers, sin el modelo, o un import a la vez que
    otro hilo carga torch), se estima y se reintenta pasado REINTENTO_TOKENIZER_S."""
    global _tokenizador, _tokenizador_fallo
    if _tokenizador or (_tokenizador is False and time.time() - _tokenizador_fallo < REINTENTO_TOKENIZER_S):
        return _tokenizador
    with _tokenizador_lock:
        if _tokenizador:
            return _tokenizador
        try:
            from transformers import CLIPTokenizer
            _tokenizador = CLIPTokenizer.from_pretrained(CALIDADES["rapida"].modelo, subfolder="tokenizer",
                                                         local_files_only=True)
        except Exception as e:  # noqa: BLE001 - se estima hasta el próximo intento
            _tokenizador, _tokenizador_fallo = False, time.time()
            print(f"tokenizer CLIP no disponible ({type(e).__name__}: {e}); se estima el largo del prompt",
                  file=sys.stderr, flush=True)
    return _tokenizador


def tokens_clip(texto: str) -> tuple[int, bool]:
    """Cuenta tokens CLIP o estima su longitud en modo Vulkan."""
    import os
    import re

    if vulkan.activo() or os.environ.get("COMPRESSO_IMAGE_BACKEND", "").lower() == "lemonade":
        return len(re.findall(r"\\w+|[^\\w\\s]", texto)), False

    tok = _tokenizer()
    if tok:
        return len(tok(texto, add_special_tokens=False, truncation=False)["input_ids"]), True

    return len(re.findall(r"\\w+|[^\\w\\s]", texto)), False


def prompt_final(g: Guion, e: Escena) -> str:
    """El texto que llega a SDXL: el prompt con el @personaje ya reemplazado, más el estilo visual."""
    try:
        base = personajes.resolver(e.prompt).prompt
    except ValueError:  # personaje que no existe: el error sale al generar; aquí se cuenta tal cual
        base = e.prompt
    return combinar_prompt(base, g.estilo_visual)


def combinar_prompt(base: str, estilo: str) -> str:
    return ", ".join(parte.strip() for parte in (base, estilo) if parte.strip())


def limitar_prompt(texto: str) -> str:
    palabras = list(PALABRA_VISUAL.finditer(texto))
    return texto[:palabras[MAX_PALABRAS_VISUALES - 1].end()] if len(palabras) > MAX_PALABRAS_VISUALES else texto


def presupuesto_visual(texto: str) -> dict:
    palabras = len(PALABRA_VISUAL.findall(texto))
    return {"palabras": palabras, "limite": MAX_PALABRAS_VISUALES, "aviso": aviso_clip(limitar_prompt(texto))}


def aviso_clip(texto: str) -> dict | None:
    """{"tokens", "max", "exacto"} si el prompt pasa del límite y se va a cortar; None si cabe."""
    if not texto.strip():
        return None
    n, exacto = tokens_clip(texto)
    return {"tokens": n, "max": CLIP_MAX, "exacto": exacto} if n > CLIP_MAX else None


def generar_estudio(prompt: str, calidad: str, size: tuple[int, int], seed: int,
                    foto=None, intensidad: float = 0.5, progreso=None):
    """Usa los pesos compartidos, exclusivamente locales, dentro del worker único.

    El pipeline img2img es una vista temporal de los mismos componentes. Sus hooks
    se retiran y el offload de texto se restaura incluso si la inferencia falla.
    """
    prompt = limitar_prompt(prompt)
    if vulkan.activo():
        if progreso:
            progreso("cargado", 0)
        imagen = vulkan.generar(prompt, calidad, size, seed, negativo=NEGATIVO, foto=foto, intensidad=intensidad)
        if progreso:
            n = CALIDADES[calidad].pasos
            for paso in range(1, (int(n * intensidad) if foto is not None else n) + 1):
                progreso("paso", paso)
        return imagen

    import torch
    from diffusers import AutoPipelineForImage2Image

    q = CALIDADES[calidad]
    base = _cargar_pipe(q.modelo, solo_local=True)
    pipe = base
    try:
        if foto is not None:
            base.remove_all_hooks()
            pipe = AutoPipelineForImage2Image.from_pipe(base)
            # Los timesteps son mutables: no compartir el scheduler entre tareas.
            pipe.scheduler = type(base.scheduler).from_config(base.scheduler.config)
            _offload(pipe, q.modelo)
        if progreso:
            progreso("cargado", 0)

        def al_paso(actual, indice, timestep, datos):
            if progreso:
                progreso("paso", indice + 1)
            return datos

        extra = {"image": foto, "strength": intensidad} if foto is not None else {}
        generator = torch.Generator("cuda" if torch.cuda.is_available() else "cpu").manual_seed(seed)
        return pipe(prompt=prompt, negative_prompt=NEGATIVO, width=size[0], height=size[1],
                    num_inference_steps=q.pasos, guidance_scale=q.guidance, generator=generator,
                    callback_on_step_end=al_paso, **extra).images[0]
    finally:
        if foto is not None:
            try:
                pipe.remove_all_hooks()
                if pipe is not base:
                    base.remove_all_hooks()
                _offload(base, q.modelo)
            except Exception:
                liberar()  # no dejar hooks incompletos para Videos o Personajes
                raise


def hash_imagen(g: Guion, e: Escena, demo: bool = False, calidad: str = "rapida") -> str:
    # los sufijos solo se agregan si aplican, así no se invalidan las imágenes ya generadas
    base = g.estilo_visual + e.prompt
    if g.formato != "short":
        base += f"|{g.formato}"
    if e.semilla:
        base += f"#{e.semilla}"
    q = calidad_de(e, calidad)
    if q != "rapida":  # las de alta tienen su propio hash: no pisan a las rápidas
        base += f"|{q}"
    if demo:
        base += "|demo"
    elif vulkan.activo():
        modelo = vulkan.ruta_modelo(q)
        version = modelo.stat().st_mtime_ns if modelo and modelo.is_file() else 0
        base += f"|vulkan|{q}|{modelo}|{version}"
    if not demo:
        visual = prompt_final(g, e)
        if limitar_prompt(visual) != visual:
            base += "|visual-40"
    nombres = personajes.menciones(e.prompt)
    if nombres:  # el personaje, sus fotos y su fuerza: si cambia una referencia, la imagen se regenera
        try:
            base += f"|@{nombres[0]}:{personajes.cargar(nombres[0]).huella()}"
        except ValueError:
            base += f"|@{nombres[0]}:?"  # no existe: no hay imagen en caché y al generar sale el error
    return hashlib.md5(base.encode()).hexdigest()[:8]


def buscar(g: Guion, e: Escena, carpeta: Path, demo: bool = False, calidad: str = "rapida") -> Path | None:
    """La imagen ya generada para este prompt, aunque sea de otra posición (escenas reordenadas)."""
    hp = hash_imagen(g, e, demo, calidad)
    propia = carpeta / f"img_{e.idx:02d}_{hp}.jpg"
    if propia.exists():
        return propia
    return next(iter(carpeta.glob(f"img_*_{hp}.jpg")), None)


def buscar_cualquiera(g: Guion, e: Escena, carpeta: Path, demo: bool = False,
                      calidad: str = "rapida") -> tuple[Path | None, str]:
    """Para mostrar en la app: la imagen de la calidad que toca y, si aún no existe, la de la otra
    (se regenera al producir). Devuelve (ruta, calidad de esa imagen)."""
    q = calidad_de(e, calidad)
    for otra in [q] + [c for c in CALIDADES if c != q]:
        if ruta := buscar(g, replace(e, calidad=otra), carpeta, demo):
            return ruta, otra
    return None, q


def _difundir(pipe, prompt: str, q: Calidad, size: tuple[int, int], seed: int, refs: list | None = None):
    import torch

    gen = torch.Generator("cuda" if torch.cuda.is_available() else "cpu").manual_seed(seed)
    extra = {"ip_adapter_image": [refs]} if refs else {}  # una lista de fotos para el único adapter
    return pipe(
        prompt=prompt,
        negative_prompt=NEGATIVO_PERSONAJE if refs else NEGATIVO,  # con guidance 0 (turbo) no hace nada
        num_inference_steps=q.pasos,
        guidance_scale=q.guidance,
        width=size[0], height=size[1],
        generator=gen,
        **extra,
    ).images[0]


def _referencias(p: "personajes.Personaje") -> list:
    from PIL import Image

    return [personajes.recorte_cara(Image.open(f).convert("RGB")) for f in p.fotos]


# con personaje: plus-face a veces arma un collage de varias fotos de la misma persona (medido en planos
# de cuerpo entero); esto lo evita sin tocar las escenas sin personaje
NEGATIVO_PERSONAJE = NEGATIVO + ", collage, grid, multiple panels, split screen, multiple views, photo frame, duplicate person"


def _pipe_para(q: Calidad, p: "personajes.Personaje | None"):
    """El pipeline listo para generar: con el adapter y la fuerza del personaje, o sin adapter."""
    pipe = _cargar_pipe(q.modelo, adapter=p is not None)
    if p is not None:
        pipe.set_ip_adapter_scale(p.fuerza)
    return pipe


def _escalar(origen: Path, destino: Path, size: tuple[int, int]):
    import subprocess

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(origen), "-vf",
                    f"scale={size[0]}:{size[1]}:flags=lanczos", "-q:v", "2", str(destino)], check=True)


def generar_escena(g: Guion, e: Escena, carpeta: Path, demo: bool = False, calidad: str = "rapida",
                   modelo: str | None = None, pasos: int | None = None) -> tuple[Path, bool]:
    """Imagen de una escena. Devuelve (ruta, venia_de_cache).
    El nombre incluye un hash del prompt: si cambias el prompt se regenera sola.
    modelo/pasos (opcionales) cambian los de la calidad, para probar otros modelos desde la consola."""
    # COMPRESSO_VULKAN_V2
    import os
    usar_vulkan = os.getenv("COMPRESSO_IMAGE_BACKEND") == "lemonade"
    carpeta.mkdir(parents=True, exist_ok=True)
    hp = hash_imagen(g, e, demo, calidad)
    if usar_vulkan and not demo:
        import hashlib
        q_cache = CALIDADES[calidad_de(e, calidad)]
        modelo_cache = modelo or q_cache.modelo
        pasos_cache = pasos or q_cache.pasos
        cfg_cache = 0.4 if modelo_cache in ("stabilityai/sdxl-turbo", "SDXL-Turbo") else q_cache.guidance
        huella = hashlib.sha256(f"lemonade-v2|{hp}|{modelo_cache}|{pasos_cache}|{cfg_cache}".encode()).hexdigest()[:8]
        ruta = carpeta / f"img_{e.idx:02d}_{huella}.jpg"
        previa = ruta if ruta.exists() else next(iter(carpeta.glob(f"img_*_{huella}.jpg")), None)
    else:
        ruta = carpeta / f"img_{e.idx:02d}_{hp}.jpg"
        previa = buscar(g, e, carpeta, demo, calidad)
    if previa == ruta:
        return ruta, True
    if previa:
        shutil.copyfile(previa, ruta)
        return ruta, True

    size = GEN[g.formato]
    seed = int(hp, 16)
    r = personajes.resolver(e.prompt)  # @nombre -> descripción; error si el personaje no existe
    if aviso := personajes.aviso_varios(e.idx, r):
        decir(aviso.clave, **aviso.params)
    if demo:
        _placeholder(ruta, f"[{e.idx}] {r.prompt}", size, seed)
        return ruta, False
    prompt = limitar_prompt(combinar_prompt(r.prompt, g.estilo_visual))
    if vulkan.activo():
        perfil = calidad_de(e, calidad)
        ref = _referencias(r.personaje) if r.personaje else ()
        img = vulkan.generar(prompt, perfil, size, seed, negativo=NEGATIVO,
                            pasos=pasos, referencias=ref, fuerza=r.personaje.fuerza if r.personaje else 0.5)
        img.save(ruta, quality=94)
        return ruta, False
    if not usar_vulkan:
        import torch

    q = CALIDADES[calidad_de(e, calidad)]
    if modelo or pasos:
        q = Calidad(q.nombre, modelo or q.modelo, pasos or q.pasos, q.guidance)
    if usar_vulkan and r.personaje:
        raise RuntimeError("IP-Adapter aún no está soportado por Vulkan")
    pipe = None if usar_vulkan else _pipe_para(q, r.personaje)
    refs = _referencias(r.personaje) if r.personaje and not usar_vulkan else None
    if r.personaje:
        decir("log.personaje", nombre=r.personaje.nombre, fuerza=f"{r.personaje.fuerza:.2f}", n=len(refs))
    if largo := aviso_clip(prompt):
        decir("log.prompt_largo", n=f"{e.idx:02d}", tokens=largo["tokens"], max=largo["max"])
    if usar_vulkan:
        from compresso_vulkan import generar_imagen
        cfg = 0.4 if q.modelo in ("stabilityai/sdxl-turbo", "SDXL-Turbo") else q.guidance
        img = generar_imagen(prompt, modelo=q.modelo, size=size,
                            steps=q.pasos, cfg_scale=cfg, seed=seed,
                            negative_prompt=NEGATIVO)
        img.save(ruta, quality=94)
        return ruta, False
    try:
        img = _difundir(pipe, prompt, q, size, seed, refs)
    except torch.cuda.OutOfMemoryError:
        # plan B: generar más chico y escalar con ffmpeg al tamaño de siempre
        torch.cuda.empty_cache()
        chico = (size[0] * 3 // 4 // 64 * 64, size[1] * 3 // 4 // 64 * 64)
        decir("log.sin_vram_escala", w=size[0], h=size[1], w2=chico[0], h2=chico[1])
        img = _difundir(pipe, prompt, q, chico, seed, refs)
        tmp = ruta.with_suffix(".chica.png")
        img.save(tmp)
        _escalar(tmp, ruta, size)
        tmp.unlink()
        return ruta, False
    img.save(ruta, quality=94)
    return ruta, False


# ---------- prueba de un personaje ----------

POSES = [
    "close-up portrait of {d}, facing the camera, neutral expression, plain dark grey background, soft studio light",
    "profile shot, side view of {d}, her whole face in profile looking toward the left edge of the frame, looking away from the camera, plain dark grey background, soft studio light",
    "a single full body photo of {d}, one person standing alone on a city sidewalk, wide shot from head to shoes, daytime, natural light",
]


def probar_personaje(p: "personajes.Personaje", demo: bool = False, calidad: str = "rapida", avisar=None) -> Path:
    """assets/personajes/<nombre>/prueba.jpg: tres imágenes lado a lado (de frente, de perfil, cuerpo
    entero) para ver si la cara se reconoce. Misma calidad y fuerza que en un guion.
    avisar(paso, estado) opcional, con pasos "pose-1".."pose-3" (la app muestra 1/3, 2/3, 3/3)."""
    from PIL import Image

    q = CALIDADES["alta" if not TURBO_CON_PERSONAJE else calidad]
    size = GEN["short"]  # el formato de los Shorts: así la prueba muestra lo que saldrá en un guion
    desc = p.descripcion or "a person"
    imgs = []
    refs_vulkan = _referencias(p) if vulkan.activo() and not demo else ()
    for i, pose in enumerate(POSES, start=1):
        t0 = time.time()
        if avisar:
            avisar(f"pose-{i}", "corriendo")
        prompt = pose.format(d=desc) + ", photorealistic, high detail"
        tmp = p.carpeta / f".pose_{i}.jpg"
        if demo:
            _placeholder(tmp, f"@{p.nombre}: {prompt}", size, 1000 + i)
            img = Image.open(tmp)
        elif vulkan.activo():
            img = vulkan.generar(prompt, "alta", size, 7000 + i, negativo=NEGATIVO,
                                referencias=(refs_vulkan[(i - 1) % len(refs_vulkan)],), fuerza=p.fuerza)
        else:
            img = _difundir(_pipe_para(q, p), prompt, q, size, 7000 + i, _referencias(p))
        imgs.append(img.convert("RGB"))
        tmp.unlink(missing_ok=True)
        decir("log.pose", n=i, s=f"{time.time() - t0:.0f}")
        if avisar:
            avisar(f"pose-{i}", "ok")
    tira = Image.new("RGB", (size[0] * 3, size[1]))
    for i, img in enumerate(imgs):
        tira.paste(img.resize(size), (i * size[0], 0))
    ruta = p.carpeta / "prueba.jpg"
    tira.save(ruta, quality=90)
    (p.carpeta / "prueba.huella").write_text(p.huella(), encoding="utf-8")
    return ruta


# ---------- imagen o clip propio ----------

# Los archivos que sube Daniel no se pueden regenerar: viven fuera de salida/ (que se limpia y se borra),
# en assets/propios/<slug>/. En el guion siguen como "propios/<archivo>".
PROPIOS = rutas.PROPIOS


def carpeta_propios(slug: str) -> Path:
    from .medios import privada
    return privada(PROPIOS, slug)


def ruta_propia(slug: str, media: str) -> Path:
    from .medios import privada
    return privada(PROPIOS, slug, media)


def migrar_propios(salida: Path) -> int:
    """Mueve salida/<slug>/propios/* (de antes) a assets/propios/<slug>/. Devuelve cuántos archivos movió."""
    n = 0
    for viejo in salida.glob("*/propios") if salida.exists() else []:
        if not viejo.is_dir():
            continue
        dest = carpeta_propios(viejo.parent.name)
        dest.mkdir(parents=True, exist_ok=True)
        for f in viejo.iterdir():
            if f.is_file() and not (dest / f.name).exists():
                shutil.move(str(f), dest / f.name)
                n += 1
        shutil.rmtree(viejo, ignore_errors=True)  # lo que quede ya está en assets/propios (mismo nombre = mismo hash)
    return n


def propia(g: Guion, e: Escena, carpeta: Path) -> Path:
    """El archivo propio de la escena (assets/propios/<slug>/...). ValueError si falta."""
    ruta = ruta_propia(g.slug, e.media)
    if not ruta.exists():
        viejo = carpeta / e.media
        # El legado también debe quedar dentro de salida/<slug>/propios.
        if (any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (viejo, viejo.parent))
                or not viejo.resolve().is_relative_to((carpeta / 'propios').resolve())):
            raise error('clip.ruta_invalida')
        if not viejo.exists():
            raise error("err.propio_falta", n=e.idx, media=Path(e.media).name, slug=g.slug)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(viejo), ruta)
    return ruta


def poster(video: Path) -> Path:
    """Un cuadro representativo de un clip (filtro thumbnail de ffmpeg), junto al clip: <nombre>_poster.jpg."""
    import subprocess
    import os
    import tempfile

    jpg = video.with_name(f"{video.stem}_poster.jpg")
    if jpg.is_symlink() or (hasattr(jpg, 'is_junction') and jpg.is_junction()):
        raise error('clip.ruta_invalida')
    if not jpg.exists() or jpg.stat().st_mtime < video.stat().st_mtime:
        fd, nombre = tempfile.mkstemp(dir=video.parent, prefix='.poster-', suffix='.jpg')
        os.close(fd)
        tmp = Path(nombre)
        try:
            r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-protocol_whitelist", "file,pipe",
                                "-format_whitelist", "mov", "-i", str(video), "-map", "0:v:0", "-vf",
                                "scale=640:640:force_original_aspect_ratio=decrease,thumbnail=30",
                                "-frames:v", "1", "-q:v", "3", str(tmp)], capture_output=True, text=True, timeout=20)
            if r.returncode or not tmp.stat().st_size:
                raise ValueError()
            os.replace(tmp, jpg)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise error('clip.decodificacion') from None
        finally:
            tmp.unlink(missing_ok=True)
    return jpg


def imagen_base(g: Guion, e: Escena, carpeta: Path) -> Path:
    """La imagen fija de una escena con archivo propio: la propia, o un cuadro del clip."""
    ruta = propia(g, e, carpeta)
    return poster(ruta) if e.es_clip else ruta


def vista(g: Guion, e: Escena, carpeta: Path, demo: bool = False, calidad: str = "rapida") -> tuple[Path | None, str | None]:
    """Lo que se muestra en la app para la escena: (imagen, calidad). Calidad None = es propia."""
    if e.media:
        try:
            return imagen_base(g, e, carpeta), None
        except ValueError:
            return None, None
    if not e.prompt or not carpeta.exists():
        return None, None
    return buscar_cualquiera(g, e, carpeta, demo, calidad)


def generar(g: Guion, carpeta: Path, demo: bool = False, calidad: str = "rapida", modelo: str | None = None,
            pasos: int | None = None, avisar=None) -> list[Path]:
    """Una ruta por escena: la imagen generada, o el archivo propio (imagen o clip) si la escena lo tiene."""
    rutas = []
    for e in g.escenas:
        if avisar:
            avisar(f"img-{e.idx}", "corriendo")
        if e.media:
            rutas.append(propia(g, e, carpeta))
            decir("log.imagen_escena_clip" if e.es_clip else "log.imagen_escena_propia", n=f"{e.idx:02d}", media=e.media)
            if avisar:
                avisar(f"img-{e.idx}", "cache")
            continue
        t0 = time.time()
        ruta, cache = generar_escena(g, e, carpeta, demo, calidad, modelo, pasos)
        rutas.append(ruta)
        q = calidad_de(e, calidad)
        decir("log.imagen_escena_cache" if cache else "log.imagen_escena_ok", n=f"{e.idx:02d}", calidad=q,
              s=f"{time.time() - t0:.1f}")
        if avisar:
            avisar(f"img-{e.idx}", "cache" if cache else "ok")
    return rutas
