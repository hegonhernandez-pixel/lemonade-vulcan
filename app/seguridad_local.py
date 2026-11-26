"""Defensa del servidor local contra DNS rebinding y solicitudes web externas.

La cabecera propia fuerza preflight en navegadores externos; no se habilita CORS.
No autentica otros procesos del mismo usuario Windows.
"""
from urllib.parse import urlsplit


class SeguridadLocal:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {}
        duplicada = False
        for k, v in scope.get("headers", []):
            if k in headers and k in (b"host", b"origin", b"sec-fetch-site"):
                duplicada = True
            headers[k] = v.decode("latin-1")
        host = headers.get(b"host", "").lower()
        port = scope.get("server", (None, None))[1]
        permitidos = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            permitidos |= {"127.0.0.1", "localhost"}
        valido = not duplicada and host in permitidos
        origin = headers.get(b"origin")
        if origin is not None:
            try:
                origen = urlsplit(origin)
                valido &= origen.scheme == "http" and origen.netloc.lower() == host and not origen.path and not origen.query and not origen.fragment
            except ValueError:
                valido = False
        valido &= headers.get(b"sec-fetch-site", "none") in ("none", "same-origin")
        if scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD"):
            valido &= headers.get(b"x-dcrypto-local") == "1"
        if not valido:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b'{"detail":"Solicitud local no permitida / Local request denied"}'})
            return
        await self.app(scope, receive, send)
