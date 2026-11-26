"""DcryptoStudio.exe: arranca la app instalada sin consola y la deja en la bandeja del sistema.

- Busca un puerto libre desde el 8765 (el de app/config.py) y arranca runtime\\python.exe app\\server.py
  con los datos del usuario en %LOCALAPPDATA%\\DcryptoStudio (DCRYPTO_DATA) y ffmpeg en el PATH.
- Abre el navegador cuando el servidor responde.
- Icono en la bandeja (en español o inglés, según Windows): Abrir Dcrypto Studio, Reiniciar, Salir. Al salir
  cierra el servidor (y lo que haya lanzado, p. ej. ffmpeg).
- "Cerrar Dcrypto Studio" en la interfaz: el servidor escribe el archivo de salida (DCRYPTO_LAUNCHER_QUIT) y
  el lanzador se cierra igual que con Salir.
- Una sola instancia. Abrir otra vez: si la que corre responde, solo se abre el navegador; si su servidor
  murió, la que corre lo reinicia (launcher.abrir); si la que corre no contesta (colgada), se ofrece cerrarla
  y arrancar de nuevo. Un launcher.json viejo (de un lanzador que ya no existe) no bloquea nada.
- Logs en %LOCALAPPDATA%\\DcryptoStudio\\logs\\ (launcher.log y server.log).
- Modos para el instalador (sin logs ni arranque): --check-stopped, --jobs y --quit-and-wait <carpeta>.

Se distribuye como .pyw con Python embebido. El control de actualización usa psutil ya incluido.
"""
import ctypes
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

NOMBRE = "Dcrypto Studio"
PUERTO_BASE = 8765
CREATE_NO_WINDOW = 0x08000000
ARRANQUE_MAX_S = 120  # un lanzador "arrancando" desde hace más que esto se considera colgado
ESPERA_ABRIR_S = 10  # lo que espera una segunda apertura a que la instancia que corre conteste

# carpeta de la app: la del .exe (PyInstaller), la del .pyw (runtime\pythonw.exe DcryptoStudio.pyw, junto a
# runtime\) o dist\DcryptoStudio (probando build\launcher.py a mano)
if getattr(sys, "frozen", False):
    RAIZ = Path(sys.executable).parent
elif (Path(__file__).resolve().parent / "runtime").is_dir():
    RAIZ = Path(__file__).resolve().parent
else:
    RAIZ = Path(__file__).resolve().parent.parent / "dist" / "DcryptoStudio"
DATOS = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "DcryptoStudio"

log = logging.getLogger("launcher")


# Rutas en DATOS (funciones: los tests cambian DATOS)
def ruta_logs() -> Path:
    return DATOS / "logs"


def ruta_estado() -> Path:
    return DATOS / "launcher.json"  # pid, puerto y estado de la instancia que corre


def ruta_salir() -> Path:
    # "--quit" (desinstalador), "--quit-and-wait" (instalador) y "Cerrar Dcrypto Studio" (servidor) lo crean:
    # la instancia que corre se cierra
    return DATOS / "launcher.quit"


def ruta_abrir() -> Path:
    return DATOS / "launcher.abrir"  # una segunda apertura pide a la instancia que corre que abra (o reviva)


def espanol() -> bool:
    """Idioma de Windows: la bandeja y los avisos del lanzador no ven el idioma de la interfaz web."""
    try:
        return ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0A  # LANG_SPANISH
    except (AttributeError, OSError):
        return False


def tr(es: str, en: str) -> str:
    return es if espanol() else en


