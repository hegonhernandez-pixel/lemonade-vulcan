"""Diario de exportaciones y finalización recuperable; nunca ejecuta renders.

RegistroExportaciones recibe la raíz DATOS (no salida/). Sus archivos viven en
DATOS/exportaciones_pendientes, que el integrador no debe publicar por HTTP.
El bloqueo protege hilos de este proceso; no coordina procesos independientes.
"""
import copy
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

VERSION = 1
_LOCK = threading.RLock()
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,80}")
_RESERVADOS = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}
TRANSICIONES = {
    "preparado": frozenset({"renderizando", "fallido", "cancelado", "bloqueado"}),
    "renderizando": frozenset({"render_terminado", "fallido", "cancelado", "bloqueado"}),
    "render_terminado": frozenset({"validado", "fallido", "bloqueado"}),
    "validado": frozenset({"liquidado", "bloqueado"}),
    "liquidado": frozenset({"publicado", "bloqueado"}),
    "publicado": frozenset(),
    "fallido": frozenset(),
    "cancelado": frozenset(),
    "bloqueado": frozenset(),  # solo puede volver a estado_anterior
}
TERMINALES = frozenset({"publicado", "fallido", "cancelado"})


class ErrorExportacion(RuntimeError):
    """Error seguro: nunca incluye el contenido del diario."""


def _id(valor):
    if not isinstance(valor, str):
        raise ErrorExportacion("Identificador de intento inválido.")
    try:
        valido = str(uuid.UUID(valor)) == valor
    except ValueError:
        valido = False
    if not valido:
        raise ErrorExportacion("Identificador de intento inválido.")
    return valor


def _slug(valor):
    if not isinstance(valor, str) or not _SLUG.fullmatch(valor) or valor in _RESERVADOS:
        raise ErrorExportacion("Slug de exportación inválido.")
    return valor


def _sin_enlace(ruta):
    try:
        s = ruta.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ErrorExportacion("No se pudo comprobar la ruta privada del intento.") from None
    if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
        raise ErrorExportacion("No se permiten enlaces ni puntos de reanálisis.")


def _fecha():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _objeto(pares):
    d = {}
    for k, v in pares:
        if k in d:
            raise ValueError("Campo duplicado")
        d[k] = v
    return d


def _exigir_validacion(d):
    """Comprueba la evidencia persistida: metadatos devueltos por validar_mp4.

    El integrador confiable debe guardarlos solo tras validar el MP4 privado.
    No ejecuta ffprobe ni vuelve a inspeccionar o publicar el archivo.
    """
    try:
        esperada, tamano = d["duracion_esperada"], d["tamano_esperado"]
        if esperada is None or tamano is None:
            raise ValueError()
        datos = d["validacion"]
        formato = datos["format"]
        if "mp4" not in formato["format_name"].split(","):
            raise ValueError()
        pistas = datos["streams"]
        if not isinstance(pistas, list):
            raise ValueError()
        videos = [p for p in pistas if p.get("codec_type") == "video"
                  and not p.get("disposition", {}).get("attached_pic", 0)]
        audios = [p for p in pistas if p.get("codec_type") == "audio"]
        if not videos or not audios:
            raise ValueError()
        if any([p.get("width"), p.get("height")] != tamano for p in videos):
            raise ValueError()
        for elemento in [formato, *videos, *audios]:
            valor = elemento["duration"]
            duracion = float(valor)
            if (isinstance(valor, bool) or not math.isfinite(duracion)
                    or duracion <= 0 or abs(duracion - esperada) > 0.5):
                raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ErrorExportacion("El intento no contiene una validación MP4 confirmada y coherente.") from None


def _huella(ruta):
    """Identidad de bytes para recuperar un replace; no decodifica el video."""
    _sin_enlace(ruta)
    try:
        with ruta.open("rb") as f:
            h, cantidad = hashlib.sha256(), 0
            for bloque in iter(lambda: f.read(1024 * 1024), b""):
                h.update(bloque)
                cantidad += len(bloque)
        return {"sha256": h.hexdigest(), "bytes": cantidad}
    except FileNotFoundError:
        return None
    except OSError:
        raise ErrorExportacion("No se pudo comprobar el archivo de exportación.") from None


