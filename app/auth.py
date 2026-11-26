"""Autenticación Supabase para una estación de producción local."""
import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fastapi import APIRouter, Form, Request as WebRequest
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from pipeline import rutas

COOKIE = "compresso_session"
PUBLICAS = {"/entrar", "/api/salud", "/favicon.ico"}
router = APIRouter()


def activa():
    return rutas.LINUX and os.environ.get("COMPRESSO_LOCAL_DEMO") != "1"


def configuracion():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    owners = {email.strip().lower() for email in os.environ.get("COMPRESSO_AUTH_EMAILS", "").split(",") if email.strip()}
    partes = urlsplit(url)
    if partes.scheme != "https" or not partes.hostname or partes.path or partes.query or partes.fragment or partes.username:
        return None
    return (url, key, owners) if key and owners else None


def supabase(config, path, *, body=None, token=None):
    url, key, _ = config
    headers = {"apikey": key, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    solicitud = Request(f"{url}/auth/v1/{path}", headers=headers,
                        data=json.dumps(body).encode() if body is not None else None)
    if path == "logout":
        solicitud.method = "POST"
    with urlopen(solicitud, timeout=8) as respuesta:
        return {} if respuesta.status == 204 else json.load(respuesta)


def permitido(user, config):
    return bool(user.get("id") and user.get("email_confirmed_at")
                and (user.get("email") or "").lower() in config[2])


class Sesion:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not activa() or scope["path"] in PUBLICAS:
            return await self.app(scope, receive, send)
        solicitud = WebRequest(scope)
        config = configuracion()
        token = solicitud.cookies.get(COOKIE)
        if config and token:
            try:
                usuario = await asyncio.to_thread(supabase, config, "user", token=token)
                if permitido(usuario, config):
                    async def sin_cache(mensaje):
                        if mensaje["type"] == "http.response.start":
                            mensaje["headers"] = [(k, v) for k, v in mensaje.get("headers", []) if k != b"cache-control"]
                            mensaje["headers"].append((b"cache-control", b"private, no-store"))
                        await send(mensaje)
                    return await self.app(scope, receive, sin_cache)
            except (URLError, ValueError, OSError):
                pass
        respuesta = (RedirectResponse("/entrar", status_code=303) if scope["path"] == "/" else
                     JSONResponse({"detail": "Inicia sesión en CompressOS Studio."}, status_code=401))
        await respuesta(scope, receive, send)


@router.get("/entrar")
def formulario():
    if not activa():
        return RedirectResponse("/", status_code=303)
    return FileResponse(rutas.APP / "app/static/entrar.html", headers={"Cache-Control": "no-store"})


@router.post("/entrar")
def entrar(email: str = Form(...), password: str = Form(...)):
    config = configuracion()
    if not activa() or not config:
        return JSONResponse({"detail": "Configura el proyecto Supabase y la lista de correos autorizados."}, status_code=503)
    try:
        datos = supabase(config, "token?grant_type=password", body={"email": email.strip(), "password": password})
        if not permitido(datos.get("user") or {}, config):
            return JSONResponse({"detail": "Acceso no autorizado."}, status_code=403)
        respuesta = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        respuesta.set_cookie(COOKIE, datos["access_token"], max_age=min(int(datos["expires_in"]), 3600),
                             httponly=True, samesite="strict", path="/")
        return respuesta
    except HTTPError as exc:
        if exc.code < 500:
            return JSONResponse({"detail": "Verifica el correo y la contraseña."}, status_code=401)
    except (URLError, ValueError, OSError, KeyError):
        pass
    return JSONResponse({"detail": "Supabase Auth no está disponible."}, status_code=503)


@router.post("/api/auth/salir")
def salir(request: WebRequest):
    config = configuracion()
    if config and (token := request.cookies.get(COOKIE)):
        try:
            supabase(config, "logout?scope=local", token=token)
        except (URLError, OSError, ValueError):
            pass
    respuesta = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    respuesta.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    return respuesta