def configurar_log():
    ruta_logs().mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    h = RotatingFileHandler(ruta_logs() / "launcher.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)


def actualizacion_en_curso() -> bool:
    """Un error de Windows no autoriza arrancar durante una actualización."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    abrir = kernel.OpenMutexW
    abrir.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    abrir.restype = ctypes.c_void_p
    cerrar = kernel.CloseHandle
    cerrar.argtypes = [ctypes.c_void_p]
    cerrar.restype = ctypes.c_int
    h = abrir(0x00100000, False, 'Local\\DcryptoStudioUpdating')
    if h:
        cerrar(h)
        return True
    if ctypes.get_last_error() != 2:  # ERROR_FILE_NOT_FOUND es la única ausencia confirmada.
        raise OSError('No se pudo comprobar el bloqueo de actualización')
    return False


def comprobar_cierre(carpeta: Path) -> bool:
    """Solo inspecciona procesos. No envía señales ni termina procesos.

    También detecta server.py iniciado con otro Python y FFmpeg de Studio.
    Ante permisos insuficientes para un candidato, no confirma el cierre.
    """
    import psutil
    raiz = carpeta.resolve()
    scripts = {raiz / 'DcryptoStudio.pyw', raiz / 'app' / 'server.py'}
    candidatos = {'python.exe', 'pythonw.exe', 'dcryptostudio.exe', 'ffmpeg.exe', 'ffprobe.exe'}
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.pid == os.getpid():
            continue  # Este comprobador usa el runtime anterior, sin ejecutar la app.
        try:
            nombre = (proc.info.get('name') or proc.name()).lower()
            if nombre not in candidatos:
                continue
            exe = Path(proc.exe()).resolve()
            propio = exe.is_relative_to(raiz)
            if nombre in ('ffmpeg.exe', 'ffprobe.exe'):
                propio |= exe.is_relative_to((DATOS / 'ffmpeg').resolve())
            if nombre.startswith('python') and not propio:
                args = proc.cmdline()
                if any(args[i:i + 2] == ['-m', 'app.server'] for i in range(len(args) - 1)):
                    propio = Path(proc.cwd()).resolve() == raiz
                for arg in args[1:]:
                    if not arg.lower().endswith(('.py', '.pyw')):
                        continue
                    p = Path(arg)
                    if not p.is_absolute():
                        p = Path(proc.cwd()) / p
                    if p.resolve() in scripts:
                        propio = True
                        break
            if propio and proc.is_running():
                return False
        except psutil.NoSuchProcess:
            continue  # La identidad concreta del proceso ya terminó; no usamos su PID para matarlo.
        except (psutil.AccessDenied, OSError):
            return False
    return True


def comprobar_para_instalador(carpeta: str) -> int:
    try:
        return 0 if comprobar_cierre(Path(carpeta)) else 2
    except Exception:
        # Incluye psutil ausente, inspección incompleta o instalación dañada.
        return 3


def trabajos_para_instalador() -> int:
    """--jobs: 0 sin trabajos, 4 con trabajos generándose o en cola, 5 si no se sabe (no contesta).
    Pregunta a /api/sistema (existe desde la 1.0.0) de la instancia que corre. Solo lee."""
    puerto = leer_estado().get("puerto")
    if not isinstance(puerto, int):
        return 5
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/sistema", timeout=8) as r:
            cola = json.loads(r.read().decode("utf-8")).get("cola") or []
    except Exception:  # noqa: BLE001 - sin respuesta: el instalador avisa de forma genérica
        return 5
    return 4 if any(t.get("tipo") != "liberar" for t in cola if isinstance(t, dict)) else 0


def salir_para_instalador(carpeta: str) -> int:
    """--quit-and-wait: pide a la instancia que corre que se cierre (como "Salir" en la bandeja; la 1.0.0 y la
    1.0.1 también lo entienden) y espera. Devuelve el código de --check-stopped. No termina procesos: si la
    instancia no lo atiende, el instalador sigue sin poder continuar, como antes."""
    pedir_salida()
    return comprobar_para_instalador(carpeta)


def version() -> str:
    try:
        return re.search(r'VERSION\s*=\s*"([^"]+)"', (RAIZ / "app" / "version.py").read_text(encoding="utf-8")).group(1)
    except (OSError, AttributeError):
        return "?"


def puerto_config() -> int:
    try:
        return int(re.search(r"^PUERTO\s*=\s*(\d+)", (RAIZ / "app" / "config.py").read_text(encoding="utf-8"), re.M).group(1))
    except (OSError, AttributeError, ValueError):
        return PUERTO_BASE


def aviso(texto: str, error: bool = True) -> None:
    ctypes.windll.user32.MessageBoxW(None, texto, NOMBRE, 0x10 if error else 0x40)


def preguntar(texto: str) -> bool:
    """Sí / No (No por defecto)."""
    MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2, IDYES = 0x4, 0x30, 0x100, 6
    return ctypes.windll.user32.MessageBoxW(None, texto, NOMBRE, MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES


def puerto_libre(p: int) -> bool:
    """Comprueba disponibilidad exclusivamente en el loopback IPv4."""
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", p)) == 0:
            return False
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False


def buscar_puerto() -> int:
    base = puerto_config()
    for p in range(base, base + 50):
        if puerto_libre(p):
            return p
    raise RuntimeError(f"No free port between {base} and {base + 49}.")


def responde(puerto: int, timeout: float = 1.5) -> bool:
    """True si en ese puerto contesta Dcrypto Studio (y no otro programa)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/salud", timeout=timeout) as r:
            return r.status == 200 and json.loads(r.read().decode("utf-8")) == {"aplicacion": "Dcrypto Studio", "estado": "ok"}
    except Exception:
        return False


# ---------- launcher.json ----------

def leer_estado() -> dict:
    try:
        d = json.loads(ruta_estado().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def guardar_estado(**cambios) -> None:
    """pid y puerto de esta instancia, y en qué está: arrancando | listo | caido."""
    d = {**leer_estado(), "pid": os.getpid(), **cambios, "t": time.time()}
    try:
        ruta_estado().parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta_estado().with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(ruta_estado())
    except OSError:
        log.exception("no se pudo escribir launcher.json")


def borrar(ruta: Path) -> None:
    try:
        ruta.unlink()
    except OSError:
        pass


class Servidor:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.puerto: int | None = None
        self._log = None
        self.saliendo = False
        self.reiniciando = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.puerto}"

    def vivo(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def entorno(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONSTARTUP", "CONDA_PREFIX")}
        env.update({
            "DCRYPTO_DATA": str(DATOS),
            "HF_HOME": str(DATOS / "modelos" / "huggingface"),
            "TORCH_HOME": str(DATOS / "modelos" / "torch"),
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "PATH": os.pathsep.join([str(RAIZ / "runtime"), str(RAIZ / "runtime" / "Scripts"), env.get("PATH", "")]),
            # "Cerrar Dcrypto Studio" en la interfaz: el servidor escribe este archivo y el lanzador se cierra
            "DCRYPTO_LAUNCHER_QUIT": str(ruta_salir()),
        })
        return env

    def arrancar(self, puerto: int | None = None) -> None:
        self.puerto = puerto or buscar_puerto()
        python = RAIZ / "runtime" / "python.exe"
        if not python.exists():
            raise RuntimeError(tr(f"Falta {python}. Reinstala Dcrypto Studio.", f"Missing {python}. Reinstall Dcrypto Studio."))
        server_log = ruta_logs() / "server.log"
        if server_log.exists() and server_log.stat().st_size > 5_000_000:
            server_log.replace(ruta_logs() / "server.old.log")
        self._log = open(server_log, "a", encoding="utf-8", errors="replace")
        self._log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} Dcrypto Studio {version()} en el puerto {self.puerto} =====\n")
        self._log.flush()
        cmd = [str(python), "-X", "utf8", str(RAIZ / "app" / "server.py"), "--puerto", str(self.puerto), "--sin-navegador"]
        log.info("arrancando: %s", " ".join(cmd))
        guardar_estado(puerto=self.puerto, estado="arrancando")
        self.proc = subprocess.Popen(cmd, cwd=RAIZ, env=self.entorno(), stdout=self._log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def esperar(self, segundos: float = 90) -> bool:
        fin = time.time() + segundos
        while time.time() < fin:
            if self.proc.poll() is not None:
                log.error("el servidor terminó al arrancar (código %s)", self.proc.returncode)
                guardar_estado(estado="caido")
                return False
            if responde(self.puerto):
                log.info("servidor listo en %s", self.url)
                guardar_estado(estado="listo")
                return True
            time.sleep(0.4)
        log.error("el servidor no respondió en %s s", segundos)
        guardar_estado(estado="caido")
        return False

    def parar(self) -> None:
        if self.proc and self.proc.poll() is None:
            log.info("cerrando el servidor (pid %s)", self.proc.pid)
            # /T: también ffmpeg u otros procesos que haya lanzado
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None
        if self._log:
            self._log.close()
            self._log = None


def otra_instancia() -> bool:
    """Mutex con nombre: si ya existe, hay otro lanzador corriendo. El handle queda abierto en este proceso
    (también si ya existía): si luego se cierra una instancia colgada, el mutex sigue siendo de esta."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DcryptoStudioLauncher")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def instancia_viva() -> bool:
    """El mutex de la instancia existe mientras su proceso vive (sin crearlo, a diferencia de otra_instancia)."""
    h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\DcryptoStudioLauncher")  # SYNCHRONIZE
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
    return bool(h)


def pedir_salida() -> None:
    """--quit: avisa a la instancia que corre y espera a que su proceso termine del todo (hasta 30 s), para
    que el desinstalador pueda borrar los archivos después."""
    if not instancia_viva():
        return
    ruta_salir().parent.mkdir(parents=True, exist_ok=True)
    ruta_salir().write_text("1", encoding="utf-8")
    fin = time.time() + 30
    while time.time() < fin and instancia_viva():
        time.sleep(0.3)
    time.sleep(1.0)  # el cargador de PyInstaller (proceso padre) sale un instante después


# ---------- abrir otra vez ----------

def pedir_abrir(espera: float = ESPERA_ABRIR_S) -> bool:
    """Pide a la instancia que corre que abra el navegador (y que reviva su servidor si murió).
    True si lo atendió (borró launcher.abrir) a tiempo."""
    try:
        ruta_abrir().parent.mkdir(parents=True, exist_ok=True)
        ruta_abrir().write_text(str(time.time()), encoding="utf-8")
    except OSError:
        return False
    fin = time.time() + espera
    while time.time() < fin:
        if not ruta_abrir().exists():
            return True
        time.sleep(0.3)
    borrar(ruta_abrir())  # nadie lo atendió: que no lo encuentre una instancia futura
    return False


def es_lanzador(pid) -> bool:
    """El proceso es un lanzador de esta instalación (pythonw/python con DcryptoStudio.pyw, o el .exe)."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    try:
        import psutil
        p = psutil.Process(pid)
        nombre = p.name().lower()
        if nombre == "dcryptostudio.exe":
            return Path(p.exe()).resolve().is_relative_to(RAIZ.resolve())
        if nombre not in ("python.exe", "pythonw.exe"):
            return False
        for arg in p.cmdline()[1:]:
            if arg.lower().endswith(".pyw"):
                ruta = Path(arg) if Path(arg).is_absolute() else Path(p.cwd()) / arg
                if ruta.resolve() == (RAIZ / "DcryptoStudio.pyw").resolve():
                    return True
        return False
    except Exception:  # noqa: BLE001 - sin psutil, sin permisos o el proceso ya no existe
        return False


def cerrar_colgada(pid) -> bool:
    """Cierra un lanzador colgado de esta instalación y lo que lanzó (servidor, ffmpeg). Solo si el PID es de
    verdad nuestro lanzador; lo pidió el usuario (preguntar)."""
    if not es_lanzador(pid):
        log.warning("el pid %s de launcher.json no es un lanzador de Dcrypto Studio: no se toca", pid)
        return False
    log.warning("cerrando el lanzador colgado (pid %s) y sus procesos", pid)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    try:
        import psutil
        psutil.Process(pid).wait(10)
    except Exception:  # noqa: BLE001 - ya terminó
        pass
    return not es_lanzador(pid)


def abrir_existente() -> bool:
    """Ya hay un lanzador. Devuelve True si esta apertura debe seguir y arrancar la app (la otra estaba colgada
    y el usuario aceptó cerrarla); False si ya está resuelto (navegador abierto, o la otra se ocupa)."""
    est = leer_estado()
    p = est.get("puerto")
    if isinstance(p, int) and responde(p):
        webbrowser.open(f"http://127.0.0.1:{p}")
        return False
    arrancando = est.get("estado") == "arrancando" and time.time() - float(est.get("t") or 0) < ARRANQUE_MAX_S
    if arrancando and es_lanzador(est.get("pid")):
        log.info("la otra instancia está arrancando: ella abrirá el navegador")
        return False
    if pedir_abrir():
        log.info("la otra instancia atendió el pedido de abrir")
        return False
    log.warning("la otra instancia no contesta (estado %s)", est.get("estado"))
    if not preguntar(tr("Dcrypto Studio ya está abierto pero no responde.\n\n¿Cerrarlo y volver a abrirlo?\n"
                        "Si hay un video generándose, se perderá lo que falte.",
                        "Dcrypto Studio is already open but isn't responding.\n\nClose it and start again?\n"
                        "If a video is being generated, the remaining work will be lost.")):
        return False
    if cerrar_colgada(est.get("pid")):
        return True
    aviso(tr("No se pudo cerrar la instancia anterior de Dcrypto Studio. Reinicia el equipo o ciérrala desde el "
             "Administrador de tareas.",
             "Couldn't close the previous Dcrypto Studio instance. Restart your computer or end it from Task Manager."))
    return False


# ---------- bandeja ----------

def bandeja(srv: Servidor) -> None:
    import pystray
    from PIL import Image

    def icono():
        # el PNG grande con transparencia: Windows lo reduce al tamaño de la bandeja (16-32 px según el escalado)
        for ruta in (RAIZ / "assets" / "logo" / "logo_dcrypto_studio_1024.png", RAIZ / "dcrypto.ico"):
            try:
                return Image.open(ruta)
            except OSError:
                continue
        return Image.new("RGBA", (64, 64), (232, 163, 61, 255))  # ámbar liso si faltara el logo

    def titulo_ok():
        return f"{NOMBRE} {version()} · {srv.url}"

    def abrir(_icon=None, _item=None):
        webbrowser.open(srv.url)

    def reiniciar_servidor(icon, abrir_al_terminar=False):
        if srv.reiniciando or srv.saliendo:
            return
        srv.reiniciando = True

        def tarea():
            try:
                icon.title = tr(f"{NOMBRE}: reiniciando…", f"{NOMBRE}: restarting…")
                puerto = srv.puerto
                srv.parar()
                try:
                    srv.arrancar(puerto if puerto and puerto_libre(puerto) else None)
                    ok = srv.esperar()
                except Exception as e:  # noqa: BLE001
                    log.exception("reinicio fallido")
                    ok = False
                    aviso(tr(f"Dcrypto Studio no pudo reiniciarse:\n{e}\n\nLog: {ruta_logs()}",
                             f"Dcrypto Studio could not restart:\n{e}\n\nLog: {ruta_logs()}"))
                icon.title = titulo_ok() if ok else tr(f"{NOMBRE}: detenido", f"{NOMBRE}: not running")
                if ok:
                    if abrir_al_terminar:
                        abrir()
                    else:
                        icon.notify(tr("Dcrypto Studio se reinició.", "Dcrypto Studio restarted."), NOMBRE)
            finally:
                srv.reiniciando = False
        threading.Thread(target=tarea, daemon=True).start()

    def reiniciar(icon, _item):
        reiniciar_servidor(icon)

    def salir(icon, _item):
        srv.saliendo = True
        srv.parar()
        icon.stop()

    def atender_abrir(icon):
        """Otra apertura (acceso directo) pidió abrir: si el servidor murió, se reinicia y luego se abre."""
        borrar(ruta_abrir())
        if srv.reiniciando:
            return  # al terminar, el reinicio deja la app lista; el navegador ya se pidió o se pide abajo
        if srv.vivo():
            abrir()
        else:
            log.info("pedido de abrir con el servidor caído: se reinicia")
            reiniciar_servidor(icon, abrir_al_terminar=True)

    menu = pystray.Menu(
        pystray.MenuItem(tr("Abrir Dcrypto Studio", "Open Dcrypto Studio"), abrir, default=True),
        pystray.MenuItem(tr("Reiniciar", "Restart"), reiniciar),
        pystray.MenuItem(tr("Salir", "Quit"), salir),
    )
    icon = pystray.Icon("DcryptoStudio", icono(), titulo_ok(), menu)

    def vigilar():  # si el servidor se cae solo, se dice en el icono y en el log; y atiende --quit y las aperturas
        while not srv.saliendo:
            time.sleep(1)
            if ruta_salir().exists():
                log.info("pedido de salida (--quit o Cerrar Dcrypto Studio)")
                borrar(ruta_salir())
                salir(icon, None)
                return
            if ruta_abrir().exists():
                try:
                    atender_abrir(icon)
                except Exception:  # noqa: BLE001 - vigilar no muere por esto
                    log.exception("no se pudo atender el pedido de abrir")
            if srv.proc is not None and srv.proc.poll() is not None and not srv.saliendo and not srv.reiniciando:
                log.error("el servidor se cerró solo (código %s)", srv.proc.returncode)
                icon.title = tr(f"{NOMBRE}: detenido. Usa Reiniciar.", f"{NOMBRE}: stopped. Use Restart.")
                guardar_estado(estado="caido")
                srv.proc = None

    threading.Thread(target=vigilar, daemon=True).start()
    icon.run()


def main() -> None:
    # Modos del instalador: sin logs y sin arrancar nada, aunque haya una actualización en curso.
    for bandera, fn in (('--check-stopped', comprobar_para_instalador), ('--quit-and-wait', salir_para_instalador)):
        if bandera in sys.argv:
            posicion = sys.argv.index(bandera)
            if posicion + 1 >= len(sys.argv):
                raise SystemExit(3)
            raise SystemExit(fn(sys.argv[posicion + 1]))
    if '--jobs' in sys.argv:
        raise SystemExit(trabajos_para_instalador())
    if actualizacion_en_curso():
        aviso(tr('Dcrypto Studio se está actualizando. Espera a que termine el instalador.',
                 'Dcrypto Studio is being updated. Wait until setup finishes.'), error=False)
        return
    configurar_log()
    log.info("lanzador %s, app en %s, datos en %s", version(), RAIZ, DATOS)
    if "--quit" in sys.argv:
        pedir_salida()
        return
    borrar(ruta_salir())  # uno viejo no debe cerrar esta instancia
    if otra_instancia():
        log.info("ya hay una instancia")
        if not abrir_existente():
            return
        log.info("la instancia anterior estaba colgada y se cerró: esta arranca")
    borrar(ruta_abrir())  # uno viejo (de una instancia que ya no existe) no debe repetir nada
    # Segunda comprobación tras adquirir el mutex de instancia: cubre el arranque simultáneo.
    if actualizacion_en_curso():
        return
    srv = Servidor()
    try:
        srv.arrancar()
        if not srv.esperar():
            srv.parar()
            aviso(tr(f"Dcrypto Studio no pudo arrancar.\n\nLos detalles están en:\n{ruta_logs() / 'server.log'}",
                     f"Dcrypto Studio could not start.\n\nDetails are in:\n{ruta_logs() / 'server.log'}"))
            return
    except Exception as e:  # noqa: BLE001
        log.exception("no arrancó")
        srv.parar()
        aviso(tr(f"Dcrypto Studio no pudo arrancar:\n{e}\n\nLog: {ruta_logs()}",
                 f"Dcrypto Studio could not start:\n{e}\n\nLog: {ruta_logs()}"))
        return
    webbrowser.open(srv.url)
    try:
        bandeja(srv)
    finally:
        srv.parar()
        borrar(ruta_estado())
        log.info("cerrado")


if __name__ == "__main__":
    main()
