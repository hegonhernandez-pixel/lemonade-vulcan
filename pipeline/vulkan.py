"""Stable Diffusion local mediante sd-cli nativo de Linux."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

PERFILES = {
    "rapida": ("SD Turbo GGUF", "SD_TURBO_GGUF", 4, 1),
    "sd_turbo": ("SD Turbo", "SD_TURBO", 4, 1),
    "sdxl_turbo": ("SDXL Turbo", "SDXL_TURBO", 4, 1),
    "alta": ("SDXL Base 1.0", "SDXL_BASE", 25, 7),
}


def activo():
    return os.environ.get("COMPRESSO_IMAGE_BACKEND") == "vulkan"


def binario():
    ruta = shutil.which(os.path.expanduser(os.environ.get("COMPRESSO_SD_CLI") or "sd-cli"))
    return str(Path(ruta).resolve()) if ruta else None


def dispositivo():
    nombre = os.environ.get("COMPRESSO_VULKAN_DEVICE", "vulkan0")
    if not re.fullmatch(r"vulkan[0-9]+", nombre):
        raise ValueError("COMPRESSO_VULKAN_DEVICE debe identificar un dispositivo Vulkan, como vulkan0.")
    return nombre


def ruta_modelo(perfil):
    valor = os.environ.get("COMPRESSO_" + PERFILES[perfil][1], "")
    return Path(valor).expanduser().resolve() if valor else None


def estado():
    ejecutable = binario()
    salida = ""
    if ejecutable:
        try:
            salida = subprocess.run([ejecutable, "--list-devices"], capture_output=True, text=True,
                                    timeout=6, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            pass
    elegido = dispositivo()
    linea = next((l.strip() for l in salida.splitlines() if re.search(rf"\b{elegido}\b", l, re.I)), "")
    return {"motor": "Stable Diffusion.cpp · Vulkan", "dispositivo": linea or None,
            "binario": bool(ejecutable),
            "modelos": {k: {"nombre": v[0], "configurado": bool(p and p.is_file())}
                        for k, v in PERFILES.items() for p in [ruta_modelo(k)]}}


def generar(prompt, perfil, size, seed, *, negativo="", pasos=None, foto=None,
            intensidad=0.5, referencias=(), fuerza=0.5):
    exe, modelo = binario(), ruta_modelo(perfil)
    if not exe or not modelo or not modelo.is_file():
        raise RuntimeError(f"Configura sd-cli y COMPRESSO_{PERFILES[perfil][1]} en el entorno del servidor.")
    _, _, por_defecto, cfg = PERFILES[perfil]
    w, h = size
    if perfil in ("rapida", "sd_turbo"):
        escala = min(1, 768 / max(w, h))
        w, h = (max(64, round(n * escala / 64) * 64) for n in (w, h))
    if "<image:" in prompt.lower():
        raise ValueError("Sube las imágenes de referencia mediante el formulario.")
    with TemporaryDirectory(prefix="compresso-sd-") as directorio:
        tmp = Path(directorio)
        salida = tmp / "imagen.png"
        cmd = [exe, "--backend", dispositivo(), "-m", str(modelo), "-p", prompt,
               "-W", str(w), "-H", str(h), "--seed", str(seed), "--steps", str(pasos or por_defecto),
               "--cfg-scale", str(cfg), "--sampling-method", "euler", "-o", str(salida)]
        if cfg == 1:
            cmd += ["--scheduler", "discrete"]
        if cfg > 1 and negativo:
            cmd += ["-n", negativo]
        if foto is not None:
            entrada = tmp / "entrada.png"
            foto.resize((w, h), Image.Resampling.LANCZOS).save(entrada)
            cmd += ["--init-img", str(entrada), "--strength", str(intensidad)]
        if referencias:
            ruta = tmp / "referencia.png"
            referencias[seed % len(referencias)].save(ruta)
            flujo = tmp / "personaje.dgl"
            flujo.write_text(guion_personaje(modelo, ruta, salida, prompt, negativo, seed, w, h,
                                             pasos or por_defecto, cfg, fuerza), encoding="utf-8")
            cmd = [exe, "-M", "dgl", "--script", str(flujo)]
        try:
            subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, check=True, timeout=600)
            with Image.open(salida) as imagen:
                return imagen.convert("RGB").resize(size, Image.Resampling.LANCZOS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Vulkan no generó la imagen con {PERFILES[perfil][0]} en {dispositivo()}.") from exc

def guion_personaje(modelo, referencia, salida, prompt, negativo, seed, width, height, steps, cfg, fuerza):
    adapter = os.path.expanduser(os.environ.get("COMPRESSO_IP_ADAPTER", ""))
    encoder = os.path.expanduser(os.environ.get("COMPRESSO_CLIP_VISION", ""))
    if not all(p and Path(p).is_file() for p in (adapter, encoder)):
        raise RuntimeError("Configura COMPRESSO_IP_ADAPTER y COMPRESSO_CLIP_VISION para usar personajes.")
    valores = {
        "weights": str(modelo), "vision_weights": str(Path(encoder).resolve()),
        "adapter_weights": str(Path(adapter).resolve()), "input_path": str(referencia),
        "output_path": str(salida), "prompt": prompt, "negative_prompt": negativo, "device": dispositivo(),
        "seed": seed, "width": width, "height": height, "steps": steps, "cfg_scale": cfg, "strength": fuerza,
    }
    encabezado = "\n".join(f"    {k} = {json.dumps(v, ensure_ascii=False)}" for k, v in valores.items())
    cuerpo = Path(__file__).with_name("sdxl_personaje.dgl").read_text(encoding="utf-8")
    return f"# version: 0.0.0\nvar {{\n{encabezado}\n}}\n\n{cuerpo}"
