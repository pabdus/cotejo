"""Arranca la interfaz web de Cotejo en http://127.0.0.1:8010"""
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn   # noqa: E402

from cotejo.web import app   # noqa: E402

if __name__ == "__main__":
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else 8010
    if "--sin-navegador" not in sys.argv:
        try:
            webbrowser.open(f"http://127.0.0.1:{puerto}")
        except Exception:
            pass
    uvicorn.run(app, host="127.0.0.1", port=puerto, log_level="warning")
