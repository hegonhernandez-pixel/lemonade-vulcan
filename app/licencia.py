"""Licencia comercial offline de Dcrypto Studio y compatibilidad con el contador histórico.

Clave: "DCS-" + base32 (sin relleno, en bloques de 5) de  datos + firma.
  datos = versión (1 B) · serie (4 B) · días desde 2024-01-01 (2 B) · nombre UTF-8 · 0x1F · email UTF-8
  firma = ed25519 sobre FIRMA_PREFIJO + datos  (64 B), con la clave privada de tools/gen_license.py
V2: cabecera >BIH16sH (versión 2, serie, días, equipo seudónimo, generación),
seguida de nombre UTF-8 · 0x1F · email UTF-8. Dominio firmado DCS-LIC-2.
V1 conserva su formato y validez histórica, sin vinculación al dispositivo.
La app solo tiene la clave pública (CLAVE_PUBLICA): puede verificar, no generar. ~180 caracteres: se pega.

El modo prueba histórico concedía PRUEBA_VIDEOS proyectos distintos con marca TRIAL.
Regenerar uno ya contado no gastaba otro. Los videos contados se guardan en licencia.json y en el registro de Windows
(HKCU\\Software\\DcryptoStudio); vale la unión de los dos.

El paquete comercial exige licencia: no concede cupos ni aplica marca TRIAL.
Los contadores y operaciones históricos se conservan para compatibilidad.
Solo el checkout local sin marca comercial mantiene el modo desarrollador.

Desde la 1.0.1 Dcrypto Studio es gratuito: LICENCIA_REQUERIDA = False apaga todo lo anterior (sin clave,
sin cupos, sin marca TRIAL). El código de claves V1/V2 se conserva intacto para una posible versión Pro:
basta poner la bandera en True. Una clave ya activada se queda en licencia.json, sin leerse ni borrarse.
"""
import base64
import datetime
import json
import os
import re
import struct
import tempfile
import threading
from functools import wraps
from pathlib import Path

from pipeline import rutas

# LA bandera: False = versión gratuita (la app abre y funciona completa sin clave). True = exige licencia.
LICENCIA_REQUERIDA = False

# la pone "python tools/gen_license.py --init" (32 bytes en hex)
CLAVE_PUBLICA = "f2bf88a9398b432843d1de3f3e3cd9eab87c8cff068e4538511905522ff92b37"
FIRMA_PREFIJO = b"DCS-LIC-1"
FIRMA_PREFIJO_V2 = b"DCS-LIC-2"
VERSION = 1
VERSION_V2 = 2
PREFIJO = "DCS-"
EPOCA = datetime.date(2024, 1, 1)
PRUEBA_VIDEOS = 3
ARCHIVO = rutas.DATOS / "licencia.json"
REGISTRO = r"Software\DcryptoStudio"


_lock = threading.RLock()


