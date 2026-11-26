"""Arranque del estudio audiovisual en Linux."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main():
    if not sys.platform.startswith("linux"):
        sys.exit("Este lanzador requiere Linux.")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if os.environ.get("COMPRESSO_LOCAL_DEMO") == "1" and not os.environ.get("COMPRESSO_DATA_DIR"):
        raiz = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        os.environ["COMPRESSO_DATA_DIR"] = str(raiz / "compresso-studio-demo")
    os.environ["COMPRESSO_LINUX"] = "1"
    os.environ["COMPRESSO_IMAGE_BACKEND"] = "vulkan"
    from app.server import main as servir

    servir()


if __name__ == "__main__":
    main()
