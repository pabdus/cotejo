"""Interfaz web local de Cotejo.

Arranque:  python servidor.py      ->  http://127.0.0.1:8010

Todo corre en la máquina del abogado: el escrito no sale de ella. Lo único que
se consulta fuera son las fuentes oficiales —el índice de datos.gov.co y la
relatoría de la Corte—, y solo con el número de la sentencia, nunca con el
texto del escrito. Para un despacho eso importa: un escrito antes de
radicarse es información confidencial del cliente.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import documentos, informe
from .fuentes import corte_constitucional as cc
from .verificacion import verificar

ESTATICOS = Path(__file__).parent / "static"

app = FastAPI(title="Cotejo", version="0.1.0")
_indice: cc.Indice | None = None


def indice() -> cc.Indice:
    global _indice
    if _indice is None:
        _indice = cc.Indice.cargar()
    return _indice


@app.get("/")
def inicio() -> FileResponse:
    return FileResponse(ESTATICOS / "index.html")


@app.get("/api/estado")
def estado() -> dict:
    ind = indice()
    return {"providencias": len(ind), "actualizado": ind.actualizado}


@app.post("/api/verificar")
def api_verificar(fichero: UploadFile | None = File(None), texto: str = Form("")) -> JSONResponse:
    """Recibe un fichero o un texto pegado y devuelve el resultado completo.

    Es `def` y no `async def` a propósito: la verificación hace peticiones de
    red bloqueantes y así va al threadpool sin congelar el servidor.
    """
    if fichero is not None and fichero.filename:
        datos = fichero.file.read()
        try:
            doc = documentos.importar(fichero.filename, datos)
        except documentos.ErrorDocumento as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        contenido, nombre = doc["texto"], fichero.filename
    elif texto.strip():
        contenido, nombre = texto, "Texto pegado"
    else:
        raise HTTPException(status_code=400, detail="Sube un escrito o pega el texto.")

    if len(contenido) > 2_000_000:
        raise HTTPException(status_code=413, detail="El escrito es demasiado largo.")

    inf = verificar(contenido, documento=nombre, indice=indice())
    resultado = informe.a_json(inf)
    resultado["html"] = informe.a_html(inf)
    return JSONResponse(resultado)
