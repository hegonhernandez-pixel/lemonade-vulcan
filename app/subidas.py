"""Límite de la subida de medios por escena, antes del parser multipart."""
import re

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from pipeline.medios import MAX_BYTES
from pipeline.textos import Texto


class LimiteSubidaPropios:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or not re.fullmatch(r'/api/guiones/[^/]+/propios', scope.get('path', ''))):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        limite = MAX_BYTES + 65536  # campos y cabeceras multipart
        longitud = headers.get(b'content-length')
        idioma = headers.get(b'x-idioma', b'es').decode('latin-1')
        if longitud is not None and (not longitud.isdigit() or int(longitud) > limite):
            return await JSONResponse({'detail': Texto('clip.tamano').en(idioma)}, status_code=413)(scope, receive, send)
        total = 0

        async def limitado():
            nonlocal total
            mensaje = await receive()
            total += len(mensaje.get('body', b''))
            if total > limite:
                raise HTTPException(413, Texto('clip.tamano'))
            return mensaje
        await self.app(scope, limitado, send)
