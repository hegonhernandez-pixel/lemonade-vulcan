"""Parche opt-in para generar_escena; conserva CUDA y separa caché."""
import argparse
import ast
import shutil
from pathlib import Path

MARK = '# COMPRESSO_VULKAN_V2'


def aplicar(raiz: Path) -> Path:
    p = raiz / 'pipeline' / 'imagenes.py'
    original = p.read_text(encoding='utf-8')
    if MARK in original or 'COMPRESSO_IMAGE_BACKEND' in original:
        raise ValueError('Ya existe un parche; no se modificó nada')
    tree = ast.parse(original)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'generar_escena']
    if len(funcs) != 1:
        raise ValueError('Función generar_escena no encontrada')
    node = funcs[0]
    lines = original.splitlines(keepends=True)
    old = ''.join(lines[node.lineno-1:node.end_lineno])
    changes = [
        ('    carpeta.mkdir(parents=True, exist_ok=True)', '''    # COMPRESSO_VULKAN_V2
    import os
    usar_vulkan = os.getenv("COMPRESSO_IMAGE_BACKEND") == "lemonade"
    carpeta.mkdir(parents=True, exist_ok=True)'''),
        ('''    ruta = carpeta / f"img_{e.idx:02d}_{hp}.jpg"
    previa = buscar(g, e, carpeta, demo, calidad)''', '''    if usar_vulkan and not demo:
        import hashlib
        q_cache = CALIDADES[calidad_de(e, calidad)]
        modelo_cache = modelo or q_cache.modelo
        pasos_cache = pasos or q_cache.pasos
        cfg_cache = 0.4 if modelo_cache in ("stabilityai/sdxl-turbo", "SDXL-Turbo") else q_cache.guidance
        huella = hashlib.sha256(f"lemonade-v2|{hp}|{modelo_cache}|{pasos_cache}|{cfg_cache}".encode()).hexdigest()[:8]
        ruta = carpeta / f"img_{e.idx:02d}_{huella}.jpg"
        previa = ruta if ruta.exists() else next(iter(carpeta.glob(f"img_*_{huella}.jpg")), None)
    else:
        ruta = carpeta / f"img_{e.idx:02d}_{hp}.jpg"
        previa = buscar(g, e, carpeta, demo, calidad)'''),
        ('''    import torch

    q = CALIDADES[calidad_de(e, calidad)]''', '''    if not usar_vulkan:
        import torch

    q = CALIDADES[calidad_de(e, calidad)]'''),
        ('''    pipe = _pipe_para(q, r.personaje)
    refs = _referencias(r.personaje) if r.personaje else None''', '''    if usar_vulkan and r.personaje:
        raise RuntimeError("IP-Adapter aún no está soportado por Vulkan")
    pipe = None if usar_vulkan else _pipe_para(q, r.personaje)
    refs = _referencias(r.personaje) if r.personaje and not usar_vulkan else None'''),
        ('''    try:
        img = _difundir(pipe, prompt, q, size, seed, refs)''', '''    if usar_vulkan:
        from compresso_vulkan import generar_imagen
        cfg = 0.4 if q.modelo in ("stabilityai/sdxl-turbo", "SDXL-Turbo") else q.guidance
        img = generar_imagen(prompt, modelo=q.modelo, size=size,
                            steps=q.pasos, cfg_scale=cfg, seed=seed,
                            negative_prompt=NEGATIVO)
        img.save(ruta, quality=94)
        return ruta, False
    try:
        img = _difundir(pipe, prompt, q, size, seed, refs)''')
    ]
    for before, after in changes:
        if old.count(before) != 1:
            raise ValueError('Versión incompatible; no se modificó nada: ' + before[:50])
        old = old.replace(before, after, 1)
    updated = ''.join(lines[:node.lineno-1]) + old + ''.join(lines[node.end_lineno:])
    ast.parse(updated)
    backup = p.with_suffix('.py.bak')
    if backup.exists():
        raise ValueError('Ya existe copia .bak; no se sobrescribirá')
    shutil.copy2(p, backup)
    try:
        p.write_text(updated, encoding='utf-8')
    except Exception:
        shutil.copy2(backup, p)
        raise
    return p


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directorio', type=Path)
    args = parser.parse_args()
    print('Parche aplicado:', aplicar(args.directorio))
