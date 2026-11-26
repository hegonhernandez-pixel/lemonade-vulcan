"""Personajes: la misma cara en varias escenas con IP-Adapter (SDXL).

Cada personaje vive en assets/personajes/<nombre>/:
  personaje.json   {"descripcion": "...", "fuerza": 0.65}
  *.jpg / *.png    1 a 4 fotos de referencia (las que sube la app se llaman ref_<hash>.jpg)
  prueba.jpg       la tira de prueba (de frente, de perfil, cuerpo entero), si se generó

En el prompt de una escena, "@nombre" llama al personaje: se reemplaza por su descripción y la imagen se
genera con sus fotos como referencia. Solo el primer @ de la escena usa las fotos; los demás se describen
con texto (y se avisa).
"""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .textos import Texto, error
from . import rutas

RAIZ = Path(__file__).parent.parent
CARPETA = rutas.PERSONAJES
NOMBRE_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MENCION = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_-]{0,31})")
FUERZA_MIN, FUERZA_MAX, FUERZA_DEFECTO = 0.4, 0.9, 0.65
MAX_FOTOS = 4
RECORTE = "cara-v1"  # cómo se preparan las fotos para el adapter (va en la huella: si cambia, se regenera)
EXT_FOTOS = (".jpg", ".jpeg", ".png", ".webp")
# IP-Adapter "plus-face" (ViT-H): el que mejor conserva la cara. El general (ip-adapter_sdxl_vit-h) copia
# también ropa, fondo y encuadre de las fotos, y la cara se parece menos.
ADAPTER = {"repo": "h94/IP-Adapter", "subfolder": "sdxl_models",
           "weight_name": "ip-adapter-plus-face_sdxl_vit-h.safetensors", "image_encoder_folder": "models/image_encoder"}


@dataclass(frozen=True)
class Personaje:
    nombre: str
    descripcion: str
    fuerza: float
    fotos: tuple[Path, ...]
    modalidad: str = "completo"

    @property
    def carpeta(self) -> Path:
        return CARPETA / self.nombre

    def huella(self) -> str:
        """Cambia si cambian la descripción, la fuerza o el contenido de cualquier foto: va en el hash de
        las imágenes, así una escena se regenera al cambiar una referencia."""
        h = hashlib.md5(f"{self.descripcion}|{self.fuerza:.2f}|{RECORTE}".encode())
        for f in self.fotos:
            h.update(f.read_bytes())
        return h.hexdigest()[:8]


def recorte_cara(img):
    """La foto recortada a un cuadrado alrededor de la cara más grande (con pelo y gafas).

    plus-face está entrenado con caras recortadas: con la foto entera, el codificador (que recorta el
    centro a 224x224) se lleva también ropa y fondo, y esos se cuelan en cada escena (medido: la chaqueta y
    el fondo verde de las referencias aparecían en una escena de acantilado al atardecer). Detector Haar de
    OpenCV: basta para fotos de frente. Sin OpenCV o sin cara detectada, la foto entera."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return img
    gris = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    lado_min = max(40, min(img.size) // 10)
    caras = detector.detectMultiScale(gris, scaleFactor=1.1, minNeighbors=5, minSize=(lado_min, lado_min))
    if len(caras) == 0:
        return img
    x, y, w, h = max(caras, key=lambda c: c[2] * c[3])
    lado = min(int(w * 1.9), *img.size)  # la cara con su pelo, sin hombros
    cx, cy = x + w / 2, y + h / 2 - 0.08 * lado  # un poco hacia arriba: frente y pelo
    izq = int(min(max(0, cx - lado / 2), img.width - lado))
    arr = int(min(max(0, cy - lado / 2), img.height - lado))
    return img.crop((izq, arr, izq + lado, arr + lado))


def nombre_valido(nombre: str) -> bool:
    return bool(NOMBRE_OK.match(nombre or ""))


def fotos_de(carpeta: Path) -> list[Path]:
    """Toda imagen de la carpeta es una referencia (también las que pongas a mano), salvo la tira de prueba."""
    return sorted(p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() in EXT_FOTOS
                  and p.name != "prueba.jpg" and not p.name.startswith("."))


def cargar(nombre: str) -> Personaje:
    nombre = nombre.lower()
    carpeta = CARPETA / nombre
    if not nombre_valido(nombre) or not carpeta.is_dir():
        raise error("err.personaje_no_existe", nombre=nombre)
    try:
        datos = json.loads((carpeta / "personaje.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        datos = {}
    fotos = fotos_de(carpeta)
    if not fotos:
        raise error("err.personaje_sin_fotos", nombre=nombre)
    fuerza = min(FUERZA_MAX, max(FUERZA_MIN, float(datos.get("fuerza") or FUERZA_DEFECTO)))
    modalidad = "foto" if datos.get("modalidad") == "foto" else "completo"
    return Personaje(nombre, (datos.get("descripcion") or "").strip(), round(fuerza, 2), tuple(fotos), modalidad)


def listar() -> list[Personaje]:
    res = []
    for d in sorted(CARPETA.iterdir()) if CARPETA.is_dir() else []:
        if d.is_dir() and nombre_valido(d.name):
            try:
                res.append(cargar(d.name))
            except ValueError:
                continue
    return res


def menciones(prompt: str) -> list[str]:
    """Los @nombre del prompt, en orden y sin repetir."""
    vistos = []
    for m in MENCION.findall(prompt or ""):
        if m.lower() not in vistos:
            vistos.append(m.lower())
    return vistos


@dataclass(frozen=True)
class Resuelto:
    prompt: str  # con cada @nombre reemplazado por su descripción
    personaje: Personaje | None  # el que pone la cara (el primero)
    ignorados: tuple[str, ...]  # los demás @: solo texto


def resolver(prompt: str) -> Resuelto:
    """Reemplaza los @nombre por su descripción. El primero pone la cara (IP-Adapter); si no existe,
    error. Los demás solo aportan su descripción (o su nombre sin @ si no existen)."""
    nombres = menciones(prompt)
    if not nombres:
        return Resuelto(prompt, None, ())
    principal = cargar(nombres[0])
    descripciones = {principal.nombre: principal.descripcion or principal.nombre}
    for n in nombres[1:]:
        try:
            p = cargar(n)
            descripciones[n] = p.descripcion or n
        except ValueError:
            descripciones[n] = n
    texto = MENCION.sub(lambda m: descripciones.get(m.group(1).lower(), m.group(1)), prompt)
    return Resuelto(re.sub(r"\s{2,}", " ", texto).strip(), principal, tuple(nombres[1:]))


def aviso_varios(n_escena: int, r: Resuelto) -> Texto | None:
    if not r.ignorados:
        return None
    return Texto("log.personaje_varios", n=n_escena, usado=r.personaje.nombre, otros=", ".join("@" + x for x in r.ignorados))
