"""Código seudónimo de equipo; consulta local, sin persistir UUID ni usar la red."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

_DOMINIO = b"DcryptoStudio/device/v1\0"
_CHECKSUM = b"DcryptoStudio/device-code/v1\0"
_COMANDO = "(Get-CimInstance -ClassName Win32_ComputerSystemProduct -Property UUID -ErrorAction Stop).UUID"
_lock = threading.Lock()
_cache = None
_reintentar = 0.0


class ErrorEquipo(RuntimeError):
    """No fue posible obtener una identidad fiable; la identificación puede reintentarse."""


def derivar(valor: str) -> bytes:
    try:
        if not isinstance(valor, str) or len(valor) > 80:
            raise ValueError()
        identificador = uuid.UUID(valor.strip())
        if identificador.int in (0, (1 << 128) - 1) or identificador.hex in {
            "03000200040005000006000700080009", "00020003000400050006000700080009",
            "12345678123412341234123456789abc",
        }:
            raise ValueError()
        return hashlib.sha256(_DOMINIO + identificador.bytes).digest()[:16]
    except (ValueError, AttributeError):
        raise ErrorEquipo("No se pudo identificar el equipo mediante SMBIOS. Reintenta o contacta al vendedor.") from None


def codificar(identificador: bytes) -> str:
    if not isinstance(identificador, bytes) or len(identificador) != 16 or not any(identificador):
        raise ValueError("Código de equipo inválido.")
    comprobacion = hashlib.sha256(_CHECKSUM + identificador).digest()[:2]
    texto = base64.b32encode(identificador + comprobacion).decode("ascii").rstrip("=")
    return "DCE1-" + "-".join(texto[i:i + 5] for i in range(0, len(texto), 5))


def decodificar(codigo: str) -> bytes:
    try:
        if not isinstance(codigo, str) or len(codigo) > 100:
            raise ValueError()
        texto = "".join(codigo.split()).upper().replace("-", "")
        if not texto.startswith("DCE1") or len(texto) != 33:
            raise ValueError()
        cuerpo = texto[4:]
        datos = base64.b32decode(cuerpo + "=" * (-len(cuerpo) % 8))
        identificador = datos[:16]
        if codificar(identificador).replace("-", "")[4:] != cuerpo:
            raise ValueError()
        return identificador
    except (ValueError, base64.binascii.Error):
        raise ValueError("Código de equipo inválido; cópialo completo desde la aplicación.") from None


def codigo_actual() -> str:
    global _cache, _reintentar
    with _lock:
        if _cache is not None:
            return _cache
        if time.monotonic() < _reintentar:
            raise ErrorEquipo("SMBIOS no está disponible. Reintenta en unos segundos o contacta al vendedor.")
        try:
            if os.name != "nt":
                raise OSError()
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            resultado = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _COMANDO],
                capture_output=True, text=True, timeout=5, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _cache = codificar(derivar(resultado.stdout))
            return _cache
        except (OSError, subprocess.SubprocessError, ErrorEquipo, ValueError):
            _reintentar = time.monotonic() + 15
            raise ErrorEquipo("No se pudo consultar SMBIOS. Reintenta en unos segundos o contacta al vendedor.") from None
