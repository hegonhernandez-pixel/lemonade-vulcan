"""Escribe y traduce guiones con IA. Los prompts se arman aquí y solo aquí.

Proveedor (Ajustes, "proveedor_ia"):
  claude_code  Claude Code en modo no interactivo (`claude -p`), con la suscripción del usuario (sin API key)
  anthropic    API de Anthropic con su API key (app/ia_anthropic.py)
  openai       API de OpenAI con su API key (app/ia_openai.py)
  ninguno      sin IA integrada: "Generar guion" da el prompt para copiarlo a otra IA y "Traducir" el guion como
               texto numerado; el usuario pega la respuesta (leer_traduccion_manual, al final)

Para escribir, la IA solo puede buscar en la web; para traducir, ninguna herramienta. Nunca lee ni escribe
archivos: devuelve el JSON y la app lo valida y lo guarda.
"""
import json
import re
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from app.ia_anthropic import LlamadaAnthropic
from app.ia_openai import LlamadaOpenAI
from app.ia_texto import LlamadaTexto
from pipeline import guion as mod_guion
from pipeline.subtitulos import ajuste_global
from pipeline.textos import Error, ErrorEjecucion, Texto, Unidos, de, error

RAIZ = Path(__file__).resolve().parent.parent
REGLAS = Path(__file__).resolve().parent / "reglas_guion.md"  # reglas de escritura: van en el producto (CLAUDE.md no)
TIMEOUT = 300  # s; un guion suele tardar 30 s-2 min


class Cancelado(Exception):
    pass


class SinFormato(Exception):
    """Claude respondió, pero ni al segundo intento salió un guion válido. 'crudo' es su respuesta."""

    def __init__(self, mensaje: Texto, crudo: str):
        super().__init__(mensaje)
        self.texto, self.crudo = mensaje, crudo


