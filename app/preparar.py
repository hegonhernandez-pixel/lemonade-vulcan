"""Setup de la primera ejecución: comprobar el equipo, descargar lo que falta y verificar.

Componentes (cada uno se puede bajar, pausar y reanudar; lo ya bajado no se repite):
- ffmpeg: gyan.dev "essentials" (GPL v3), a DATOS\\ffmpeg\\. No va dentro del paquete por su licencia.
- ia: torch+CUDA (cu124) y las librerías de app/requirements-ia.txt, a DATOS\\python\\ (pip --target).
- turbo, kokoro, whisper, base y adapter: todos los modelos en la descarga comercial completa,
  de commits fijos de Hugging Face, escritos en su caché (HF_HOME) como los dejaría huggingface_hub.

Las descargas son nuestras (urllib con Range): progreso por archivo, velocidad, pausa y reanudación aunque se
cierre la app (quedan .part). En modo desarrollador no se instala nada con pip ni se baja ffmpeg: se usa el
venv y el ffmpeg del PATH; solo se informa qué hay.
"""
import hashlib
import importlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path

from pipeline import rutas

UA = {"User-Agent": "DcryptoStudio-setup"}
ESTADO_ARCHIVO = rutas.DATOS / "setup.json"
REQUISITOS = {"vram_gb": 6, "disco_gb": 25, "driver_min": 527.41}  # driver: CUDA 12.x en Windows
# La compilación "essentials" de gyan.dev. Primero su copia en GitHub (GyanD/codexffmpeg, versión fija): en la
# prueba bajó a ~9 MB/s, mientras www.gyan.dev limitaba a menos de 1 KB/s. www.gyan.dev queda de respaldo.
FFMPEG_VERSION = "9.0.2"
FFMPEG_URLS = [
    f"https://github.com/GyanD/codexffmpeg/releases/download/{FFMPEG_VERSION}/ffmpeg-{FFMPEG_VERSION}-essentials_build.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]
TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
REQ_IA = Path(__file__).resolve().parent / "requirements-ia.txt"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@contextmanager
def temporal_verificacion(diagnostico):
    """El padre es dueño del único temporal; nunca recorre temporales anteriores."""
    raiz = Path(tempfile.gettempdir()).resolve()
    disco = {'libre_antes_bytes': shutil.disk_usage(raiz).free,
             'temporales_previstos': ['dcrypto-verif-padre-*'], 'temporales_eliminados': []}
    diagnostico['disco_verificacion'] = disco
    carpeta = None
    identidad = None
    try:
        carpeta = Path(tempfile.mkdtemp(prefix='dcrypto-verif-padre-', dir=raiz))
        st = carpeta.lstat()
        identidad = (st.st_dev, st.st_ino)
        disco['temporales_previstos'] = [str(carpeta)]
        entorno = os.environ.copy()
        entorno.update(DCRYPTO_VERIFY_TMP=str(carpeta), TEMP=str(carpeta), TMP=str(carpeta))
        yield carpeta, entorno
    finally:
        if carpeta is not None:
            try:
                # Una ruta sustituida por un enlace no autoriza borrar otro directorio.
                if (carpeta.parent != raiz or carpeta.is_symlink()
                        or carpeta.resolve() != carpeta or
                        (carpeta.exists() and getattr(carpeta.lstat(), 'st_file_attributes', 0) & 0x400)):
                    raise OSError('Temporal de verificación sustituido; no se elimina')
                if carpeta.exists():
                    st = carpeta.lstat()
                    if (st.st_dev, st.st_ino) != identidad:
                        raise OSError('Cambió la identidad del temporal; no se elimina')
                    shutil.rmtree(carpeta)
                disco['temporales_eliminados'] = [str(carpeta)]
            except OSError as exc:
                disco['residuo_temporal'] = str(carpeta)
                disco['error_limpieza'] = type(exc).__name__
        disco['libre_despues_bytes'] = shutil.disk_usage(raiz).free


@dataclass
class Modelo:
    repo: str
    commit: str  # el que funciona en el equipo de desarrollo; refs/main apunta aquí
    archivos: list[str] = field(default_factory=list)
    carpetas: list[str] = field(default_factory=list)  # todo lo de dentro (p. ej. voices/)


_SDXL = ["model_index.json", "scheduler/scheduler_config.json",
         "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
         "text_encoder_2/config.json", "text_encoder_2/model.fp16.safetensors",
         *[f"{t}/{f}" for t in ("tokenizer", "tokenizer_2") for f in ("merges.txt", "special_tokens_map.json", "tokenizer_config.json", "vocab.json")],
         "unet/config.json", "unet/diffusion_pytorch_model.fp16.safetensors",
         "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors"]

MODELOS = {
    "turbo": Modelo("stabilityai/sdxl-turbo", "71153311d3dbb46851df1931d3ca6e939de83304", _SDXL),
    "kokoro": Modelo("hexgrad/Kokoro-82M", "f3ff3571791e39611d31c381e3a41a3af07b4987", ["config.json", "kokoro-v1_0.pth"], ["voices/"]),
    "whisper": Modelo("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120",
                      ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]),
    "base": Modelo("stabilityai/stable-diffusion-xl-base-1.0", "462165984030d82259a11f4367a4eed129e94a7b",
                   _SDXL + ["vae_1_0/diffusion_pytorch_model.fp16.safetensors"]),
    "adapter": Modelo("h94/IP-Adapter", "018e402774aeeddd60609b4ecdb7e298259dc729",
                      ["models/image_encoder/config.json", "models/image_encoder/model.safetensors",
                       "sdxl_models/ip-adapter-plus-face_sdxl_vit-h.safetensors"]),
}

