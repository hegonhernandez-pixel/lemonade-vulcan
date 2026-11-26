import tempfile
import unittest
from pathlib import Path
from aplicar_parche import aplicar

class TestParche(unittest.TestCase):
    def test_aplicar_sobre_fuente_local(self):
        original = Path(__file__).with_name('imagenes_original_para_prueba.py')
        if not original.exists():
            self.skipTest('Copiar imagenes.py original aquí para prueba de compatibilidad')
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'pipeline' / 'imagenes.py'
            dest.parent.mkdir()
            dest.write_bytes(original.read_bytes())
            aplicar(Path(tmp))
            text = dest.read_text()
            self.assertIn('COMPRESSO_VULKAN_V2', text)
            self.assertIn('lemonade-v2|', text)
            self.assertIn('cfg = 0.4 if', text)
            self.assertEqual(dest.with_suffix('.py.bak').read_bytes(), original.read_bytes())
            with self.assertRaises(ValueError):
                aplicar(Path(tmp))
    def test_rechaza_codigo_desconocido(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'pipeline' / 'imagenes.py'
            dest.parent.mkdir()
            dest.write_text('def generar_escena(): pass\n')
            with self.assertRaises(ValueError):
                aplicar(Path(tmp))
            self.assertFalse(dest.with_suffix('.py.bak').exists())
