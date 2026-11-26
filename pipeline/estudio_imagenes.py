"""Almacenamiento privado del Estudio; no crea guiones ni carga modelos al importar."""
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid
import warnings

from PIL import Image, ImageOps

from . import rutas
from .textos import error

FORMATOS = {"9:16": (576, 1024), "16:9": (1024, 576), "1:1": (768, 768), "4:5": (768, 960)}
PASOS = {"rapida": 4, "alta": 25}
MAX_SUBIDA = 12 * 1024 * 1024
MAX_PIXELES = 24_000_000
ID = re.compile(r"^[0-9a-f]{32}$")


def validar(prompt, formato, calidad, intensidad):
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
        raise error("est.err_prompt")
    if (not isinstance(formato, str) or not isinstance(calidad, str)
            or formato not in FORMATOS or calidad not in PASOS):
        raise error("est.err_opciones")
    if (isinstance(intensidad, bool) or not isinstance(intensidad, (float, int))
            or not math.isfinite(intensidad) or not (1 / PASOS[calidad] <= intensidad <= 1)):
        raise error("est.err_intensidad")
    return {"prompt": prompt.strip(), "formato": formato, "calidad": calidad, "intensidad": intensidad}


def fotografia(crudo: bytes) -> Image.Image:
    if not crudo or len(crudo) > MAX_SUBIDA:
        raise error("est.err_foto")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(crudo)) as im:
                if (im.format not in ("PNG", "JPEG", "WEBP") or im.width * im.height > MAX_PIXELES
                        or getattr(im, "n_frames", 1) != 1):
                    raise ValueError()
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
                return im.copy()
    except Exception:
        raise error("est.err_foto") from None


