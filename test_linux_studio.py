import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app import auth, credenciales
from pipeline import vulkan


class AuthTest(unittest.TestCase):
    def test_session_guards_media_and_validates_confirmed_owner(self):
        app = FastAPI()
        app.add_middleware(auth.Sesion)
        app.include_router(auth.router)

        @app.get("/")
        @app.get("/salida/video.mp4")
        def video():
            return {"ok": True}

        config = ("https://project.supabase.co", "publishable", {"owner@example.org"})
        owner = {"id": "owner", "email": "owner@example.org", "email_confirmed_at": "2026-11-26"}
        token = {"user": owner, "access_token": "access-token", "expires_in": 3600}
        with patch.object(auth, "activa", return_value=True), patch.object(auth, "configuracion", return_value=config):
            with TestClient(app, follow_redirects=False) as client:
                self.assertEqual(client.get("/").headers["location"], "/entrar")
                self.assertEqual(client.get("/salida/video.mp4").status_code, 401)
                with patch.object(auth, "supabase", return_value=token):
                    response = client.post("/entrar", data={"email": owner["email"], "password": "demo"})
                self.assertIn("HttpOnly", response.headers["set-cookie"])
                with patch.object(auth, "supabase", return_value=owner) as supabase:
                    self.assertEqual(client.get("/salida/video.mp4").status_code, 200)
                    supabase.assert_called_once_with(config, "user", token="access-token")
                with patch.object(auth, "supabase", return_value={**owner, "email": "other@example.org"}):
                    self.assertEqual(client.get("/salida/video.mp4").status_code, 401)


@unittest.skipIf(os.name == "nt", "Fernet se usa en Linux")
class CredentialTest(unittest.TestCase):
    def test_api_key_is_encrypted_and_recovered(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"COMPRESSO_CREDENTIAL_KEY": Fernet.generate_key().decode()}):
            path = Path(d) / "ajustes.json"
            credenciales.guardar(path, {"gemini_api_key": "local-test-value"}, ("gemini_api_key",))
            self.assertNotIn("local-test-value", path.read_text())
            self.assertEqual(credenciales.leer(path, ("gemini_api_key",))["gemini_api_key"], "local-test-value")


class VulkanTest(unittest.TestCase):
    def test_uses_configured_linux_device_and_returns_requested_format(self):
        with tempfile.TemporaryDirectory() as d:
            model = Path(d) / "sd-turbo.gguf"
            model.touch()
            env = {"COMPRESSO_SD_TURBO_GGUF": str(model), "COMPRESSO_VULKAN_DEVICE": "vulkan1"}

            def ejecutar(cmd, **kwargs):
                self.assertIn("vulkan1", cmd)
                self.assertIn(str(model), cmd)
                self.assertIn("--init-img", cmd)
                self.assertEqual(cmd[cmd.index("--cfg-scale") + 1], "1")
                Image.new("RGB", (448, 768)).save(cmd[cmd.index("-o") + 1])

            with patch.dict(os.environ, env), patch.object(vulkan, "binario", return_value="/usr/local/bin/sd-cli"):
                with patch.object(vulkan.subprocess, "run", side_effect=ejecutar):
                    result = vulkan.generar("Fog over the valley", "rapida", (640, 1152), 44,
                                           foto=Image.new("RGB", (640, 1152)))
                    self.assertEqual(result.size, (640, 1152))
