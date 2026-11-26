"""Guion a partir de texto normal: lo pegado se toma como la narración completa y se parte en escenas.

- Escenas de 1 o 2 frases, cortando siempre en un punto (o ? ! …), con ~35 palabras como máximo. Un salto
  de párrafo siempre empieza escena nueva. Una frase de más de 35 palabras se parte en una coma o un ; :
  cerca de la mitad.
- Cada escena lleva un prompt provisional ("prompt_sugerido": true): los sustantivos clave de la frase
  traducidos al inglés con un diccionario (con su adjetivo: "oficial soviético" -> "Soviet officer"), más la
  década de la frase o, si no tiene año, la de la historia. Si la frase da 2 claves o menos, una plantilla de
  escena con el protagonista y el lugar de la historia. Sin nombres propios (los prompts no llevan nombres de
  personas reales). El estilo visual lo añade el pipeline a cada prompt.

Sin IA ni internet: es un punto de partida para editar, no un guion terminado.
"""
import re
import unicodedata

from pipeline.subtitulos import ABREVIATURAS

MAX_PALABRAS = 35

# Fin de frase: . ? ! … (y comillas o paréntesis de cierre) seguido de espacio.
_FIN = re.compile(r'([.?!…]+["»”’)\]]*)\s+')


def _palabras(t: str) -> int:
    return len(t.split())


def _frases(parrafo: str) -> list[str]:
    frases, inicio = [], 0
    for m in _FIN.finditer(parrafo):
        antes = parrafo[inicio:m.end(1)]
        ultima = antes.split()[-1].lower() if antes.split() else ""
        if ultima in ABREVIATURAS or re.fullmatch(r"\d+\.", ultima) and m.group(1) == ".":
            continue  # "Dr." o "3." en una lista: no es fin de frase
        frases.append(antes.strip())
        inicio = m.end()
    resto = parrafo[inicio:].strip()
    if resto:
        frases.append(resto)
    return [f for f in frases if f]


def _partir_larga(frase: str, maximo: int) -> list[str]:
    """Una frase de más de 'maximo' palabras, en trozos: primero en , ; : cerca de la mitad, si no por palabras."""
    if _palabras(frase) <= maximo:
        return [frase]
    palabras = frase.split()
    mitad = len(palabras) / 2
    cortes = [i + 1 for i, p in enumerate(palabras[:-1]) if p[-1] in ",;:" and 5 <= i + 1 <= len(palabras) - 5]
    i = min(cortes, key=lambda c: abs(c - mitad)) if cortes else int(mitad)
    return _partir_larga(" ".join(palabras[:i]), maximo) + _partir_larga(" ".join(palabras[i:]), maximo)


def escenas(texto: str, maximo: int = MAX_PALABRAS) -> list[str]:
    """El texto de cada escena, en orden."""
    res = []
    for parrafo in re.split(r"\n\s*\n|\r\n\s*\r\n", texto.strip()):
        parrafo = " ".join(parrafo.split())
        if not parrafo:
            continue
        trozos = [t for f in _frases(parrafo) for t in _partir_larga(f, maximo)]
        actual: list[str] = []
        for t in trozos:
            if actual and (len(actual) == 2 or _palabras(" ".join(actual + [t])) > maximo):
                res.append(" ".join(actual))
                actual = []
            actual.append(t)
        if actual:
            res.append(" ".join(actual))
    return res


# ---------- prompt sugerido ----------

