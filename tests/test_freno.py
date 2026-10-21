"""Pruebas del freno automático de la demo (infra/freno/handler.py, DISEÑO §3).

Ejecutar con:  python -m unittest discover -s tests -v

Sin red ni AWS: el cliente de Lambda es falso. Lo que se vigila es lo que puede
fallar sin que nadie lo vea hasta la factura: una alarma que no frena, un freno
que pone otra cosa que 0, o un mensaje raro que lo deja sin frenar.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]

# Se carga por ruta: «handler» a secas chocaría con cualquier otro módulo de ese nombre
_spec = importlib.util.spec_from_file_location("freno_handler", RAIZ / "infra" / "freno" / "handler.py")
freno = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(freno)


class LambdaFalsa:
    def __init__(self, error: Exception | None = None):
        self.llamadas: list[dict] = []
        self.error = error

    def put_function_concurrency(self, **kwargs):
        self.llamadas.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ReservedConcurrentExecutions": kwargs["ReservedConcurrentExecutions"]}


def evento(*mensajes) -> dict:
    """Lo que SNS le pasa a una Lambda suscrita: un Record por mensaje."""
    return {"Records": [{"EventSource": "aws:sns",
                         "Sns": {"Message": m if isinstance(m, str) else json.dumps(m)}}
                        for m in mensajes]}


def alarma(estado: str = "ALARM", **extra) -> dict:
    return {"AlarmName": "cotejo-demo-invocaciones-hora", "NewStateValue": estado,
            "NewStateReason": "Threshold Crossed: 1 datapoint [1234.0] was greater than the threshold (1000.0).",
            **extra}


class TestFreno(unittest.TestCase):
    def setUp(self):
        self.aws = LambdaFalsa()
        pila = contextlib.ExitStack()
        self.addCleanup(pila.close)
        pila.enter_context(mock.patch.dict(os.environ, {"FUNCION": "cotejo-demo"}))
        pila.enter_context(mock.patch.object(freno, "_cliente", return_value=self.aws))
        self.salida = pila.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_una_alarma_frena_poniendo_la_concurrencia_a_cero(self):
        r = freno.handler(evento(alarma()), None)
        self.assertEqual(self.aws.llamadas,
                         [{"FunctionName": "cotejo-demo", "ReservedConcurrentExecutions": 0}])
        self.assertTrue(r["frenada"])
        registro = json.loads(self.salida.getvalue().splitlines()[-1])
        self.assertEqual(registro["evento"], "frenada")
        self.assertEqual(registro["concurrencia"], 0)
        # Lo que hay que mirar antes de reactivar: qué alarma y con cuántas invocaciones
        self.assertEqual(registro["avisos"][0]["alarma"], "cotejo-demo-invocaciones-hora")
        self.assertIn("1234.0", registro["avisos"][0]["motivo"])

    def test_la_vuelta_a_ok_no_frena(self):
        # Si algún día las alarmas tienen ok_actions, volver a OK justo después de
        # reactivar no puede parar la demo otra vez
        for estado in ("OK", "INSUFFICIENT_DATA"):
            r = freno.handler(evento(alarma(estado)), None)
            self.assertFalse(r["frenada"])
        self.assertEqual(self.aws.llamadas, [])

    def test_lo_que_no_se_entiende_frena(self):
        # Ante la duda, una demo parada cuesta 0 y una inundación no
        for ev in (evento("no es JSON"), evento(["una", "lista"]), evento({"sin": "estado"}),
                   {"Records": "roto"}, {}, None):
            self.aws.llamadas.clear()
            r = freno.handler(ev, None)
            self.assertTrue(r["frenada"], ev)
            self.assertEqual(len(self.aws.llamadas), 1, ev)

    def test_basta_un_aviso_de_alarma_entre_varios(self):
        freno.handler(evento(alarma("OK"), alarma("ALARM")), None)
        self.assertEqual(len(self.aws.llamadas), 1)

    def test_el_mensaje_no_elige_la_concurrencia(self):
        # IAM no puede limitar el número de PutFunctionConcurrency, y en el tema
        # puede publicar cualquiera con sns:Publish en la cuenta
        freno.handler(evento(alarma(ReservedConcurrentExecutions=900, FunctionName="otra")), None)
        self.assertEqual(self.aws.llamadas,
                         [{"FunctionName": "cotejo-demo", "ReservedConcurrentExecutions": 0}])

    def test_sin_funcion_en_el_entorno_falla_a_la_vista(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                freno.handler(evento(alarma()), None)
        self.assertEqual(self.aws.llamadas, [])

    def test_un_error_de_aws_sube_para_que_lambda_reintente(self):
        self.aws.error = RuntimeError("ThrottlingException")
        with self.assertRaises(RuntimeError):
            freno.handler(evento(alarma()), None)
        self.assertNotIn('"frenada"', self.salida.getvalue())


class TestReactivacion(unittest.TestCase):
    """La reactivación a mano de la cabecera de infra/main.tf es texto, no código.

    Cuando la concurrencia de serie bajó de 2 a 1 (DISEÑO §2), los comandos de
    la cabecera seguían devolviéndole 2: seguirlos tras un freno habría dejado
    dos contenedores a la vez contra la relatoría, justo lo que el cambio evita,
    y ni terraform validate ni plan lo ven.
    """

    def test_los_comandos_devuelven_la_concurrencia_de_serie(self):
        variables = (RAIZ / "infra" / "variables.tf").read_text(encoding="utf-8")
        principal = (RAIZ / "infra" / "main.tf").read_text(encoding="utf-8")
        bloque = re.search(r'variable "concurrencia_maxima" \{.*?\n\}', variables, re.S)
        self.assertIsNotNone(bloque)
        de_serie = re.search(r"\n\s*default\s*=\s*(\d+)\s*\n", bloque.group(0))
        self.assertIsNotNone(de_serie)
        comandos = re.findall(r"--reserved-concurrent-executions (\d+)", principal)
        plan = re.findall(r"reserved_concurrent_executions: 0 -> (\d+)", principal)
        self.assertTrue(comandos and plan)
        self.assertEqual(set(comandos + plan), {de_serie.group(1)})


if __name__ == "__main__":
    unittest.main()