class Almacen:
    def __init__(self, raiz=None):
        self.raiz = Path(raiz) if raiz is not None else rutas.DATOS / "estudio_imagenes"

    def _seguro(self, ruta):
        # Rechazar también junctions de Windows y archivos enlazados.
        for p in (self.raiz, ruta.parent, ruta):
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                raise error("est.err_ruta")
        if not ruta.resolve().is_relative_to(self.raiz.resolve()):
            raise error("est.err_ruta")
        return ruta

    def carpeta(self, id_):
        if not isinstance(id_, str) or not ID.fullmatch(id_):
            raise error("est.err_ruta")
        return self._seguro(self.raiz / id_)

    def archivo(self, id_, nombre):
        if nombre not in ("imagen.png", "referencia.png", "datos.json"):
            raise error("est.err_ruta")
        return self._seguro(self.carpeta(id_) / nombre)

    def guardar(self, datos):
        p = self.archivo(datos['id'], 'datos.json')
        tmp = self._seguro(p.with_suffix('.tmp'))
        try:
            tmp.write_text(json.dumps(datos, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)

    def crear(self, opciones, foto=None):
        opciones = validar(**opciones)
        id_ = uuid.uuid4().hex
        carpeta = self.carpeta(id_)
        carpeta.mkdir(parents=True, exist_ok=False)
        datos = {**opciones, 'id': id_, 'semilla': secrets.randbelow(2**32), 'creado': time.time(),
                 'con_foto': foto is not None, 'estado': 'en cola', 'error': None}
        try:
            if foto is not None:
                foto.save(self.archivo(id_, 'referencia.png'), format='PNG')
            self.guardar(datos)
        except Exception:
            for nombre in ('referencia.png', 'datos.json'):
                self.archivo(id_, nombre).unlink(missing_ok=True)
            carpeta.rmdir()
            raise
        return datos

    def leer(self, id_):
        datos = json.loads(self.archivo(id_, 'datos.json').read_text(encoding='utf-8'))
        if (not isinstance(datos, dict) or datos.get('id') != id_
                or type(datos.get('con_foto')) is not bool
                or type(datos.get('semilla')) is not int or not 0 <= datos['semilla'] < 2**32
                or not isinstance(datos.get('creado'), (int, float))
                or not math.isfinite(datos['creado'])
                or datos.get('estado') not in ('en cola', 'corriendo', 'listo', 'error')):
            raise error('est.err_ruta')
        validar(**{k: datos.get(k) for k in ('prompt', 'formato', 'calidad', 'intensidad')})
        return datos

    def listar(self):
        if not self.raiz.exists():
            return []
        self._seguro(self.raiz)
        res = []
        for d in self.raiz.iterdir():
            if ID.fullmatch(d.name):
                try:
                    res.append(self.leer(d.name))
                except (OSError, ValueError):
                    continue  # los archivos dañados se conservan
        return sorted(res, key=lambda d: d['creado'], reverse=True)

    def publicar(self, datos, imagen):
        destino = self.archivo(datos['id'], 'imagen.png')
        tmp = self._seguro(destino.with_suffix('.tmp'))
        try:
            imagen.convert('RGB').save(tmp, format='PNG')
            os.replace(tmp, destino)
            self.guardar({**datos, 'estado': 'listo', 'error': None})
        except OSError:
            raise error('est.err_guardar') from None
        finally:
            tmp.unlink(missing_ok=True)

    def eliminar(self, id_):
        self.leer(id_)
        carpeta = self.carpeta(id_)
        permitidos = {'datos.json', 'referencia.png', 'imagen.png'}
        archivos = list(carpeta.iterdir())
        if any(p.name not in permitidos or not self._seguro(p).is_file() for p in archivos):
            raise error('est.err_ruta')
        for p in archivos:
            p.unlink()
        carpeta.rmdir()


def ejecutar(t):
    """Solo se invoca desde la cola GPU existente."""
    from app import licencia
    from . import imagenes
    from .textos import Texto

    almacen = Almacen()
    datos = almacen.leer(t.slug.removeprefix('estudio:'))

    def permiso():
        if not licencia.habilitada():
            raise error('est.licencia_requerida')

    n = PASOS[datos['calidad']]
    efectivos = int(n * datos['intensidad']) if datos['con_foto'] else n
    t.pasos = [{'id': 'carga', 'nombre': Texto('est.cargando'), 'estado': 'pendiente'}]
    t.pasos += [{'id': f'paso-{i}', 'nombre': Texto('est.paso', n=i, total=efectivos),
                 'estado': 'pendiente'} for i in range(1, efectivos + 1)]
    t.pasos += [{'id': 'guardar', 'nombre': Texto('est.guardando'), 'estado': 'pendiente'}]
    try:
        permiso()
        almacen.guardar({**datos, 'estado': 'corriendo'})
        t.avisar('carga', 'corriendo')
        foto = None
        if datos['con_foto']:
            with Image.open(almacen.archivo(datos['id'], 'referencia.png')) as im:
                foto = ImageOps.fit(im.convert('RGB'), FORMATOS[datos['formato']],
                                    method=Image.Resampling.LANCZOS)

        def progreso(tipo, indice):
            if tipo == 'cargado':
                t.avisar('carga', 'ok')
                t.avisar('paso-1', 'corriendo')
            else:
                t.avisar(f'paso-{indice}', 'ok')
                t.avisar(f'paso-{indice + 1}', 'corriendo')

        resultado = imagenes.generar_estudio(datos['prompt'], datos['calidad'], FORMATOS[datos['formato']],
                                            datos['semilla'], foto, datos['intensidad'], progreso)
        t.avisar('guardar', 'corriendo')
        # Revalidar tras la inferencia: una revocación no puede publicar el resultado.
        with licencia._lock:
            permiso()
            almacen.publicar(datos, resultado)
        t.avisar('guardar', 'ok')
        t.resultado = {'imagen': datos['id']}
    except Exception as exc:
        clave = getattr(getattr(exc, 'texto', None), 'clave', 'est.err_generar')
        try:
            almacen.guardar({**datos, 'estado': 'error', 'error_clave': clave})
        except OSError:
            pass  # conservar el error original si el disco está lleno
        raise
