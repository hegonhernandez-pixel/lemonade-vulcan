"""Cola de trabajos en segundo plano. Un solo worker: la GPU genera una imagen a la vez.

Cada trabajo guarda sus pasos (voz/imagen por escena, ensamblaje), su log y su estado,
y la interfaz los consulta cada segundo.
"""
import itertools
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from pipeline import guion, imagenes, personajes, textos, voz, voz_local
from pipeline.producir import Opciones, carpeta, pasos, producir
from pipeline.textos import Texto, decir
from app.exportaciones import RegistroExportaciones

ACTIVOS = ("en cola", "corriendo")
# Sin trabajos durante este tiempo, los modelos (SDXL, IP-Adapter, Kokoro, whisper) salen de la VRAM y la RAM.
# Se vuelven a cargar solos en el siguiente trabajo, como tras "Liberar GPU".
INACTIVIDAD_S = 600
_REVISION_S = 30  # cada cuánto mira el vigía si toca soltarlos


@dataclass
class Trabajo:
    id: int
    slug: str
    tipo: str  # video | prueba | miniatura | voz | imagen | liberar | escribir
    escena: int | None = None  # 1-based, solo para voz / imagen
    opciones: Opciones = field(default_factory=Opciones)
    estado: str = "en cola"  # en cola | corriendo | listo | error | cancelado
    pasos: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)  # str o Texto (se traduce al responder)
    error: str | None = None  # str o Texto
    creado: float = field(default_factory=time.time)
    inicio: float | None = None
    fin: float | None = None
    resultado: dict | None = None
    cancelar: Callable[[], None] | None = field(default=None, repr=False)

    def avisar(self, paso: str, estado: str):
        for p in self.pasos:
            if p["id"] == paso:
                p["estado"] = estado
                if estado == "corriendo":
                    p["inicio"] = time.time()
                elif "inicio" in p:
                    p["segundos"] = round(time.time() - p["inicio"], 1)

    def a_dict(self) -> dict:
        hechos = sum(p["estado"] in ("ok", "cache") for p in self.pasos)
        return {
            "id": self.id, "slug": self.slug, "tipo": self.tipo, "escena": self.escena,
            "estado": self.estado, "error": self.error,
            "pasos": [{k: v for k, v in p.items() if k != "inicio"} for p in self.pasos],
            "progreso": hechos / len(self.pasos) if self.pasos else (1.0 if self.estado == "listo" else 0.0),
            "log": self.log[-300:],
            "creado": self.creado, "inicio": self.inicio, "fin": self.fin, "resultado": self.resultado,
        }


_cola: "queue.Queue[Trabajo]" = queue.Queue()
_todos: list[Trabajo] = []
_ids = itertools.count(1)
_lock = threading.Lock()
_hilo_worker: threading.Thread | None = None
_actual: Trabajo | None = None
_ultimo_uso = time.time()  # fin del último trabajo del worker (para soltar los modelos sin uso)


class _Salida:
    """Reemplaza sys.stdout: lo que imprime el pipeline va a la consola y, si viene del worker,
    también al log del trabajo en curso. Así el pipeline no necesita saber nada de la app."""

    def __init__(self, real):
        self.real = real
        self._pendiente = ""

    def write(self, s):
        self.real.write(s)
        if threading.current_thread() is _hilo_worker and _actual is not None:
            self._pendiente += s
            *lineas, self._pendiente = self._pendiente.split("\n")
            _actual.log.extend(l for l in lineas if l.strip())
        return len(s)

    def texto(self, t: Texto, sangria: str = ""):
        """textos.decir(): a la consola en español y al log del trabajo con su clave (se traduce al mostrarlo)."""
        self.real.write(sangria + t + "\n")
        if threading.current_thread() is _hilo_worker and _actual is not None:
            _actual.log.append(t)

    def flush(self):
        self.real.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


