"""API privada de Estudio de imágenes; comparte licencia y worker con Studio."""
import io
import threading

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from PIL import Image

from app import licencia, trabajos
from pipeline import estudio_imagenes as estudio, textos
from pipeline.textos import Texto

_lock = threading.RLock()


def exigir_licencia():
    if not licencia.habilitada():
        raise HTTPException(402, Texto('est.licencia_requerida'))


class Respuesta(JSONResponse):
    def render(self, content):
        return super().render(textos.local(content))


class RutaLimitada(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def limitada(pedido):
            if pedido.method == 'POST':
                exigir_licencia()
                maximo = estudio.MAX_SUBIDA + 64 * 1024
                longitud = pedido.headers.get('content-length')
                if longitud is not None and (not longitud.isdigit() or int(longitud) > maximo):
                    raise HTTPException(413, Texto('est.err_foto'))
                crudo = bytearray()
                async for fragmento in pedido.stream():
                    if len(crudo) + len(fragmento) > maximo:
                        raise HTTPException(413, Texto('est.err_foto'))
                    crudo.extend(fragmento)
                # Starlette reutiliza el cuerpo acotado al analizar el multipart.
                pedido._body = bytes(crudo)
            return await original(pedido)
        return limitada


router = APIRouter(prefix='/api/estudio', dependencies=[Depends(exigir_licencia)],
                   default_response_class=Respuesta, route_class=RutaLimitada)


def _leer(almacen, id_):
    try:
        return almacen.leer(id_)
    except FileNotFoundError:
        raise HTTPException(404, Texto('est.err_no_existe')) from None
    except (ValueError, OSError):
        raise HTTPException(400, Texto('est.err_ruta')) from None


def _publico(almacen, datos):
    id_ = datos['id']
    ts = trabajos.listar(f'estudio:{id_}')
    t = ts[-1] if ts else None
    estado = t.estado if t else datos['estado']
    err = t.error if t and t.estado == 'error' else None
    if estado in ('en cola', 'corriendo') and t is None:
        estado, err = 'error', Texto('est.err_interrumpido')
    if estado == 'cancelado':
        estado, err = 'error', Texto('est.err_interrumpido')
    if estado == 'error' and err is None:
        err = Texto(datos.get('error_clave') or 'est.err_generar')
    existe = estado == 'listo' and almacen.archivo(id_, 'imagen.png').is_file()
    return {k: datos[k] for k in ('id', 'prompt', 'formato', 'calidad', 'intensidad', 'con_foto', 'creado')} | {
        'estado': estado, 'error': err, 'trabajo': t.a_dict() if t else None,
        'url': f'/api/estudio/{id_}/imagen' if existe else None,
    }


@router.get('')
def galeria(limite: int = Query(24, ge=1, le=60), offset: int = Query(0, ge=0)):
    almacen = estudio.Almacen()
    with _lock:
        datos = almacen.listar()
        return {'imagenes': [_publico(almacen, d) for d in datos[offset:offset + limite]], 'total': len(datos)}


@router.post('', status_code=202)
def generar(request: Request, prompt: str = Form(...), formato: str = Form('1:1'), calidad: str = Form('rapida'),
            intensidad: float = Form(0.5), archivo: UploadFile | None = File(None)):
    almacen = estudio.Almacen()
    try:
        opciones = estudio.validar(prompt, formato, calidad, intensidad)
        foto = estudio.fotografia(archivo.file.read(estudio.MAX_SUBIDA + 1)) if archivo is not None else None
        with _lock:
            # Evitar una cola accidental por doble clic o varias pestañas.
            if any(t.tipo == 'estudio' and t.estado in trabajos.ACTIVOS for t in trabajos.listar()):
                raise HTTPException(409, Texto('est.err_ocupado'))
            with licencia._lock:
                exigir_licencia()
                datos = almacen.crear(opciones, foto)
                try:
                    trabajos.encolar(f"estudio:{datos['id']}", 'estudio', opciones=request.app.state.opciones_render())
                except Exception:
                    almacen.eliminar(datos['id'])
                    raise
            return _publico(almacen, datos)
    except ValueError as exc:
        raise HTTPException(400, textos.de(exc)) from None
    except OSError:
        raise HTTPException(500, Texto('est.err_guardar')) from None
    finally:
        if archivo is not None:
            archivo.file.close()


@router.get('/{id_}/imagen')
def descargar(id_: str, formato: str = Query('png', pattern='^(png|jpg)$'), descarga: bool = False):
    almacen = estudio.Almacen()
    with _lock:
        datos = _leer(almacen, id_)
        p = almacen.archivo(id_, 'imagen.png')
        if datos['estado'] != 'listo' or not p.is_file():
            raise HTTPException(404, Texto('est.err_no_existe'))
        nombre = f'dcrypto-{id_[:8]}.{formato}'
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if formato == 'jpg':
            with Image.open(p) as im:
                salida = io.BytesIO()
                im.convert('RGB').save(salida, format='JPEG', quality=95)
            if descarga:
                headers['Content-Disposition'] = f'attachment; filename="{nombre}"'
            return Response(salida.getvalue(), media_type='image/jpeg', headers=headers)
        if descarga:
            headers['Content-Disposition'] = f'attachment; filename="{nombre}"'
        # Leer bajo el mismo lock que eliminar: no dejar un FileResponse pendiente
        # apuntando a un maestro que otra pestaña acaba de borrar.
        return Response(p.read_bytes(), media_type='image/png', headers=headers)


@router.delete('/{id_}')
def eliminar(id_: str):
    almacen = estudio.Almacen()
    with _lock:
        _leer(almacen, id_)
        if any(t.estado in trabajos.ACTIVOS for t in trabajos.listar(f'estudio:{id_}')):
            raise HTTPException(409, Texto('est.err_ocupado'))
        try:
            almacen.eliminar(id_)
        except (OSError, ValueError):
            raise HTTPException(400, Texto('est.err_ruta')) from None
    return {'ok': True}