def _huella_valida(valor):
    return (isinstance(valor, dict) and set(valor) == {"sha256", "bytes"}
            and isinstance(valor["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", valor["sha256"])
            and type(valor["bytes"]) is int and valor["bytes"] >= 0)


class RegistroExportaciones:
    def __init__(self, datos):
        # La raíz es una entrada confiable del integrador, no una ruta del JSON.
        self.raiz = Path(datos).resolve() / "exportaciones_pendientes"

    def _carpeta(self, identificador):
        _sin_enlace(self.raiz)
        carpeta = self.raiz / _id(identificador)
        _sin_enlace(carpeta)
        return carpeta

    def ruta_video(self, identificador):
        """Ruta derivada y privada; no crea ni abre el MP4."""
        with _LOCK:
            ruta = self._carpeta(identificador) / "video.mp4"
            _sin_enlace(ruta)
            return ruta

    def _publico(self, slug):
        salida = self.raiz.parent / "salida"
        publico = salida / _slug(slug) / f"{slug}.mp4"
        for ruta in (salida, publico.parent, publico):
            _sin_enlace(ruta)
        return publico

    def huella_publica(self, slug):
        """Captura el destino anterior ANTES de renderizar para no pisar otro posterior."""
        with _LOCK:
            return _huella(self._publico(slug))

    @staticmethod
    def _validar(d, identificador):
        campos = {"version", "id", "slug", "estado", "revision", "modo_inicio",
                  "duracion_esperada", "tamano_esperado", "validacion", "liquidacion",
                  "miniatura", "estado_anterior", "creado", "actualizado"}
        if not isinstance(d, dict) or set(d) != campos:
            raise ErrorExportacion("Estructura del intento inválida.")
        if type(d["version"]) is not int or d["version"] != VERSION:
            raise ErrorExportacion("Versión de intento no compatible.")
        if d["id"] != identificador or _id(d["id"]) != identificador:
            raise ErrorExportacion("Identidad del intento inconsistente.")
        _slug(d["slug"])
        if not isinstance(d["estado"], str) or d["estado"] not in TRANSICIONES:
            raise ErrorExportacion("Estado de intento inválido.")
        if type(d["revision"]) is not int or d["revision"] < 0:
            raise ErrorExportacion("Revisión de intento inválida.")
        if d["modo_inicio"] not in ("prueba", "pago", "desarrollo"):
            raise ErrorExportacion("Modo de intento inválido.")
        anterior = d["estado_anterior"]
        if d["estado"] == "bloqueado":
            if not isinstance(anterior, str) or anterior not in TRANSICIONES or anterior in TERMINALES | {"bloqueado"}:
                raise ErrorExportacion("Estado de recuperación inválido.")
        elif anterior is not None:
            raise ErrorExportacion("Estado anterior inesperado.")
        for campo in ("creado", "actualizado"):
            if not isinstance(d[campo], str):
                raise ErrorExportacion("Fecha de intento inválida.")
            try:
                fecha = datetime.datetime.fromisoformat(d[campo])
                if fecha.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ErrorExportacion("Fecha de intento inválida.") from None
        dur = d["duracion_esperada"]
        if dur is not None and (type(dur) not in (int, float) or not math.isfinite(dur) or dur <= 0):
            raise ErrorExportacion("Duración esperada inválida.")
        tam = d["tamano_esperado"]
        if tam is not None and (not isinstance(tam, list) or len(tam) != 2 or any(type(n) is not int or n <= 0 for n in tam)):
            raise ErrorExportacion("Dimensiones esperadas inválidas.")
        if d["validacion"] is not None and not isinstance(d["validacion"], dict):
            raise ErrorExportacion("Metadatos de validación inválidos.")
        if d["liquidacion"] not in (None, "contabilizado", "ya_contabilizado", "exento_pago", "exento_gratis",
                                    "exento_desarrollo"):
            raise ErrorExportacion("Liquidación inválida.")
        if d["miniatura"] not in ("pendiente", "lista", "fallida", "no_aplica"):
            raise ErrorExportacion("Estado de miniatura inválido.")

    @staticmethod
    def _escribir(ruta, datos):
        temporal = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ruta.parent,
                                             prefix=".intento-", suffix=".tmp", delete=False) as f:
                temporal = Path(f.name)
                json.dump(datos, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, ruta)
        except (OSError, ValueError, TypeError):
            raise ErrorExportacion("No se pudo guardar el intento; no se confirmó el cambio.") from None
        finally:
            if temporal is not None:
                try:
                    temporal.unlink(missing_ok=True)
                except OSError:
                    pass  # un temporal huérfano nunca se interpreta como diario confirmado

    def crear(self, slug, modo_inicio="prueba"):
        with _LOCK:
            _slug(slug)
            identificador = str(uuid.uuid4())
            fecha = _fecha()
            d = {"version": VERSION, "id": identificador, "slug": slug,
                 "estado": "preparado", "revision": 0, "modo_inicio": modo_inicio,
                 "duracion_esperada": None, "tamano_esperado": None,
                 "validacion": None, "liquidacion": None, "miniatura": "pendiente",
                 "estado_anterior": None, "creado": fecha, "actualizado": fecha}
            self._validar(d, identificador)
            carpeta = self._carpeta(identificador)
            try:
                self.raiz.mkdir(parents=True, exist_ok=True)
                carpeta.mkdir(exist_ok=False)  # nunca reutilizar ni sobrescribir un UUID
            except OSError:
                raise ErrorExportacion("No se pudo crear un directorio nuevo para el intento.") from None
            self._escribir(carpeta / "intento.json", d)
            return copy.deepcopy(d)

    def leer(self, identificador):
        with _LOCK:
            ruta = self._carpeta(identificador) / "intento.json"
            _sin_enlace(ruta)
            try:
                d = json.loads(ruta.read_text(encoding="utf-8"), object_pairs_hook=_objeto)
            except (OSError, ValueError, RecursionError):
                raise ErrorExportacion("No se pudo leer el intento; se conserva sin modificar.") from None
            self._validar(d, identificador)
            return d

    def transicionar(self, identificador, nuevo, revision_esperada, **cambios):
        with _LOCK:
            d = self.leer(identificador)
            if type(revision_esperada) is not int or d["revision"] != revision_esperada:
                raise ErrorExportacion("El intento cambió; vuelve a leerlo antes de actualizar.")
            anterior = d["estado"]
            permitidos = {d["estado_anterior"]} if anterior == "bloqueado" else TRANSICIONES[anterior]
            if not isinstance(nuevo, str) or nuevo not in permitidos:
                raise ErrorExportacion("Transición de intento no permitida.")
            if set(cambios) - {"duracion_esperada", "tamano_esperado", "validacion", "liquidacion", "miniatura"}:
                raise ErrorExportacion("No se pueden reemplazar campos de identidad o control.")
            d.update(copy.deepcopy(cambios))
            d.update(estado=nuevo, estado_anterior=anterior if nuevo == "bloqueado" else None,
                     revision=d["revision"] + 1, actualizado=_fecha())
            self._validar(d, identificador)
            ruta = self._carpeta(identificador) / "intento.json"
            _sin_enlace(ruta)
            self._escribir(ruta, d)
            return copy.deepcopy(d)

    def contabilizar_trial(self, identificador, slug):
        """Liquida un intento TRIAL validado, sin publicar ni tocar el MP4.

        Orden de bloqueos: exportaciones -> licencia, siempre en este proceso.
        Un fallo deja el intento validado y conserva los contadores parciales;
        repetir con la misma identidad reconcilia antes de liquidar. Un fallo
        posterior al replace del intento se resuelve releyendo su recibo.
        """
        from . import licencia

        with _LOCK, licencia._lock:
            d = self.leer(identificador)
            if d["slug"] != _slug(slug) or d["modo_inicio"] != "prueba":
                raise ErrorExportacion("El intento no corresponde al proyecto TRIAL indicado.")
            if d["estado"] not in ("validado", "liquidado", "publicado"):
                raise ErrorExportacion("El intento no está validado para contabilizarse.")
            _exigir_validacion(d)
            recibo = d["liquidacion"]
            if d["estado"] == "validado" and recibo is not None:
                raise ErrorExportacion("El intento tiene una liquidación inconsistente.")
            if d["estado"] != "validado" and recibo is None:
                raise ErrorExportacion("El intento no tiene una liquidación confirmada.")
            try:
                if d["estado"] != "validado":
                    if recibo in ("contabilizado", "ya_contabilizado"):
                        licencia._contabilizar_trial_confirmado(slug, exigir_existente=True)
                    return d  # preservar recibo y revisión, incluidas exenciones previas
                resultado = licencia._contabilizar_trial_confirmado(slug)
            except (RuntimeError, OSError, ValueError, TypeError, RecursionError):
                raise ErrorExportacion("No se confirmó el cupo TRIAL; se conserva el intento para reintentar.") from None
            return self.transicionar(identificador, "liquidado", d["revision"], liquidacion=resultado)

    def finalizar(self, identificador):
        """Valida, liquida y publica; también es la recuperación explícita por ID.

        No se llama al iniciar ni re-renderiza. preparado/renderizando necesitan
        una generación nueva: un archivo parcial nunca demuestra un render completo.
        La huella y el destino anterior se guardan dentro de la evidencia existente.
        Solo se reemplaza ese destino anterior o se reconoce el nuevo ya publicado.
        Requiere un único proceso escritor, como el resto del diario.
        """
        from . import licencia
        from pipeline.validar_mp4 import ErrorValidacionMP4, validar_mp4

        with _LOCK:
            d = self.leer(identificador)
            if d["estado"] not in ("render_terminado", "validado", "liquidado", "publicado"):
                raise ErrorExportacion("El intento no puede finalizarse sin un render completo.")
            privado = self.ruta_video(identificador)
            publico = self._publico(d["slug"])
            if d["estado"] == "render_terminado":
                previo = d["validacion"]
                if (not isinstance(previo, dict) or not isinstance(previo.get("publicacion"), dict)
                        or "anterior" not in previo["publicacion"]):
                    raise ErrorExportacion("Falta la identidad del destino anterior al render; requiere revisión.")
                anterior = previo["publicacion"]["anterior"]
                if anterior is not None and not _huella_valida(anterior):
                    raise ErrorExportacion("La identidad del destino anterior es inválida.")
                try:
                    evidencia = validar_mp4(privado, d["tamano_esperado"], d["duracion_esperada"])
                except ErrorValidacionMP4:
                    self.transicionar(identificador, "fallido", d["revision"])
                    raise
                archivo = _huella(privado)
                if archivo is None or archivo["bytes"] <= 0:
                    raise ErrorExportacion("No se conserva el MP4 validado; no se contabilizó.")
                evidencia["publicacion"] = {"archivo": archivo, "anterior": anterior}
                d = self.transicionar(identificador, "validado", d["revision"], validacion=evidencia)
            _exigir_validacion(d)
            publicacion = d["validacion"].get("publicacion")
            if (not isinstance(publicacion, dict) or set(publicacion) != {"archivo", "anterior"}
                    or not _huella_valida(publicacion["archivo"]) or publicacion["archivo"]["bytes"] <= 0
                    or (publicacion["anterior"] is not None and not _huella_valida(publicacion["anterior"]))):
                raise ErrorExportacion("Falta la identidad persistida del MP4; requiere revisión.")
            esperada = publicacion["archivo"]
            actual = _huella(publico)
            if d["estado"] == "publicado":
                if actual != esperada:
                    raise ErrorExportacion("La publicación fue sustituida; no se reemplazará de nuevo.")
                return publico
            if actual != esperada:
                if _huella(privado) != esperada:
                    raise ErrorExportacion("El MP4 privado falta o cambió; no se publicará.")
                if actual != publicacion["anterior"]:
                    raise ErrorExportacion("El MP4 público cambió; no se sobrescribirá una exportación posterior.")
            # No permitir que activar/quitar licencia se intercale con la autorización
            # final y el replace. El orden sigue siendo exportaciones -> licencia.
            with licencia._lock:
                if licencia.comercial():
                    licencia.preparar_exportacion(d["slug"])
                if d["modo_inicio"] == "prueba":
                    d = self.contabilizar_trial(identificador, d["slug"])
                elif d["estado"] == "validado":
                    if licencia.rutas.INSTALADO:
                        if licencia.LICENCIA_REQUERIDA and licencia.activa() is None:
                            raise ErrorExportacion("Se necesita la licencia de pago para publicar este render sin TRIAL.")
                        recibo = "exento_pago" if licencia.LICENCIA_REQUERIDA else "exento_gratis"
                    else:
                        recibo = "exento_desarrollo"
                    d = self.transicionar(identificador, "liquidado", d["revision"], liquidacion=recibo)
                recibo = d["liquidacion"]
                if recibo in ("contabilizado", "ya_contabilizado"):
                    if d["modo_inicio"] != "prueba":
                        raise ErrorExportacion("Liquidación incompatible con el modo del render.")
                elif recibo == "exento_pago":
                    if licencia.LICENCIA_REQUERIDA and licencia.activa() is None:
                        raise ErrorExportacion("La exención de pago ya no está vigente; no se publicará.")
                elif recibo == "exento_gratis":
                    if licencia.LICENCIA_REQUERIDA:
                        raise ErrorExportacion("Esta versión exige licencia; no se publicará.")
                elif recibo == "exento_desarrollo":
                    if licencia.rutas.INSTALADO:
                        raise ErrorExportacion("La exención de desarrollo no permite publicar en modo instalado.")
                else:
                    raise ErrorExportacion("La contabilización no está confirmada; no se publicará.")
                if actual != esperada:
                    try:
                        publico.parent.mkdir(parents=True, exist_ok=True)
                        with privado.open("r+b") as f:
                            os.fsync(f.fileno())
                        os.replace(privado, publico)
                    except OSError:
                        raise ErrorExportacion("No se pudo publicar el MP4; conserva el intento para reintentar.") from None
                self.transicionar(identificador, "publicado", d["revision"])
                return publico

    def pendientes(self):
        """Inventario de solo lectura, incluidos diarios ilegibles y directorios huérfanos.

        Devuelve {intentos: [...], incidencias: [{id, motivo}]}; no recupera,
        migra, elimina ni abre los MP4. Ignora archivos temporales.
        """
        with _LOCK:
            _sin_enlace(self.raiz)
            resultado = {"intentos": [], "incidencias": []}
            if not self.raiz.exists():
                return resultado
            try:
                entradas = sorted(self.raiz.iterdir(), key=lambda p: p.name)
            except OSError:
                raise ErrorExportacion("No se pudo consultar el directorio de intentos.") from None
            for entrada in entradas:
                try:
                    _id(entrada.name)
                except ErrorExportacion:
                    resultado["incidencias"].append({"id": None, "motivo": "Entrada desconocida conservada."})
                    continue
                try:
                    d = self.leer(entrada.name)
                    if d["estado"] not in TERMINALES:
                        resultado["intentos"].append(d)
                except ErrorExportacion as e:
                    resultado["incidencias"].append({"id": entrada.name, "motivo": str(e)})
            return resultado