def encolar(slug: str, tipo: str, escena: int | None = None, opciones: Opciones | None = None) -> Trabajo:
    with _lock:
        # pedir dos veces lo mismo mientras sigue en cola no suma trabajo
        for t in _todos:
            if t.estado == "en cola" and (t.slug, t.tipo, t.escena) == (slug, tipo, escena):
                t.opciones = opciones or t.opciones
                return t
        t = Trabajo(id=next(_ids), slug=slug, tipo=tipo, escena=escena, opciones=opciones or Opciones())
        _todos.append(t)
        del _todos[:-100]
    _cola.put(t)
    return t


def lanzar(slug: str, tipo: str, fn: Callable[[Trabajo], dict | None]) -> Trabajo:
    """Trabajo que no usa la GPU (escribir un guion con Claude): corre ya en su propio hilo, sin esperar
    detrás de un render. fn(t) devuelve el resultado; puede poner t.cancelar para poder cortarlo."""
    t = Trabajo(id=next(_ids), slug=slug, tipo=tipo, estado="corriendo", inicio=time.time())
    with _lock:
        _todos.append(t)
        del _todos[:-100]

    def correr():
        try:
            t.resultado = fn(t)
            t.estado = "listo"
        except Exception as e:  # noqa: BLE001 - el error queda en el trabajo, como en el worker
            if t.estado == "cancelado":
                return
            t.estado, t.error = "error", textos.de(e)
            t.resultado = getattr(e, "resultado", None)
        finally:
            t.fin = time.time()

    threading.Thread(target=correr, daemon=True, name=f"{tipo}-{t.id}").start()
    return t


def buscar(id_: int) -> Trabajo | None:
    with _lock:
        return next((t for t in _todos if t.id == id_), None)


def cancelar(id_: int) -> bool:
    with _lock:
        t = next((t for t in _todos if t.id == id_), None)
        if not t or t.estado not in ACTIVOS:
            return False
        # FFmpeg y las etapas de GPU no ofrecen interrupción cooperativa.
        if t.tipo in ("video", "estudio") and t.estado == "corriendo":
            return False
        t.estado = "cancelado"
    if t.cancelar:
        t.cancelar()
    return True


def listar(slug: str | None = None) -> list[Trabajo]:
    with _lock:
        return [t for t in _todos if slug is None or t.slug == slug]


def olvidar(slug: str):
    """El guion se fue a la papelera: sus trabajos en cola se cancelan (el worker ya no los corre)."""
    with _lock:
        for t in _todos:
            if t.slug == slug and t.estado == "en cola":
                t.estado = "cancelado"


def activo(slug: str, tipo: str = "video") -> bool:
    return any(t.tipo == tipo and t.estado in ACTIVOS for t in listar(slug))


def pendientes() -> list[Trabajo]:
    """Lo que se perdería al cerrar la app: trabajos corriendo o en cola (sin los internos de "liberar")."""
    return [t for t in listar() if t.estado in ACTIVOS and t.tipo != "liberar"]


def cancelar_todos() -> bool:
    """Para cerrar la app: los trabajos en cola se cancelan; los que corren se cortan si se puede.
    Devuelve True si queda alguno corriendo (un render o una imagen en la GPU no se interrumpen: los corta
    el cierre del proceso)."""
    with _lock:
        en_marcha = [t for t in _todos if t.estado in ACTIVOS]
        for t in en_marcha:
            if t.estado == "en cola":
                t.estado = "cancelado"
    sigue = False
    for t in en_marcha:
        if t.estado == "corriendo":
            if t.cancelar:
                t.estado = "cancelado"
                try:
                    t.cancelar()
                except Exception:  # noqa: BLE001 - se está cerrando: lo que no se corte lo corta el cierre
                    sigue = True
            else:
                sigue = True
    return sigue or _actual is not None


def ocupado() -> bool:
    """El worker de la GPU está corriendo algo (no se pueden soltar los modelos debajo de él)."""
    return _actual is not None


def liberar_modelos() -> None:
    imagenes.liberar()
    voz_local.liberar()


def _modelos_cargados() -> bool:
    return imagenes.modelo_cargado() is not None or voz_local.cargado()


