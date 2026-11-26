"""Adaptador independiente para Lemonade /api/v1/images/generations."""
import base64
import io
import json
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

MODELS = {
    'stabilityai/sdxl-turbo': 'SDXL-Turbo',
    'stabilityai/stable-diffusion-xl-base-1.0': 'SDXL-Base-1.0',
}

class BackendError(RuntimeError):
    pass


def generar_imagen(prompt, modelo='SDXL-Turbo', size=(512, 512), steps=4,
                   cfg_scale=0.4, seed=None, negative_prompt=None,
                   endpoint=None, timeout=240):
    """Devuelve PIL.Image; no carga torch ni cambia configuración de Lemonade."""
    from PIL import Image
    model_id = MODELS.get(modelo, modelo)
    if model_id not in MODELS.values():
        raise BackendError(f'Modelo no habilitado: {modelo}')
    w, h = map(int, size)
    if w <= 0 or h <= 0 or w % 64 or h % 64:
        raise ValueError('La resolución debe ser positiva y múltiplo de 64')
    payload = {'model': model_id, 'prompt': prompt, 'size': f'{w}x{h}',
               'n': 1, 'steps': int(steps), 'cfg_scale': float(cfg_scale),
               'response_format': 'b64_json'}
    if seed is not None:
        payload['seed'] = int(seed)
    if negative_prompt:
        payload['negative_prompt'] = negative_prompt
    base = (endpoint or os.getenv('COMPRESSO_LEMONADE_URL') or 'http://127.0.0.1:13305').rstrip('/')
    if not (base.startswith('http://127.0.0.1:') or base.startswith('http://localhost:')):
        raise BackendError('Solo se permiten endpoints locales HTTP')
    req = Request(base + '/api/v1/images/generations',
                  data=json.dumps(payload).encode('utf-8'),
                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BackendError(f'Fallo en Lemonade: {exc}') from exc
    if 'error' in result:
        raise BackendError(f'Lemonade: {result["error"]}')
    try:
        raw = base64.b64decode(result['data'][0]['b64_json'], validate=True)
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert('RGB')
    except (KeyError, IndexError, ValueError, OSError) as exc:
        raise BackendError('Respuesta de imagen inválida') from exc
