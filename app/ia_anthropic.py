"""Proveedor de IA "Anthropic API": escribe y traduce guiones con la API de mensajes (SDK oficial `anthropic`).

Mismo contrato que escribir.Llamada (Claude Code): correr(prompt, buscar) -> texto; cancelar(); resumen.
Con buscar=True lleva la búsqueda web del servidor (web_search), como WebSearch en Claude Code.
"""
import threading
import time

from pipeline.textos import ErrorEjecucion, Texto

NOMBRE = "Anthropic"
MODELO_DEFECTO = "claude-opus-5-5"
TIMEOUT = 600  # s por petición (la búsqueda web puede tardar)
MAX_REANUDAR = 5  # pause_turn: el servidor pausa las búsquedas largas y se reanudan


class LlamadaAnthropic:
    nombre = NOMBRE

    def __init__(self, api_key: str, modelo: str | None = None, cancelado=Exception):
        self.api_key, self.modelo = api_key, (modelo or MODELO_DEFECTO).strip()
        self._cancelado_exc = cancelado
        self._cancelada = threading.Event()
        self.resumen: Texto | str = ""
        self.segundos = 0

    def cancelar(self):
        # el SDK no corta una petición en curso desde otro hilo: la respuesta se descarta al llegar
        self._cancelada.set()

    def _revisar(self):
        if self._cancelada.is_set():
            raise self._cancelado_exc()

    def correr(self, prompt: str, buscar: bool = True) -> str:
        import anthropic

        self._revisar()
        cliente = anthropic.Anthropic(api_key=self.api_key, timeout=TIMEOUT)
        herramientas = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}] if buscar else []
        t0, busquedas = time.time(), 0
        mensajes = [{"role": "user", "content": prompt}]
        contenido: list = []  # lo que va respondiendo, a través de las pausas
        try:
            for _ in range(MAX_REANUDAR + 1):
                with cliente.beta.messages.stream(
                    model=self.modelo,
                    max_tokens=32000,
                    messages=mensajes,
                    output_config={"effort": "high"},
                    # si el modelo rechaza la petición, la API la reintenta con otro modelo por su cuenta
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **({"tools": herramientas} if herramientas else {}),
                ) as stream:
                    r = stream.get_final_message()
                self._revisar()
                contenido += list(r.content)
                uso = getattr(getattr(r.usage, "server_tool_use", None), "web_search_requests", None)
                busquedas += uso or sum(1 for b in r.content if b.type == "server_tool_use")
                if r.stop_reason != "pause_turn":
                    break
                mensajes = [mensajes[0], {"role": "assistant", "content": contenido}]
        except anthropic.AuthenticationError:
            raise ErrorEjecucion(Texto("err.ia_clave", proveedor=NOMBRE))
        except anthropic.PermissionDeniedError as e:
            raise ErrorEjecucion(Texto("err.ia_fallo", proveedor=NOMBRE, detalle=e.message))
        except anthropic.NotFoundError:
            raise ErrorEjecucion(Texto("err.ia_modelo", proveedor=NOMBRE, modelo=self.modelo))
        except anthropic.RateLimitError:
            raise ErrorEjecucion(Texto("err.ia_limite", proveedor=NOMBRE))
        except anthropic.APIStatusError as e:
            raise ErrorEjecucion(Texto("err.ia_fallo", proveedor=NOMBRE, detalle=f"{e.status_code}: {e.message}"))
        except anthropic.APIConnectionError:
            raise ErrorEjecucion(Texto("err.ia_conexion", proveedor=NOMBRE))
        if r.stop_reason == "refusal":
            raise ErrorEjecucion(Texto("err.ia_rechazo", proveedor=NOMBRE))
        if r.stop_reason == "max_tokens":
            raise ErrorEjecucion(Texto("err.ia_cortada", proveedor=NOMBRE))
        self.segundos = round(time.time() - t0)
        self.resumen = Texto.plural("log.resumen", busquedas, s=self.segundos)
        return "".join(b.text for b in contenido if b.type == "text")