def _ejecutar(t: Trabajo, ruta_de):
    if t.tipo == "estudio":
        from pipeline.estudio_imagenes import ejecutar
        ejecutar(t)
        return

    if t.tipo == "liberar":
        imagenes.liberar()
        voz_local.liberar()
        if t.slug == "_inactivo":  # lo encoló el vigía tras INACTIVIDAD_S sin uso
            decir("log.modelo_inactivo", sangria=0, minutos=INACTIVIDAD_S // 60)
        else:
            decir("log.modelo_liberado", sangria=0)
        return

    if t.tipo == "personaje":  # tira de prueba: de frente, de perfil, cuerpo entero
        p = personajes.cargar(t.slug.removeprefix("@"))
        t.pasos = [{"id": f"pose-{i}", "nombre": Texto("paso.pose", n=i, total=len(imagenes.POSES)), "estado": "pendiente"}
                   for i in range(1, len(imagenes.POSES) + 1)]
        ruta = imagenes.probar_personaje(p, t.opciones.demo, t.opciones.calidad, avisar=t.avisar)
        decir("log.listo", sangria=0, ruta=ruta)
        return

    ruta_guion = ruta_de(t.slug)  # solo los trabajos de un guion tienen archivo (liberar y personaje no)

    if t.tipo == "video":
        g = guion.cargar(ruta_guion)  # estricto: no se produce un guion incompleto
        t.pasos = [{"id": i, "nombre": n, "estado": "pendiente"} for i, n in pasos(g, t.opciones)]
        decir("log.guion", sangria=0, titulo=g.titulo, n=len(g.escenas))
        from app import licencia
        if t.opciones.solo or t.opciones.limite:
            raise ValueError("Una exportación completa no puede ser parcial.")
        if g.slug != t.slug:
            raise ValueError("El proyecto no coincide con la tarea de exportación.")
        with licencia._lock:
            t.opciones.marca_prueba = licencia.preparar_exportacion(t.slug)
            modo = "prueba" if t.opciones.marca_prueba else "pago" if licencia.rutas.INSTALADO else "desarrollo"
        registro = RegistroExportaciones(licencia.rutas.DATOS)
        intento = registro.crear(t.slug, modo)
        t.resultado = {"exportacion": intento["id"]}

        def al_renderizar(duracion, tamano):
            nonlocal intento
            intento = registro.transicionar(intento["id"], "renderizando", intento["revision"],
                                            duracion_esperada=duracion, tamano_esperado=list(tamano))

        try:
            anterior = registro.huella_publica(t.slug)
            producir(g, t.opciones, avisar=t.avisar, destino_mp4=registro.ruta_video(intento["id"]),
                     al_renderizar=al_renderizar)
            # Ancla previa al render; aún no es una validación del MP4 nuevo.
            intento = registro.transicionar(intento["id"], "render_terminado", intento["revision"],
                                            miniatura="lista", validacion={"publicacion": {"anterior": anterior}})
            final = registro.finalizar(intento["id"])
        except Exception:
            # Solo un render incompleto se descarta. Lo validado/liquidado se reintenta.
            try:
                actual = registro.leer(intento["id"])
                if actual["estado"] in ("preparado", "renderizando"):
                    registro.transicionar(actual["id"], "fallido", actual["revision"])
            except RuntimeError:
                pass  # el diario anterior se conserva si tampoco se puede escribir
            raise
        decir("log.listo", sangria=0, ruta=final)
        return

    if t.tipo in ("prueba", "miniatura"):
        if t.tipo == "prueba":
            from app import licencia
            if licencia.comercial():
                t.opciones.marca_prueba = licencia.preparar_exportacion(t.slug)
        g = guion.cargar(ruta_guion, estricto=False)
        nombre = Texto("paso.miniatura") if t.tipo == "miniatura" else Texto("paso.prueba", s=f"{t.opciones.limite:.0f}")
        t.pasos = [{"id": t.tipo, "nombre": nombre, "estado": "pendiente"}]
        t.avisar(t.tipo, "corriendo")
        producir(g, t.opciones)
        t.avisar(t.tipo, "ok")
        return

    g = guion.cargar(ruta_guion, estricto=False)
    if not t.escena or t.escena > len(g.escenas):
        raise textos.error("err.escena_no_existe", n=t.escena)
    e = g.escenas[t.escena - 1]
    destino = carpeta(g)
    op = t.opciones
    if t.tipo == "voz":
        if not e.texto:
            raise textos.error("err.escena_sin_texto", n=e.idx)
        t.pasos = [{"id": f"voz-{e.idx}", "nombre": Texto("paso.voz", n=e.idx), "estado": "pendiente"}]
        t.avisar(f"voz-{e.idx}", "corriendo")
        info, cache = voz.generar_escena(g, e, destino, op.demo, op.rate)
        decir("log.voz_escena_cache" if cache else "log.voz_escena", sangria=0, n=f"{e.idx:02d}", s=f"{info['duracion']:.1f}")
        t.avisar(f"voz-{e.idx}", "cache" if cache else "ok")
    elif t.tipo == "imagen":
        if e.media:
            raise textos.error("err.escena_media", n=e.idx, media=e.media)
        if not e.prompt:
            raise textos.error("err.escena_sin_prompt", n=e.idx)
        t.pasos = [{"id": f"img-{e.idx}", "nombre": Texto("paso.imagen", n=e.idx), "estado": "pendiente"}]
        t.avisar(f"img-{e.idx}", "corriendo")
        t0 = time.time()
        q = imagenes.calidad_de(e, op.calidad)
        _, cache = imagenes.generar_escena(g, e, destino, op.demo, op.calidad)
        decir("log.imagen_escena_cache" if cache else "log.imagen_escena_ok", sangria=0, n=f"{e.idx:02d}", calidad=q,
              s=f"{time.time() - t0:.1f}")
        t.avisar(f"img-{e.idx}", "cache" if cache else "ok")
    else:
        raise textos.error("err.tipo_trabajo", tipo=t.tipo)


def soltar_si_inactivo(ahora: float | None = None) -> bool:
    """Tras INACTIVIDAD_S sin trabajos, encola la descarga de los modelos (la hace el worker, nunca debajo de
    una generación). True si la encoló."""
    ahora = time.time() if ahora is None else ahora
    if ahora - _ultimo_uso < INACTIVIDAD_S or _actual is not None or not _modelos_cargados():
        return False
    with _lock:
        if any(t.estado in ACTIVOS for t in _todos):
            return False
    encolar("_inactivo", "liberar")
    return True


def _vigia():
    while True:
        time.sleep(_REVISION_S)
        try:
            soltar_si_inactivo()
        except Exception:  # noqa: BLE001 - el vigía nunca muere
            traceback.print_exc()


def _bucle(ruta_de):
    global _actual, _ultimo_uso
    while True:
        t = _cola.get()
        with _lock:
            if t.estado != "en cola":  # cancelado mientras esperaba
                continue
            _actual, t.estado, t.inicio = t, "corriendo", time.time()
        try:
            _ejecutar(t, ruta_de)
            t.estado = "listo"
        except Exception as e:  # el worker nunca muere: el error queda en el trabajo
            t.estado, t.error = "error", textos.de(e)
            for p in t.pasos:
                if p["estado"] == "corriendo":
                    p["estado"] = "error"
            t.log.append(Texto("log.error", error=t.error))
            if not isinstance(e, (ValueError, RuntimeError)):
                t.log.extend(traceback.format_exc().splitlines()[-8:])
            if "out of memory" in t.error.lower():
                imagenes.liberar()
                t.log.append(Texto("log.sin_vram"))
        finally:
            t.fin = _ultimo_uso = time.time()
            _actual = None


def iniciar(ruta_de):
    """ruta_de(slug) -> Path del guion. Arranca el worker una sola vez."""
    global _hilo_worker
    if _hilo_worker:
        return
    sys.stdout = _Salida(sys.stdout)
    _hilo_worker = threading.Thread(target=_bucle, args=(ruta_de,), daemon=True, name="worker-gpu")
    _hilo_worker.start()
    threading.Thread(target=_vigia, daemon=True, name="vigia-inactividad").start()