def _sin_tildes(p: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", p) if unicodedata.category(c) != "Mn")


# Diccionario español -> inglés de lo que se puede dibujar en historias reales, misterios, ciencia, historia y
# guerra ("_" = espacio). Por categorías: las personas y los lugares de la historia sirven también de contexto
# para las escenas que por sí solas no dan imagen (ver prompt_sugerido). Las formas en plural y femenino se
# buscan solas (_buscar), así que va solo el singular masculino salvo que la palabra cambie.
_PERSONAS = """
hombre:man mujer:woman nino:child nina:girl chico:boy chica:girl joven:young_man anciano:old_man anciana:old_woman
padre:father madre:mother hijo:son hija:daughter hermano:brother hermana:sister familia:family esposa:wife esposo:husband
marido:husband bebe:baby amigo:friend gente:people persona:person multitud:crowd vecino:neighbor abuelo:grandfather
abuela:grandmother tio:uncle tia:aunt gemelo:twins pareja:couple novio:groom novia:bride viuda:widow huerfano:orphan
adolescente:teenager cientifico:scientist medico:doctor doctor:doctor doctora:doctor cirujano:surgeon enfermera:nurse
paciente:patient policia:police_officer detective:detective soldado:soldier  piloto:pilot marinero:sailor
marino:sailor capitan:captain rey:king reina:queen principe:prince princesa:princess presidente:president
politico:politician espia:spy agente:agent asesino:killer ladron:thief preso:prisoner prisionero:prisoner juez:judge
abogado:lawyer periodista:journalist reportero:reporter escritor:writer pintor:painter musico:musician actor:actor
actriz:actress director:director profesor:professor maestro:teacher estudiante:student ingeniero:engineer
inventor:inventor matematico:mathematician fisico:physicist quimico:chemist biologo:biologist astronomo:astronomer
filosofo:philosopher poeta:poet cantante:singer bailarina:dancer astronauta:astronaut cosmonauta:cosmonaut monje:monk
monja:nun sacerdote:priest cura:priest obispo:bishop misionero:missionary chaman:shaman curandero:healer bruja:witch
obrero:worker trabajador:worker empleado:clerk granjero:farmer pescador:fisherman minero:miner campesino:peasant
viajero:traveler explorador:explorer aventurero:adventurer cazador:hunter guerrero:warrior caballero:knight
vikingo:viking pirata:pirate samurai:samurai emperador:emperor emperatriz:empress faraon:pharaoh oficial:officer
coronel:colonel comandante:commander sargento:sergeant teniente:lieutenant superior:superior_officer jefe:boss
operador:operator operadora:operator tecnico:technician guardia:guard centinela:sentry testigo:witness victima:victim
sospechoso:suspect criminal:criminal mafioso:mobster banquero:banker empresario:businessman millonario:millionaire
mendigo:beggar refugiado:refugee rebelde:rebel guerrillero:guerrilla_fighter atleta:athlete deportista:athlete
futbolista:soccer_player boxeador:boxer nadador:swimmer alpinista:mountaineer buzo:diver aviador:aviator
conductor:driver taxista:taxi_driver carpintero:carpenter herrero:blacksmith panadero:baker cocinero:cook
mesero:waiter secretaria:secretary vendedor:salesman comerciante:merchant hacker:hacker programador:programmer
telefonista:switchboard_operator cartero:mailman bombero:firefighter rescatista:rescuer psiquiatra:psychiatrist
psicologo:psychologist dentista:dentist mago:magician payaso:clown tripulacion:crew equipo:team neurocientifico:neuroscientist
investigador:researcher genio:genius
"""
_LUGARES = """
casa:house hogar:home habitacion:room cuarto:room sala:room salon:hall pasillo:corridor cocina:kitchen despacho:study
atico:attic sotano:basement garaje:garage almacen:warehouse cabana:cabin choza:hut campamento:camp refugio:shelter
calle:street callejon:alley avenida:avenue esquina:street_corner ciudad:city pueblo:village aldea:village barrio:neighborhood
carretera:road camino:path puente:bridge plaza:town_square parque:park edificio:building rascacielos:skyscraper
torre:tower iglesia:church catedral:cathedral convento:convent monasterio:monastery hospital:hospital clinica:clinic
manicomio:asylum orfanato:orphanage quirofano:operating_room laboratorio:laboratory observatorio:observatory
oficina:office escuela:school universidad:university aula:classroom biblioteca:library museo:museum carcel:prison
prision:prison celda:prison_cell tribunal:courtroom fabrica:factory mina:mine tunel:tunnel cueva:cave pozo:well
granja:farm castillo:castle palacio:palace templo:temple piramide:pyramid ruina:ruins cementerio:cemetery tumba:grave
hotel:hotel bar:bar cafe:cafe restaurante:restaurant casino:casino banco:bank embajada:embassy parlamento:parliament
ministerio:ministry tienda:shop mercado:market estacion:train_station aeropuerto:airport puerto:harbor muelle:pier
faro:lighthouse base:military_base cuartel:barracks trinchera:trench bunker:bunker hangar:hangar 
control:control_room cabina:cockpit cubierta:ship_deck camarote:ship_cabin metro:subway vagon:train_car carpa:tent
circo:circus teatro:theater cine:cinema escenario:stage  estadio:stadium mar:sea oceano:ocean
playa:beach costa:coast acantilado:cliff isla:island rio:river lago:lake montana:mountain sierra:mountain_range
colina:hill valle:valley glaciar:glacier cascada:waterfall pantano:swamp bosque:forest selva:jungle desierto:desert
campo:field jardin:garden espacio:outer_space
"""
_COSAS = """
puerta:door ventana:window pared:wall escalera:staircase
cielo:sky nube:clouds lluvia:rain nieve:snow tormenta:storm viento:wind niebla:fog sol:sun luna:moon estrella:stars
noche:night dia:daylight manana:morning amanecer:dawn atardecer:sunset invierno:winter verano:summer
galaxia:galaxy cometa:comet meteorito:meteorite eclipse:eclipse aurora:aurora rayo:lightning relampago:lightning
huracan:hurricane tornado:tornado inundacion:flood sequia:drought ola:wave marea:tide arena:sand roca:rock piedra:stone
cristal:crystal polvo:dust ceniza:ash lava:lava volcan:volcano terremoto:earthquake naufragio:shipwreck
reflejo:reflection horizonte:horizon paisaje:landscape planeta:planet universo:universe tierra:earth
fuego:fire incendio:fire humo:smoke agua:water hielo:ice arbol:tree flor:flower
barco:ship submarino:submarine avion:airplane helicoptero:helicopter cohete:rocket nave:spacecraft globo:balloon
paracaidas:parachute tren:train coche:car auto:car carro:car camion:truck tanque:tank moto:motorcycle
motocicleta:motorcycle bicicleta:bicycle bote:boat lancha:motorboat velero:sailboat balsa:raft ancla:anchor
caballo:horse perro:dog gato:cat pajaro:bird cuervo:raven lobo:wolf serpiente:snake animal:animal
mesa:table escritorio:desk silla:chair cama:bed libro:book carta:letter papel:papers documento:documents mapa:map
foto:photograph fotografia:photograph periodico:newspaper revista:magazine diario:diary cuaderno:notebook
pizarra:blackboard telegrama:telegram mensaje:message informe:report expediente:file_folder archivo:archive
carpeta:folder contrato:contract firma:signature pasaporte:passport protocolo:rulebook codigo:code
reloj:clock calendario:calendar telefono:telephone radio:radio television:television pantalla:screen monitor:monitor
computadora:computer computador:computer ordenador:computer maquina:machine motor:engine boton:button palanca:lever
cable:cables antena:antenna senal:signal radar:radar satelite:satellite alarma:alarm sirena:siren
microscopio:microscope telescopio:telescope bisturi:scalpel jeringa:syringe frasco:jar probeta:test_tube
arma:weapon pistola:pistol rifle:rifle fusil:rifle canon:cannon espada:sword escudo:shield bomba:bomb misil:missile
municion:ammunition granada:grenade
dinero:money billete:banknotes moneda:coins oro:gold joya:jewels tesoro:treasure cofre:chest llave:key caja:box
maleta:suitcase maletin:briefcase mochila:backpack botella:bottle vaso:glass copa:wine_glass taza:cup plato:plate
cuchillo:knife comida:food pan:bread vino:wine cigarro:cigarette cigarrillo:cigarette fosforo:match vela:candle
lampara:lamp linterna:flashlight antorcha:torch farol:lantern bombilla:light_bulb espejo:mirror cuadro:painting
pintura:painting estatua:statue escultura:sculpture piano:piano guitarra:guitar violin:violin disco:vinyl_record
cinta:tape grabadora:tape_recorder microfono:microphone camara:camera proyector:projector pelicula:film_reel
brujula:compass craneo:skull esqueleto:skeleton hueso:bones ataud:coffin cruz:cross biblia:bible corona:crown
medalla:medal anillo:ring collar:necklace sombrero:hat abrigo:coat uniforme:uniform vestido:dress traje:suit
cuerda:rope cadena:chain candado:padlock jaula:cage
cuerpo:body cara:face mano:hand ojo:eyes corazon:heart cerebro:brain mente:mind sueno:dream
guerra:war batalla:battle ejercito:army bandera:flag frontera:border explosion:explosion accidente:accident
ataque:attack contraataque:counterattack invasion:invasion bombardeo:bombing disparo:gunshot tiroteo:shootout
protesta:protest manifestacion:protest_march revolucion:revolution epidemia:epidemic pandemia:pandemic virus:virus
vacuna:vaccine fiesta:party boda:wedding funeral:funeral entierro:burial ceremonia:ceremony reunion:meeting
conferencia:conference entrevista:interview  viaje:journey huida:escape escape:escape rescate:rescue
desaparicion:disappearance persecucion:chase interrogatorio:interrogation trampa:trap
juicio:trial crimen:crime_scene misterio:mystery secreto:secret experimento:experiment descubrimiento:discovery
cirugia:surgery operacion:surgery enfermedad:illness medicina:medicine pastilla:pills
silencio:silence oscuridad:darkness sombra:shadows luz:light
minuto:ticking_clock hora:clock tiempo:clock ano:old_calendar decada:old_calendar recuerdo:old_photographs
memoria:old_photographs soledad:lonely_figure conciencia:consciousness realidad:reality vida:life muerte:death
"""
# Adjetivos: se juntan con el sustantivo de al lado ("oficial soviético" -> "Soviet officer",
# "falsa alarma" -> "false alarm", "guerra mundial" -> "world war").
_ADJETIVOS = """
sovietico:Soviet ruso:Russian americano:American estadounidense:American mexicano:Mexican colombiano:Colombian
argentino:Argentine chileno:Chilean peruano:Peruvian venezolano:Venezuelan cubano:Cuban espanol:Spanish frances:French
aleman:German ingles:English britanico:British italiano:Italian japones:Japanese chino:Chinese coreano:Korean
indio:Indian egipcio:Egyptian griego:Greek romano:Roman europeo:European africano:African nazi:Nazi
militar:military nuclear:nuclear atomico:atomic secreto:secret antiguo:ancient viejo:old abandonado:abandoned
vacio:empty oscuro:dark enorme:huge gigante:giant pequeno:small solitario:lonely falso:false mundial:world
medieval:medieval moderno:modern electrico:electric helado:frozen congelado:frozen roto:broken destruido:destroyed
quemado:burned hundido:sunken perdido:lost misterioso:mysterious extrano:strange rico:wealthy pobre:poor
blanco:white negro:black rojo:red azul:blue verde:green dorado:golden gris:gray amarillo:yellow
"""


def _tabla(bloque: str) -> dict:
    return {w: e.replace("_", " ") for w, e in (par.split(":", 1) for par in bloque.split())}


_PERSONAS_EN = _tabla(_PERSONAS)
_LUGARES_EN = _tabla(_LUGARES)
_ES_EN = {**_tabla(_COSAS), **_LUGARES_EN, **_PERSONAS_EN}
_ADJ_EN = _tabla(_ADJETIVOS)

_STOP_EN = set("""a an the and or but if of to in on at by for with from as is was were be been are it its this that these those
he she they we you i his her their our your him them there here then than so not no yes do did does done have has had
will would can could should may might must about into over after before when while where which who whom what why how
all any each every some more most other such only own same very just also again once one two three said says
near went came took gave made told until still even like then later never always nobody nothing someone
four five six seven eight nine ten hundred thousand million right knew know knows known saw seen thought found
felt kept became begun began heard held stood sent brought bought caught taught wrote written spoke spoken
got gone left meant
almost nearly perhaps maybe today tonight yesterday tomorrow""".split())

# Para detectar el idioma de lo pegado: las palabras más frecuentes de cada uno.
_FREC = {
    "es": set("el la los las de del que y en un una por con para se su sus no es al lo como pero fue era muy ya cuando".split()),
    "en": set("the and of to in a was he she it that with for on his her they were is at as but had when this".split()),
}


def idioma_de(texto: str) -> str | None:
    """'es' o 'en' según las palabras más frecuentes; None si no está claro."""
    palabras = re.findall(r"[a-záéíóúñü]+", texto.lower())
    es, en = (sum(w in _FREC[k] for w in palabras) for k in ("es", "en"))
    if en > es * 1.5 and en >= 3:
        return "en"
    if es > en * 1.5 and es >= 3:
        return "es"
    return None


def _decada(texto: str) -> str | None:
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", texto)
    return f"{m.group(1)[:3]}0s" if m else None


def _buscar(tabla: dict, w: str) -> str | None:
    """La palabra en la tabla, también en plural o femenino: misiles, soviética, nucleares, luces, alemana."""
    for cand in (w, w[:-1] if w.endswith("s") else None, w[:-2] if w.endswith("es") else None,
                 w[:-1] + "o" if w.endswith("a") else None, w[:-2] + "o" if w.endswith("as") else None,
                 w[:-3] + "z" if w.endswith("ces") else None):
        if cand and cand in tabla:
            return tabla[cand]
    return None


def _claves_es(frase: str) -> list[tuple[str, str]]:
    """[(inglés, tipo)] de una frase en español, en orden; tipo: persona | lugar | cosa. El adjetivo se junta con
    el sustantivo de al lado (antes o después)."""
    claves: list[list[str]] = []  # [inglés, tipo, índice del token]
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", frase)
    pendiente = None  # (adjetivo, índice) que espera al sustantivo siguiente: "falsa alarma"
    for i, tok in enumerate(tokens):
        if i > 0 and tok[0].isupper():
            continue  # nombre propio a mitad de frase: los prompts no llevan nombres de personas reales
        w = _sin_tildes(tok.lower())
        adj = _buscar(_ADJ_EN, w)
        if adj and claves and claves[-1][2] == i - 1 and adj not in claves[-1][0]:
            claves[-1][0] = f"{adj} {claves[-1][0]}"  # "oficial soviético"
            claves[-1][2] = i
            continue
        en = _buscar(_ES_EN, w)
        if en:
            tipo = "persona" if en in _PERSONAS_EN.values() else "lugar" if en in _LUGARES_EN.values() else "cosa"
            if pendiente and pendiente[1] == i - 1 and pendiente[0] not in en:
                en = f"{pendiente[0]} {en}"
            pendiente = None
            if all(en != c[0] for c in claves):
                claves.append([en, tipo, i])
        elif adj:
            pendiente = (adj, i)
    return [(c[0], c[1]) for c in claves]


def contexto(texto: str, idioma: str = "es") -> dict:
    """Lo que da continuidad a todas las escenas de una historia: el protagonista (la primera persona que
    aparece), el lugar principal y la década (el primer año). Para las escenas que solas no dan imagen."""
    ctx = {"persona": None, "lugar": None, "decada": _decada(texto)}
    if idioma == "es":
        for en, tipo in _claves_es(texto):
            if tipo in ("persona", "lugar") and not ctx[tipo]:
                ctx[tipo] = en
    return ctx


def _articulo(s: str) -> str:
    return f"{'an' if s[0].lower() in 'aeiou' else 'a'} {s}"


def prompt_sugerido(frase: str, idioma: str = "es", ctx: dict | None = None) -> str:
    """Un prompt en inglés para empezar: los sustantivos clave de la frase (con sus adjetivos) y la década.
    Si la frase da 2 claves o menos ("Así que reportó una falsa alarma."), una plantilla de escena con el
    protagonista y el lugar de la historia (ctx, de contexto()): "a Soviet officer in a bunker, false alarm, ..."."""
    ctx = ctx or {}
    if idioma == "en":  # sin diccionario: palabras con contenido, sin verbos ni adverbios evidentes
        claves = []
        for i, tok in enumerate(re.findall(r"[A-Za-z]+", frase)):
            w = tok.lower()
            if (i == 0 or not tok[0].isupper()) and len(w) > 3 and w not in _STOP_EN \
                    and not w.endswith(("ed", "ing", "ly")) and w not in claves:
                claves.append(w)
        claves = claves[:5]
        tipos = {}
    else:
        pares = _claves_es(frase)[:5]
        claves, tipos = [c for c, _ in pares], dict(pares)
    decada = _decada(frase) or ctx.get("decada")
    if len(claves) <= 2:
        persona = next((c for c in claves if tipos.get(c) == "persona"), None) or ctx.get("persona")
        lugar = next((c for c in claves if tipos.get(c) == "lugar"), None) or ctx.get("lugar")
        sujeto = _articulo(persona) if persona else "a lone figure"
        if lugar:
            sujeto += f" in {_articulo(lugar)}"
        resto = [c for c in claves if c not in (persona, lugar)]
        partes = [sujeto, *resto, "tense quiet moment", "medium shot", "documentary scene"]
    else:
        partes = claves
    if decada:
        partes.append(decada)
    return ", ".join(partes)


def guion(texto: str, base: dict) -> dict:
    """{titulo, escenas: [{texto, prompt, prompt_sugerido}]} a partir de la narración pegada. Si el texto está
    en otro idioma que el del video (columna Idea), el guion queda en el idioma del texto, con su voz."""
    from pipeline.guion import MOTORES, VOCES_DEFECTO, motor_de

    del_video = base.get("idioma") if base.get("idioma") in ("es", "en") else "es"
    idioma = idioma_de(texto) or del_video
    partes = escenas(texto)
    titulo = (base.get("titulo") or base.get("tema") or "").strip()
    if not titulo and partes:
        primeras = partes[0].split()[:10]
        titulo = " ".join(primeras).rstrip(".,;:!?…") + ("…" if len(partes[0].split()) > 10 else "")
    ctx = contexto(texto, idioma)
    data = {"titulo": titulo or "Video",
            "escenas": [{"texto": t, "prompt": prompt_sugerido(t, idioma, ctx), "prompt_sugerido": True} for t in partes]}
    if idioma != del_video:
        motor = base.get("motor_voz") if base.get("motor_voz") in MOTORES else motor_de(base.get("voz", ""))
        data.update(idioma=idioma, motor_voz=motor, voz=VOCES_DEFECTO[motor][idioma])
    return data


def parece_json(texto: str) -> bool:
    """Lo pegado es un guion en JSON (o un bloque ```json de un chat), no una narración."""
    t = texto.strip()
    return t.startswith("{") or t.startswith("[") or "```" in t
