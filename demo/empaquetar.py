"""Arma build/lambda.zip y build/indice.json.gz para la demo (DISEÑO.md §2), sin Docker.

    .venv/Scripts/python demo/empaquetar.py

- lambda.zip: rapidfuzz como rueda de manylinux para Python 3.12 x86_64 (la
  Lambda es python3.12 en x86_64), el paquete cotejo/ sin la interfaz web y los
  tres módulos de la demo. Todo plano en la raíz del zip: el handler es
  lambda_function.lambda_handler.
- indice.json.gz: el índice de la Corte Constitucional, el de .cache si tiene
  menos de 7 días; si no, se descarga. Terraform lo sube solo la primera vez;
  después lo renueva la propia Lambda cada semana.
"""
from __future__ import annotations

import gzip
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from cotejo.fuentes import corte_constitucional as cc  # noqa: E402
from cotejo.fuentes import red  # noqa: E402

BUILD = RAIZ / "build"
ZIP = BUILD / "lambda.zip"
INDICE = BUILD / "indice.json.gz"
MODULOS_DEMO = ("lambda_function.py", "espejo.py", "cuota.py")
# La interfaz web local (fastapi) no va: en la Lambda no se usa y no está en el paquete
FUERA_DE_COTEJO = {"web.py", "static", "__pycache__"}
# Un índice con muchas menos filas es una descarga a medias o una caché rota.
# En septiembre de 2026 tenía 29.474.
MINIMO_PROVIDENCIAS = 25_000
LIMITE_LAMBDA = 250 * 1024 * 1024       # descomprimido


def _rapidfuzz() -> str:
    """La misma versión que en .venv: la que pasaron las pruebas."""
    try:
        return f"rapidfuzz=={importlib.metadata.version('rapidfuzz')}"
    except importlib.metadata.PackageNotFoundError:
        return "rapidfuzz>=3.0"


def _dependencias(destino: Path) -> str:
    requisito = _rapidfuzz()
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(destino),
                    "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
                    "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:",
                    requisito], check=True)
    for basura in list(destino.rglob("__pycache__")) + list(destino.rglob("tests")):
        shutil.rmtree(basura, ignore_errors=True)
    return requisito


def _copiar_codigo(destino: Path) -> None:
    origen = RAIZ / "cotejo"
    for f in sorted(origen.rglob("*")):
        rel = f.relative_to(origen)
        if f.is_dir() or any(p in FUERA_DE_COTEJO for p in rel.parts):
            continue
        (destino / "cotejo" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, destino / "cotejo" / rel)
    for nombre in MODULOS_DEMO:
        shutil.copyfile(RAIZ / "demo" / nombre, destino / nombre)


def _zip(paquete: Path) -> int:
    """Zip reproducible: mismo contenido, mismos bytes.

    Terraform redespliega la función cuando cambia el hash del zip
    (source_code_hash). Con las fechas de cada archivo dentro, empaquetar dos
    veces lo mismo daba un zip distinto y un despliegue que no cambiaba nada.
    """
    BUILD.mkdir(exist_ok=True)
    tam = 0
    nuevo = ZIP.with_suffix(".zip.nuevo")
    with zipfile.ZipFile(nuevo, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(p for p in paquete.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f.relative_to(paquete).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            datos = f.read_bytes()
            tam += len(datos)
            z.writestr(info, datos, compresslevel=9)
    nuevo.replace(ZIP)
    return tam


def _indice() -> tuple[int, str | None, str]:
    """Copia el índice de .cache si está fresco; si no, lo descarga (y queda en .cache)."""
    cache = red.dir_cache() / "corte_constitucional_indice.json.gz"
    if cache.exists() and time.time() - cache.stat().st_mtime < cc.CADUCIDAD_INDICE:
        origen = f"de {cache} ({(time.time() - cache.stat().st_mtime) / 3600:.0f} h)"
    else:
        cc.Indice.cargar(forzar_descarga=True)
        origen = "descargado de datos.gov.co"
    with gzip.open(cache, "rt", encoding="utf-8") as f:
        indice = cc.Indice(json.load(f))
    if len(indice) < MINIMO_PROVIDENCIAS:
        sys.exit(f"El índice {origen} solo tiene {len(indice)} providencias: no se empaqueta.")
    BUILD.mkdir(exist_ok=True)
    shutil.copyfile(cache, INDICE)
    return len(indice), indice.fecha_mas_reciente, origen


def main() -> None:
    # El paquete se arma fuera del repositorio: con las dependencias dentro de
    # build/, la prueba que lee cada .py del proyecto en binario recorrería
    # también los de rapidfuzz
    with tempfile.TemporaryDirectory(prefix="cotejo-lambda-") as tmp:
        paquete = Path(tmp)
        requisito = _dependencias(paquete)
        _copiar_codigo(paquete)
        tam = _zip(paquete)
    if tam > LIMITE_LAMBDA:
        sys.exit(f"{ZIP}: {tam / 1e6:.1f} MB descomprimido, más que los 250 MB de Lambda")
    n, hasta, origen = _indice()
    print(f"{ZIP}: {ZIP.stat().st_size / 1e6:.2f} MB comprimido, {tam / 1e6:.2f} MB descomprimido "
          f"({requisito}, límite de Lambda 250 MB)")
    print(f"{INDICE}: {INDICE.stat().st_size / 1e6:.2f} MB, {n} providencias hasta {hasta}, {origen}")


if __name__ == "__main__":
    main()