# orden de descarga y perfiles; estimado (GB) hasta saber el tamaño exacto
COMPONENTES = {
    "ffmpeg": {"gb": 0.1, "perfiles": ("voz", "esencial", "completo")},
    "ia": {"gb": 3.4, "perfiles": ("esencial", "completo")},
    "turbo": {"gb": 6.9, "perfiles": ("esencial", "completo")},
    "kokoro": {"gb": 0.35, "perfiles": ("esencial", "completo")},
    "whisper": {"gb": 0.47, "perfiles": ("esencial", "completo")},
    "base": {"gb": 7.1, "perfiles": ("completo",)},
    "adapter": {"gb": 3.2, "perfiles": ("completo",)},
}
MODULOS_IA = ("torch", "diffusers", "transformers", "kokoro", "faster_whisper", "cv2", "soundfile", "en_core_web_sm")


class Pausado(Exception):
    pass


def comercial() -> bool:
    return getattr(rutas, "COMERCIAL", False)


def espacio_necesario_gb() -> int:
    """Estimación conservadora de lo pendiente, incluida instalación y temporales."""
    faltan = [cid for cid in COMPONENTES if not presente(cid)]
    pendiente = 0.0
    for cid in faltan:
        if cid in MODELOS:
            carpeta = _snapshot(MODELOS[cid])
            existentes = sum(p.stat().st_size for p in carpeta.rglob("*") if p.is_file()) if carpeta.exists() else 0
        elif cid == "ia":
            carpeta = rutas.DESCARGAS / "paquetes"
            existentes = sum(p.stat().st_size for p in carpeta.glob("*") if p.is_file()) if carpeta.exists() else 0
        else:
            existentes = sum(p.stat().st_size for p in rutas.DESCARGAS.glob("*.zip*") if p.is_file())
        pendiente += max(0.0, COMPONENTES[cid]["gb"] - existentes / 1e9)
    return math.ceil(pendiente + (2 * COMPONENTES["ia"]["gb"] if "ia" in faltan else 0) + 2)


# ---------------------------------------------------------------- equipo

_equipo_cache: tuple[float, dict] | None = None


def equipo(fresco: bool = False) -> dict:
    """GPU (nvidia-smi viene con el driver), VRAM, driver y espacio libre; y si cumple los requisitos.
    Se guarda 30 s: la interfaz pregunta el estado cada segundo mientras descarga."""
    global _equipo_cache
    if not fresco and _equipo_cache and time.time() - _equipo_cache[0] < 30:
        return _equipo_cache[1]
    _equipo_cache = (time.time(), _equipo())
    return _equipo_cache[1]


