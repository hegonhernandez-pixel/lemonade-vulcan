"""Cerrar Dcrypto Studio desde la interfaz (POST /api/cerrar).

Orden: se cancelan los trabajos en cola, se sueltan los modelos (SDXL, IP-Adapter, Kokoro, whisper) si el
worker no está usando la GPU, se responde a la interfaz y, un instante después, se apaga todo:
- Con el lanzador (app instalada): el lanzador pasa DCRYPTO_LAUNCHER_QUIT, la ruta de su archivo de salida.
  Escribirlo le pide cerrarse: él termina este servidor con todo lo que lanzó (ffmpeg, claude -p) y quita
  el icono de la bandeja.
- Sin lanzador (desarrollo, o server.py a mano): este proceso termina a sus hijos (ffmpeg…) y sale.
Si el lanzador no lo hace en ESPERA_LANZADOR_S, el servidor sale igual por su cuenta.
"""
import os
import threading
import time
import traceback
from pathlib import Path

from app import trabajos

ENV_LANZADOR = "DCRYPTO_LAUNCHER_QUIT"
RETARDO_S = 0.6  # para que la respuesta llegue al navegador antes de cortar
ESPERA_LANZADOR_S = 15

_lock = threading.Lock()
_cerrando = False


def cerrando() -> bool:
    return _cerrando


def cerrar() -> dict:
    """Prepara el cierre y lo programa. Idempotente: un segundo pedido no hace nada más."""
    global _cerrando
    with _lock:
        if _cerrando:
            return {"ok": True, "lanzador": bool(os.environ.get(ENV_LANZADOR))}
        _cerrando = True
    corriendo = trabajos.cancelar_todos()
    if not trabajos.ocupado():  # con algo en la GPU no se sueltan los pesos debajo: los suelta el fin del proceso
        try:
            trabajos.liberar_modelos()
        except Exception:  # noqa: BLE001 - se cierra igual: al terminar el proceso la memoria queda libre
            traceback.print_exc()
    con_lanzador = bool(os.environ.get(ENV_LANZADOR))
    print(f"\nCerrando Dcrypto Studio (pedido desde la interfaz{', con un trabajo en curso' if corriendo else ''}).",
          flush=True)
    threading.Thread(target=apagar, args=(RETARDO_S,), daemon=True, name="cierre").start()
    return {"ok": True, "lanzador": con_lanzador}


def avisar_lanzador() -> bool:
    """Escribe el archivo de salida del lanzador. True si había lanzador y se pudo avisar."""
    ruta = os.environ.get(ENV_LANZADOR, "").strip()
    if not ruta:
        return False
    try:
        Path(ruta).write_text("1", encoding="utf-8")
        return True
    except OSError:
        traceback.print_exc()
        return False


def terminar_hijos(espera: float = 3.0) -> None:
    """ffmpeg, ffprobe, claude -p… que haya lanzado este proceso (y los hijos de esos)."""
    try:
        import psutil
    except ImportError:
        return
    try:
        hijos = psutil.Process().children(recursive=True)
    except psutil.Error:
        return
    for h in hijos:
        try:
            h.terminate()
        except psutil.Error:
            pass
    _, vivos = psutil.wait_procs(hijos, timeout=espera)
    for h in vivos:
        try:
            h.kill()
        except psutil.Error:
            pass


def salir(codigo: int = 0) -> None:  # aparte para que los tests lo reemplacen
    os._exit(codigo)


def apagar(retardo: float = RETARDO_S) -> None:
    time.sleep(retardo)
    if avisar_lanzador():
        fin = time.time() + ESPERA_LANZADOR_S
        while time.time() < fin:  # lo normal: el lanzador termina este proceso antes de que acabe la espera
            time.sleep(0.5)
    terminar_hijos()
    salir(0)
