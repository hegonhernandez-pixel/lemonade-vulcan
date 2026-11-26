"""Aviso de versión nueva: una consulta a una URL fija, sin datos del usuario ni del equipo.

- Un solo GET a URL_VERSION, sin parámetros, sin cookies y con un User-Agent genérico (sin versión ni sistema).
  El servidor web ve la IP, como en cualquier petición; no se envía nada más.
- Se hace una vez por arranque (en segundo plano) y el resultado queda en memoria.
- Sin internet, con un JSON raro o con cualquier fallo, no hay aviso y no hay error: devuelve None.
- Solo se acepta un enlace de descarga https en dcryptostudio.com o gumroad.com.

Formato de version.json (en la web):
  {"version": "1.0.2", "url": "https://dcryptostudio.gumroad.com/l/dcrypto-studio",
   "notas_es": "...", "notas_en": "..."}
"""
import json
import re
import threading
import urllib.request
from urllib.parse import urlsplit

from app.version import VERSION

URL_VERSION = "https://dcryptostudio.com/version.json"
TIMEOUT_S = 4
MAX_BYTES = 16_384
AGENTE = "DcryptoStudio"  # genérico: ni versión, ni sistema operativo, ni nada del equipo
DOMINIOS = ("dcryptostudio.com", "gumroad.com")
MAX_NOTAS = 400

_lock = threading.Lock()
_resultado: dict | None = None
_consultado = False


def numeros(v) -> tuple[int, ...] | None:
    """"1.0.2" -> (1, 0, 2). None si no es una versión x.y.z (de 1 a 4 números)."""
    if not isinstance(v, str) or not re.fullmatch(r"\d{1,4}(\.\d{1,4}){0,3}", v.strip()):
        return None
    t = tuple(int(x) for x in v.strip().split("."))
    return t + (0,) * (4 - len(t))


def es_mas_nueva(remota, local: str = VERSION) -> bool:
    r, l = numeros(remota), numeros(local)
    return r is not None and l is not None and r > l


def url_permitida(url) -> bool:
    if not isinstance(url, str) or len(url) > 300:
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return (p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
            and any(host == d or host.endswith("." + d) for d in DOMINIOS))


def interpretar(datos, local: str = VERSION) -> dict | None:
    """El aviso a mostrar, o None si no hay versión nueva o el JSON no sirve."""
    if not isinstance(datos, dict) or not es_mas_nueva(datos.get("version"), local) or not url_permitida(datos.get("url")):
        return None
    notas = {}
    for idioma in ("es", "en"):
        n = datos.get(f"notas_{idioma}")
        notas[idioma] = n.strip()[:MAX_NOTAS] if isinstance(n, str) else ""
    return {"version": datos["version"].strip(), "url": datos["url"], "notas": notas}


def _descargar() -> dict | None:
    pedido = urllib.request.Request(URL_VERSION, headers={"User-Agent": AGENTE, "Accept": "application/json"})
    # Sin cookies ni credenciales: urllib no las guarda ni las envía si no se le pide.
    with urllib.request.urlopen(pedido, timeout=TIMEOUT_S) as r:
        if r.status != 200:
            return None
        cuerpo = r.read(MAX_BYTES + 1)
    if len(cuerpo) > MAX_BYTES:
        return None
    return json.loads(cuerpo.decode("utf-8"))


def consultar() -> dict | None:
    """Consulta la web una sola vez por arranque. Nunca lanza excepciones."""
    global _resultado, _consultado
    with _lock:
        if _consultado:
            return _resultado
        try:
            _resultado = interpretar(_descargar())
        except Exception:  # noqa: BLE001 - sin internet, DNS, TLS, JSON raro: sin aviso y sin error
            _resultado = None
        _consultado = True
        return _resultado


def consultar_en_segundo_plano() -> None:
    threading.Thread(target=consultar, daemon=True, name="version-nueva").start()


def resultado() -> dict | None:
    """Lo que ya se sabe, sin esperar a la red (None mientras consulta o si no hay nada nuevo)."""
    return _resultado if _consultado else None


def consultado() -> bool:
    return _consultado