def _equipo() -> dict:
    gpu = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            nombre, mem, driver = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            gpu = {"nombre": nombre, "vram_gb": round(int(mem) / 1024, 1), "driver": driver}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    rutas.DATOS.mkdir(parents=True, exist_ok=True)
    libre = shutil.disk_usage(rutas.DATOS).free / 1e9
    problemas = []
    if not gpu:
        problemas.append("sin_gpu")
    else:
        if gpu["vram_gb"] < REQUISITOS["vram_gb"] - 0.3:  # "6 GB" suele reportar 6.0 o 5.9
            problemas.append("vram")
        try:
            if float(gpu["driver"]) < REQUISITOS["driver_min"]:
                problemas.append("driver")
        except ValueError:
            pass
    requisitos = dict(REQUISITOS)
    if comercial():
        requisitos["disco_gb"] = espacio_necesario_gb()
    if libre < requisitos["disco_gb"]:
        problemas.append("disco")
    return {"gpu": gpu, "disco_libre_gb": round(libre, 1), "ruta_datos": str(rutas.DATOS), "requisitos": requisitos,
            "problemas": problemas, "cumple": not problemas}


# ---------------------------------------------------------------- qué hay instalado

def _hub() -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE
    return Path(HF_HUB_CACHE)


def _snapshot(m: Modelo) -> Path:
    return _hub() / ("models--" + m.repo.replace("/", "--")) / "snapshots" / m.commit


def _ffmpeg_presente() -> bool:
    if rutas.INSTALADO:
        return (rutas.FFMPEG / "ffmpeg.exe").exists() and (rutas.FFMPEG / "ffprobe.exe").exists()
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _marca_ia() -> Path:
    return rutas.LIBS / ".dcrypto-ia.json"


def _hash_req() -> str:
    return hashlib.sha256(REQ_IA.read_bytes()).hexdigest()[:16]


def _ia_presente() -> bool:
    if rutas.INSTALADO:
        try:
            return json.loads(_marca_ia().read_text(encoding="utf-8")).get("req") == _hash_req()
        except (OSError, ValueError):
            return False
    importlib.invalidate_caches()
    return all(importlib.util.find_spec(m) for m in MODULOS_IA)


def _modelo_presente(clave: str) -> bool:
    # en desarrollo (o bajado por la app antes): están los archivos fijos del commit
    m = MODELOS[clave]
    snap = _snapshot(m)
    if comercial() and m.carpetas and not (snap / ".dcrypto-completo").is_file():
        return False  # completar el plan de voces; reutiliza cada archivo ya descargado
    return snap.is_dir() and all((snap / f).is_file() and (snap / f).stat().st_size > 0 for f in m.archivos) and \
        all((snap / c).is_dir() and any(p.is_file() and not p.name.endswith(".part") and p.stat().st_size > 0
                                      for p in (snap / c).rglob("*")) for c in m.carpetas)


def presente(cid: str) -> bool:
    if cid == "ffmpeg":
        return _ffmpeg_presente()
    if cid == "ia":
        return _ia_presente()
    return _modelo_presente(cid)


# ---------------------------------------------------------------- descarga de un archivo

class Velocidad:
    """Bytes por segundo de los últimos ~5 s."""
    def __init__(self):
        self.muestras: list[tuple[float, int]] = []
        self.lock = threading.Lock()

    def sumar(self, n: int):
        with self.lock:
            ahora = time.time()
            self.muestras.append((ahora, n))
            self.muestras = [(t, b) for t, b in self.muestras if ahora - t < 5]

    def valor(self) -> float:
        with self.lock:
            self.muestras = [(t, b) for t, b in self.muestras if time.time() - t < 5]  # sin bajar nada: 0
            if len(self.muestras) < 2:
                return 0.0
            dt = max(time.time() - self.muestras[0][0], 0.5)
            return sum(b for _, b in self.muestras) / dt


