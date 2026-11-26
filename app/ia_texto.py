"""Proveedores de guion Gemini y Ollama."""
import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pipeline.textos import ErrorEjecucion, Texto


class LlamadaTexto:
    def __init__(self, proveedor, modelo, cancelado, *, key="", buscar_google=False):
        self.gemini = proveedor == "gemini"
        self.nombre = "Gemini" if self.gemini else "Ollama"
        self.modelo = modelo
        self.key = key
        self.buscar_google = buscar_google
        self._cancelada = threading.Event()
        self._exc = cancelado
        self.resumen = ""
        self.segundos = 0

    def cancelar(self):
        self._cancelada.set()

    def correr(self, prompt, buscar=True):
        if self._cancelada.is_set():
            raise self._exc()
        headers = {"Content-Type": "application/json"}
        if self.gemini:
            if not re.fullmatch(r"[\w.-]+", self.modelo):
                raise ErrorEjecucion(Texto("err.ia_modelo", proveedor=self.nombre, modelo=self.modelo))
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.modelo}:generateContent"
            headers["x-goog-api-key"] = self.key
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            if buscar and self.buscar_google:
                payload["tools"] = [{"google_search": {}}]
        else:
            url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
            payload = {"model": self.modelo, "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "format": "json"}
        if buscar and not (self.gemini and self.buscar_google):
            nota = "La búsqueda web está deshabilitada. Usa el material provisto y omite detalles sin verificar."
            if self.gemini:
                payload["contents"][0]["parts"].append({"text": nota})
            else:
                payload["messages"].insert(0, {"role": "system", "content": nota})
        inicio = time.monotonic()
        try:
            r = Request(url, headers=headers, data=json.dumps(payload).encode())
            with urlopen(r, timeout=240) as respuesta:
                data = json.load(respuesta)
            if self.gemini:
                candidato = data["candidates"][0]
                resultado = "\n".join(p["text"] for p in candidato["content"]["parts"]
                                      if p.get("text") and not p.get("thought"))
                fuentes = candidato.get("groundingMetadata", {}).get("groundingChunks") or []
                self.resumen = f"Google Search · {len(fuentes)} fuentes" if fuentes else ""
            else:
                resultado = data["message"]["content"]
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise ErrorEjecucion(Texto("err.ia_clave", proveedor=self.nombre)) from None
            if exc.code == 404:
                raise ErrorEjecucion(Texto("err.ia_modelo", proveedor=self.nombre, modelo=self.modelo)) from None
            raise RuntimeError(f"{self.nombre} devolvió HTTP {exc.code}.") from None
        except (URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            raise RuntimeError(f"{self.nombre} no devolvió un guion. Revisa que el proveedor esté disponible.") from exc
        self.segundos = time.monotonic() - inicio
        if self._cancelada.is_set():
            raise self._exc()
        return resultado
