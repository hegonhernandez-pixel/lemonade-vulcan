"""Proveedor de IA "OpenAI API": escribe y traduce guiones con la API Responses (SDK oficial `openai`).

Mismo contrato que escribir.Llamada: correr(prompt, buscar) -> texto; cancelar(); resumen.
Con buscar=True lleva la herramienta web_search.
"""
import threading
import time

from pipeline.textos import ErrorEjecucion, Texto

NOMBRE = "OpenAI"
MODELO_DEFECTO = "gpt-5"
TIMEOUT = 600


class LlamadaOpenAI:
    nombre = NOMBRE

    def __init__(self, api_key: str, modelo: str | None = None, cancelado=Exception):
        self.api_key, self.modelo = api_key, (modelo or MODELO_DEFECTO).strip()
        self._cancelado_exc = cancelado
        self._cancelada = threading.Event()
        self.resumen: Texto | str = ""
        self.segundos = 0

    def cancelar(self):
        self._cancelada.set()  # la respuesta en curso se descarta al llegar

    def correr(self, prompt: str, buscar: bool = True) -> str:
        import openai

        if self._cancelada.is_set():
            raise self._cancelado_exc()
        cliente = openai.OpenAI(api_key=self.api_key, timeout=TIMEOUT)
        t0 = time.time()
        try:
            r = cliente.responses.create(model=self.modelo, input=prompt,
                                         **({"tools": [{"type": "web_search"}]} if buscar else {}))
        except openai.AuthenticationError:
            raise ErrorEjecucion(Texto("err.ia_clave", proveedor=NOMBRE))
        except openai.NotFoundError:
            raise ErrorEjecucion(Texto("err.ia_modelo", proveedor=NOMBRE, modelo=self.modelo))
        except openai.RateLimitError:
            raise ErrorEjecucion(Texto("err.ia_limite", proveedor=NOMBRE))
        except openai.APIStatusError as e:
            raise ErrorEjecucion(Texto("err.ia_fallo", proveedor=NOMBRE, detalle=f"{e.status_code}: {e.message}"))
        except openai.APIConnectionError:
            raise ErrorEjecucion(Texto("err.ia_conexion", proveedor=NOMBRE))
        if self._cancelada.is_set():
            raise self._cancelado_exc()
        if getattr(r, "status", "completed") == "incomplete":
            raise ErrorEjecucion(Texto("err.ia_cortada", proveedor=NOMBRE))
        busquedas = sum(1 for o in (r.output or []) if getattr(o, "type", "") == "web_search_call")
        self.segundos = round(time.time() - t0)
        self.resumen = Texto.plural("log.resumen", busquedas, s=self.segundos)
        return r.output_text or ""