def bajar(url: str, destino: Path, tam: int | None, parar: threading.Event, avance, sha256: str | None = None) -> None:
    """Descarga reanudable: sigue el .part si existe (Range). avance(n) por cada bloque. Pausado si 'parar'."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    part = destino.with_name(destino.name + ".part")
    for intento in range(4):
        hecho = part.stat().st_size if part.exists() else 0
        if tam and hecho > tam:
            part.unlink()
            hecho = 0
        if tam and hecho == tam:
            break
        cab = dict(UA)
        if hecho:
            cab["Range"] = f"bytes={hecho}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=30) as r:
                if hecho and r.status != 206:  # el servidor no reanuda: desde cero
                    avance(-hecho)
                    hecho = 0
                with open(part, "ab" if hecho else "wb") as f:
                    while True:
                        if parar.is_set():
                            raise Pausado()
                        bloque = r.read(1 << 20)
                        if not bloque:
                            break
                        f.write(bloque)
                        avance(len(bloque))
            break
        except Pausado:
            raise
        except OSError as e:  # red caída, timeout, 5xx: reintento con espera
            if intento == 3:
                raise RuntimeError(f"{destino.name}: {e}") from e
            time.sleep(3 * (intento + 1))
    if tam and part.stat().st_size != tam:
        raise RuntimeError(f"{destino.name}: tamaño {part.stat().st_size} en vez de {tam}")
    if sha256:
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for b in iter(lambda: f.read(1 << 22), b""):
                h.update(b)
        if h.hexdigest() != sha256:
            part.unlink()
            raise RuntimeError(f"{destino.name}: el archivo llegó dañado (SHA256 distinto). Vuelve a intentarlo.")
    part.replace(destino)


def _tam_url(url: str) -> int | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA), timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- plan de cada componente

@dataclass
class Archivo:
    url: str
    destino: Path
    tam: int | None
    sha256: str | None = None


def archivo_correcto(a: Archivo) -> bool:
    if not a.destino.is_file() or a.destino.stat().st_size == 0:
        return False
    if a.tam is not None and a.destino.stat().st_size != a.tam:
        return False
    if a.sha256:
        h = hashlib.sha256()
        with a.destino.open("rb") as f:
            for bloque in iter(lambda: f.read(1 << 22), b""):
                h.update(bloque)
        return h.hexdigest() == a.sha256
    return True


def _plan_modelo(m: Modelo) -> list[Archivo]:
    """Archivos del commit fijo con su tamaño (API de Hugging Face)."""
    url = f"https://huggingface.co/api/models/{m.repo}/tree/{m.commit}?recursive=true"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        arbol = json.loads(r.read().decode("utf-8"))
    tam = {x["path"]: (x.get("lfs") or {}).get("size") or x.get("size") for x in arbol if x.get("type") == "file"}
    lista = list(m.archivos) + [p for p in tam for c in m.carpetas if p.startswith(c)]
    snap = _snapshot(m)
    faltan = [p for p in lista if p not in tam]
    if faltan:
        raise RuntimeError(f"{m.repo}@{m.commit[:8]}: no encuentro {', '.join(faltan)}")
    return [Archivo(f"https://huggingface.co/{m.repo}/resolve/{m.commit}/{p}", snap / p, tam[p]) for p in lista]


def _plan_ia(registro) -> list[Archivo]:
    """pip calcula qué paquetes faltan (sin instalar nada); los bajamos nosotros, con su SHA256."""
    reporte = rutas.DESCARGAS / "ia-reporte.json"
    rutas.DESCARGAS.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--disable-pip-version-check",
           "--report", str(reporte), "--index-url", TORCH_INDEX, "--extra-index-url", "https://pypi.org/simple",
           "-r", str(REQ_IA)]
    registro("pip: calculando paquetes…")
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("pip no pudo calcular los paquetes:\n" + (r.stderr or r.stdout)[-1500:])
    instalar = json.loads(reporte.read_text(encoding="utf-8")).get("install", [])
    carpeta = rutas.DESCARGAS / "paquetes"
    archivos = []
    for p in instalar:
        info = p["download_info"]
        url = info["url"]
        # el índice de PyTorch da "torch-2.6.0%2Bcu124-…whl": pip necesita el nombre real ("+cu124")
        nombre = urllib.parse.unquote(url.split("/")[-1].split("#")[0].split("?")[0])
        sha = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
        archivos.append(Archivo(url, carpeta / nombre, None, sha))
    registro(f"pip: {len(archivos)} paquetes; midiendo tamaños…")
    for a in archivos:
        a.tam = _tam_url(a.url)
    return archivos


def plan(cid: str, registro) -> list[Archivo]:
    if cid == "ffmpeg":
        for url in FFMPEG_URLS:  # la primera que responda
            tam = _tam_url(url)
            if tam:
                return [Archivo(url, rutas.DESCARGAS / url.rsplit("/", 1)[1], tam)]
        raise RuntimeError("ffmpeg: no responde ni GitHub ni gyan.dev. ¿Hay internet?")
    if cid == "ia":
        return _plan_ia(registro)
    return _plan_modelo(MODELOS[cid])


# ---------------------------------------------------------------- terminar cada componente

def _terminar_ffmpeg(archivos: list[Archivo]) -> None:
    z = archivos[0].destino
    rutas.FFMPEG.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as zz:
        for n in zz.namelist():
            base = n.rsplit("/", 1)[-1]
            if n.endswith("/bin/ffmpeg.exe") or n.endswith("/bin/ffprobe.exe"):
                (rutas.FFMPEG / base).write_bytes(zz.read(n))
            elif base in ("LICENSE", "README.txt") and n.count("/") == 1:
                (rutas.FFMPEG / ("LICENSE.txt" if base == "LICENSE" else base)).write_bytes(zz.read(n))
    z.unlink(missing_ok=True)


def _terminar_ia(archivos: list[Archivo], registro) -> None:
    registro("pip: instalando paquetes (sin internet, desde lo descargado)…")
    cmd = [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-build-isolation", "--upgrade",
           "--disable-pip-version-check", "--no-warn-script-location", "--target", str(rutas.LIBS),
           *[str(a.destino) for a in archivos]]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("pip no pudo instalar:\n" + (r.stderr or r.stdout)[-1500:])
    _marca_ia().write_text(json.dumps({"req": _hash_req(), "fecha": time.time()}), encoding="utf-8")
    shutil.rmtree(rutas.DESCARGAS / "paquetes", ignore_errors=True)
    importlib.invalidate_caches()


def _terminar_modelo(cid: str) -> None:
    m = MODELOS[cid]
    raiz = _hub() / ("models--" + m.repo.replace("/", "--"))
    (raiz / "refs").mkdir(parents=True, exist_ok=True)
    (raiz / "refs" / "main").write_text(m.commit, encoding="utf-8")
    (_snapshot(m) / ".dcrypto-completo").write_text("1", encoding="utf-8")


# ---------------------------------------------------------------- el gestor

SUBETAPAS_VERIFICACION = frozenset((
    "ffmpeg", "pytorch_importacion", "pytorch_cuda", "kokoro_importacion", "kokoro_carga",
    "whisper_importacion", "ctranslate2_importacion", "whisper_carga", "sintesis", "transcripcion",
    "ffmpeg_audio", "liberacion_memoria", "sdxl_importacion", "sdxl_carga", "imagen_generacion_1",
    "imagen_generacion_2", "imagen_guardado"))


def _texto_capturado(valor) -> str:
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return valor if isinstance(valor, str) else ""


class Preparador:
    def __init__(self):
        self.lock = threading.Lock()
        self.hilo: threading.Thread | None = None
        self.parar = threading.Event()
        self.velocidad = Velocidad()
        self.fase = "inactivo"  # inactivo | descargando | pausado | error | verificando | listo
        self.perfil: str | None = None
        self.items: dict[str, dict] = {}
        self.error: str | None = None
        self.log: list[str] = []
        self.guardado = self._leer()
        self._verificacion_actual = None
        self._diagnostico_verificacion = None  # privado: nunca se devuelve en estado()

    # --- persistencia (perfil elegido, setup visto, última verificación)
    def _leer(self) -> dict:
        try:
            return json.loads(ESTADO_ARCHIVO.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def guardar(self, **kw) -> None:
        self.guardado.update(kw)
        ESTADO_ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
        ESTADO_ARCHIVO.write_text(json.dumps(self.guardado, indent=2), encoding="utf-8")

    def registro(self, msg: str) -> None:
        self.log = (self.log + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]
        print(f"setup: {msg}", flush=True)

    # --- estado para la interfaz
    def estado(self) -> dict:
        comps = {}
        for cid, c in COMPONENTES.items():
            it = self.items.get(cid, {})
            ok = presente(cid)
            comps[cid] = {
                "presente": ok, "perfiles": c["perfiles"],
                "estado": "listo" if ok else it.get("estado", "pendiente"),
                "hecho": it.get("total") if ok and it.get("total") else it.get("hecho", 0),
                "total": it.get("total") or int(c["gb"] * 1e9), "exacto": bool(it.get("total")),
                "error": None if ok else it.get("error"),
                # en desarrollo pip y ffmpeg son cosa del venv y del PATH
                "gestionable": rutas.INSTALADO or cid not in ("ia", "ffmpeg"),
            }
        return {"fase": self.fase, "perfil": "completo" if comercial() else self.perfil or self.guardado.get("perfil"), "componentes": comps,
                "velocidad": round(self.velocidad.valor()), "error": self.error, "log": self.log[-30:],
                "instalado": rutas.INSTALADO, "comercial": comercial(),
                "visto": bool(self.guardado.get("visto")) and (not comercial() or self.completo()),
                "verificacion": self._verificacion_actual or self.guardado.get("verificacion"), "equipo": equipo()}

    # --- acciones
    def iniciar(self, perfil: str) -> None:
        if comercial() and perfil != "completo":
            raise ValueError("srv.setup_completo")
        if perfil not in ("voz", "esencial", "completo"):
            raise ValueError(perfil)
        with self.lock:
            if self.hilo and self.hilo.is_alive():
                return
            if comercial() and not equipo(fresco=True)["cumple"]:
                raise ValueError("srv.setup_requisitos")
            self.perfil, self.error, self.fase = perfil, None, "descargando"
            self.guardar(perfil=perfil)
            self.parar.clear()
            self.hilo = threading.Thread(target=self._correr, args=(perfil,), daemon=True)
            self.hilo.start()

    def pausar(self) -> None:
        self.parar.set()

    def _correr(self, perfil: str) -> None:
        try:
            if comercial() and perfil != "completo":
                raise ValueError("srv.setup_completo")
            pendientes = [cid for cid, c in COMPONENTES.items() if perfil in c["perfiles"] and not presente(cid)
                          and (rutas.INSTALADO or cid not in ("ia", "ffmpeg"))]
            for cid in pendientes:
                if self.parar.is_set():
                    raise Pausado()
                self._componente(cid)
            self.fase = "listo" if not self.parar.is_set() else "pausado"
            self.registro("descargas completas")
            if (pendientes or comercial()) and self.fase == "listo":
                self.verificar()  # la prueba final sola, al terminar de bajar
        except Pausado:
            self.fase = "pausado"
            self.registro("en pausa")
        except Exception as e:  # noqa: BLE001  (red, disco, pip: se muestra y se puede reintentar)
            self.fase, self.error = "error", str(e)
            self.registro(f"ERROR: {e}")

    def _componente(self, cid: str) -> None:
        it = self.items.setdefault(cid, {})
        it.update(estado="preparando", error=None)
        self.registro(f"{cid}: preparando")
        try:
            archivos = plan(cid, self.registro)
            correctos = {a.destino for a in archivos if archivo_correcto(a)}
            total = sum(a.tam or 0 for a in archivos)
            hecho = 0
            for a in archivos:  # lo ya bajado (completo o .part) cuenta desde el principio
                if a.destino in correctos:
                    hecho += a.tam or a.destino.stat().st_size
                elif a.destino.with_name(a.destino.name + ".part").exists():
                    hecho += a.destino.with_name(a.destino.name + ".part").stat().st_size
            it.update(estado="descargando", total=total, hecho=hecho)
            if comercial():
                falta = sum(max(0, (a.tam or int(COMPONENTES[cid]["gb"] * 1e9 / max(1, len(archivos))))
                                - (a.destino.stat().st_size if a.destino in correctos else
                                   a.destino.with_name(a.destino.name + ".part").stat().st_size
                                   if a.destino.with_name(a.destino.name + ".part").exists() else 0))
                            for a in archivos)
                reserva = max(total * 2, int(2 * COMPONENTES[cid]["gb"] * 1e9)) if cid == "ia" else 0
                if shutil.disk_usage(rutas.DATOS).free < falta + reserva + 2 * 10**9:
                    raise RuntimeError("Espacio insuficiente para completar la descarga e instalación. Libera espacio y reintenta.")

            def avance(n):
                it["hecho"] = it.get("hecho", 0) + n
                if n > 0:
                    self.velocidad.sumar(n)

            for a in archivos:
                if a.destino in correctos:
                    continue
                bajar(a.url, a.destino, a.tam, self.parar, avance, a.sha256)
            it["estado"] = "instalando"
            if cid == "ffmpeg":
                _terminar_ffmpeg(archivos)
            elif cid == "ia":
                _terminar_ia(archivos, self.registro)
            else:
                _terminar_modelo(cid)
            it.update(estado="listo", hecho=total)
            self.registro(f"{cid}: listo ({total / 1e9:.2f} GB)")
        except Pausado:
            it["estado"] = "pausado"
            raise
        except Exception as e:
            it.update(estado="error", error=str(e))
            raise

    # --- verificación: en otro proceso (suelta la VRAM al terminar y ve las librerías recién instaladas)
    def verificar(self) -> dict:
        try:
            return self._verificar()
        except Exception as exc:  # última barrera del hilo: ni protocolo ni persistencia pueden congelarlo
            diagnostico = self._diagnostico_verificacion or {}
            diagnostico["excepcion_backend"] = type(exc).__name__
            self._diagnostico_verificacion = diagnostico
            res = self._verificacion_actual or {"pasos": {}}
            res.update(ok=False, error="No se completó la verificación; consulta el diagnóstico local.")
            self._verificacion_actual = res
            self.error = res["error"]
            try:
                self.guardar(verificacion=res, diagnostico_verificacion=diagnostico)
            except Exception:
                pass  # los objetos anteriores siguen disponibles en memoria
            return res
        finally:
            self.fase = "listo" if self._verificacion_actual and self._verificacion_actual.get("ok") else "error"

    def _verificar(self) -> dict:
        self.fase = "verificando"
        self.error = None
        res = {"ok": False, "pasos": {}}
        diagnostico = {"codigo_salida": None, "stderr": "", "stdout": "", "eventos": [],
                       "ultima_subetapa": None}
        self._verificacion_actual, self._diagnostico_verificacion = res, diagnostico
        try:
            if comercial() and not self.componentes_completos():
                raise ValueError("srv.setup_incompleto")
            self.registro("verificando: imagen de prueba y una frase de voz")
            script = Path(__file__).resolve().parent / "verificar_setup.py"
            with temporal_verificacion(diagnostico) as (_, entorno):
                r = subprocess.run([sys.executable, "-u", "-X", "utf8", "-X", "faulthandler", str(script)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   creationflags=CREATE_NO_WINDOW, timeout=1800, cwd=rutas.APP, env=entorno)
            diagnostico.update(codigo_salida=r.returncode, stdout=_texto_capturado(r.stdout),
                               stderr=_texto_capturado(r.stderr))
            resultado = json.loads(diagnostico["stdout"].strip().splitlines()[-1])
            diagnostico["resultado"] = resultado
            if not isinstance(resultado, dict) or not isinstance(resultado.get("pasos", {}), dict):
                raise ValueError("Resultado inválido")
            # Solo campos conocidos: errores/trazas del hijo se conservan en el diagnóstico local.
            campos = {"ok", "segundos", "version", "palabras", "dispositivo", "carga_s", "sintesis_s",
                      "imagen_s", "cuda", "gpu", "motor"}
            for nombre in ("ffmpeg", "voz", "imagen"):
                paso = resultado.get("pasos", {}).get(nombre)
                if paso is not None:
                    if not isinstance(paso, dict):
                        raise ValueError("Paso inválido")
                    res["pasos"][nombre] = {k: v for k, v in paso.items() if k in campos}
            res["solo_voz"] = resultado.get("solo_voz") is True
            res["ok"] = (r.returncode == 0 and resultado.get("ok") is True and
                         (not comercial() or (not res["solo_voz"] and all(
                             res["pasos"].get(k, {}).get("ok") is True for k in ("ffmpeg", "voz", "imagen")))))
            if resultado.get("subetapa_fallo") in SUBETAPAS_VERIFICACION:
                res["subetapa_fallo"] = resultado["subetapa_fallo"]
        except subprocess.TimeoutExpired as exc:
            diagnostico.update(stdout=_texto_capturado(exc.stdout), stderr=_texto_capturado(exc.stderr),
                               excepcion="TimeoutExpired")
        except Exception as exc:  # incluye errores del protocolo y de inicialización; nunca dejar verificando
            diagnostico.update(excepcion=type(exc).__name__, detalle=str(exc))
            res["ok"] = False
        if diagnostico.get('disco_verificacion', {}).get('residuo_temporal'):
            res['ok'] = False
        for linea in diagnostico["stderr"].splitlines():
            if linea.startswith("DCS_SETUP_ETAPA:"):
                etapa = linea.partition(":")[2].strip()
                if etapa in SUBETAPAS_VERIFICACION:
                    diagnostico["eventos"].append(etapa)
        if diagnostico["eventos"]:
            diagnostico["ultima_subetapa"] = diagnostico["eventos"][-1]
        codigo = diagnostico["codigo_salida"]
        diagnostico["codigo_hex"] = f"0x{codigo & 0xffffffff:08X}" if type(codigo) is int else None
        res.update(codigo_salida=codigo, codigo_hex=diagnostico["codigo_hex"],
                   ultima_subetapa=diagnostico["ultima_subetapa"])
        if not res["ok"]:
            res["error"] = ("La verificación superó el tiempo máximo; consulta el diagnóstico local."
                            if diagnostico.get("excepcion") == "TimeoutExpired" else
                            "No se completó la verificación; consulta el diagnóstico local.")
            if res.get("subetapa_fallo") or res["ultima_subetapa"]:
                res["error"] += f" Subetapa: {res.get('subetapa_fallo') or res['ultima_subetapa']}."
            if res["codigo_hex"]:
                res["error"] += f" Salida: {res['codigo_hex']}."
        res["fecha"] = time.time()
        self._verificacion_actual, self._diagnostico_verificacion = res, diagnostico
        try:
            self.guardar(verificacion=res, diagnostico_verificacion=diagnostico)
        except Exception as exc:
            diagnostico["error_guardado"] = type(exc).__name__
            res.update(ok=False, error="No se pudo guardar la verificación; el diagnóstico se conserva en memoria.")
        self.fase = "listo" if res.get("ok") else "error"
        self.error = None if res.get("ok") else res.get("error")
        try:
            for etapa in diagnostico["eventos"]:
                self.registro(f"verificación: {etapa}")
            self.registro(f"verificación: {'OK' if res.get('ok') else 'FALLO'}")
        except Exception:
            self.fase = "error"
            res.update(ok=False, error="No se pudo registrar la verificación; el diagnóstico se conserva en memoria.")
            self.error = res["error"]
        return res

    def componentes_completos(self) -> bool:
        return all(presente(cid) for cid in COMPONENTES)

    def completo(self) -> bool:
        v = self._verificacion_actual or self.guardado.get("verificacion") or {}
        return (self.componentes_completos() and bool(v.get("ok")) and not v.get("solo_voz")
                and all(v.get("pasos", {}).get(k, {}).get("ok") for k in ("ffmpeg", "voz", "imagen")))


PREPARADOR = Preparador()