def _sincronizado(fn):
    @wraps(fn)
    def protegido(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return protegido


# ---------------------------------------------------------------- formato de la clave (también lo usa tools/)

def empaquetar(serie: int, nombre: str, email: str, emitida: datetime.date, *,
               codigo_equipo: str | None = None, generacion: int = 0) -> bytes:
    if not isinstance(nombre, str) or not isinstance(email, str):
        raise ValueError("formato")
    nombre, email = nombre.strip(), email.strip()
    if "\x1f" in nombre + email:
        raise ValueError("carácter no permitido")
    if codigo_equipo is None:
        if generacion != 0:
            raise ValueError("formato")
        return struct.pack(">BIH", VERSION, serie, (emitida - EPOCA).days) + nombre.encode() + b"\x1f" + email.encode()
    from app import equipo
    if (type(serie) is not int or not 1 <= serie <= 0xffffffff
            or type(generacion) is not int or not 0 <= generacion <= 0xffff
            or type(emitida) is not datetime.date or not 0 <= (emitida - EPOCA).days <= 0xffff):
        raise ValueError("formato")
    datos = (struct.pack(">BIH16sH", VERSION_V2, serie, (emitida - EPOCA).days,
                         equipo.decodificar(codigo_equipo), generacion)
             + nombre.encode("utf-8") + b"\x1f" + email.encode("utf-8"))
    leer_datos(datos)  # aplicar exactamente las mismas restricciones que el receptor
    return datos


def codificar(datos: bytes, firma: bytes) -> str:
    b32 = base64.b32encode(datos + firma).decode().rstrip("=")
    return PREFIJO + "-".join(b32[i:i + 5] for i in range(0, len(b32), 5))


def decodificar(clave: str) -> tuple[bytes, bytes]:
    """(datos, firma) de una clave pegada como sea: con espacios, saltos de línea, minúsculas o sin guiones."""
    if not isinstance(clave, str) or len(clave) > 4096:
        raise ValueError("formato")
    s = "".join(clave.split()).upper()
    if s.startswith("DCS"):
        s = s[3:]
    s = s.replace("-", "")
    try:
        crudo = base64.b32decode(s + "=" * (-len(s) % 8))
    except (ValueError, base64.binascii.Error) as e:
        raise ValueError("formato") from e
    if len(crudo) < 7 + 1 + 64 or base64.b32encode(crudo).decode().rstrip("=") != s:
        raise ValueError("formato")
    return crudo[:-64], crudo[-64:]


def leer_datos(datos: bytes) -> dict:
    if not isinstance(datos, bytes) or len(datos) < 8:
        raise ValueError("formato")
    version, serie, dias = struct.unpack(">BIH", datos[:7])
    if version not in (VERSION, VERSION_V2):
        raise ValueError("version")
    inicio, extra = 7, {}
    if version == VERSION_V2:
        from app import equipo
        if len(datos) < 28 or len(datos) > 1024 or serie == 0:
            raise ValueError("formato")
        _, _, _, dispositivo, generacion = struct.unpack(">BIH16sH", datos[:25])
        if not any(dispositivo):
            raise ValueError("formato")
        inicio = 25
        extra = {"version": VERSION_V2, "codigo_equipo": equipo.codificar(dispositivo), "generacion": generacion}
    try:
        texto = datos[inicio:].decode("utf-8", errors="strict")
        nombre, separador, email = texto.partition("\x1f")
        if version == VERSION_V2 and (
                not separador or texto.count("\x1f") != 1 or not nombre or len(nombre.encode()) > 200
                or len(email.encode()) > 254 or nombre != nombre.strip() or email != email.strip()
                or any(not c.isprintable() for c in nombre + email)
                or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
            raise ValueError("formato")
        return {"serie": serie, "nombre": nombre, "email": email,
                "emitida": (EPOCA + datetime.timedelta(days=dias)).isoformat(), **extra}
    except UnicodeError:
        raise ValueError("formato") from None


def verificar(clave: str, publica: str | None = None) -> dict:
    """Los datos de una clave válida; ValueError("formato" | "firma" | "version" | "sin_clave_publica") si no."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    publica = publica or CLAVE_PUBLICA
    if not publica:
        raise ValueError("sin_clave_publica")
    datos, firma = decodificar(clave)
    if datos[0] not in (VERSION, VERSION_V2):
        raise ValueError("version")
    dominio = FIRMA_PREFIJO if datos[0] == VERSION else FIRMA_PREFIJO_V2
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(publica)).verify(firma, dominio + datos)
    except InvalidSignature as e:
        raise ValueError("firma") from e
    return leer_datos(datos)


def id_corto(serie: int) -> str:
    """Número de licencia para soporte: DCS-000123."""
    return f"DCS-{serie:06d}"


# ---------------------------------------------------------------- estado guardado

def _sin_duplicados(pares):
    d = {}
    for campo, valor in pares:
        if campo in d:
            raise ValueError("Campo duplicado")
        d[campo] = valor
    return d


def _proyectos(valor) -> set[str]:
    if not isinstance(valor, list) or any(not isinstance(s, str) for s in valor):
        raise RuntimeError("Contador de prueba inválido; se conserva sin modificar.")
    return set(valor)


@_sincronizado
def _leer(*, estricto=False) -> dict:
    try:
        return json.loads(ARCHIVO.read_text(encoding="utf-8"),
                          object_pairs_hook=_sin_duplicados if estricto else None)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise RuntimeError("No se pudo leer el estado de licencia; no se modificó.") from None


@_sincronizado
def _escribir(d: dict) -> None:
    temporal = None
    try:
        ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ARCHIVO.parent,
                                         prefix=".licencia-", suffix=".tmp", delete=False) as f:
            temporal = Path(f.name)
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, ARCHIVO)
    except (OSError, ValueError):
        raise RuntimeError("No se pudo guardar el estado de licencia. El archivo anterior se conservó.") from None
    finally:
        if temporal is not None:
            temporal.unlink(missing_ok=True)


def _registro_leer(*, estricto=False) -> set[str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRO) as k:
            valor, tipo = winreg.QueryValueEx(k, "p")
            if estricto:
                if tipo != winreg.REG_SZ:
                    raise RuntimeError("Tipo de contador de Windows inválido.")
                return _proyectos(json.loads(valor))
            return set(json.loads(valor))
    except FileNotFoundError:
        return set()  # clave o valor aún no creados
    except (OSError, ValueError, ImportError):
        if estricto:
            raise RuntimeError("No se pudo confirmar el contador de Windows.") from None
        return set()


def _registro_escribir(slugs: set[str]) -> None:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRO) as k:
            winreg.SetValueEx(k, "p", 0, winreg.REG_SZ, json.dumps(sorted(slugs)))
    except (OSError, ImportError):
        raise RuntimeError("No se pudo guardar el contador de prueba en el registro de Windows.") from None


@_sincronizado
def videos_prueba() -> set[str]:
    return set(_leer().get("prueba", [])) | _registro_leer()


@_sincronizado
def activa() -> dict | None:
    clave = _leer().get("clave")
    if not clave:
        return None
    try:
        datos = verificar(clave)
        _comprobar_equipo(datos)
        return datos
    except ValueError:
        return None


def _comprobar_equipo(datos):
    if datos.get("version") != VERSION_V2:
        return
    from app import equipo
    try:
        actual = equipo.codigo_actual()
    except equipo.ErrorEquipo:
        raise ValueError("equipo") from None
    if actual != datos["codigo_equipo"]:
        raise ValueError("dispositivo")


def requerida() -> bool:
    """True si esta instalación exige una clave para exportar (versión Pro)."""
    return LICENCIA_REQUERIDA and comercial()


def habilitada() -> bool:
    """True si se puede usar todo: versión gratuita, o versión Pro con una clave válida."""
    return not requerida() or activa() is not None


@_sincronizado
def en_prueba() -> bool:
    """True si los videos que se exporten llevan la marca TRIAL."""
    if not LICENCIA_REQUERIDA or comercial():
        return False
    return rutas.INSTALADO and activa() is None


def comercial() -> bool:
    return getattr(rutas, "COMERCIAL", False)


@_sincronizado
def puede_exportar(slug: str) -> bool:
    if not LICENCIA_REQUERIDA:
        return True
    if comercial():
        return activa() is not None
    if not en_prueba():
        return True
    usados = videos_prueba()
    return slug in usados or len(usados) < PRUEBA_VIDEOS


@_sincronizado
def preparar_exportacion(slug: str) -> bool:
    """Exige licencia comercial; conserva la política histórica fuera del paquete comercial."""
    if not LICENCIA_REQUERIDA:
        return False
    if comercial():
        if activa() is None:
            from pipeline.textos import error
            raise error("srv.licencia_requerida")
        return False
    prueba = en_prueba()
    if prueba and not puede_exportar(slug):
        from pipeline.textos import error
        raise error("srv.prueba_agotada", n=PRUEBA_VIDEOS)
    return prueba


@_sincronizado
def registrar_video(slug: str) -> None:
    """Un video completo exportado en modo prueba (una vez por guion)."""
    if not en_prueba():
        return
    usados = videos_prueba() | {slug}
    d = _leer()
    d["prueba"] = sorted(usados)
    anterior = _leer()
    _escribir(d)
    try:
        _registro_escribir(usados)
    except Exception:
        # SetValueEx escribe un único valor. Si falla, restaurar el JSON previo.
        _escribir(anterior)
        raise RuntimeError("No se pudo registrar la exportación de prueba; no se confirmó el cupo.") from None


@_sincronizado
def _contabilizar_trial_confirmado(slug: str, *, exigir_existente=False) -> str:
    """Uso exclusivo de RegistroExportaciones.contabilizar_trial bajo ambos bloqueos.

    No hay transacción entre JSON y Windows: se conserva cualquier consumo parcial
    y el reintento reconcilia la unión. Nunca se devuelve éxito sin releer ambos.
    exigir_existente verifica un recibo previo, incluso si después se activó pago.
    """
    if not LICENCIA_REQUERIDA:
        return "exento_gratis"  # versión gratuita: nunca se consumen ni alteran cupos
    if comercial():
        if activa() is None:
            raise RuntimeError("Se requiere una licencia válida para finalizar la exportación.")
        return "exento_pago"  # recuperar un intento histórico sin consumir ni alterar cupos
    d = _leer(estricto=True)
    if not isinstance(d, dict):
        raise RuntimeError("Estado de licencia inválido; se conserva sin modificar.")
    json_usados = _proyectos(d.get("prueba", []))
    if not exigir_existente:
        if not rutas.INSTALADO:
            return "exento_desarrollo"
        if activa() is not None:
            return "exento_pago"
    registro_usados = _registro_leer(estricto=True)
    usados = json_usados | registro_usados
    existente = slug in usados
    if exigir_existente and not existente:
        raise RuntimeError("El consumo liquidado no consta en los contadores; requiere revisión.")
    if not existente and len(usados) >= PRUEBA_VIDEOS:
        raise RuntimeError("Cupo de prueba agotado; no se contabilizó la exportación.")
    usados.add(slug)
    if json_usados != usados:
        d["prueba"] = sorted(usados)
        _escribir(d)
    if registro_usados != usados:
        _registro_escribir(usados)
    confirmado = _leer(estricto=True)
    if (not isinstance(confirmado, dict)
            or _proyectos(confirmado.get("prueba", [])) != usados
            or _registro_leer(estricto=True) != usados):
        raise RuntimeError("No se confirmó el consumo en ambos contadores; reintenta la operación.")
    return "ya_contabilizado" if existente else "contabilizado"


@_sincronizado
def estado() -> dict:
    if not LICENCIA_REQUERIDA:
        return {"estado": "gratis", "visto": True}
    d = _leer()
    if not rutas.INSTALADO:
        return {"estado": "desarrollador", "visto": True}
    lic = activa()
    if lic:
        return {"estado": "activa", "visto": True, **lic, "id": id_corto(lic["serie"])}
    if comercial():
        return {"estado": "sin_licencia", "visto": bool(d.get("visto")),
                "clave_invalida": bool(d.get("clave"))}
    usados = videos_prueba()
    return {"estado": "prueba", "visto": bool(d.get("visto")), "usados": len(usados), "limite": PRUEBA_VIDEOS,
            "restantes": max(0, PRUEBA_VIDEOS - len(usados)), "videos": sorted(usados),
            "clave_invalida": bool(d.get("clave"))}


@_sincronizado
def activar(clave: str) -> dict:
    if not LICENCIA_REQUERIDA:
        raise ValueError("gratis")  # no hay nada que activar: no se guarda nada
    datos = verificar(clave)  # ValueError si no vale: no se guarda nada
    _comprobar_equipo(datos)
    d = _leer()
    d.update(clave=" ".join(clave.split()), visto=True)
    _escribir(d)
    return datos


def estado_equipo() -> dict:
    """Respuesta ampliada solo al abrir Licencia; un error WMI no borra datos."""
    from app import equipo
    d = estado()
    try:
        d.update(codigo_equipo=equipo.codigo_actual(), error_equipo=None)
    except equipo.ErrorEquipo:
        d.update(codigo_equipo=None, error_equipo="equipo")
    clave = _leer().get("clave")
    if clave and d["estado"] != "activa":
        try:
            _comprobar_equipo(verificar(clave))
        except ValueError as exc:
            if str(exc) in ("equipo", "dispositivo"):
                d["motivo_clave_invalida"] = str(exc)
    return d


@_sincronizado
def quitar() -> None:
    d = _leer()
    d.pop("clave", None)
    _escribir(d)


@_sincronizado
def marcar_visto() -> None:
    d = _leer()
    d["visto"] = True
    _escribir(d)