def ubicar_claude() -> str:
    """En Windows `claude` es un shim .cmd de npm: se usa el claude.exe nativo que hay detrás,
    así el prompt no pasa por cmd.exe (comillas, saltos de línea, %...)."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    ruta = shutil.which("claude")
    if not ruta:
        raise ErrorEjecucion(Texto("err.ia_no_instalada", proveedor="Claude Code"))
    p = Path(ruta)
    if p.suffix.lower() in (".cmd", ".bat", ".ps1", ""):
        exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
        if exe.exists():
            return str(exe)
    return ruta


# ---------- prompt ----------

def frase_cierre() -> dict:
    """La frase de cierre de Ajustes ({"es": ..., "en": ...}); vacía = el guion termina sin frase fija."""
    f = ajuste_global("frase_cierre") or {}
    return {k: (f.get(k) or "").strip() for k in ("es", "en")}


def reglas(idioma: str = "es") -> str:
    """app/reglas_guion.md con la frase de cierre de Ajustes en su lugar ({{FRASE_CIERRE}})."""
    frase = frase_cierre().get(idioma) or ""
    cierre = (f' o con la frase de cierre del canal, tal cual: "{frase}".' if frase else
              ". No hay una frase de cierre fija: no inventes un lema ni una despedida.")
    return REGLAS.read_text(encoding="utf-8").replace("{{FRASE_CIERRE}}", cierre)


def construir_prompt(datos: dict, nota: str | None = None, actual: dict | None = None) -> str:
    formato = datos.get("formato", "short")
    pmin, pmax, emin, emax = mod_guion.LIMITES[formato]
    tema = (datos.get("tema") or "").strip()
    material = (datos.get("material") or "").strip()
    idioma = datos.get("idioma", "es")

    partes = [
        "Eres el guionista de un canal de YouTube de historias reales. Estas son las reglas del canal:",
        f"<reglas>\n{reglas(idioma)}\n</reglas>",
        "No tienes acceso a archivos: tu única salida es el JSON del guion.",
        "",
        f"TAREA: escribe el guion de un {'Short (vertical, 45-60 s)' if formato == 'short' else 'video largo (16:9, 8-12 min)'}.",
        *([] if datos.get("idioma", "es") != "en" else [
            "IDIOMA: este video es en INGLÉS. Escribe 'titulo', 'titulo_miniatura' y el 'texto' de las escenas en "
            "inglés estadounidense natural y hablado (no traducido del español). Lo que las reglas dicen del español "
            "aplica igual al inglés."]),
        f"Tema: {tema or '(ver material)'}",
        "",
        "Datos fijos (ya están elegidos; no los incluyas en tu respuesta):",
        f"- formato: {formato}",
        f"- voz: {datos.get('voz', '')}",
        f"- estilo_visual: {datos.get('estilo_visual', '')} (se añade solo a cada prompt de imagen: no lo repitas)",
        f"- musica: {datos.get('musica') or 'sin música'}",
        "",
    ]
    if material:
        partes += [
            "Basa el guion en este MATERIAL y condénsalo. No añadas hechos que no estén en él; si buscas en la web "
            "es solo para confirmar un dato del material, no para cambiar la historia.",
            f"<material>\n{material}\n</material>",
        ]
    else:
        partes.append("No hay material: investiga el tema con la búsqueda web antes de escribir (fechas, lugares, nombres).")
    partes += [
        "",
        "EXTENSIÓN:",
        f"- {pmin}-{pmax} palabras en total sumando el 'texto' de todas las escenas.",
        f"- {emin}-{emax} escenas. Máximo {mod_guion.MAX_PALABRAS_ESCENA} palabras por escena.",
        "",
        "VERACIDAD:",
        "- No inventes fechas, cifras ni nombres que no estén en el material o que no hayas verificado.",
        "- Si dudas de un detalle, dilo en general (\"en octubre de 2011\", \"según varios testimonios\") en vez de "
        "precisarlo. No uses \"// VERIFICAR\": el guion tiene que poder producirse tal cual.",
        "",
        "PROMPTS DE IMAGEN (campo 'prompt', en inglés):",
        "- Sin texto en la imagen. Sin personas reales reconocibles: descríbelas por rasgos genéricos "
        "(\"a Soviet officer in his 40s in uniform\"), nunca por su nombre.",
        "- Nada de órganos, sangre, heridas ni gore. Sugiere en vez de mostrar.",
        "",
        "TÍTULO DE MINIATURA: 'titulo_miniatura', máximo 6 palabras, con la palabra clave entre *asteriscos*.",
    ]
    if actual:
        partes += [
            "",
            "REESCRITURA: este es el guion actual. Reescríbelo aplicando la nota del autor; conserva lo que no "
            "contradiga la nota.",
            f"<guion_actual>\n{json.dumps(actual, ensure_ascii=False, indent=1)}\n</guion_actual>",
            f"Nota del autor: {nota or '(sin nota: mejóralo)'}",
        ]
    partes += [
        "",
        "RESPUESTA: SOLO un objeto JSON válido, sin texto antes ni después y sin ```. Exactamente esta forma:",
        '{"titulo": "...", "titulo_miniatura": "...", "escenas": [{"texto": "...", "prompt": "..."}]}',
    ]
    return "\n".join(partes)


def _prompt_reparar(crudo: str, error: str) -> str:
    return (
        "Este texto tenía que ser el JSON de un guion de video y no es válido.\n"
        f"Problema: {error}\n\n"
        f"<texto>\n{crudo}\n</texto>\n\n"
        "Devuelve SOLO el JSON corregido, sin texto antes ni después y sin ```, con esta forma exacta:\n"
        '{"titulo": "...", "titulo_miniatura": "...", "escenas": [{"texto": "...", "prompt": "..."}]}\n'
        "No cambies el contenido: solo arregla el formato."
    )


# ---------- ejecución ----------

class Llamada:
    """Una invocación de `claude -p` (proveedor Claude Code) que se puede cancelar desde otro hilo."""

    nombre = "Claude Code"

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._cancelada = threading.Event()
        self.resumen: Texto | str = ""
        self.segundos = 0

    def cancelar(self):
        self._cancelada.set()
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    def correr(self, prompt: str, buscar: bool = True) -> str:
        """buscar=False: sin ninguna herramienta (traducir no necesita la web)."""
        if self._cancelada.is_set():
            raise Cancelado()
        args = [ubicar_claude(), "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config"]
        # solo WebSearch: sin herramientas de lectura ni escritura. También en el reintento: con --tools ""
        # y una respuesta previa que mencione buscar, claude -p puede quedarse colgado esperando una herramienta.
        args += ["--tools", "WebSearch", "--allowedTools", "WebSearch"] if buscar else ["--tools", ""]
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        # en una carpeta vacía: así Claude Code no carga el CLAUDE.md del repo por su cuenta (va en el prompt)
        with tempfile.TemporaryDirectory(prefix="dcrypto-escribir-") as tmp:
            self._proc = subprocess.Popen(args, cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, encoding="utf-8", **extra)
            try:
                out, err = self._proc.communicate(prompt, timeout=TIMEOUT)  # el prompt va por stdin
            except subprocess.TimeoutExpired:
                self._proc.kill()
                raise ErrorEjecucion(Texto("err.ia_tiempo", proveedor=self.nombre, min=TIMEOUT // 60))
        if self._cancelada.is_set():
            raise Cancelado()
        try:
            resp = json.loads(out)
        except json.JSONDecodeError:
            raise ErrorEjecucion(Texto("err.ia_fallo", proveedor=self.nombre,
                                       detalle=f"{self._proc.returncode}: {(err or out).strip()[:600]}"))
        if resp.get("is_error") or resp.get("subtype") != "success":
            raise ErrorEjecucion(Texto("err.ia_fallo", proveedor=self.nombre, detalle=str(resp.get("result") or resp)[:600]))
        # WebSearch corre en un modelo auxiliar: las búsquedas se cuentan en modelUsage, no en usage
        busquedas = sum(m.get("webSearchRequests", 0) for m in (resp.get("modelUsage") or {}).values())
        self.segundos = round(resp.get("duration_ms", 0) / 1000)
        self.resumen = Texto.plural("log.resumen", busquedas, s=self.segundos)
        return resp.get("result") or ""


# ---------- elegir proveedor ----------

PROVEEDORES = {"claude_code": "Claude Code", "anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Gemini", "ollama": "Ollama", "ninguno": ""}
VAR_ENTORNO = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


def clave_api(ajustes: dict, proveedor: str) -> str:
    """La API key de Ajustes; si no hay, la de la variable de entorno."""
    return (ajustes.get(f"{proveedor}_api_key") or os.environ.get(VAR_ENTORNO.get(proveedor, ""), "") or "").strip()


def claude_instalado() -> bool:
    try:
        ubicar_claude()
        return True
    except ErrorEjecucion:
        return False


def disponible(ajustes: dict) -> bool:
    p = ajustes.get("proveedor_ia")
    if p == "claude_code":
        return claude_instalado()
    if p == "ollama":
        return bool(ajustes.get("ollama_modelo"))
    if p in VAR_ENTORNO:
        return bool(clave_api(ajustes, p))
    return False


def nueva_llamada(ajustes: dict):
    """La llamada del proveedor elegido en Ajustes: Llamada (claude -p), LlamadaAnthropic o LlamadaOpenAI."""
    p = ajustes.get("proveedor_ia")
    if p == "anthropic" and clave_api(ajustes, p):
        return LlamadaAnthropic(clave_api(ajustes, p), ajustes.get("anthropic_modelo"), Cancelado)
    if p == "openai" and clave_api(ajustes, p):
        return LlamadaOpenAI(clave_api(ajustes, p), ajustes.get("openai_modelo"), Cancelado)
    if p == "gemini" and clave_api(ajustes, p):
        return LlamadaTexto(p, ajustes.get("gemini_modelo") or "gemini-2.5-flash", Cancelado,
                           key=clave_api(ajustes, p), buscar_google=bool(ajustes.get("buscar_google")))
    if p == "ollama" and ajustes.get("ollama_modelo"):
        return LlamadaTexto(p, ajustes["ollama_modelo"], Cancelado)
    if p == "claude_code":
        return Llamada()
    raise ErrorEjecucion(Texto("srv.sin_ia"))


def _nombre(llamada) -> str:
    return getattr(llamada, "nombre", None) or getattr(llamada, "NOMBRE", "")


def _interpretar(texto: str) -> dict:
    """El guion de la respuesta, ya validado y solo con los campos que pedimos. ValueError si no sirve."""
    data = mod_guion.desde_texto(texto)
    errores = mod_guion.validar(data)
    if errores:
        raise Error(Unidos(errores, "; "))
    return {
        "titulo": data["titulo"].strip(),
        "titulo_miniatura": (data.get("titulo_miniatura") or "").strip(),
        "escenas": [{"texto": e["texto"].strip(), "prompt": e["prompt"].strip()} for e in data["escenas"]],
    }


def escribir_guion(datos: dict, llamada: Llamada, log, nota: str | None = None, actual: dict | None = None) -> dict:
    """{titulo, titulo_miniatura, escenas}. Si el JSON viene mal, reintenta una vez pidiendo solo JSON;
    si vuelve a fallar, lanza SinFormato con la respuesta cruda para arreglarla a mano."""
    log(Texto("log.pidiendo" if (datos.get("material") or "").strip() else "log.pidiendo_buscando", proveedor=_nombre(llamada)))
    crudo = llamada.correr(construir_prompt(datos, nota, actual))
    log(Texto("log.respondio", resumen=llamada.resumen))
    try:
        return _interpretar(crudo)
    except ValueError as e:
        log(Texto("log.reintento", error=de(e)))
        error = str(e)
    crudo2 = llamada.correr(_prompt_reparar(crudo, error))
    try:
        return _interpretar(crudo2)
    except ValueError as e:
        raise SinFormato(Texto("err.sin_formato", error=de(e)), crudo2 or crudo)


# ---------- traducir ----------

def construir_prompt_traduccion(doc: dict) -> str:
    escenas = [{"texto": e.get("texto", "")} for e in doc["escenas"]]
    origen = {"titulo": doc.get("titulo", ""), "titulo_miniatura": doc.get("titulo_miniatura", ""), "escenas": escenas}
    n = len(escenas)
    return "\n".join([
        "Eres el guionista de un canal de YouTube de historias reales. "
        "Traduce al INGLÉS este guion de un video narrado por una voz sintética.",
        f"<guion>\n{json.dumps(origen, ensure_ascii=False, indent=1)}\n</guion>",
        "",
        "REGLAS:",
        "- Inglés estadounidense natural y hablado, que suene escrito en inglés y no traducido. Frases cortas, "
        "fáciles de decir en voz alta.",
        "- Conserva exactamente los hechos: fechas, cifras, lugares y nombres. No añadas ni quites información.",
        f"- Exactamente {n} escenas, en el mismo orden: una traducción por escena. No unas ni partas escenas.",
        f"- Máximo {mod_guion.MAX_PALABRAS_ESCENA} palabras por escena.",
        "- Puntúa bien: los subtítulos cortan sus grupos en . , ; : ? ! Evita frases larguísimas sin puntuación.",
        *([f"- La frase de cierre del canal \"{fc['es']}\" se dice siempre \"{fc['en']}\""]
          if (fc := frase_cierre())["es"] and fc["en"] else []),
        "- \"La historia completa está en el canal\" se dice \"The full story is on the channel.\"",
        "- titulo: título de YouTube en inglés, con el mismo gancho que el original.",
        "- titulo_miniatura: máximo 6 palabras, con la palabra clave entre *asteriscos*. Adáptalo para que "
        "funcione en inglés (la contradicción, el dato raro): no hace falta que sea literal.",
        "",
        "RESPUESTA: SOLO un objeto JSON válido, sin texto antes ni después y sin ```. Exactamente esta forma:",
        '{"titulo": "...", "titulo_miniatura": "...", "escenas": [{"texto": "..."}]}',
    ])


def _interpretar_traduccion(texto: str, n: int) -> dict:
    data = mod_guion.desde_texto(texto)
    if not isinstance(data, dict):
        raise error("err.traduccion_no_objeto")
    errores = []
    for k in ("titulo", "titulo_miniatura"):
        if not isinstance(data.get(k), str) or not data[k].strip():
            errores.append(Texto("err.traduccion_falta", campo=k))
    escenas = data.get("escenas")
    if not isinstance(escenas, list) or len(escenas) != n:
        errores.append(Texto("err.traduccion_escenas", n=n, m=len(escenas) if isinstance(escenas, list) else 0))
    else:
        for i, e in enumerate(escenas, start=1):
            if not isinstance(e, dict) or not isinstance(e.get("texto"), str) or not e["texto"].strip():
                errores.append(Texto("err.traduccion_escena", n=i))
    if errores:
        raise Error(Unidos(errores, "; "))
    return {"titulo": data["titulo"].strip(), "titulo_miniatura": data["titulo_miniatura"].strip(),
            "escenas": [e["texto"].strip() for e in escenas]}


def traducir_guion(doc: dict, llamada: Llamada, log) -> dict:
    """{titulo, titulo_miniatura, escenas: [texto, ...]} en inglés, una por escena del original.
    Reintenta una vez si el JSON viene mal o con otro número de escenas."""
    n = len(doc["escenas"])
    log(Texto("log.traduciendo", n=n, proveedor=_nombre(llamada)))
    crudo = llamada.correr(construir_prompt_traduccion(doc), buscar=False)
    log(Texto("log.respondio", resumen=Texto("log.resumen_corto", s=llamada.segundos)))
    try:
        return _interpretar_traduccion(crudo, n)
    except ValueError as e:
        log(Texto("log.traduccion_reintento", error=de(e)))
        error = str(e)
    crudo2 = llamada.correr(
        f"Esta traducción de un guion no es válida.\nProblema: {error}\n\n<texto>\n{crudo}\n</texto>\n\n"
        f"Devuelve SOLO el JSON corregido, con exactamente {n} escenas, sin texto antes ni después y sin ```:\n"
        '{"titulo": "...", "titulo_miniatura": "...", "escenas": [{"texto": "..."}]}', buscar=False)
    try:
        return _interpretar_traduccion(crudo2, n)
    except ValueError as e:
        raise ErrorEjecucion(Texto("err.traduccion_invalida", error=de(e)))


# ---------- traducir sin IA integrada ----------
# La app copia el guion como texto (título, título de miniatura y escenas numeradas: lo arma la interfaz),
# el usuario lo traduce donde quiera (Claude, ChatGPT, Gemini, DeepL, Google Translate) y pega el resultado.
# Las traducciones cambian las etiquetas ("Título:" -> "Title:"), añaden negritas o ``` y a veces parten una
# escena en dos líneas: por eso se lee con tolerancia y las escenas se emparejan por su número.

_NUMERO = re.compile(r"^\s*(?:\*\*)?(?:#\s*)?(\d{1,3})\s*(?:\*\*)?\s*[.)\-:–—]\s*(?:\*\*)?\s*(.*)$")
_ETIQUETA = re.compile(r"^\s*[#>*_\s]*([^:：]{1,40})[:：]\s*(?:\*\*|__)?\s*(.*)$")


class TraduccionIncompleta(Exception):
    """La traducción pegada no cuadra con el original: dice qué escenas faltan, sobran o se repiten."""

    def __init__(self, errores: list, faltan=(), sobran=(), repetidas=()):
        super().__init__("; ".join(str(e) for e in errores))
        self.errores, self.faltan, self.sobran, self.repetidas = errores, list(faltan), list(sobran), list(repetidas)


def _limpiar(s: str) -> str:
    """Sin negritas de markdown alrededor ni comillas que algunos traductores añaden."""
    s = s.strip()
    if s.startswith("**") and s.endswith("**") and len(s) > 4:
        s = s[2:-2].strip()
    s = s.removesuffix("**").removeprefix("**").strip()
    if len(s) > 1 and s[0] == s[-1] and s[0] in "\"“”«»'":
        s = s[1:-1].strip()
    return s.strip("“”«»").strip()


def leer_traduccion_manual(texto: str, n: int) -> dict:
    """{titulo, titulo_miniatura, escenas: [texto, ...]} de una traducción pegada, una por escena del
    original (n). Las líneas "1. ...", "2) ..." son escenas; antes de ellas, el título y el título de
    miniatura (por su etiqueta en cualquier idioma, o por orden si no la tienen).
    TraduccionIncompleta si falta algo o el número de escenas no coincide."""
    titulo = miniatura = ""
    sueltas: list[str] = []  # líneas sin etiqueta antes de la primera escena
    escenas: dict[int, list[str]] = {}
    repetidas: list[int] = []
    actual = None
    for linea in (texto or "").splitlines():
        l = linea.strip()
        if not l or l.startswith("```") or set(l) <= set("-=*_—"):
            actual = None  # una línea vacía o un ``` cierra la escena: lo que siga no es parte de ella
            continue
        m = _NUMERO.match(l)
        if m:
            num = int(m.group(1))
            if num in escenas:
                repetidas.append(num)
            escenas[num] = [m.group(2)]
            actual = num
            continue
        if actual is not None:  # una escena partida en dos líneas
            escenas[actual].append(l)
            continue
        if escenas:
            continue  # tras las escenas: "¿Necesitas algo más?" y otros comentarios del traductor
        e = _ETIQUETA.match(l)
        etiqueta = _limpiar(e.group(1)).lower() if e else ""
        if e and not _limpiar(e.group(2)):
            continue  # "Aquí está la traducción:" y otras líneas que solo presentan
        if e and any(k in etiqueta for k in ("miniatur", "thumbnail", "vignette", "portada", "cover")):
            miniatura = _limpiar(e.group(2))
        elif e and any(k in etiqueta for k in ("título", "titulo", "title", "titre", "titel")):
            titulo = _limpiar(e.group(2))
        else:
            sueltas.append(_limpiar(l))
    # sin etiquetas: la primera línea es el título y la segunda el de miniatura
    for s in sueltas:
        if not titulo:
            titulo = s
        elif not miniatura:
            miniatura = s
    textos_ = {k: _limpiar(" ".join(v)) for k, v in escenas.items()}
    faltan = [i for i in range(1, n + 1) if not textos_.get(i)]
    sobran = sorted(k for k in textos_ if k < 1 or k > n)
    errores = []
    if not titulo:
        errores.append(Texto("err.tr_manual_titulo"))
    if not miniatura:
        errores.append(Texto("err.tr_manual_miniatura"))
    if not escenas:
        errores.append(Texto("err.tr_manual_sin_escenas", n=n))
    else:
        if len(textos_) != n or faltan or sobran:
            errores.append(Texto("err.tr_manual_cuenta", n=n, m=len(textos_)))
        if faltan:
            errores.append(Texto("err.tr_manual_faltan", lista=", ".join(map(str, faltan))))
        if sobran:
            errores.append(Texto("err.tr_manual_sobran", lista=", ".join(map(str, sobran))))
        if repetidas:
            errores.append(Texto("err.tr_manual_repetidas", lista=", ".join(map(str, sorted(set(repetidas))))))
    if errores:
        raise TraduccionIncompleta(errores, faltan, sobran, sorted(set(repetidas)))
    return {"titulo": titulo, "titulo_miniatura": miniatura, "escenas": [textos_[i] for i in range(1, n + 1)]}
