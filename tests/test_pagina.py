"""La página de la demo (demo/pagina/) frente al contrato de la Lambda. Sin red y sin navegador.

Lo que la página le dice al visitante sale de sus propios textos por código de
error (cotejo.js, mensaje()), no del «mensaje» del servidor. Si la Lambda o
cuota.py ganan un código o un tope que la página no conoce, el visitante ve
«respuesta inesperada», o peor, un aviso que afirma algo falso: el de
limite_global decía «tope de 100 consultas» cuando lo que se había agotado era
el presupuesto de peticiones, con 34 consultas hechas (reproducido).
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAGINA = RAIZ / "demo" / "pagina"


def _cargar_vista():
    # Por ruta y con otro nombre: demo/pagina no es un paquete, y «vista» a secas
    # podría chocar con otro módulo en sys.path
    spec = importlib.util.spec_from_file_location("vista_de_la_pagina", PAGINA / "vista.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


vista = _cargar_vista()
JS = (PAGINA / "cotejo.js").read_text(encoding="utf-8")


class _Peticion:
    """Lo que _simular lee de http.server.BaseHTTPRequestHandler, y lo que responde."""

    def __init__(self, texto: str):
        cuerpo = json.dumps({"texto": texto}).encode("utf-8")
        self.headers = {"Content-Length": str(len(cuerpo))}
        self.rfile = io.BytesIO(cuerpo)
        self.respuesta: tuple | None = None
        self.close_connection = False

    def _json(self, codigo, datos, cabeceras=None):
        self.respuesta = (codigo, datos, cabeceras or {})


def _simular(texto: str) -> tuple:
    p = _Peticion(texto)
    vista._simular(p)
    return p.respuesta


def _codigos_de_la_lambda() -> set[str]:
    fuente = (RAIZ / "demo" / "lambda_function.py").read_text(encoding="utf-8")
    codigos = set(re.findall(r'_Rechazo\(\d+, "(\w+)"', fuente))
    codigos |= set(re.findall(r'_error\(\d+, "(\w+)"', fuente))
    codigos |= set(re.findall(r'\{"error": "(\w+)"\}', fuente))
    return codigos


def _topes_globales() -> set[str]:
    """Los valores de LimiteGlobal.de en cuota.py, que son los de "limite" en el 429 (DISEÑO.md §2)."""
    fuente = (RAIZ / "demo" / "cuota.py").read_text(encoding="utf-8")
    return set(re.findall(r'\bde(?:: str)? ?= ?"(\w+)"', fuente))


class TestApiSimulada(unittest.TestCase):
    def test_limite_global_en_sus_tres_formas(self):
        for orden, limite in (("!limite_global", None), ("!limite_global:consultas", "consultas"),
                              ("!limite_global:peticiones", "peticiones")):
            with self.subTest(orden):
                codigo, cuerpo, cabeceras = _simular(orden)
                self.assertEqual(codigo, 429)
                self.assertEqual(cuerpo["error"], "limite_global")
                self.assertEqual(cuerpo.get("limite"), limite)
                # Como la Lambda: hasta la medianoche de Colombia, nunca más de un día
                self.assertTrue(0 < int(cabeceras["Retry-After"]) <= 86400)

    def test_cada_tope_de_cuota_tiene_su_caso_simulado(self):
        topes = _topes_globales()
        self.assertEqual(topes, {"consultas", "peticiones"})
        for tope in topes:
            self.assertIn(f"!limite_global:{tope}", vista._ERRORES_SIMULADOS)

    def test_ocupado_no_trae_nada_nuestro(self):
        # La página lo lee como «ocupado» (y dice que no se gastó nada) solo si el cuerpo no trae "error"
        codigo, cuerpo, cabeceras = _simular("!ocupado")
        self.assertEqual(codigo, 429)
        self.assertNotIn("error", cuerpo)
        self.assertNotIn("Retry-After", cabeceras)


class TestLaPaginaConoceElContrato(unittest.TestCase):
    def test_cada_codigo_de_la_lambda_tiene_su_mensaje(self):
        lista = re.search(r"var CODIGOS = \[([^\]]*)\]", JS)
        self.assertIsNotNone(lista, "cotejo.js ya no define CODIGOS")
        conocidos = set(re.findall(r"'(\w+)'", lista.group(1)))
        codigos = _codigos_de_la_lambda()
        self.assertGreaterEqual(codigos, {"mal_formada", "texto_vacio", "texto_largo", "demasiadas_citas",
                                          "limite_ip", "limite_global", "interno", "ruta"})
        for codigo in codigos:
            with self.subTest(codigo):
                self.assertIn(codigo, conocidos)
                self.assertIn(f"case '{codigo}':", JS)

    def test_cada_tope_global_tiene_su_redaccion(self):
        cuerpo = re.search(r"function limiteGlobal\(cual\) \{(.*?)\n  \}", JS, re.S)
        self.assertIsNotNone(cuerpo, "cotejo.js ya no tiene limiteGlobal(cual)")
        for tope in _topes_globales():
            with self.subTest(tope):
                self.assertIn(f"cual === '{tope}'", cuerpo.group(1))

    def test_ningun_aviso_de_limite_global_promete_un_numero_de_consultas(self):
        cuerpo = re.search(r"function limiteGlobal\(cual\) \{(.*?)\n  \}", JS, re.S).group(1)
        self.assertNotRegex(cuerpo, r"\b100\b")

    def test_sin_bytes_de_control_ni_crlf(self):
        for nombre in ("cotejo.html", "cotejo.css", "cotejo.js", "vista.py"):
            with self.subTest(nombre):
                datos = (PAGINA / nombre).read_bytes()
                self.assertNotIn(b"\r\n", datos)
                self.assertEqual([b for b in set(datos) if b < 0x20 and b not in (0x09, 0x0A)], [])


if __name__ == "__main__":
    unittest.main()
