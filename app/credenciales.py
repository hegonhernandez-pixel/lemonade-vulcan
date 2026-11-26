"""API keys protegidas con DPAPI del usuario actual; nunca importar el servidor aquí."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import threading

PREFIJO = "dpapi:v1:"
_lock = threading.RLock()


class ErrorCredenciales(Exception):
    def __init__(self):
        super().__init__("No se pudieron proteger o recuperar las credenciales de Windows.")


def _dpapi(valor: bytes, proteger: bool) -> bytes:
    if os.name != "nt":
        raise ErrorCredenciales()

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(valor)
    entrada = Blob(len(valor), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    salida = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    funcion = crypt.CryptProtectData if proteger else crypt.CryptUnprotectData
    funcion.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    funcion.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; sin LOCAL_MACHINE: vinculado al usuario.
    if not funcion(ctypes.byref(entrada), None, None, None, None, 1, ctypes.byref(salida)):
        raise ErrorCredenciales()
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        kernel.LocalFree(salida.pbData)


def recuperar(valor: str) -> str:
    if not valor.startswith(PREFIJO):
        return valor
    try:
        return _dpapi(base64.b64decode(valor[len(PREFIJO):], validate=True), False).decode("utf-8")
    except Exception:
        raise ErrorCredenciales() from None


def proteger(valor: str) -> str:
    try:
        protegido = PREFIJO + base64.b64encode(_dpapi(valor.encode("utf-8"), True)).decode("ascii")
        if recuperar(protegido) != valor:
            raise ErrorCredenciales()
        return protegido
    except Exception:
        raise ErrorCredenciales() from None


def guardar(ruta: Path, datos: dict, claves) -> None:
    with _lock:
        temporal = None
        try:
            copia = dict(datos)
            for clave in claves:
                valor = copia.get(clave, "")
                if not isinstance(valor, str):
                    raise ErrorCredenciales()
                copia[clave] = proteger(valor) if valor else ""
            # Solo después de verificar TODOS los valores: escribir únicamente datos protegidos.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ruta.parent,
                                             prefix=".ajustes-", suffix=".tmp", delete=False) as f:
                temporal = Path(f.name)
                json.dump(copia, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, ruta)
        except Exception:
            raise ErrorCredenciales() from None
        finally:
            if temporal is not None:
                temporal.unlink(missing_ok=True)


def afectadas(ruta: Path, claves) -> list[str]:
    """Solo nombres de campos ilegibles; no devuelve valores ni migra ajustes."""
    with _lock:
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            raise ErrorCredenciales() from None
        resultado = []
        for clave in claves:
            valor = datos.get(clave, "")
            if isinstance(valor, str) and valor.startswith(PREFIJO):
                try:
                    recuperar(valor)
                except ErrorCredenciales:
                    resultado.append(clave)
        return resultado


def reemplazar_afectada(ruta: Path, clave: str, nueva: str, claves) -> None:
    """Reemplazo explícito de un solo campo: conserva los demás valores serializados."""
    with _lock:
        temporal = None
        try:
            if clave not in claves or not isinstance(nueva, str) or not nueva.strip():
                raise ErrorCredenciales()
            if clave not in afectadas(ruta, claves):
                raise ErrorCredenciales()
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            datos[clave] = proteger(nueva.strip())  # incluye verificación de recuperación
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ruta.parent,
                                             prefix=".ajustes-", suffix=".tmp", delete=False) as f:
                temporal = Path(f.name)
                json.dump(datos, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, ruta)
        except Exception:
            raise ErrorCredenciales() from None
        finally:
            if temporal is not None:
                temporal.unlink(missing_ok=True)


def leer(ruta: Path, claves) -> dict:
    with _lock:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        claros = dict(datos)
        migrar = False
        for clave in claves:
            valor = datos.get(clave, "")
            if not isinstance(valor, str):
                raise ErrorCredenciales()
            claros[clave] = recuperar(valor)
            migrar |= bool(valor) and not valor.startswith(PREFIJO)
        if migrar:
            guardar(ruta, claros, claves)
        return claros
