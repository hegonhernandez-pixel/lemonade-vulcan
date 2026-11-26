import base64
import io
import json
import unittest
from unittest.mock import patch
from PIL import Image
import compresso_vulkan as backend

class TestBackend(unittest.TestCase):
    def test_generacion_y_parametros(self):
        img = Image.new('RGB', (64, 64))
        buf = io.BytesIO(); img.save(buf, format='PNG')
        result = {'data': [{'b64_json': base64.b64encode(buf.getvalue()).decode()}]}
        class Response:
            def __enter__(self): return io.BytesIO(json.dumps(result).encode())
            def __exit__(self, *args): pass
        with patch.object(backend, 'urlopen', return_value=Response()) as mock:
            output = backend.generar_imagen('ciudad', size=(512, 512), seed=42)
            payload = json.loads(mock.call_args.args[0].data)
        self.assertEqual(output.size, (64, 64))
        self.assertEqual(payload['model'], 'SDXL-Turbo')
        self.assertEqual(payload['seed'], 42)
        self.assertEqual(payload['size'], '512x512')

    def test_modelo_no_autorizado(self):
        with self.assertRaises(backend.BackendError):
            backend.generar_imagen('test', modelo='desconocido')

    def test_resolucion_invalida(self):
        with self.assertRaises(ValueError):
            backend.generar_imagen('test', size=(513, 512))

    def test_error_api(self):
        class Response:
            def __enter__(self): return io.BytesIO(b'{"error":"modelo no disponible"}')
            def __exit__(self, *args): pass
        with patch.object(backend, 'urlopen', return_value=Response()):
            with self.assertRaises(backend.BackendError):
                backend.generar_imagen('test')

if __name__ == '__main__': unittest.main()
