"""Pruebas de Cotejo.

Ejecutar con:  python -m unittest discover -s tests -v
Sin red por defecto. Las que consultan las fuentes oficiales de verdad se
activan con la variable COTEJO_RED=1.

Lo que más se vigila es lo que puede hacerle daño a un abogado:
  - declarar inexistente una sentencia que existe (sobre todo por un fallo de red),
  - dar por buena una cita textual que cambia una palabra que invierte el sentido,
  - atribuir un pasaje a la sentencia equivocada,
  - confundir el número de expediente con el de una sentencia.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import http.client
import importlib
import io
import json
import os
import random
import re
import shutil
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from cotejo import comparacion, extraccion, informe, verificacion   # noqa: E402
from cotejo.fuentes import corte_constitucional as cc               # noqa: E402
from cotejo.fuentes import corte_suprema as cs                      # noqa: E402
from cotejo.fuentes import red                                      # noqa: E402
from cotejo.verificacion import Estado                               # noqa: E402


def claves(texto: str) -> list[str]:
    return [c.clave for c in extraccion.extraer(texto)]


def _error_http(codigo: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://ejemplo.invalid/", codigo, "simulado", {}, None)


class _Cuerpo:
    """Una respuesta de urlopen con su código, como la de verdad."""

    def __init__(self, cuerpo: bytes, status: int = 200):
        self.cuerpo, self.status = cuerpo, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.cuerpo


# Lo que la relatoría devuelve desde 2026 para una dirección que no conoce, con
# su firma (DISEÑO §1c): <app-root> vacío y el título. La de verdad, de 8.607
# bytes, está en tests/datos/carcasa.htm.gz.
CARCASA = (b"<html><head><title>CORTE CONSTITUCIONAL DE COLOMBIA</title></head>"
           b"<body><app-root></app-root></body></html>")
DATOS = Path(__file__).resolve().parent / "datos"


def _real(nombre: str) -> bytes:
    """Bytes reales de la relatoría, guardados (y recortados) para probar sin red."""
    return gzip.decompress((DATOS / nombre).read_bytes())


# Comprobar si una sentencia fue anulada es una petición a la relatoría por
# cada sentencia verificada. Ninguna prueba sin red debe hacerla sin querer:
# se anula para todo el módulo y las pruebas que la necesitan la simulan.
_nota_de_nulidad_real = cc.nota_de_nulidad
_parche_nulidad = mock.patch.object(cc, "nota_de_nulidad", return_value=None)

# «Hoy» fijo, el día en que se escribieron estas pruebas. Que una ausencia sea
# «no existe» o «no encontrada» depende de la fecha (Indice.quiza_reciente): con
# el reloj del sistema, la T-999/2019 inventada de estas pruebas pasaría a «quizá
# reciente» hacia 2032 con un índice de prueba que llega a 2010, y la prueba
# fallaría sin que nada hubiera cambiado.
HOY = dt.date(2026, 10, 20)
_parche_hoy = mock.patch.object(cc, "_hoy", return_value=HOY)


def setUpModule():
    _parche_nulidad.start()
    _parche_hoy.start()


def tearDownModule():
    _parche_hoy.stop()
    _parche_nulidad.stop()


# ============================================================ extracción
class TestExtraccion(unittest.TestCase):
    def test_formas_habituales(self):
        self.assertEqual(claves("Sentencia T-406 de 1992"), ["T-406/1992"])
        self.assertEqual(claves("ver T-406/92"), ["T-406/1992"])
        self.assertEqual(claves("la SU-111 de 1997"), ["SU-111/1997"])
        self.assertEqual(claves("sentencia No. C-355 de 2006"), ["C-355/2006"])
        self.assertEqual(claves("la T-1025 de 2002"), ["T-1025/2002"])

    def test_sentencia_bis_con_letra(self):
        self.assertEqual(claves("la T-042A de 2025"), ["T-042A/2025"])
        self.assertEqual(claves("la T-42a de 2025"), ["T-042A/2025"])

    def test_ceros_delante_son_la_misma_en_la_constitucional(self):
        """Como en el índice: «T-25» y «T-025» de 2004 son la T-025/04, no dos sentencias."""
        self.assertEqual(claves("La T-025 de 2004 y la T-25 de 2004, y la T-0760 de 2008."),
                         ["T-025/2004", "T-025/2004", "T-760/2008"])
        self.assertEqual(claves("la T-1025 de 2002 y la SU-1 de 1992"), ["T-1025/2002", "SU-001/1992"])

    def test_de_en_mayusculas(self):
        self.assertEqual(claves("SENTENCIA T-406 DE 1992"), ["T-406/1992"])

    def test_el_expediente_no_es_una_sentencia(self):
        """Es el falso positivo más frecuente en un escrito de tutela."""
        self.assertEqual(claves("Expediente T-7.123.456"), [])
        self.assertEqual(claves("Expediente T-7123456"), [])
        self.assertEqual(claves("Exp. T-8.456.123, accionante: Ana"), [])

    def test_no_confunde_otras_cosas(self):
        self.assertEqual(claves("El COVID-19 y la Ley 100 de 1993"), [])

    def test_el_anio_se_comparte_en_una_enumeracion(self):
        citas = extraccion.extraer("las sentencias T-123, T-456 y T-789 de 2010")
        self.assertEqual([c.clave for c in citas], ["T-123/2010", "T-456/2010", "T-789/2010"])
        self.assertEqual([c.anio_heredado for c in citas], [True, True, False])

    def test_sin_anio_solo_si_se_anuncia(self):
        self.assertEqual(claves("un C-19 suelto"), [])
        cita = extraccion.extraer("la sentencia T-760 sin más")[0]
        self.assertFalse(cita.verificable)
        self.assertIn("año", cita.motivo_no_verificable)

    def test_anterior_a_1992_no_es_de_la_corte_constitucional(self):
        cita = extraccion.extraer("Sentencia T-406 de 1988")[0]
        self.assertFalse(cita.verificable)

    def test_otras_fuentes_se_detectan_pero_no_se_verifican(self):
        texto = ("Según la SC5686-2018 y el AC739-2026, rad. "
                 "11001-02-03-000-2019-01234-00, y el Auto 092 de 2008. "
                 "Consejo de Estado, Sección Tercera, sentencia de 12 de marzo de 2015.")
        citas = extraccion.extraer(texto)
        self.assertEqual(len(citas), 5)
        # Los códigos de la Corte Suprema ya se verifican; el resto, no
        self.assertTrue(all(c.verificable == (c.clase == "codigo") for c in citas))
        self.assertEqual({c.clase for c in citas}, {"codigo", "radicado", "auto", "mencion"})

    def test_ponente(self):
        c = extraccion.extraer("Sentencia T-406 de 1992 (M.P. Ciro Angarita Barón).")[0]
        self.assertEqual(c.ponente, "Ciro Angarita Barón")
        c = extraccion.extraer("T-760 de 2008, Magistrado Ponente: Manuel José Cepeda Espinosa")[0]
        self.assertEqual(c.ponente, "Manuel José Cepeda Espinosa")

    def test_ponente_nombrado_antes_de_la_cita(self):
        c = extraccion.extraer(
            "En este sentido, el Magistrado Rodrigo del Corral precisó en la Sentencia SU-573 de 2019 que…")[0]
        self.assertEqual(c.ponente, "Rodrigo del Corral")

    def test_el_ponente_de_una_cita_no_se_le_pega_a_la_siguiente(self):
        citas = extraccion.extraer(
            "Sentencia T-406 de 1992 (M.P. Ciro Angarita Barón) y la Sentencia T-760 de 2008.")
        self.assertEqual(citas[0].ponente, "Ciro Angarita Barón")
        self.assertIsNone(citas[1].ponente)

    def test_pasaje_anunciado_en_el_parrafo_anterior(self):
        texto = ("En la Sentencia T-760 de 2008 se dijo algo.\n\n"
                 "En la misma providencia la Corte precisó: “uno dos tres cuatro cinco "
                 "seis siete ocho nueve diez”.\n\n"
                 "3. Luego, la Sentencia T-406 de 1992 dijo otra cosa.")
        citas = {c.clave: c for c in extraccion.extraer(texto)}
        self.assertEqual(len(citas["T-760/2008"].citas_textuales), 1)
        self.assertEqual(len(citas["T-406/1992"].citas_textuales), 0,
                         "un pasaje no es de la cita que abre el párrafo siguiente")

    def test_pasaje_cerrado_por_la_cita(self):
        texto = "“uno dos tres cuatro cinco seis siete ocho nueve diez” (Sentencia T-406 de 1992)."
        c = extraccion.extraer(texto)[0]
        self.assertEqual(len(c.citas_textuales), 1)

    def test_comillas_anidadas_del_mismo_signo(self):
        """Antes el pasaje se cortaba en el primer cierre y lo de detrás no se miraba."""
        texto = "“las prisiones implican que una celda, un “camastro”, sean absolutamente escasos”"
        pasajes = extraccion.pasajes_entre_comillas(texto)
        self.assertEqual(len(pasajes), 1)
        self.assertTrue(pasajes[0][2].endswith("absolutamente escasos"))

    def test_comillas_espanolas_con_inglesas_dentro(self):
        texto = "«la Corte habló del llamado “mínimo vital” como garantía de subsistencia»"
        pasajes = extraccion.pasajes_entre_comillas(texto)
        self.assertEqual(len(pasajes), 1)
        self.assertIn("garantía de subsistencia", pasajes[0][2])

    def test_comilla_de_mas_no_deja_el_pasaje_sin_comprobar(self):
        """El banco grande encontró 18 pasajes que se saltaban en silencio así."""
        texto = "la Corte dijo: ““En el caso de la paciente las trombosis tienen otros factores de riesgo.”"
        pasajes = extraccion.pasajes_entre_comillas(texto)
        self.assertEqual(len(pasajes), 1)
        self.assertIn("factores de riesgo", pasajes[0][2])

    def test_comilla_sin_cerrar_no_cruza_parrafos(self):
        texto = "Dijo: “uno dos tres cuatro cinco seis siete ocho\n\nOtro párrafo con “una cita cerrada de verdad aquí”."
        pasajes = extraccion.pasajes_entre_comillas(texto)
        self.assertEqual(len(pasajes), 1)
        self.assertEqual(pasajes[0][2], "una cita cerrada de verdad aquí")

    def test_comillas_rectas(self):
        pasajes = extraccion.pasajes_entre_comillas('dijo "uno dos tres cuatro cinco seis" y "otra cosa más larga aquí"')
        self.assertEqual(len(pasajes), 2)

    def test_comillas_cortas_no_son_citas_textuales(self):
        c = extraccion.extraer("La T-406 de 1992 habla del “mínimo vital”.")[0]
        self.assertEqual(c.citas_textuales, [])


# ============================================================ comparación
SENTENCIA = (
    "CONSIDERACIONES. El juez, en el Estado social de derecho también es un portador "
    "de la visión institucional del interés general. El juez, al poner en relación la "
    "Constitución -sus principios y sus normas- con la ley y con los hechos hace uso de "
    "una discrecionalidad interpretativa que necesariamente delimita el sentido político."
)


class TestComparacion(unittest.TestCase):
    def setUp(self):
        self.sent = comparacion.normalizar(SENTENCIA)

    def estado(self, pasaje: str) -> str:
        return comparacion.buscar_pasaje(pasaje, self.sent).estado

    def test_literal(self):
        self.assertEqual(self.estado(
            "El juez, en el Estado social de derecho también es un portador de la visión"),
            "coincide")

    def test_tolera_tildes_mayusculas_y_puntuacion(self):
        self.assertEqual(self.estado(
            "el juez en el estado social de derecho TAMBIEN es un portador de la vision"),
            "coincide")

    def test_una_palabra_que_invierte_el_sentido_no_coincide(self):
        """El caso que la primera versión daba por bueno con un 96,7 %."""
        r = comparacion.buscar_pasaje(
            "El juez, en el Estado social de derecho no es un portador de la visión "
            "institucional del interés general.", self.sent)
        self.assertEqual(r.estado, "difiere")
        self.assertIn(("no", "también"), r.diferencias)

    def test_elision_entre_parentesis(self):
        self.assertEqual(self.estado(
            "El juez, en el Estado social de derecho (...) portador de la visión "
            "institucional del interés general"), "coincide")

    def test_insercion_editorial_entre_corchetes(self):
        self.assertEqual(self.estado(
            "[el juez] al poner en relación la Constitución sus principios y sus normas "
            "con la ley y con los hechos"), "coincide")

    def test_pasaje_inventado(self):
        self.assertEqual(self.estado(
            "la vida del nasciturus prevalece en todos los casos sin excepción alguna"),
            "no_aparece")

    def test_omision_sin_marcar(self):
        r = comparacion.buscar_pasaje(
            "El juez, en el Estado social de derecho es un portador de la visión "
            "institucional del interés general", self.sent)
        self.assertEqual(r.estado, "difiere")
        self.assertIn(("", "también"), r.diferencias)

    def test_el_texto_real_se_devuelve_con_tildes(self):
        r = comparacion.buscar_pasaje(
            "el juez en el estado social de derecho no es un portador de la vision", self.sent)
        self.assertIn("visión", r.fragmentos[0].mas_parecido)


# ============================================================ verificación
FILAS = [
    {"sentencia": "T-406/92", "fecha_sentencia": "1992-06-05T00:00:00.000",
     "magistrado_a": "Ciro Angarita Baron", "sala": "Salas de Revisión", "proceso": "Tutela"},
    {"sentencia": "T-760/08", "fecha_sentencia": "2008-07-31T00:00:00.000",
     "magistrado_a": "Manuel José Cepeda Espinosa"},
    {"sentencia": "T-760/10", "fecha_sentencia": "2010-09-23T00:00:00.000",
     "magistrado_a": "Otra Persona Cualquiera"},
    {"sentencia": "C-355/06", "fecha_sentencia": "2006-05-10T00:00:00.000",
     "magistrado_a": "Clara Inés Vargas Hernández; Jaime Araujo Rentería"},
]


def _relatoria_simulada(textos: dict):
    def falsa(tipo, numero, anio):
        clave = cc.id_oficial(tipo, numero, anio)
        valor = textos.get(clave)
        if isinstance(valor, Exception):
            raise valor
        return valor
    return falsa


class TestVerificacion(unittest.TestCase):
    def setUp(self):
        self.indice = cc.Indice(FILAS)

    def verificar(self, texto, textos=None, **kw):
        with mock.patch.object(cc, "texto_relatoria", _relatoria_simulada(textos or {})):
            return verificacion.verificar(texto, indice=self.indice, **kw)

    def test_existente(self):
        inf = self.verificar("Sentencia T-760 de 2008.")
        self.assertEqual(inf.hallazgos[0].estado, Estado.VERIFICADA)

    def test_inexistente_con_doble_comprobacion(self):
        inf = self.verificar("Sentencia T-999 de 2019.", textos={"T-999/19": None})
        self.assertEqual(inf.hallazgos[0].estado, Estado.NO_EXISTE)

    def test_un_fallo_de_red_nunca_da_no_existe(self):
        """La regla que manda sobre todas: ante la duda, sin confirmar."""
        inf = self.verificar("Sentencia T-999 de 2019.",
                             textos={"T-999/19": TimeoutError("sin respuesta")})
        self.assertEqual(inf.hallazgos[0].estado, Estado.SIN_CONFIRMAR)

    def test_sin_red_tampoco_da_no_existe(self):
        inf = self.verificar("Sentencia T-999 de 2019.", relatoria=False)
        self.assertEqual(inf.hallazgos[0].estado, Estado.SIN_CONFIRMAR)

    def test_reciente_que_aun_no_esta_en_el_indice(self):
        inf = self.verificar("Sentencia T-100 de 2026.",
                             textos={"T-100/26": "texto de la sentencia " * 500})
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.VERIFICADA)
        self.assertEqual(h.fuente, "relatoria")

    def test_sugiere_el_anio_vecino(self):
        # 2009 tiene que estar asentado en el índice (DISEÑO §1d): con FILAS
        # sola, el índice no trae ninguna de 2009 y la T-760/09 podría ser una
        # que aún no se ha publicado («no encontrada»)
        self.indice = cc.Indice(FILAS + [{"sentencia": "T-900/09", "fecha_sentencia": "2009-11-30T00:00:00.000"}])
        inf = self.verificar("Sentencia T-760 de 2009.", textos={"T-760/09": None})
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.NO_EXISTE)
        self.assertEqual([p.id for p in h.posibles], ["T-760/08", "T-760/10"])
        # Con FILAS sola tampoco hay ninguna de 2009, pero el índice llega al
        # 23-09-2010: 2009 es un año cerrado y anterior al tramo sin asentar, y que
        # falte en él significa algo. Antes los días de un año cerrado se contaban
        # hasta hoy y salía «no encontrada» (revisión 4).
        self.indice = cc.Indice(FILAS)
        h = self.verificar("Sentencia T-760 de 2009.", textos={"T-760/09": None}).hallazgos[0]
        self.assertEqual(h.estado, Estado.NO_EXISTE)
        self.assertEqual([p.id for p in h.posibles], ["T-760/08", "T-760/10"], "y también sugiere")
        # Un año que no está cerrado respecto del índice sí da «no encontrada»
        self.indice = cc.Indice(FILAS + [{"sentencia": "T-001/11", "fecha_sentencia": "2011-01-20T00:00:00.000"}])
        h = self.verificar("Sentencia T-30 de 2011.", textos={"T-030/11": None}).hallazgos[0]
        self.assertEqual(h.estado, Estado.NO_ENCONTRADA)

    def test_ponente_equivocado(self):
        inf = self.verificar("Sentencia T-406 de 1992 (M.P. Eduardo Cifuentes Muñoz).")
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertIn("Ciro Angarita", h.observaciones[0])

    def test_ponente_abreviado_vale(self):
        inf = self.verificar("Sentencia T-406 de 1992 (M.P. Ciro Angarita).")
        self.assertEqual(inf.hallazgos[0].estado, Estado.VERIFICADA)

    def test_dos_ponentes_no_se_mezclan(self):
        self.assertTrue(verificacion.mismo_ponente(
            "Jaime Araujo Rentería", "Clara Inés Vargas Hernández; Jaime Araujo Rentería"))
        self.assertFalse(verificacion.mismo_ponente(
            "Clara Araujo", "Clara Inés Vargas Hernández; Jaime Araujo Rentería"))

    def test_pasaje_invertido_se_marca(self):
        texto = ("En la Sentencia T-406 de 1992 se afirmó: “El juez, en el Estado social "
                 "de derecho no es un portador de la visión institucional del interés general.”")
        inf = self.verificar(texto, textos={"T-406/92": SENTENCIA})
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertIn("«no»", h.observaciones[0])
        self.assertIn("«también»", h.observaciones[0])

    def test_otras_fuentes_quedan_como_no_verificables(self):
        inf = self.verificar("Según el Consejo de Estado, en sentencia del 3 de marzo de 2020.")
        self.assertEqual(inf.hallazgos[0].estado, Estado.NO_VERIFICABLE)

    def test_citas_repetidas_se_agrupan(self):
        inf = self.verificar("La T-760 de 2008 y otra vez la Sentencia T-760/08.")
        self.assertEqual(len(inf.hallazgos), 1)
        self.assertEqual(len(inf.hallazgos[0].citas), 2)

    # -- plazo (la demo pública tiene un tiempo máximo por consulta) --------
    MOTIVO_PLAZO = "no se alcanzó a consultar: la verificación llegó a su tiempo máximo"

    def test_con_el_plazo_vencido_no_se_consulta_nada(self):
        """Lo que falta se lista como «sin confirmar», sin pedirle nada a nadie."""
        texto = ("Sentencia T-760 de 2008. Sentencia T-999 de 2019. Según la SC5686-2018 "
                 "y la STH45-2020. Y el Consejo de Estado, en sentencia del 3 de marzo de 2020.")
        antes = (cc.cliente.peticiones, cs.cliente.peticiones)
        with mock.patch.object(cc, "texto_relatoria") as relatoria, \
                mock.patch.object(cc, "nota_de_nulidad") as nulidad, \
                mock.patch.object(cs, "buscar") as buscar, \
                mock.patch.object(red.urllib.request, "urlopen") as urlopen:
            inf = verificacion.verificar(texto, indice=self.indice, hasta=time.monotonic() - 1)
        por_clave = {h.clave: h for h in inf.hallazgos}
        for clave in ("T-760/2008", "T-999/2019", "SC5686-2018"):
            self.assertEqual(por_clave[clave].estado, Estado.SIN_CONFIRMAR, clave)
            self.assertEqual(por_clave[clave].motivo, self.MOTIVO_PLAZO)
        # Las no verificables no consultaban nada: salen como siempre, con su motivo
        self.assertEqual(por_clave["STH45-2020"].estado, Estado.NO_VERIFICABLE)
        self.assertIn("STH", por_clave["STH45-2020"].motivo)
        otras = [h for h in inf.hallazgos if h.primera.tribunal == extraccion.CONSEJO_ESTADO]
        self.assertEqual([h.estado for h in otras], [Estado.NO_VERIFICABLE])
        self.assertTrue(inf.cortado_por_tiempo)
        for simulado in (relatoria, nulidad, buscar, urlopen):
            simulado.assert_not_called()
        self.assertEqual((cc.cliente.peticiones, cs.cliente.peticiones), antes)

    def test_el_plazo_corta_esa_y_las_siguientes(self):
        """Se mira antes de cada providencia: la que ya se consultó vale."""
        relojes = iter([0.0, 10.0])
        with mock.patch.object(verificacion, "time") as reloj:
            reloj.monotonic.side_effect = lambda: next(relojes, 10.0)
            inf = self.verificar(
                "Sentencia T-760 de 2008. Sentencia T-406 de 1992. Sentencia C-355 de 2006.",
                hasta=5.0)
        self.assertEqual([h.estado for h in inf.hallazgos],
                         [Estado.VERIFICADA, Estado.SIN_CONFIRMAR, Estado.SIN_CONFIRMAR])
        self.assertEqual(inf.hallazgos[1].motivo, self.MOTIVO_PLAZO)
        self.assertTrue(inf.cortado_por_tiempo)

    def test_con_plazo_de_sobra_no_cambia_nada(self):
        inf = self.verificar("Sentencia T-760 de 2008.", hasta=time.monotonic() + 3600)
        self.assertEqual(inf.hallazgos[0].estado, Estado.VERIFICADA)
        self.assertFalse(inf.cortado_por_tiempo)
        self.assertFalse(self.verificar("Sentencia T-760 de 2008.").cortado_por_tiempo)
        self.assertFalse(verificacion.Informe("escrito", [], 0).cortado_por_tiempo,
                         "el campo nuevo no rompe a quien construye un Informe")

    def test_verificar_restaura_el_plazo_de_los_clientes(self):
        """También si algo lanza: un plazo olvidado cortaría la consulta siguiente del contenedor."""
        vistos = []

        def falla(*args, **kwargs):
            vistos.append((cc.cliente.hasta, cs.cliente.hasta))
            raise RuntimeError("algo se rompió a mitad")
        plazo = time.monotonic() + 3600
        cc.cliente.hasta, cs.cliente.hasta = None, 77.0
        try:
            with mock.patch.object(verificacion, "_verificar_cc", side_effect=falla):
                with self.assertRaises(RuntimeError):
                    verificacion.verificar("Sentencia T-760 de 2008.", indice=self.indice, hasta=plazo)
            self.assertEqual(vistos, [(plazo, plazo)], "mientras dura, los dos clientes llevan el plazo")
            self.assertEqual((cc.cliente.hasta, cs.cliente.hasta), (None, 77.0), "y después, el de antes")
        finally:
            cc.cliente.hasta = cs.cliente.hasta = None

    # -- tope (una consulta de la demo no puede pedirle a la Corte sin límite) --
    def test_el_tope_de_providencias(self):
        """446 sentencias inventadas en un texto eran 41 peticiones a la relatoría por una consulta."""
        texto = ("El Auto 092 de 2008 y la STH45-2020. Sentencia T-760 de 2008. "
                 "Sentencia T-406 de 1992. Sentencia C-355 de 2006. Según la SC5686-2018.")
        with mock.patch.object(cc, "nota_de_nulidad", return_value=None) as nulidad, \
                mock.patch.object(cs, "buscar") as buscar:
            inf = self.verificar(texto, max_consultadas=2)
            justo = self.verificar("Sentencia T-760 de 2008. Sentencia T-406 de 1992.", max_consultadas=2)
        motivo = verificacion.MOTIVO_TOPE.format(n=2)
        self.assertEqual(motivo, "no se consultó: esta verificación consulta como mucho 2 providencias por texto")
        por_clave = {h.clave: h for h in inf.hallazgos}
        self.assertEqual({c: h.estado for c, h in por_clave.items()}, {
            "Auto 092 de 2008": Estado.NO_VERIFICABLE, "STH45-2020": Estado.NO_VERIFICABLE,
            "T-760/2008": Estado.VERIFICADA, "T-406/1992": Estado.VERIFICADA,
            "C-355/2006": Estado.SIN_CONFIRMAR, "SC5686-2018": Estado.SIN_CONFIRMAR},
            "las no verificables no preguntan nada y no cuentan")
        for clave in ("C-355/2006", "SC5686-2018"):
            self.assertEqual(por_clave[clave].motivo, motivo, clave)
        self.assertEqual(nulidad.call_count, 2 + 2, "dos en cada verificación: las de detrás del tope, no")
        buscar.assert_not_called()
        self.assertTrue(inf.cortado_por_tope)
        self.assertFalse(inf.cortado_por_tiempo)
        self.assertFalse(justo.cortado_por_tope, "con tantas como el tope no se corta nada")
        self.assertFalse(verificacion.Informe("escrito", [], 0).cortado_por_tope)

    def test_la_misma_con_ceros_delante_es_un_solo_hallazgo_y_cuenta_una_vez(self):
        """Antes la T-0760 contaba como otra para el tope y dejaba sin consultar la T-406/92."""
        self.indice = cc.Indice(FILAS + [{"sentencia": "T-025/04", "fecha_sentencia": "2004-01-22T00:00:00.000",
                                          "magistrado_a": "Manuel José Cepeda Espinosa"}])
        inf = self.verificar("La T-025 de 2004 y la T-25 de 2004. Sentencia T-406 de 1992.", max_consultadas=2)
        self.assertEqual([(h.clave, h.estado, len(h.citas)) for h in inf.hallazgos],
                         [("T-025/2004", Estado.VERIFICADA, 2), ("T-406/1992", Estado.VERIFICADA, 1)],
                         "un solo hallazgo, con las dos menciones")
        self.assertFalse(inf.cortado_por_tope)
        inf = self.verificar("Sentencia T-760 de 2008, T-0760 de 2008 y T-406 de 1992.", max_consultadas=2)
        self.assertEqual({h.clave: h.estado for h in inf.hallazgos},
                         {"T-760/2008": Estado.VERIFICADA, "T-406/1992": Estado.VERIFICADA})
        self.assertFalse(inf.cortado_por_tope, "son dos providencias, no tres")


# ================================================================ fuente
class TestFuente(unittest.TestCase):
    def test_identificadores(self):
        self.assertEqual(cc.id_oficial("t", "12", 92), "T-012/92")
        self.assertEqual(cc.id_oficial("T", "042a", 2025), "T-042A/25")
        self.assertEqual(cc.id_oficial("T", "1025", 2002), "T-1025/02")

    def test_anio_de_dos_cifras(self):
        self.assertEqual(cc.anio_completo(92), 1992)
        self.assertEqual(cc.anio_completo(8), 2008)
        self.assertEqual(cc.anio_completo(26), 2026)

    def test_url_de_la_relatoria(self):
        self.assertEqual(cc.url_relatoria("T", "406", 1992),
                         "https://www.corteconstitucional.gov.co/relatoria/1992/T-406-92.htm")

    def test_las_de_unificacion_van_sin_guion_en_la_relatoria(self):
        """Las 280 SU consultadas en la evaluación daban todas «no existe»."""
        self.assertEqual(cc.url_relatoria("SU", "214", 2023),
                         "https://www.corteconstitucional.gov.co/relatoria/2023/SU214-23.htm")
        self.assertEqual(len(cc.urls_relatoria("SU", "214", 2023)), 2,
                         "las SU prueban la otra forma como respaldo")
        self.assertEqual(len(cc.urls_relatoria("T", "406", 1992)), 1,
                         "las T no: duplicaría la carga sobre el servidor")

    def test_reconoce_la_sentencia_con_cualquier_separador(self):
        relleno = " texto" * 600
        self.assertTrue(cc._parece_la_sentencia("SU.214/16" + relleno, "SU", "214"))
        self.assertTrue(cc._parece_la_sentencia("SU214-23" + relleno, "SU", "214"))
        self.assertFalse(cc._parece_la_sentencia("SU2140-23" + relleno, "SU", "214"))

    def test_reconoce_la_pagina_de_no_encontrado(self):
        """La relatoría responde 200 también para lo que no existe."""
        self.assertFalse(cc._parece_la_sentencia("CORTE CONSTITUCIONAL " * 50, "T", "999"))
        self.assertTrue(cc._parece_la_sentencia(
            "Sentencia T-406/92 " + "texto " * 600, "T", "406"))
        self.assertFalse(cc._parece_la_sentencia(
            "Sentencia T-407/92 " + "texto " * 600, "T", "406"),
            "una página larga que no menciona el número no cuenta")


class _SocketLento(io.RawIOBase):
    """El socket por debajo de una respuesta HTTP de verdad (http.client.HTTPResponse).

    Cada lectura trae como mucho `trozo` bytes y adelanta `reloj[0]` en `paso`
    segundos: un servidor que manda despacio pero sin pararse, que es lo que
    el tiempo de espera de urlopen (por recepción) no ve. Con `sin_fin`, tras
    `crudo` sigue mandando «x», hasta 20 MB: más que el tope, y con final, para
    que si el tope se perdiera la prueba fallara en vez de colgar la batería.
    """

    FIN = 20 * 1024 * 1024

    def __init__(self, crudo: bytes, trozo: int = 1024, reloj: list | None = None,
                 paso: float = 0.0, sin_fin: bool = False):
        self.crudo, self.pos, self.trozo = crudo, 0, trozo
        self.reloj, self.paso, self.sin_fin = reloj, paso, sin_fin

    def readable(self):
        return True

    def readinto(self, b):
        if self.reloj is not None:
            self.reloj[0] += self.paso
        if self.pos >= (self.FIN if self.sin_fin else len(self.crudo)):
            return 0
        trozo = self.crudo[self.pos:self.pos + min(len(b), self.trozo)] or b"x" * min(len(b), self.trozo)
        b[:len(trozo)] = trozo
        self.pos += len(trozo)
        return len(trozo)

    def makefile(self, *args, **kwargs):
        return io.BufferedReader(self)


def _http(crudo: _SocketLento) -> http.client.HTTPResponse:
    """La respuesta que daría urlopen, con su cabecera ya leída."""
    r = http.client.HTTPResponse(crudo, method="GET")
    r.begin()
    return r


class TestRed(unittest.TestCase):
    """El cliente cortés: qué cuenta como fallo y cuántas peticiones se hacen."""

    URL = "https://ejemplo.invalid/pagina"

    def setUp(self):
        self.cliente = red.Cliente("El servidor de prueba", pausa=0, reintentos=3,
                                   fallos_para_cortar=2)
        self.sueno = mock.patch.object(red.time, "sleep")
        self.dormir = self.sueno.start()

    def tearDown(self):
        self.sueno.stop()

    @staticmethod
    def respuesta(cuerpo: bytes = b"ok"):
        r = mock.MagicMock()
        r.__enter__.return_value.read.return_value = cuerpo
        return r

    @staticmethod
    def servidor(**kw):
        """El servidor simulado: lo que responde cada urlopen."""
        return mock.patch.object(red.urllib.request, "urlopen", **kw)

    def test_una_redireccion_es_un_fallo_y_cuenta_el_salto(self):
        """Revisión 4: urllib seguía las redirecciones sin plazo, sin tope y sin contarlas.

        Ninguna fuente redirige lo que se le pide; un 3xx viene de un
        intermediario y su destino no es lo pedido.
        """
        r = self.respuesta(b"<html>Access denied</html>")
        r.__enter__.return_value.url = "https://cortafuegos.invalid/desafio"
        with self.servidor(return_value=r) as urlopen:
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertEqual(urlopen.call_count, 1, "no se reintenta")
        self.assertIn("redirigió", str(ctx.exception))
        self.assertEqual(self.cliente.peticiones, 2, "la pedida y el salto")
        self.assertEqual(self.cliente.fallos_seguidos, 1)
        r.__enter__.return_value.read.assert_not_called()
        # La misma dirección no es una redirección (urllib pone ahí la pedida)
        r = self.respuesta(b"ok")
        r.__enter__.return_value.url = self.URL
        with self.servidor(return_value=r):
            self.assertEqual(self.cliente.pedir(self.URL), b"ok")
        self.assertEqual(self.cliente.fallos_seguidos, 0)

    def test_un_403_no_se_reintenta(self):
        """Un cortafuegos que veta los rangos de AWS no cambia de idea en un segundo."""
        with self.servidor(side_effect=_error_http(403)) as urlopen:
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(self.cliente.fallos_seguidos, 1)

    def test_los_403_abren_el_cortacircuitos(self):
        """Antes no contaban: se seguía llamando a una puerta cerrada."""
        with self.servidor(side_effect=_error_http(403)) as urlopen:
            for _ in range(2):
                with self.assertRaises(red.ServicioNoDisponible):
                    self.cliente.pedir(self.URL)
            self.assertFalse(self.cliente.disponible())
            with self.assertRaises(red.ServicioNoDisponible):
                self.cliente.pedir(self.URL)
        self.assertEqual(urlopen.call_count, 2, "con el circuito abierto no vuelve a llamar")
        self.assertEqual(self.cliente.peticiones, 2)

    def test_cuenta_cada_peticion_real(self):
        with self.servidor(return_value=self.respuesta()):
            self.cliente.pedir(self.URL)
            self.cliente.pedir(self.URL)
        self.assertEqual(self.cliente.peticiones, 2)
        with self.servidor(side_effect=_error_http(503)):
            with self.assertRaises(red.ServicioNoDisponible):
                self.cliente.pedir(self.URL)
        self.assertEqual(self.cliente.peticiones, 5, "cada reintento es una petición")

    def test_los_demas_codigos_siguen_igual(self):
        with self.servidor(side_effect=_error_http(404)):
            self.assertEqual(self.cliente.pedir(self.URL), b"", "un 404 es una respuesta")
        with self.servidor(side_effect=_error_http(400)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                self.cliente.pedir(self.URL)
        self.assertEqual(urlopen.call_count, 1)
        with self.servidor(side_effect=[_error_http(429), self.respuesta(b"al fin")]):
            self.assertEqual(self.cliente.pedir(self.URL), b"al fin", "un 429 se reintenta")
        self.assertEqual(self.cliente.fallos_seguidos, 0)

    def test_un_cuerpo_cortado_se_reintenta_y_abre_el_cortacircuitos(self):
        """IncompleteRead y BadStatusLine no son OSError: antes salían sin reintentar ni contar."""
        cortada = self.respuesta()
        cortada.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"<html>", 8601)
        with self.servidor(side_effect=[http.client.BadStatusLine("basura"), cortada,
                                        self.respuesta(b"entera")]):
            self.assertEqual(self.cliente.pedir(self.URL), b"entera", "se reintenta")
        with self.servidor(return_value=cortada) as urlopen:
            for _ in range(2):
                with self.assertRaises(red.ServicioNoDisponible):
                    self.cliente.pedir(self.URL)
            self.assertFalse(self.cliente.disponible(), "dos seguidas abren el cortacircuitos")
            with self.assertRaises(red.ServicioNoDisponible):
                self.cliente.pedir(self.URL)
        self.assertEqual(urlopen.call_count, 6, "tres intentos por petición; con el circuito abierto, ninguno")

    def test_las_respuestas_raras_abren_el_cortacircuitos_aunque_lleguen_con_200(self):
        """Un desafío llega con 200: pedir() lo da por bueno, y solo quien lo lee sabe que no lo es."""
        with self.servidor(return_value=self.respuesta(b"<script>desafio()</script>")):
            self.cliente.pedir(self.URL)
            self.cliente.respuesta_irreconocible()
            self.cliente.pedir(self.URL)
            self.assertEqual((self.cliente.fallos_seguidos, self.cliente.raras_seguidas), (0, 1),
                             "el 200 siguiente no borra la cuenta de las raras")
            self.cliente.respuesta_irreconocible()
            self.assertFalse(self.cliente.disponible(), "con fallos_para_cortar=2, a la segunda")
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertIn("dejó de responder", str(ctx.exception))
        self.assertEqual(self.cliente.peticiones, 2, "con el circuito abierto no se pregunta")
        self.cliente.reiniciar()
        self.assertEqual(self.cliente.raras_seguidas, 0, "reiniciar también las olvida")
        self.cliente.respuesta_irreconocible()
        self.cliente.respuesta_valida()
        self.cliente.respuesta_irreconocible()
        self.assertTrue(self.cliente.disponible(), "una reconocida en medio corta la racha")

    def test_un_200_vacio_no_es_un_404(self):
        """pedir() da b"" para los dos; solo el código dice cuál fue."""
        with self.servidor(return_value=_Cuerpo(b"", 200)):
            self.assertEqual(self.cliente.pedir(self.URL), b"")
        self.assertEqual(self.cliente.ultimo_codigo, 200)
        with self.servidor(side_effect=_error_http(404)):
            self.assertEqual(self.cliente.pedir(self.URL), b"")
        self.assertEqual(self.cliente.ultimo_codigo, 404)
        self.cliente.cortado_hasta = time.time() + 60
        with self.assertRaises(red.ServicioNoDisponible):
            self.cliente.pedir(self.URL)
        self.assertIsNone(self.cliente.ultimo_codigo, "sin respuesta no queda el código de la anterior")

    def test_sin_tiempo_antes_de_una_pausa_que_no_cabe(self):
        """Ni se duerme ni se pregunta, y no cuenta para el cortacircuitos: el servidor no tiene la culpa."""
        self.cliente.pausa = 10
        self.cliente.ultima = time.time()
        self.cliente.hasta = time.monotonic() + 5
        with self.servidor(return_value=self.respuesta()) as urlopen:
            with self.assertRaises(red.SinTiempo) as ctx:
                self.cliente.pedir(self.URL)
            self.assertIsInstance(ctx.exception, red.ServicioNoDisponible,
                                  "quien llama lo trata como cualquier «no se pudo preguntar»")
            # Sin pausa, pero con menos de lo que pide un intento
            self.cliente.pausa, self.cliente.hasta = 0, time.monotonic() + red.MINIMO_INTENTO / 2
            with self.assertRaises(red.SinTiempo):
                self.cliente.pedir(self.URL)
        urlopen.assert_not_called()
        self.dormir.assert_not_called()
        self.assertEqual((self.cliente.peticiones, self.cliente.fallos_seguidos,
                          self.cliente.cortes_por_tiempo), (0, 0, 2))
        self.assertTrue(self.cliente.disponible())

    def test_sin_tiempo_para_la_pausa_del_reintento(self):
        """La pausa crece en cada reintento: la segunda, de 6 s, ya no cabe en los 5 que quedan."""
        self.cliente.pausa = 3
        self.cliente.hasta = time.monotonic() + 5
        with self.servidor(side_effect=_error_http(503)) as urlopen:
            for _ in range(2):
                with self.assertRaises(red.SinTiempo):
                    self.cliente.pedir(self.URL)
        self.assertEqual(urlopen.call_count, 2, "un intento por petición, sin el reintento que no cabía")
        self.assertEqual((self.cliente.fallos_seguidos, self.cliente.cortes_por_tiempo), (0, 2))
        self.assertTrue(self.cliente.disponible(),
                        "dos peticiones que acaban por el plazo no abren el cortacircuitos")

    def test_el_tiempo_de_espera_del_intento_se_recorta_al_plazo(self):
        self.cliente.tiempo_espera = 60
        self.cliente.hasta = time.monotonic() + 5
        with self.servidor(return_value=self.respuesta()) as urlopen:
            self.cliente.pedir(self.URL)
        espera = urlopen.call_args.kwargs["timeout"]
        self.assertTrue(4 < espera <= 5, espera)

        # Agotar esa espera recortada es SinTiempo: con la completa, quizá habría respondido.
        # Al conectar, urlopen envuelve el TimeoutError en un URLError; al leer, no.
        for agotada in (urllib.error.URLError(TimeoutError("timed out")), TimeoutError("timed out")):
            with self.servidor(side_effect=agotada) as urlopen:
                with self.assertRaises(red.SinTiempo):
                    self.cliente.pedir(self.URL)
            self.assertEqual(urlopen.call_count, 1, "no se reintenta")
        self.assertEqual((self.cliente.fallos_seguidos, self.cliente.cortes_por_tiempo), (0, 2))

        # Con la espera entera, agotarla es un fallo del servidor, como siempre
        self.cliente.hasta = time.monotonic() + 3600
        with self.servidor(side_effect=TimeoutError("timed out")) as urlopen:
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertNotIsInstance(ctx.exception, red.SinTiempo)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)
        self.assertEqual((urlopen.call_count, self.cliente.fallos_seguidos), (3, 1))

    # -- el cuerpo, por trozos (revisión 3, contrato y costo) -----------------
    def test_un_cuerpo_que_llega_despacio_corta_por_el_plazo_entre_trozos(self):
        """El tiempo de espera de urlopen acota cada recepción, no la respuesta entera.

        1 KB cada medio segundo nunca agota un tiempo de espera de 5 s, y antes
        r.read() esperaba los 200 KB enteros (100 s) sin mirar el plazo.
        """
        reloj = [1000.0]
        cuerpo = b"<html>" + b"x" * 200_000
        sockets = []

        def servidor(req, timeout=None):
            sockets.append(_SocketLento(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(cuerpo) + cuerpo,
                                        trozo=1024, reloj=reloj, paso=0.5))
            return _http(sockets[-1])
        self.cliente.hasta = 1005.0
        with mock.patch.object(red.time, "monotonic", lambda: reloj[0]), \
                self.servidor(side_effect=servidor) as urlopen:
            with self.assertRaises(red.SinTiempo) as ctx:
                self.cliente.pedir(self.URL)
        self.assertIn("recibir la respuesta", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 1, "no se reintenta")
        self.assertEqual((self.cliente.peticiones, self.cliente.cortes_por_tiempo,
                          self.cliente.fallos_seguidos, self.cliente.raras_seguidas), (1, 1, 0, 0),
                         "cuenta como corte por tiempo, no para el cortacircuitos")
        self.assertTrue(self.cliente.disponible())
        self.assertEqual(reloj[0], 1005.0, "se rinde en el plazo, en el primer trozo que ya no cabe")
        self.assertLess(sockets[0].pos, 12 * 1024, "unos 10 KB leídos de los 200")

    def test_el_cuerpo_por_trozos_llega_entero(self):
        """Con una respuesta de verdad: por Content-Length, por trozos «chunked» y hasta el cierre."""
        cuerpo = bytes(range(256)) * 800                      # 200 KB, 13 trozos de 16 KB
        trozos = b"".join(b"%x\r\n%s\r\n" % (len(cuerpo[i:i + 5000]), cuerpo[i:i + 5000])
                          for i in range(0, len(cuerpo), 5000)) + b"0\r\n\r\n"
        respuestas = {
            "Content-Length": b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(cuerpo) + cuerpo,
            "chunked": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + trozos,
            "hasta el cierre": b"HTTP/1.0 200 OK\r\n\r\n" + cuerpo,
            "206 de un Range": b"HTTP/1.1 206 Partial Content\r\nContent-Length: 30001\r\n\r\n" + cuerpo[:30001],
        }
        for nombre, crudo in respuestas.items():
            with self.servidor(side_effect=lambda req, timeout=None: _http(_SocketLento(crudo, trozo=16384))):
                recibido = self.cliente.pedir(self.URL)
            self.assertEqual(recibido, cuerpo[:30001] if nombre.startswith("206") else cuerpo, nombre)
        self.assertEqual(self.cliente.ultimo_codigo, 206)

    def test_un_cuerpo_cortado_antes_de_su_content_length_no_pasa_por_entero(self):
        """read1() no lanza IncompleteRead como read(): devuelve b"" y el cuerpo a medias pasaría."""
        crudo = b"HTTP/1.1 200 OK\r\nContent-Length: 8607\r\n\r\n" + _real("carcasa.htm.gz")[:4000]
        with self.servidor(side_effect=lambda req, timeout=None: _http(_SocketLento(crudo))) as urlopen:
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertIn("IncompleteRead", str(ctx.exception))
        self.assertEqual((urlopen.call_count, self.cliente.fallos_seguidos), (3, 1), "se reintenta y cuenta")

    def test_mas_de_16_mb_cuenta_como_fallo(self):
        """Ninguna página de la relatoría se acerca (la C-355/06 son 3,4 MB)."""
        self.assertEqual(red.TOPE_RESPUESTA, 16 * 1024 * 1024)
        anunciada = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (red.TOPE_RESPUESTA + 1)
        sockets = []

        def servidor(crudo, sin_fin=False):
            def responder(req, timeout=None):
                sockets.append(_SocketLento(crudo, trozo=65536, sin_fin=sin_fin))
                return _http(sockets[-1])
            return responder
        # Lo anuncia el Content-Length: ni se empieza a leer
        with self.servidor(side_effect=servidor(anunciada, sin_fin=True)) as urlopen:
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertIn("16 MB", str(ctx.exception))
        self.assertEqual((urlopen.call_count, self.cliente.fallos_seguidos), (1, 1), "no se reintenta")
        self.assertLess(sockets[0].pos, 70_000)
        # Sin Content-Length, hasta el cierre, y el cierre no llega nunca
        with self.servidor(side_effect=servidor(b"HTTP/1.0 200 OK\r\n\r\n", sin_fin=True)) as urlopen:
            with self.assertRaises(red.ServicioNoDisponible) as ctx:
                self.cliente.pedir(self.URL)
        self.assertIn("16 MB", str(ctx.exception))
        self.assertLess(sockets[1].pos, red.TOPE_RESPUESTA + 2 * red.TROZO, "se deja de leer al pasar el tope")
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(self.cliente.disponible(), "con fallos_para_cortar=2, a la segunda se corta")

    def test_lo_que_solo_tiene_read_se_lee_de_una_vez(self):
        """Los dobles de las pruebas: antes un MagicMock «tenía» read1 y el bucle no acababa nunca."""
        with self.servidor(return_value=self.respuesta(b"de una vez")):
            self.assertEqual(self.cliente.pedir(self.URL), b"de una vez")
        with self.servidor(return_value=_Cuerpo(b"<html>simulada</html>")):
            self.assertEqual(self.cliente.pedir(self.URL), b"<html>simulada</html>")


class TestRelatoriaCaida(unittest.TestCase):
    """El sitio de la Corte cortó la conexión a esta máquina tras ~800 peticiones."""

    def setUp(self):
        cc.cliente.reiniciar()
        cc._canario_ok_hasta = 0.0
        self.tmp = Path(__file__).resolve().parents[1] / ".cache" / "pruebas"
        self.env = mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        cc.cliente.reiniciar()
        cc._canario_ok_hasta = 0.0
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cortado(self):
        import urllib.error
        return mock.patch.object(cc.urllib.request, "urlopen",
                                 side_effect=urllib.error.URLError("EOF en TLS"))

    def test_un_corte_de_conexion_es_una_excepcion_no_un_no_existe(self):
        with self._cortado(), mock.patch.object(cc.time, "sleep"):
            with self.assertRaises(cc.RelatoriaNoDisponible):
                cc.texto_relatoria("T", "999", 2019)

    def test_el_cortacircuitos_deja_de_insistir(self):
        with self._cortado() as urlopen, mock.patch.object(cc.time, "sleep"):
            for n in (997, 998, 999):
                with self.assertRaises(cc.RelatoriaNoDisponible):
                    cc.texto_relatoria("T", str(n), 2019)
            llamadas = urlopen.call_count
            self.assertFalse(cc.relatoria_disponible())
            with self.assertRaises(cc.RelatoriaNoDisponible):
                cc.texto_relatoria("T", "996", 2019)
            self.assertEqual(urlopen.call_count, llamadas, "no debe volver a llamar")

    def test_con_la_relatoria_caida_nunca_sale_no_existe(self):
        indice = cc.Indice(FILAS)
        with self._cortado(), mock.patch.object(cc.time, "sleep"):
            inf = verificacion.verificar("Sentencia T-999 de 2019.", indice=indice)
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.SIN_CONFIRMAR)
        self.assertIn("2010-09-23", h.motivo, "dice hasta dónde llega el índice")

    def test_un_403_de_la_relatoria_nunca_da_no_existe(self):
        """Desde AWS el riesgo es que el sitio vete los rangos de direcciones."""
        antes = cc.cliente.peticiones
        with mock.patch.object(cc.urllib.request, "urlopen",
                               side_effect=_error_http(403)) as urlopen, \
                mock.patch.object(cc.time, "sleep"):
            inf = verificacion.verificar("Sentencia T-999 de 2019.", indice=cc.Indice(FILAS))
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.SIN_CONFIRMAR)
        self.assertIn("403", h.motivo)
        self.assertEqual(urlopen.call_count, 1, "un bloqueo no se reintenta")
        self.assertEqual(cc.cliente.peticiones - antes, 1)
        self.assertEqual(cc.cliente.fallos_seguidos, 1, "cuenta para el cortacircuitos")
        self.assertFalse(cc.ruta_texto("T", "999", 2019).exists(),
                         "un bloqueo no se guarda como si fuera una ausencia")

    def test_si_la_relatoria_cambia_de_formato_no_sale_no_existe(self):
        """El sitio se rehízo en 2026: lo desconocido devuelve la carcasa vacía.

        Si cambiara el esquema de direcciones, TODO devolvería esa carcasa. El
        canario lo detecta porque tampoco sale la T-406/92.
        """
        cc._canario_ok_hasta = 0.0
        with mock.patch.object(cc, "_descargar_pagina", return_value=CARCASA):
            with self.assertRaises(cc.RelatoriaNoDisponible) as ctx:
                cc.texto_relatoria("T", "999", 2019)
            self.assertIn("T-406/92", str(ctx.exception), "lo que lo delata es el canario")
            inf = verificacion.verificar("Sentencia T-998 de 2019.", indice=cc.Indice(FILAS))
        self.assertEqual(inf.hallazgos[0].estado, Estado.SIN_CONFIRMAR)

    def test_con_el_canario_vivo_la_ausencia_vale(self):
        sentencia = ("Sentencia T-406/92 " + "texto " * 600).encode()

        # Antes valía b"<html></html>": cero palabras bastaban. Ahora la
        # ausencia exige la firma de la carcasa (test_una_pagina_vacia_sin_firma...)
        def pagina(url):
            return sentencia if "T-406-92" in url else CARCASA
        cc._canario_ok_hasta = 0.0
        with mock.patch.object(cc, "_descargar_pagina", side_effect=pagina):
            self.assertIsNone(cc.texto_relatoria("T", "999", 2019))

    SENTENCIA_T406 = ("<html><body>Sentencia T-406/92 " + "texto " * 600 + "</body></html>").encode()
    DESAFIO = ("<html><body><h1>Un momento…</h1>"
               + "Estamos comprobando que su conexión es segura antes de continuar. " * 2
               + "</body></html>").encode()

    def test_un_desafio_con_el_canario_vigente_no_da_no_existe(self):
        """Reproducido en la revisión: con el canario memorizado, un desafío se guardaba como ausencia.

        La T-981/26 recibe la carcasa y el canario se pide y vale. Después el
        sitio sirve a todo un desafío con 200 y unas veinte palabras. La
        T-980/26 salía «no existe», se guardaba un día y el espejo de la demo la
        repartía a todos los contenedores.
        """
        sitio = {"desafio": False}
        pedidas: list[str] = []

        def relatoria(req, timeout=None):
            pedidas.append(req.full_url.rsplit("/", 1)[-1])
            if sitio["desafio"]:
                return TestRed.respuesta(self.DESAFIO)
            return TestRed.respuesta(self.SENTENCIA_T406 if "T-406-92" in req.full_url else CARCASA)
        indice = cc.Indice(FILAS)
        with mock.patch.object(red.urllib.request, "urlopen", side_effect=relatoria), \
                mock.patch.object(cc.time, "sleep"):
            inf = verificacion.verificar("Sentencia T-981 de 2026.", indice=indice)
            self.assertEqual(inf.hallazgos[0].estado, Estado.NO_EXISTE, "la ausencia de verdad sigue valiendo")
            self.assertEqual(pedidas, ["T-981-26.htm", "T-406-92.htm"], "con su canario")
            sitio["desafio"] = True
            del pedidas[:]
            inf = verificacion.verificar("Sentencia T-980 de 2026.", indice=indice)
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.SIN_CONFIRMAR)
        self.assertIn("bloqueando", h.motivo)
        self.assertEqual(pedidas, ["T-980-26.htm"],
                         "el canario seguía vigente: lo que la salva es no tomar el desafío por una ausencia")
        self.assertFalse(cc.ruta_texto("T", "980", 2026).exists(), "y no se guarda nada")
        self.assertTrue(cc.ruta_texto("T", "981", 2026).exists(), "la ausencia de verdad sí")

    def test_una_ausencia_con_el_canario_de_hace_mas_de_dos_minutos_lo_vuelve_a_pedir(self):
        """Antes valía una hora: el hueco por el que se colaba el desafío."""
        pedidas: list[str] = []

        def pagina(url):
            pedidas.append(url.rsplit("/", 1)[-1])
            return self.SENTENCIA_T406 if "T-406-92" in url else CARCASA
        self.assertEqual(cc._CANARIO_VALIDEZ, 120)
        with mock.patch.object(cc, "_descargar_pagina", side_effect=pagina):
            cc._canario_ok_hasta = time.time() + cc._CANARIO_VALIDEZ - 121    # comprobado hace 121 s
            self.assertIsNone(cc.texto_relatoria("T", "997", 2019))
            self.assertEqual(pedidas, ["T-997-19.htm", "T-406-92.htm"])
            del pedidas[:]
            cc._canario_ok_hasta = time.time() + cc._CANARIO_VALIDEZ - 60     # hace un minuto: vale
            self.assertIsNone(cc.texto_relatoria("T", "996", 2019))
            self.assertEqual(pedidas, ["T-996-19.htm"])

    def test_una_anulada_sin_texto_en_anuladas_no_es_una_ausencia(self):
        """La nota de nulidad ya dice que la sentencia existe."""
        def pagina(url):
            if "/Anuladas/" in url:
                return CARCASA
            return self.SENTENCIA_T406 if "T-406-92" in url else NOTA_SU163.encode()
        with mock.patch.object(cc, "_descargar_pagina", side_effect=pagina) as descargar:
            with self.assertRaises(cc.RelatoriaNoDisponible) as ctx:
                cc.texto_relatoria("SU", "163", 2023)
            self.assertIn("nota de nulidad", str(ctx.exception))
            self.assertFalse(cc.ruta_texto("SU", "163", 2023).exists())
            # Sin el texto, pero la nota se vio: queda guardada, y no se vuelve a pedir
            pedidas = descargar.call_count
            self.assertIn("Auto 823", _nota_de_nulidad_real("SU", "163", 2023))
            self.assertEqual(descargar.call_count, pedidas)
        self.assertEqual(cc.cliente.raras_seguidas, 0,
                         "una nota y una carcasa en /Anuladas/ son respuestas que se reconocen")


NOTA_SU163 = (
    "SU163-23\n\nNOTA DE RELATORÍA\n\nMediante Auto 823 de 2 de mayo de 2024, la Sala Plena "
    "de la Corte Constitucional declaró la nulidad de oficio de la Sentencia SU-163 de 2023, "
    "por violación del debido proceso al constatar una indebida conformación del juez natural."
    "\n\nREPÚBLICA DE COLOMBIA\n\nCORTE CONSTITUCIONAL\n\nSENTENCIA SU-163 de 2023\n\n"
    "Si desea conocer el texto de la providencia anulada ingrese a la siguiente URL: ..."
)


class TestNulidad(unittest.TestCase):
    """La SU-163/23 fue anulada en 2024 y el índice oficial la sigue listando."""

    def test_lee_la_nota(self):
        nota = cc._nota_de_nulidad_en(NOTA_SU163)
        self.assertIn("Auto 823", nota)
        self.assertNotIn("REPÚBLICA", nota)

    def test_una_sentencia_normal_no_tiene_nota(self):
        self.assertIsNone(cc._nota_de_nulidad_en(SENTENCIA * 20))

    def test_una_nota_que_no_es_de_nulidad_no_cuenta(self):
        self.assertIsNone(cc._nota_de_nulidad_en(
            "NOTA DE RELATORÍA\nMediante Auto 100 de 2020 se corrigió un error de digitación."))

    # La nota real de anonimización de la T-760/08 (auto 522 de 2015), tal como
    # la sirve la relatoría en su página
    NOTA_T760 = ("<p class=MsoNormal><b>NOTA DE RELATORIA:</b> Mediante auto 522 de fecha 11 de noviembre "
                 "del 2015, el cual se anexa en la parte final de la presente providencia, se dispone "
                 "reemplazar con datos ficticios, los nombres y datos de identificación del peticionario "
                 "en el expediente T-1315769</p>")

    def test_una_nota_de_anonimizacion_delante_de_descriptores_con_nulidad_no_es_una_anulacion(self):
        """Revisión 3: la nota de la T-760/08 delante de la C-355/06 daba «La sentencia fue ANULADA».

        Se miraban los 900 caracteres tras la nota, y ahí llegan los descriptores
        de la C-355/06 («…-No origina la nulidad…»). Una anulación guardada no
        caduca, y la relatoría añade notas así años después (la T-760/08 en 2015).
        """
        pagina = _real("C-355-06.htm.gz")
        cuerpo = re.search(rb"<body[^>]*>", pagina, re.IGNORECASE).end()
        con_nota = pagina[:cuerpo] + self.NOTA_T760.encode("cp1252") + pagina[cuerpo:]
        clase, texto, nota = cc._clasificar(con_nota, "C", "355")
        self.assertEqual((clase, nota), ("sentencia", None))
        tras = texto[cc._RE_NOTA.search(texto).end():][:900]
        self.assertIn("nulidad", tras, "la trampa está en los bytes reales: «nulidad» dentro de la ventana de antes")
        self.assertNotIn("nulidad", tras.split("\n\n")[0], "pero no en el párrafo de la nota")

    def test_las_anuladas_reales_se_siguen_detectando(self):
        # La SU-163/23: «NOTA DE RELATORÍA» como encabezado suelto y la nota en el
        # párrafo siguiente; la SU-1073/12: «NOTA DE RELATORIA: Mediante…» en uno
        clase, _, nota = cc._clasificar(_real("SU163-23.htm.gz"), "SU", "163")
        self.assertEqual(clase, "nota")
        self.assertTrue(nota.startswith("Mediante Auto 823 de 2 de mayo de 2024"), nota)
        self.assertNotIn("REPÚBLICA", nota)
        clase, _, nota = cc._clasificar(_real("SU1073-12.htm.gz"), "SU", "1073")
        self.assertEqual(clase, "nota")
        self.assertIn("nulidad parcial", nota)
        self.assertIn("Auto 823", cc._nota_de_nulidad_en(NOTA_SU163), "y la de siempre, en texto")

    def test_una_nota_de_otra_cosa_delante_no_tapa_la_de_nulidad(self):
        """Revisión 4: solo se miraba la primera nota, y la nulidad parcial dejaba de verse.

        La relatoría añade notas años después (la de anonimización de la
        T-760/08 es de 2015): una de esas delante de la de nulidad de la
        SU-1073/12 la hacía salir «verificada».
        """
        pagina = _real("SU1073-12.htm.gz")
        cuerpo = re.search(rb"<body[^>]*>", pagina, re.IGNORECASE).end()
        con_dos = pagina[:cuerpo] + self.NOTA_T760.encode("cp1252") + pagina[cuerpo:]
        clase, texto, nota = cc._clasificar(con_dos, "SU", "1073")
        self.assertEqual(len(cc._RE_NOTA.findall(texto[:6000])), 2, "las dos notas están delante")
        self.assertEqual(clase, "nota")
        self.assertIn("nulidad parcial", nota)
        self.assertTrue(cc.nulidad_parcial(nota))
        # Y la de anonimización sola sigue sin ser una anulación
        self.assertIsNone(cc._nota_de_nulidad_en(cc.documentos._de_html(self.NOTA_T760.encode("cp1252"))))

    def test_el_informe_la_marca(self):
        with mock.patch.object(cc, "nota_de_nulidad", return_value="Mediante Auto 823 de 2024…"):
            inf = verificacion.verificar("Sentencia T-760 de 2008.", indice=cc.Indice(FILAS))
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertTrue(h.observaciones[0].startswith("La sentencia fue ANULADA. Nota"), h.observaciones)

    def test_nulidad_parcial_o_total(self):
        """La SU-1073/12 perdió dos numerales y sigue en lo demás: «ANULADA» a secas exagera."""
        parcial = cc._nota_de_nulidad_en(cc.documentos._de_html(_real("SU1073-12.htm.gz")))
        self.assertTrue(parcial.startswith("Mediante auto 111"), "sin los dos puntos de «NOTA DE RELATORIA:»")
        self.assertTrue(cc.nulidad_parcial(parcial))
        self.assertFalse(cc.nulidad_parcial(cc._nota_de_nulidad_en(NOTA_SU163)))
        self.assertTrue(cc.nulidad_parcial("se declara parcialmente la nulidad de la presente providencia"))
        # Detrás de la frase de la nulidad vienen los descriptores de la sentencia
        self.assertFalse(cc.nulidad_parcial(
            "Mediante Auto 1 de 2024 se declaró la nulidad de la Sentencia T-001 de 2023. "
            "NULIDAD PARCIAL DE SENTENCIA DE TUTELA-Improcedencia"))
        with mock.patch.object(cc, "nota_de_nulidad", return_value=parcial):
            inf = verificacion.verificar("Sentencia T-760 de 2008.", indice=cc.Indice(FILAS))
        self.assertTrue(inf.hallazgos[0].observaciones[0].startswith(
            "La sentencia fue ANULADA PARCIALMENTE. Nota de la relatoría: Mediante auto 111"))

    def test_el_texto_de_la_anulada_se_busca_en_anuladas(self):
        tmp = Path(__file__).resolve().parents[1] / ".cache" / "pruebas_nulidad"
        texto = ("SENTENCIA SU-163 de 2023 " + "texto " * 600).encode()

        def pagina(url):
            return texto if "/Anuladas/" in url else NOTA_SU163.encode()
        with mock.patch.dict(os.environ, {"COTEJO_CACHE": str(tmp)}), \
                mock.patch.object(cc, "_descargar_pagina", side_effect=pagina):
            t = cc.texto_relatoria("SU", "163", 2023)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNotNone(t)
        self.assertIn("SU-163", t)


class TestCacheDeNulidad(unittest.TestCase):
    """Solo se guarda lo que se reconoce; lo que no, lanza.

    El «no anulada» se recuerda 30 días: una página de bloqueo guardada así
    escondería una nulidad durante 30 días. Y devolver None ante lo que no se
    reconoce era lo mismo que «no anulada» para quien llama. Usa la
    nota_de_nulidad de verdad (el módulo la anula) con la relatoría simulada.
    """

    def setUp(self):
        # Las respuestas raras de estas pruebas suman en el cortacircuitos del
        # cliente de verdad: sin reiniciarlo, abrirían el de las pruebas siguientes
        cc.cliente.reiniciar()
        self.tmp = RAIZ / ".cache" / "pruebas_notas"
        self.env = mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        cc.cliente.reiniciar()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def consultar(pagina: bytes, tipo, numero, anio):
        """(nota, peticiones), con la relatoría respondiendo siempre `pagina`."""
        with mock.patch.object(cc.cliente, "pedir", return_value=pagina) as pedir:
            return _nota_de_nulidad_real(tipo, numero, anio), pedir.call_count

    def test_una_pagina_irreconocible_lanza_y_no_se_guarda(self):
        """Antes devolvía None, como «no anulada»: la SU-163/23 salía verificada."""
        bloqueo = ("<html><body><h1>Request blocked</h1>"
                   + "Your access to this site has been limited for security reasons. " * 60
                   + "</body></html>").encode()
        for nombre, pagina, motivo in (("desafío de un cortafuegos", bloqueo, "bloqueando"),
                                       ("carcasa vacía", CARCASA, "no tiene publicada su página")):
            with mock.patch.object(cc.cliente, "pedir", return_value=pagina) as pedir:
                with self.assertRaises(cc.RelatoriaNoDisponible, msg=nombre) as ctx:
                    _nota_de_nulidad_real("SU", "214", 2023)
            self.assertIn(motivo, str(ctx.exception), nombre)
            self.assertEqual(pedir.call_count, 2, f"{nombre}: se prueban las dos direcciones de una SU")
            self.assertFalse(cc.ruta_nota("SU", "214", 2023).exists(), nombre)

    @staticmethod
    def guardar(tipo, numero, anio, nota, dias: float) -> None:
        ruta = cc.ruta_nota(tipo, numero, anio)
        ruta.write_text(json.dumps({"nota": nota}), encoding="utf-8")
        hace = time.time() - dias * 86400
        os.utime(ruta, (hace, hace))

    def test_una_nota_de_nulidad_guardada_no_caduca(self):
        """Una anulación no se deshace: no se vuelve a preguntar, aunque la relatoría no responda."""
        self.guardar("SU", "163", 2023, "Mediante Auto 823 de 2 de mayo de 2024…", dias=400)
        with mock.patch.object(cc.cliente, "pedir",
                               side_effect=red.ServicioNoDisponible("HTTP 403")) as pedir:
            self.assertIn("Auto 823", _nota_de_nulidad_real("SU", "163", 2023))
        pedir.assert_not_called()

    def test_el_no_anulada_si_caduca(self):
        """Una sentencia puede anularse un año después: el «no anulada» se renueva."""
        self.guardar("T", "760", 2008, None, dias=31)
        with mock.patch.object(cc.cliente, "pedir",
                               side_effect=red.ServicioNoDisponible("HTTP 403")) as pedir:
            with self.assertRaises(red.ServicioNoDisponible):
                _nota_de_nulidad_real("T", "760", 2008)
        self.assertEqual(pedir.call_count, 1)
        self.guardar("T", "760", 2008, None, dias=29)
        self.assertEqual(self.consultar(CARCASA, "T", "760", 2008), (None, 0))

    def test_un_bloqueo_no_se_guarda(self):
        with mock.patch.object(cc.cliente, "pedir",
                               side_effect=red.ServicioNoDisponible("HTTP 403")):
            with self.assertRaises(red.ServicioNoDisponible,
                                   msg="quien llama tiene que saber que no se pudo mirar"):
                _nota_de_nulidad_real("T", "760", 2008)
        self.assertFalse(cc.ruta_nota("T", "760", 2008).exists())

    def test_la_nota_se_guarda_aunque_la_pagina_sea_corta(self):
        """La página de una anulada no tiene las 400 palabras de una sentencia."""
        nota, llamadas = self.consultar(NOTA_SU163.encode(), "SU", "163", 2023)
        self.assertIn("Auto 823", nota)
        self.assertEqual(llamadas, 1)
        self.assertTrue(cc.ruta_nota("SU", "163", 2023).exists())
        otra_vez, llamadas = self.consultar(CARCASA, "SU", "163", 2023)
        self.assertEqual((otra_vez, llamadas), (nota, 0), "la segunda sale de la caché")

    def test_la_sentencia_sin_nota_se_guarda(self):
        pagina = ("<html><body>Sentencia T-406/92 " + "texto " * 600 + "</body></html>").encode()
        nota, llamadas = self.consultar(pagina, "T", "406", 1992)
        self.assertIsNone(nota)
        self.assertEqual(llamadas, 1)
        guardado = cc.ruta_nota("T", "406", 1992).read_text(encoding="utf-8")
        self.assertEqual(json.loads(guardado), {"nota": None})

    def test_un_no_anulada_nunca_pisa_una_nota(self):
        """Ahora escriben la nota dos funciones: la que escribe «no anulada» no borra una anulación."""
        self.guardar("T", "406", 1992, "Mediante Auto 1 de 2026 se declaró la nulidad…", dias=1)
        pagina = ("<html><body>Sentencia T-406/92 " + "texto " * 600 + "</body></html>").encode()
        with mock.patch.object(cc.cliente, "pedir", return_value=pagina):
            self.assertIsNotNone(cc.texto_relatoria("T", "406", 1992))
        self.assertIn("Auto 1", json.loads(cc.ruta_nota("T", "406", 1992).read_text(encoding="utf-8"))["nota"])


class TestNulidadSinMirar(unittest.TestCase):
    """Si no se pudo mirar si la sentencia fue anulada, el informe lo dice.

    Reproducido en la revisión: con la relatoría dando 403, cinco sentencias del
    índice salían «verificadas», la SU-163/23 (anulada) entre ellas; con el
    tercer 403 se abría el cortacircuitos y las demás ni se preguntaban.
    """

    FILAS = FILAS + [{"sentencia": "SU-163/23", "fecha_sentencia": "2023-04-26T00:00:00.000",
                      "magistrado_a": "Ponente de Prueba"}]
    INICIO = "No se pudo comprobar si la sentencia fue anulada: "

    def setUp(self):
        cc.cliente.reiniciar()
        self.tmp = RAIZ / ".cache" / "pruebas_nulidad_sin_mirar"
        self.parches = [mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp)}),
                        mock.patch.object(cc, "nota_de_nulidad", _nota_de_nulidad_real),
                        mock.patch.object(cc.time, "sleep")]
        for parche in self.parches:
            parche.start()

    def tearDown(self):
        for parche in reversed(self.parches):
            parche.stop()
        cc.cliente.reiniciar()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def verificar(self, texto: str, **relatoria) -> tuple[dict, int]:
        with mock.patch.object(red.urllib.request, "urlopen", **relatoria) as urlopen:
            inf = verificacion.verificar(texto, indice=cc.Indice(self.FILAS))
        return {h.clave: h for h in inf.hallazgos}, urlopen.call_count

    def test_con_la_relatoria_bloqueada_la_anulada_no_sale_verificada(self):
        hallazgos, llamadas = self.verificar(
            "Sentencia SU-163 de 2023. Sentencia T-406 de 1992. Sentencia T-760 de 2008.",
            side_effect=_error_http(403))
        self.assertEqual(llamadas, 3, "un 403 por sentencia, sin reintentos")
        for clave, h in hallazgos.items():
            self.assertEqual(h.estado, Estado.CON_OBSERVACIONES, clave)
            self.assertEqual(h.observaciones, [
                self.INICIO + "la relatoría de la Corte Constitucional rechazó la petición "
                "(HTTP 403): puede estar bloqueando esta conexión."], clave)
        self.assertFalse(cc.relatoria_disponible(), "el tercer 403 abre el cortacircuitos")

        # Con el cortacircuitos abierto no se pregunta, pero tampoco se calla
        hallazgos, llamadas = self.verificar("Sentencia SU-163 de 2023.", side_effect=_error_http(403))
        h = hallazgos["SU-163/2023"]
        self.assertEqual(llamadas, 0)
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertTrue(h.observaciones[0].startswith(self.INICIO + "la relatoría"), h.observaciones)
        self.assertIn("dejó de responder", h.observaciones[0])
        self.assertEqual(list((self.tmp / "relatoria" / "notas").glob("*")), [], "nada se guarda")

    def test_con_un_desafio_la_anulada_no_sale_verificada(self):
        """La variante que vio la revisión: no se guardaba, pero se devolvía None, como «no anulada»."""
        desafio = ("<html><body><h1>Un momento…</h1>"
                   + "Estamos comprobando que su conexión es segura. " * 3 + "</body></html>").encode()
        hallazgos, llamadas = self.verificar("Sentencia SU-163 de 2023.",
                                             return_value=TestRed.respuesta(desafio))
        h = hallazgos["SU-163/2023"]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertTrue(h.observaciones[0].startswith(self.INICIO), h.observaciones)
        self.assertIn("bloqueando", h.observaciones[0])
        self.assertEqual(llamadas, 2, "las dos direcciones de una SU")
        self.assertFalse(cc.ruta_nota("SU", "163", 2023).exists())


# ================================================ tercera ronda (DISEÑO §1c)
# Un desafío de cortafuegos hecho solo de <script>: cero palabras, como la
# carcasa, pero sin su firma
DESAFIO_SCRIPT = (b"<html><head><title>Un momento</title></head><body>"
                  b"<script src='/desafio.js'></script><script>resolver()</script>"
                  b"<noscript>Active JavaScript para continuar</noscript></body></html>")


class RelatoriaSimulada:
    """La relatoría por debajo de urlopen, con bytes reales donde los hay.

    Cada archivo («C-355-06.htm», «Anuladas/SU1073-12.htm») tiene su página; el
    canario, la T-406/92; lo demás recibe `resto`, por defecto la carcasa real.
    Respeta Range como el servidor (los primeros bytes) y apunta cada petición,
    con «(Range 30 KB)» o «(Range 300 KB)» si lo llevaba, para contar lo que se
    pidió y cuánto. `servidas` suma los bytes que se mandaron.
    """

    def __init__(self, paginas: dict[str, bytes] | None = None, resto: bytes | None = None):
        self.paginas = {"T-406-92.htm": TestRelatoriaCaida.SENTENCIA_T406, **(paginas or {})}
        self.resto = _real("carcasa.htm.gz") if resto is None else resto
        self.pedidas: list[str] = []
        self.servidas = 0

    def __call__(self, req, timeout=None):
        ruta = req.full_url.split("/relatoria/", 1)[1]
        archivo = ruta if ruta.startswith("Anuladas/") else ruta.split("/", 1)[1]
        rango = req.get_header("Range")
        cuerpo = self.paginas.get(archivo, self.resto)
        if rango:
            fin = int(rango.rsplit("-", 1)[1])
            cuerpo = cuerpo[:fin + 1]
            archivo += f" (Range {round((fin + 1) / 1000)} KB)"
        self.pedidas.append(archivo)
        self.servidas += len(cuerpo)
        return TestRed.respuesta(cuerpo)


class BaseRelatoria(unittest.TestCase):
    """Caché temporal, cortacircuitos y canario de cero, la nulidad de verdad y sin pausas."""

    def setUp(self):
        cc.cliente.reiniciar()
        cc._canario_ok_hasta = 0.0
        self.tmp = RAIZ / ".cache" / f"pruebas_{type(self).__name__}"
        self.parches = [mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp)}),
                        mock.patch.object(cc, "nota_de_nulidad", _nota_de_nulidad_real),
                        mock.patch.object(cc.time, "sleep")]
        for parche in self.parches:
            parche.start()

    def tearDown(self):
        for parche in reversed(self.parches):
            parche.stop()
        cc.cliente.reiniciar()
        cc._canario_ok_hasta = 0.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def con(relatoria: RelatoriaSimulada):
        return mock.patch.object(red.urllib.request, "urlopen", side_effect=relatoria)

    def verificar(self, texto: str, relatoria: RelatoriaSimulada, filas=FILAS) -> dict:
        with self.con(relatoria):
            inf = verificacion.verificar(texto, indice=cc.Indice(filas))
        return {h.clave: h for h in inf.hallazgos}

    @staticmethod
    def nota_guardada(tipo, numero, anio) -> dict:
        return json.loads(cc.ruta_nota(tipo, numero, anio).read_text(encoding="utf-8"))


class TestFirmaDeLaCarcasa(BaseRelatoria):
    """Una ausencia solo vale con la carcasa reconocida por su firma (un 404 ya no; DISEÑO §1d)."""

    def test_la_firma(self):
        real = _real("carcasa.htm.gz")
        self.assertEqual(len(real), 8607)
        self.assertTrue(cc._es_carcasa(real))
        self.assertTrue(cc._es_carcasa(CARCASA))
        self.assertEqual(cc.documentos._de_html(DESAFIO_SCRIPT).split(), [],
                         "por las palabras, un desafío de <script> no se distingue de la carcasa")
        alterada = {
            "desafío solo de <script>": DESAFIO_SCRIPT,
            "página vacía sin firma": b"<html></html>",
            "sin cuerpo": b"",
            "la aplicación con algo dentro": real.replace(b"<app-root></app-root>",
                                                          b"<app-root>Acceso denegado</app-root>"),
            "con otro título": real.replace(b"CORTE CONSTITUCIONAL DE COLOMBIA", b"Just a moment..."),
            "sin la aplicación": real.replace(b"<app-root></app-root>", b""),
        }
        for nombre, pagina in alterada.items():
            self.assertFalse(cc._es_carcasa(pagina), nombre)

    def test_un_desafio_solo_de_script_con_el_canario_vigente_no_da_no_existe(self):
        """La revisión lo reprodujo por la Lambda: «no existe», subido a S3 y repartido 24 h."""
        relatoria = RelatoriaSimulada()
        h = self.verificar("Sentencia T-981 de 2026.", relatoria)["T-981/2026"]
        self.assertEqual(h.estado, Estado.NO_EXISTE, "la carcasa real con el canario vivo sí es ausencia")
        self.assertEqual(relatoria.pedidas, ["T-981-26.htm", "T-406-92.htm"])
        with gzip.open(cc.ruta_texto("T", "981", 2026), "rt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "", "y se guarda, como cadena vacía")

        relatoria.resto = DESAFIO_SCRIPT
        del relatoria.pedidas[:]
        h = self.verificar("Sentencia T-980 de 2026.", relatoria)["T-980/2026"]
        self.assertEqual(h.estado, Estado.SIN_CONFIRMAR)
        self.assertIn("bloqueando", h.motivo)
        self.assertEqual(relatoria.pedidas, ["T-980-26.htm"], "el canario seguía vigente: no se volvió a pedir")
        self.assertFalse(cc.ruta_texto("T", "980", 2026).exists(), "el desafío no se guarda")

    def test_ni_un_200_vacio_ni_un_404_son_una_ausencia(self):
        """El 404 antes valía: la relatoría nunca lo da (responde la carcasa), así que viene de otro."""
        cc._canario_ok_hasta = time.time() + 60
        with mock.patch.object(red.urllib.request, "urlopen", return_value=_Cuerpo(b"", 200)):
            with self.assertRaises(cc.RelatoriaNoDisponible) as ctx:
                cc.texto_relatoria("T", "979", 2026)
        self.assertIn("bloqueando", str(ctx.exception))
        self.assertFalse(cc.ruta_texto("T", "979", 2026).exists())
        with mock.patch.object(red.urllib.request, "urlopen", side_effect=_error_http(404)):
            with self.assertRaises(cc.RelatoriaNoDisponible) as ctx:
                cc.texto_relatoria("T", "979", 2026)
            self.assertIn("404", str(ctx.exception), "y lo dice como lo que es")
            with self.assertRaises(cc.RelatoriaNoDisponible):
                _nota_de_nulidad_real("T", "979", 2026)
        self.assertFalse(cc.ruta_texto("T", "979", 2026).exists())
        self.assertFalse(cc.ruta_nota("T", "979", 2026).exists())
        self.assertEqual(cc.cliente.raras_seguidas, 3, "las tres respuestas cuentan como irreconocibles")


class TestDesafioPersistente(BaseRelatoria):
    """Un desafío servido con 200 nunca abría el cortacircuitos (revisión de seguridad y costo).

    pedir() ponía a 0 los fallos con cada 200, y cada consulta de la demo volvía
    a gastar sus peticiones contra una puerta que ya filtraba, hasta agotar el
    presupuesto del día sin verificar nada.
    """

    def test_cinco_consultas_contra_un_desafio(self):
        relatoria = RelatoriaSimulada({"T-406-92.htm": DESAFIO_SCRIPT}, resto=DESAFIO_SCRIPT)
        por_consulta, motivos = [], []
        for n in range(980, 985):
            antes = len(relatoria.pedidas)
            h = self.verificar(f"Sentencia T-{n} de 2026.", relatoria)[f"T-{n}/2026"]
            por_consulta.append(len(relatoria.pedidas) - antes)
            motivos.append(h.motivo)
            self.assertEqual(h.estado, Estado.SIN_CONFIRMAR, n)
            self.assertEqual(cc.relatoria_disponible(), n < 982, f"se abre a la tercera (T-{n})")
        self.assertEqual(por_consulta, [1, 1, 1, 0, 0])
        for motivo in motivos[:3]:
            self.assertIn("bloqueando", motivo)
        for motivo in motivos[3:]:
            self.assertIn("dejó de responder", motivo)
        self.assertEqual(list((self.tmp / "relatoria").glob("*.txt.gz")), [], "nada se guarda")

    def test_la_nulidad_tambien_cuenta(self):
        relatoria = RelatoriaSimulada({"T-406-92.htm": DESAFIO_SCRIPT}, resto=DESAFIO_SCRIPT)
        hallazgos = self.verificar("Sentencia T-406 de 1992. Sentencia T-760 de 2008. "
                                   "Sentencia C-355 de 2006. Sentencia T-760 de 2010.", relatoria)
        self.assertEqual(len(relatoria.pedidas), 3, "la cuarta ya no se pide")
        for clave, h in hallazgos.items():
            self.assertEqual(h.estado, Estado.CON_OBSERVACIONES, clave)
            self.assertTrue(h.observaciones[0].startswith("No se pudo comprobar si la sentencia fue anulada"))
        self.assertIn("dejó de responder", hallazgos["T-760/2010"].observaciones[0])
        self.assertEqual(list((self.tmp / "relatoria" / "notas").glob("*")), [])

    def test_que_la_relatoria_no_tenga_publicada_la_pagina_no_cuenta(self):
        """Carcasa en las dos direcciones de una sentencia del índice: no es culpa del servidor."""
        relatoria = RelatoriaSimulada()
        with self.con(relatoria):
            for _ in range(5):
                with self.assertRaises(cc.RelatoriaNoDisponible) as ctx:
                    _nota_de_nulidad_real("SU", "214", 2023)
                self.assertIn("no tiene publicada su página", str(ctx.exception))
        self.assertEqual(len(relatoria.pedidas), 10, "las dos direcciones de una SU, cada vez")
        self.assertEqual(cc.cliente.raras_seguidas, 0)
        self.assertTrue(cc.relatoria_disponible())


class TestPaginasDeWord(BaseRelatoria):
    """La C-355/06, la SU-214/16 y la SU-1073/12 traen más de 30 KB de <style> antes de </head>.

    Con la ventana de 30 KB de la nulidad les quedaba una palabra: salían «con
    observaciones» por un bloqueo que no existía, no se guardaban nunca y una
    nota de nulidad detrás de la cabecera no se veía. Bytes reales, recortados.
    """

    FILAS = FILAS + [{"sentencia": s, "magistrado_a": "Ponente de Prueba"}
                     for s in ("SU-214/16", "SU-1073/12", "SU-163/23")]

    def test_la_ventana_de_30_kb_se_queda_en_la_cabecera(self):
        for archivo, palabra in (("C-355-06.htm.gz", "C-355-06"), ("SU214-16.htm.gz", "SU214-16"),
                                 ("SU1073-12.htm.gz", "SU1073-12")):
            ventana = _real(archivo)[:cc._BYTES_CABECERA + 1]
            self.assertNotIn(b"</head>", ventana.lower(), archivo)
            self.assertEqual(cc.documentos._de_html(ventana).split(), [palabra], archivo)

    def test_c355_y_su214_salen_verificadas_y_se_guarda_el_no_anulada(self):
        relatoria = RelatoriaSimulada({"C-355-06.htm": _real("C-355-06.htm.gz"),
                                       "SU214-16.htm": _real("SU214-16.htm.gz")})
        texto = "La Sentencia C-355 de 2006 y la SU-214 de 2016."
        hallazgos = self.verificar(texto, relatoria, self.FILAS)
        for clave in ("C-355/2006", "SU-214/2016"):
            self.assertEqual((hallazgos[clave].estado, hallazgos[clave].observaciones),
                             (Estado.VERIFICADA, []), clave)
        self.assertEqual(relatoria.pedidas,
                         ["C-355-06.htm (Range 30 KB)", "C-355-06.htm (Range 300 KB)",
                          "SU214-16.htm (Range 30 KB)", "SU214-16.htm (Range 300 KB)"],
                         "la ventana llena y sin reconocer: se mira la de 300 KB, una vez")
        self.assertEqual(self.nota_guardada("C", "355", 2006), {"nota": None})
        self.assertEqual(self.nota_guardada("SU", "214", 2016), {"nota": None})
        self.assertEqual(cc.cliente.raras_seguidas, 0, "no es un bloqueo")
        del relatoria.pedidas[:]
        self.verificar(texto, relatoria, self.FILAS)
        self.assertEqual(relatoria.pedidas, [], "la segunda vez, de la caché")

    def test_una_nota_detras_de_40_kb_de_style_se_detecta(self):
        estilo = b"p.MsoNormal, li.MsoNormal {margin:0cm; font-size:12.0pt; font-family:'Times New Roman';}\n"
        pagina = (b"<html><head><style>" + estilo * (40_000 // len(estilo) + 1) + b"</style></head><body><p>"
                  + NOTA_SU163.replace("\n\n", "</p><p>").encode() + b"</p></body></html>")
        relatoria = RelatoriaSimulada({"SU163-23.htm": pagina})
        h = self.verificar("Sentencia SU-163 de 2023.", relatoria, self.FILAS)["SU-163/2023"]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertEqual(len(h.observaciones), 1, h.observaciones)
        self.assertTrue(h.observaciones[0].startswith(
            "La sentencia fue ANULADA. Nota de la relatoría: Mediante Auto 823"), h.observaciones)
        self.assertEqual(relatoria.pedidas, ["SU163-23.htm (Range 30 KB)", "SU163-23.htm (Range 300 KB)"])
        self.assertIn("Auto 823", self.nota_guardada("SU", "163", 2023)["nota"])

    # La C-355/06 entera son 3.398.037 bytes (Content-Range de la relatoría); en
    # tests/datos están los primeros 170.000. El resto, relleno con la misma forma
    # de Word: así se ve si alguien pide la página entera.
    @staticmethod
    def c355_entera() -> bytes:
        parrafo = b"<p class=MsoNormal style='text-align:justify'>" + b"palabra " * 120 + b"</p>\r\n"
        recortada = _real("C-355-06.htm.gz")
        return recortada + parrafo * ((3_398_037 - len(recortada)) // len(parrafo) + 1)

    def test_la_nulidad_de_la_c355_se_resuelve_con_la_ventana_de_300_kb(self):
        """Sin pasajes, antes se bajaba entera (3,4 MB) para saber si estaba anulada (revisión 3, contrato y costo)."""
        entera = self.c355_entera()
        self.assertGreater(len(entera), 3_300_000)
        relatoria = RelatoriaSimulada({"C-355-06.htm": entera})
        with self.con(relatoria):
            self.assertIsNone(_nota_de_nulidad_real("C", "355", 2006))
        self.assertEqual(relatoria.pedidas, ["C-355-06.htm (Range 30 KB)", "C-355-06.htm (Range 300 KB)"],
                         "nunca la página entera")
        self.assertEqual(relatoria.servidas, 30_001 + 300_000)
        self.assertEqual(self.nota_guardada("C", "355", 2006), {"nota": None})
        self.assertEqual(cc.cliente.raras_seguidas, 0)
        # La primera ventana no basta (test_la_ventana_de_30_kb_se_queda_en_la_cabecera);
        # la segunda sí, con texto de la sentencia de sobra
        clase, texto, _ = cc._clasificar(entera[:cc._BYTES_VENTANA], "C", "355")
        self.assertEqual(clase, "sentencia")
        self.assertGreater(len(texto.split()), 2000)

    def test_si_la_de_300_kb_tampoco_basta_se_pide_entera(self):
        estilo = b"p.MsoNormal, li.MsoNormal {margin:0cm; font-size:12.0pt; font-family:'Times New Roman';}\n"
        pagina = (b"<html><head><style>" + estilo * (400_000 // len(estilo)) + b"</style></head><body><p>"
                  + ("Sentencia C-355/06 " + "texto " * 600).encode() + b"</p></body></html>")
        relatoria = RelatoriaSimulada({"C-355-06.htm": pagina})
        with self.con(relatoria):
            self.assertIsNone(_nota_de_nulidad_real("C", "355", 2006))
        self.assertEqual(relatoria.pedidas, ["C-355-06.htm (Range 30 KB)", "C-355-06.htm (Range 300 KB)",
                                             "C-355-06.htm"])
        self.assertEqual(self.nota_guardada("C", "355", 2006), {"nota": None})

    def test_texto_relatoria_deja_la_nota_y_nota_de_nulidad_no_pide_nada(self):
        relatoria = RelatoriaSimulada({"C-355-06.htm": _real("C-355-06.htm.gz")})
        with self.con(relatoria):
            self.assertIn("Sentencia C-355/06", cc.texto_relatoria("C", "355", 2006))
            self.assertEqual(relatoria.pedidas, ["C-355-06.htm"])
            self.assertEqual(self.nota_guardada("C", "355", 2006), {"nota": None})
            self.assertIsNone(_nota_de_nulidad_real("C", "355", 2006))
        self.assertEqual(relatoria.pedidas, ["C-355-06.htm"], "la nulidad sale de la caché")


class TestNulidadParcial(BaseRelatoria):
    """La SU-1073/12: la nota dice «nulidad parcial», /Anuladas/ da la carcasa y el texto sigue en su sitio.

    Antes texto_relatoria desechaba ese texto, lanzaba y no guardaba nada: los
    pasajes no se comprobaban nunca, cada consulta volvía a bajar unas 80.000
    palabras y la observación decía «ANULADA» a secas.
    """

    PASAJE = ("la Sala Plena de esta Corporación, el día 13 de octubre de 2010, decidió asumir el "
              "conocimiento de los procesos de la referencia y acumularlos para que fuesen resueltos "
              "en una sola providencia")
    INICIO = "La sentencia fue ANULADA PARCIALMENTE. Nota de la relatoría: Mediante auto 111"

    @staticmethod
    def relatoria(**paginas) -> RelatoriaSimulada:
        # /Anuladas/SU1073-12.htm no está: recibe la carcasa real, como en la relatoría
        return RelatoriaSimulada({"SU1073-12.htm": _real("SU1073-12.htm.gz"), **paginas})

    def test_texto_relatoria_usa_la_original_y_la_guarda(self):
        relatoria = self.relatoria()
        with self.con(relatoria):
            texto = cc.texto_relatoria("SU", "1073", 2012)
            self.assertIn("Sentencia SU1073/12", texto)
            self.assertEqual(relatoria.pedidas, ["SU1073-12.htm", "Anuladas/SU1073-12.htm"])
            self.assertTrue(cc.ruta_texto("SU", "1073", 2012).exists())
            nota = self.nota_guardada("SU", "1073", 2012)["nota"]
            self.assertTrue(nota.startswith("Mediante auto 111"), nota)
            self.assertIn("nulidad parcial", nota)
            self.assertEqual(cc.texto_relatoria("SU", "1073", 2012), texto)
            self.assertEqual(_nota_de_nulidad_real("SU", "1073", 2012), nota)
        self.assertEqual(len(relatoria.pedidas), 2, "lo demás, de la caché")

    def test_el_informe_dice_anulada_parcialmente_y_compara_el_pasaje(self):
        relatoria = self.relatoria()
        h = self.verificar(f"En la Sentencia SU-1073 de 2012 la Corte dijo: “{self.PASAJE}”.",
                           relatoria, TestPaginasDeWord.FILAS)["SU-1073/2012"]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertEqual(len(h.observaciones), 1, h.observaciones)
        self.assertTrue(h.observaciones[0].startswith(self.INICIO), h.observaciones)
        self.assertEqual([p.estado for p in h.pasajes], ["coincide"])
        self.assertEqual(relatoria.pedidas, ["SU1073-12.htm", "Anuladas/SU1073-12.htm"],
                         "con pasajes, el texto primero; la nulidad sale de lo que dejó escrito")

    def test_sin_pasajes_la_nulidad_la_ve_detras_de_la_cabecera(self):
        relatoria = self.relatoria()
        h = self.verificar("La Sentencia SU-1073 de 2012.", relatoria, TestPaginasDeWord.FILAS)["SU-1073/2012"]
        self.assertEqual(len(h.observaciones), 1, h.observaciones)
        self.assertTrue(h.observaciones[0].startswith(self.INICIO), h.observaciones)
        self.assertEqual(relatoria.pedidas, ["SU1073-12.htm (Range 30 KB)", "SU1073-12.htm (Range 300 KB)"])

    def test_un_desafio_en_anuladas_no_da_por_buena_la_original(self):
        """Así no se sabe si /Anuladas/ tiene la sentencia: no se compara contra lo que quizá es solo la nota."""
        relatoria = self.relatoria(**{"Anuladas/SU1073-12.htm": DESAFIO_SCRIPT})
        with self.con(relatoria):
            with self.assertRaises(cc.RelatoriaNoDisponible) as ctx:
                cc.texto_relatoria("SU", "1073", 2012)
        self.assertIn("bloqueando", str(ctx.exception))
        self.assertFalse(cc.ruta_texto("SU", "1073", 2012).exists())
        self.assertIn("nulidad parcial", self.nota_guardada("SU", "1073", 2012)["nota"], "la nota sí se vio")


def _filas_reales(hasta: str | None = None) -> list[dict]:
    """Las 794 filas reales de 2025 y 2026 del índice (llega al 2026-08-31), recortadas a `hasta` si se pide."""
    filas = json.loads(_real("indice_2025_2026.json.gz"))
    return [f for f in filas if not hasta or (f.get("fecha_sentencia") or "")[:10] <= hasta]


MOTIVO_RECIENTE = ("no figura en el índice oficial, que llega hasta el {fin}, ni en la relatoría; puede ser "
                   "una sentencia reciente que aún no se ha publicado. Compruébela antes de radicar")


class TestQuizaReciente(unittest.TestCase):
    """Sentencias reales y recientes que aún no están en ninguna de las dos fuentes (DISEÑO §1d).

    El índice va con semanas de retraso y la relatoría tarda semanas o meses en
    publicar algunas C y SU. Con el índice recortado al 12-08, la C-273/26 (del
    27-08) y la SU-261/26 (del 13-08), reales y sin página en la relatoría,
    salían «no existe» (reproducido en la revisión 3). Con el índice real de
    2025-2026 y «hoy» fijo: nada depende de la fecha del sistema.
    """

    @classmethod
    def setUpClass(cls):
        cls.indice = cc.Indice(_filas_reales())
        cls.recortado = cc.Indice(_filas_reales("2026-08-12"))

    def test_los_datos(self):
        self.assertEqual((len(self.indice), self.indice.fecha_mas_reciente), (794, "2026-08-31"))
        self.assertEqual(self.recortado.fecha_mas_reciente, "2026-08-12")
        for tipo, numero in (("C", "273"), ("SU", "261")):
            self.assertIsNotNone(self.indice.buscar(tipo, numero, 2026))
            self.assertIsNone(self.recortado.buscar(tipo, numero, 2026))

    def test_una_c_y_una_su_de_las_ultimas_semanas_dan_motivo(self):
        for tipo, numero in (("C", "273"), ("SU", "261"), ("T", "280")):
            self.assertEqual(self.recortado.quiza_reciente(tipo, numero, 2026, hoy=HOY),
                             MOTIVO_RECIENTE.format(fin="2026-08-12"), f"{tipo}-{numero}/26")
        # Con la numeración de su tipo solo (la regla literal), la SU-261 no cabía:
        # una SU cada diez días. T, C y SU comparten la serie desde 2023
        numeros = [int(re.match(r"\d+", p.numero).group()) for p in self.indice._por_id.values() if p.anio == 2026]
        self.assertEqual(len(numeros), len(set(numeros)), "ningún número se repite entre tipos en 2026")

    def test_un_numero_imposible_para_la_fecha_sigue_siendo_no_existe(self):
        for indice in (self.indice, self.recortado):
            self.assertIsNone(indice.quiza_reciente("T", "981", 2026, hoy=HOY), "la T-981/26 inventada")
        # El límite: umbral asentado (el 201, lo fechado hasta el 02-07) + 2 × ritmo
        # (1,42 al día, el de 2025, mayor que el de 2026) × 110 días hasta hoy + 20
        self.assertIsNotNone(self.indice.quiza_reciente("T", "534", 2026, hoy=HOY))
        self.assertIsNone(self.indice.quiza_reciente("T", "535", 2026, hoy=HOY))
        self.assertIsNone(self.indice.quiza_reciente("T", "1", 2027, hoy=HOY), "un año que aún no empieza")

    def test_un_tramo_ya_asentado_no_cambia(self):
        """Lo fechado hasta 60 días antes del final del índice: ahí, que falte un número significa algo."""
        for tipo in ("T", "C", "SU"):
            self.assertIsNone(self.indice.buscar(tipo, "45", 2026))
            self.assertIsNone(self.indice.quiza_reciente(tipo, "45", 2026, hoy=HOY), f"el hueco {tipo}-045/26")
            self.assertIsNone(self.indice.quiza_reciente(tipo, "417", 2025, hoy=HOY), f"el hueco {tipo}-417/25")
            self.assertIsNone(self.indice.quiza_reciente(tipo, "100", 2024, hoy=HOY), "un año pasado")
            self.assertIsNone(self.indice.quiza_reciente(tipo, "999", 2019, hoy=HOY))
        # El hueco 274, en cambio, es del 27-08 (entre la C-273 y la T-275): reciente
        self.assertIsNotNone(self.indice.quiza_reciente("T", "274", 2026, hoy=HOY))

    def test_hoy_se_inyecta_y_sin_el_es_cc_hoy(self):
        """No depende del reloj del sistema: «hoy» se pasa, o sale de cc._hoy (fijo en estas pruebas)."""
        self.assertIsNone(self.indice.quiza_reciente("T", "500", 2026, hoy=dt.date(2026, 9, 10)))
        self.assertIsNotNone(self.indice.quiza_reciente("T", "500", 2026, hoy=dt.date(2026, 12, 31)))
        self.assertIsNotNone(self.indice.quiza_reciente("T", "500", 2026), "con el HOY del módulo, 20-10")
        with mock.patch.object(cc, "_hoy", return_value=dt.date(2026, 9, 10)) as hoy:
            self.assertIsNone(self.indice.quiza_reciente("T", "500", 2026))
        hoy.assert_called_once()
        # A principios del año siguiente: el ritmo, el del año anterior
        enero = dt.date(2027, 1, 20)
        self.assertIsNotNone(self.indice.quiza_reciente("T", "10", 2027, hoy=enero))
        self.assertIsNone(self.indice.quiza_reciente("T", "100", 2027, hoy=enero))

    def test_un_anio_cerrado_no_sigue_creciendo(self):
        """Revisión 4: los días de un año ya cerrado se contaban hasta hoy.

        Ninguna sentencia dictada después del 31-12 lleva su número: con el
        índice de hoy, la T-700/25 inventada salía «no encontrada». 2025 acaba en
        la T-529, del 19-12; quedan los 20 números de margen.
        """
        self.assertIsNotNone(self.indice.buscar("T", "529", 2025))
        self.assertIsNotNone(self.indice.quiza_reciente("T", "540", 2025, hoy=HOY), "dentro del margen")
        for numero in ("550", "600", "700", "868"):
            self.assertIsNone(self.indice.quiza_reciente("T", numero, 2025, hoy=HOY), f"T-{numero}/25")
        # Con el índice parado en diciembre, el 31 sigue contando
        diciembre = cc.Indice(_filas_reales("2025-12-10"))
        self.assertIsNotNone(diciembre.quiza_reciente("T", "529", 2025, hoy=dt.date(2026, 2, 1)),
                             "la T-529/25 real, del 19-12, fuera de un índice que llega al 10-12")

    def test_un_indice_parado_no_da_falsos_no_existe(self):
        """Revisión 4: con un índice que deja de actualizarse, el ritmo caía al mínimo de 0,1.

        Si la descarga semanal falla, la Lambda conserva el índice anterior. Con
        el índice parado el 20-01-2025, el año siguiente casi no tenía filas, y la
        C-038/26 y la SU-040/26, reales (del 26-02), salían «no existe».
        """
        parado = cc.Indice(_filas_reales("2025-01-20"))
        self.assertIsNotNone(self.indice.buscar("C", "38", 2026))
        for tipo, numero in (("C", "38"), ("C", "48"), ("SU", "40")):
            self.assertIsNotNone(parado.quiza_reciente(tipo, numero, 2026, hoy=dt.date(2026, 3, 31)),
                                 f"{tipo}-{numero}/26")

    def test_un_indice_sin_fechas_no_asienta_nada(self):
        self.assertIn("no dice hasta qué fecha llega",
                      cc.Indice([{"sentencia": "T-001/26"}]).quiza_reciente("T", "981", 2026, hoy=HOY))


class TestRecientesEnLaRelatoria(BaseRelatoria):
    """La regla de §1d por verificar(), con la relatoría por debajo de urlopen y la carcasa real."""

    def test_una_c_y_una_su_recientes_sin_pagina_salen_no_encontradas(self):
        relatoria = RelatoriaSimulada()
        texto = ("La Sentencia C-273 de 2026, la SU-261 de 2026, la T-981 de 2026 y la T-999 de 2019. "
                 "Y la T-045 de 2026.")
        hallazgos = self.verificar(texto, relatoria, _filas_reales("2026-08-12"))
        motivo = MOTIVO_RECIENTE.format(fin="2026-08-12")
        self.assertEqual({c: (h.estado, h.motivo) for c, h in hallazgos.items()}, {
            "C-273/2026": (Estado.NO_ENCONTRADA, motivo),
            "SU-261/2026": (Estado.NO_ENCONTRADA, motivo),
            "T-981/2026": (Estado.NO_EXISTE, "no figura en el índice oficial de la Corte ni en su relatoría"),
            "T-999/2019": (Estado.NO_EXISTE, "no figura en el índice oficial de la Corte ni en su relatoría"),
            "T-045/2026": (Estado.NO_EXISTE, "no figura en el índice oficial de la Corte ni en su relatoría"),
        })
        self.assertEqual(relatoria.pedidas, ["C-273-26.htm", "T-406-92.htm", "SU261-26.htm", "SU-261-26.htm",
                                             "T-981-26.htm", "T-999-19.htm", "T-045-26.htm"],
                         "la carcasa en todas, con su canario")
        inf = verificacion.Informe("escrito", list(hallazgos.values()), 0)
        self.assertEqual({h.clave for h in inf.problemas}, set(hallazgos), "las no encontradas también piden atención")

    def test_un_404_con_cuerpo_de_cortafuegos_da_sin_confirmar_y_cuenta_para_el_cortacircuitos(self):
        """Revisión 3: un 404 era una ausencia, y pedir() tira su cuerpo; con el canario recién pedido, «no existe»."""
        bloqueo = (b"<html><body><h1>Request blocked</h1><p>Access denied by the web application "
                   b"firewall.</p></body></html>")
        pedidas = []

        def relatoria(req, timeout=None):
            pedidas.append(req.full_url.rsplit("/", 1)[-1])
            if "T-406-92" in req.full_url:
                return TestRed.respuesta(TestRelatoriaCaida.SENTENCIA_T406)
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(bloqueo))
        texto = "Sentencia C-273 de 2026. Sentencia T-981 de 2026. Sentencia T-982 de 2026. Sentencia T-983 de 2026."
        with mock.patch.object(red.urllib.request, "urlopen", side_effect=relatoria):
            inf = verificacion.verificar(texto, indice=cc.Indice(_filas_reales("2026-08-12")))
        hallazgos = {h.clave: h for h in inf.hallazgos}
        self.assertEqual({c: h.estado for c, h in hallazgos.items()},
                         {c: Estado.SIN_CONFIRMAR for c in ("C-273/2026", "T-981/2026", "T-982/2026", "T-983/2026")})
        for clave in ("C-273/2026", "T-981/2026", "T-982/2026"):
            self.assertIn("respondió 404", hallazgos[clave].motivo, clave)
        self.assertIn("dejó de responder", hallazgos["T-983/2026"].motivo, "tres 404 seguidos abren el cortacircuitos")
        self.assertEqual(pedidas, ["C-273-26.htm", "T-981-26.htm", "T-982-26.htm"], "y la cuarta ya no se pide")
        self.assertFalse(cc.relatoria_disponible())
        self.assertEqual(list((self.tmp / "relatoria").glob("*.txt.gz")), [], "nada se guarda como ausencia")


class TestBanco(unittest.TestCase):
    """Los bancos de evaluación hacen cientos de peticiones: una respuesta rara no puede tirar la corrida."""

    FILAS = [{"sentencia": f"T-{n}/96", "fecha_sentencia": "1996-03-01T00:00:00.000"} for n in (100, 101, 102)]

    @classmethod
    def setUpClass(cls):
        cls.banco = importlib.import_module("evaluacion.banco")

    def evaluar(self) -> tuple[str, int]:
        salida = io.StringIO()
        with mock.patch.object(cc, "texto_relatoria",
                               side_effect=cc.RelatoriaNoDisponible("la relatoría respondió otra página")) as texto, \
                contextlib.redirect_stdout(salida):
            self.banco.evaluar_pasajes(cc.Indice(self.FILAS), 1, random.Random(1))
        return salida.getvalue(), texto.call_count

    def test_evaluar_pasajes_cuenta_lo_irreconocible_como_descarte(self):
        salida, llamadas = self.evaluar()
        self.assertEqual(llamadas, 3)
        self.assertIn("T sin respuesta reconocible de la relatoría: 3", salida)

    def test_con_el_cortacircuitos_abierto_corta_el_muestreo(self):
        with mock.patch.object(cc, "relatoria_disponible", return_value=False):
            salida, llamadas = self.evaluar()
        self.assertEqual(llamadas, 1, "las que quedan no se descartan una a una")
        self.assertIn("se corta el muestreo con 0 de 1 sentencias", salida)
        self.assertIn("T sin respuesta reconocible de la relatoría: 1", salida)


# ========================================================== Corte Suprema
def _fila(titulo, doctor="Dra. Margarita Cabello Blanco", clase="SENTENCIA"):
    return {"title": titulo, "onlinePath": f"/var/www/html/Index/CIVIL/2018/{doctor}/{titulo}",
            "doctor": doctor, "ano": 2018, "autoSentencia": clase}


class TestExtraccionCSJ(unittest.TestCase):
    def test_codigos_verificables(self):
        c = extraccion.extraer("Así lo dijo la Corte en SC5686-2018.")[0]
        self.assertEqual(c.tribunal, extraccion.CORTE_SUPREMA)
        self.assertTrue(c.verificable)
        self.assertEqual(c.clave, "SC5686-2018")

    def test_formas(self):
        self.assertEqual(claves("CSJ SL1234 de 2019"), ["SL1234-2019"])
        self.assertEqual(claves("la STC-15558-2021"), ["STC15558-2021"])
        self.assertEqual(claves("AC 739 – 2026"), ["AC739-2026"])

    def test_al_en_mayusculas_no_es_un_auto_laboral(self):
        self.assertEqual(claves("CONFORME AL 15 DE 2019 SE DECIDIÓ"), [])
        self.assertEqual(claves("el AL15 de 2019"), ["AL15-2019"])

    def test_ceros_delante_son_la_misma(self):
        inf = extraccion.extraer("SP022-2019 y luego SP22-2019")
        self.assertEqual({c.clave for c in inf}, {"SP22-2019"})

    def test_no_se_confunde_con_la_constitucional(self):
        """«SC-5686» no es la C-5686 de la Constitucional."""
        self.assertEqual(claves("la SC-5686-2018"), ["SC5686-2018"])


class TestFuenteCSJ(unittest.TestCase):
    def test_titulos(self):
        self.assertTrue(cs._patron_titulo("SP", "22", 2019).match("SP022-2019(47228).doc"))
        self.assertTrue(cs._patron_titulo("SC", "5686", 2018).match("SC5686-2018 [2004-00042-01].pdf"))
        self.assertFalse(cs._patron_titulo("SC", "5686", 2018).match("SC56860-2018.pdf"))
        self.assertFalse(cs._patron_titulo("SC", "5686", 2018).match("STC5686-2018.pdf"))
        self.assertFalse(cs._patron_titulo("SC", "5686", 2018).match("AC2333-2024 [SC5686-2018].pdf"),
                         "el código tiene que abrir el título, no aparecer dentro")

    def test_ponente(self):
        self.assertEqual(cs.limpiar_ponente("Dra. Margarita Cabello Blanco"), "Margarita Cabello Blanco")
        self.assertEqual(cs.limpiar_ponente("Dr.Omar Angel Mejia Amador"), "Omar Angel Mejia Amador")
        self.assertIsNone(cs.limpiar_ponente("Conjueces"))
        self.assertIsNone(cs.limpiar_ponente(None))

    def test_se_prefiere_word_a_pdf(self):
        rutas = ["a.pdf", "a.doc", "a.docx"]
        self.assertEqual(sorted(rutas, key=cs._preferencia), ["a.docx", "a.doc", "a.pdf"])

    def test_variantes_incluyen_la_forma_con_espacios(self):
        """El AP7408-2015 se encabeza «AP – 7408 - 2015»: solo lo encuentra la frase."""
        self.assertIn("AP 7408 2015", cs._variantes("AP", "7408", 2015))
        self.assertIn("SP022-2019", cs._variantes("SP", "22", 2019))

    def test_cobertura(self):
        import datetime as dt
        hoy = dt.date(2026, 9, 23)
        self.assertTrue(cs.anio_cubierto(2025, hoy))
        self.assertFalse(cs.anio_cubierto(2026, hoy))

    def test_la_providencia_puede_estar_en_la_segunda_pagina(self):
        """La búsqueda es de texto completo: las que la citan salen antes."""
        paginas = {0: ([_fila(f"AC{i}-2019.doc") for i in range(10)], 12),
                   10: ([_fila("SC5686-2018 [2004].doc"), _fila("AC1-2020.doc")], 12)}
        with mock.patch.object(cs, "_buscar_pagina", side_effect=lambda t, s, a, i: paginas[i]):
            b = cs._consultar("SC", "5686", 2018)
        self.assertIsNotNone(b.providencia)
        self.assertEqual(b.providencia.id, "SC5686-2018")
        self.assertEqual(b.providencia.ponentes, ["Margarita Cabello Blanco"])

    def test_la_muy_citada_se_busca_por_el_final(self):
        """El AP2118-2020 salió en la posición 149 de 155: detrás de las que la citan."""
        def pagina(t, s, a, inicio):
            if inicio == 150:
                return [_fila("AP3269-2020.doc"), _fila("AP2118-2020(34017).doc"),
                        _fila("AP2118-2020(34017).pdf")], 153
            return [_fila(f"AP{3000 + inicio + i}-2020.doc") for i in range(10)], 153
        with mock.patch.object(cs, "_buscar_pagina", side_effect=pagina) as llamadas:
            b = cs._consultar("AP", "2118", 2020)
        self.assertEqual(b.providencia.id, "AP2118-2020")
        self.assertLessEqual(llamadas.call_count, 2, "primera página y la última")

    def test_version_partida_entre_dos_paginas(self):
        def pagina(t, s, a, inicio):
            if inicio == 10:
                return [_fila("SC5686-2018.pdf")] + [_fila(f"AC{i}-2019.doc") for i in range(9)], 30
            if inicio == 0:
                return [_fila(f"AC{i}-2019.doc") for i in range(9)] + [_fila("SC5686-2018.doc")], 30
            return [_fila(f"AC{i}-2020.doc") for i in range(10)], 30
        with mock.patch.object(cs, "_buscar_pagina", side_effect=pagina):
            b = cs._consultar("SC", "5686", 2018)
        self.assertEqual(sorted(d.rsplit(".", 1)[-1] for d in b.providencia.documentos), ["doc", "pdf"])

    def test_demasiados_resultados_no_es_ausencia(self):
        llena = ([_fila(f"AC{i}-2019.doc") for i in range(10)], 500)
        with mock.patch.object(cs, "_buscar_pagina", return_value=llena):
            b = cs._consultar("SC", "5686", 2018)
        self.assertIsNone(b.providencia)
        self.assertFalse(b.completa)

    def test_dos_ponentes_registrados(self):
        """El STP11082-2021 figura bajo dos magistrados."""
        filas = [_fila("STP11082-2021.doc", "Dr. Diego Eugenio Corredor Beltrán"),
                 _fila("STP11082-2021.doc", "Dr. Eugenio Fernández Carlier"),
                 _fila("STP11082-2021.pdf", None)]
        p = cs._armar("STP", 2021, "TUTELAS", filas)
        self.assertEqual(len(p.ponentes), 2)
        self.assertTrue(verificacion.mismo_ponente("Eugenio Fernández Carlier", p.ponente))


class TestVerificacionCSJ(unittest.TestCase):
    PROV = cs.ProvidenciaCSJ(id="SC5686-2018", prefijo="SC", numero="5686", anio=2018,
                             sala="CIVIL", clase="SENTENCIA",
                             ponentes=["Margarita Cabello Blanco"], documentos=["x.doc"])

    def verificar(self, texto, busqueda, texto_prov=None, fiable=True):
        buscar = (mock.patch.object(cs, "buscar", side_effect=busqueda)
                  if isinstance(busqueda, Exception)
                  else mock.patch.object(cs, "buscar", return_value=busqueda))
        with buscar, mock.patch.object(cs, "texto", return_value=texto_prov), \
                mock.patch.object(cs, "buscador_fiable", return_value=fiable):
            return verificacion.verificar(texto, indice=cc.Indice(FILAS))

    def test_existente(self):
        inf = self.verificar("La SC5686-2018 (M.P. Margarita Cabello Blanco).",
                             cs.Busqueda(self.PROV, True, 2))
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.VERIFICADA)
        self.assertEqual(h.fuente, "corte_suprema")

    def test_ponente_equivocado(self):
        inf = self.verificar("La SC5686-2018 (M.P. Luis Armando Tolosa Villabona).",
                             cs.Busqueda(self.PROV, True, 2))
        self.assertEqual(inf.hallazgos[0].estado, Estado.CON_OBSERVACIONES)

    def test_una_ausencia_nunca_es_no_existe(self):
        """Al buscador le falta una de cada veinte providencias reales."""
        inf = self.verificar("Según la SC9999-2018.", cs.Busqueda(None, True, 0))
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.NO_ENCONTRADA)
        self.assertIn(h, inf.problemas, "pide atención aunque no afirme que no existe")

    def test_buscador_caido(self):
        inf = self.verificar("Según la SC9999-2018.", red.ServicioNoDisponible("sin respuesta"))
        self.assertEqual(inf.hallazgos[0].estado, Estado.SIN_CONFIRMAR)

    def test_buscador_que_no_encuentra_ni_el_canario(self):
        inf = self.verificar("Según la SC9999-2018.", cs.Busqueda(None, True, 0), fiable=False)
        self.assertEqual(inf.hallazgos[0].estado, Estado.SIN_CONFIRMAR)

    def test_busqueda_incompleta(self):
        inf = self.verificar("Según la SC9999-2018.", cs.Busqueda(None, False, 500))
        self.assertEqual(inf.hallazgos[0].estado, Estado.SIN_CONFIRMAR)

    def test_pasaje_alterado(self):
        texto = ("En la SC5686-2018 se afirmó: “El juez, en el Estado social de derecho no es "
                 "un portador de la visión institucional del interés general.”")
        inf = self.verificar(texto, cs.Busqueda(self.PROV, True, 2),
                             texto_prov=cs.Texto(SENTENCIA, "x.doc"))
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.CON_OBSERVACIONES)
        self.assertIn("«no»", h.observaciones[0])

    def test_con_texto_de_pdf_no_se_acusa_una_diferencia(self):
        """El OCR de un PDF puede cambiar palabras: solo se cree lo que coincide."""
        texto = ("En la SC5686-2018 se afirmó: “El juez, en el Estado social de derecho no es "
                 "un portador de la visión institucional del interés general.”")
        inf = self.verificar(texto, cs.Busqueda(self.PROV, True, 2),
                             texto_prov=cs.Texto(SENTENCIA, "x.pdf"))
        h = inf.hallazgos[0]
        self.assertEqual(h.pasajes, [])
        self.assertIn("PDF", h.observaciones[0])
        self.assertNotIn("«no»", " ".join(h.observaciones))


class TestCacheCSJ(unittest.TestCase):
    """La demo replica la caché en S3 y la pide por estas rutas: tienen que ser las que se usan."""

    PROV = TestVerificacionCSJ.PROV

    def setUp(self):
        self.tmp = RAIZ / ".cache" / "pruebas_csj"
        self.env = mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_buscar_guarda_en_ruta_busqueda(self):
        with mock.patch.object(cs, "_consultar",
                               return_value=cs.Busqueda(self.PROV, True, 2)) as consultar:
            cs.buscar("sc", "05686", 2018)
            b = cs.buscar("SC", "5686", 2018)
        self.assertTrue(cs.ruta_busqueda("SC", "5686", 2018).exists())
        self.assertEqual(consultar.call_count, 1, "la segunda sale de la caché")
        self.assertEqual(b.providencia.id, "SC5686-2018")
        self.assertEqual(cs.ruta_busqueda("sc", "05686", 2018), cs.ruta_busqueda("SC", 5686, 2018),
                         "«SC05686» y «SC5686» son la misma providencia")

    def test_texto_guarda_en_ruta_texto(self):
        respuesta = {"getContentSearch": {"contentText": "<p>" + "palabra " * 80 + "</p>"}}
        with mock.patch.object(cs, "_graphql", return_value=respuesta) as graphql:
            cs.texto(self.PROV)
            t = cs.texto(self.PROV)
        self.assertEqual(graphql.call_count, 1, "la segunda sale de la caché")
        self.assertEqual(t.documento, "x.doc")
        with gzip.open(cs.ruta_texto("SC5686-2018"), "rt", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["documento"], "x.doc")


class TestPlazoDentroDeUnaProvidencia(unittest.TestCase):
    """El plazo corta también lo que está en curso, no solo lo que falta por empezar.

    Una providencia de la Corte Suprema son hasta veinte búsquedas: mirando el
    reloj solo entre providencias, una empezada cerca del final pasaba del
    tiempo de la Lambda. Con un reloj simulado, que solo avanza cuando el
    servidor responde.
    """

    def setUp(self):
        self.tmp = RAIZ / ".cache" / "pruebas_plazo"
        self.reloj = 1000.0
        self.parches = [mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp)}),
                        mock.patch.object(red.time, "monotonic", lambda: self.reloj),
                        mock.patch.object(red.time, "sleep")]
        for parche in self.parches:
            parche.start()
        self.clientes = [(c, c.tiempo_espera) for c in (cc.cliente, cs.cliente)]
        for c, _ in self.clientes:
            c.reiniciar()
            c.tiempo_espera = 60

    def tearDown(self):
        for parche in reversed(self.parches):
            parche.stop()
        for c, espera in self.clientes:
            c.reiniciar()
            c.tiempo_espera = espera
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_una_busqueda_de_la_suprema_que_se_queda_sin_plazo(self):
        """Cada página tarda 20 s y el plazo es de 30: la tercera ya no se pide."""
        esperas = []

        def buscador(req, timeout=None):
            esperas.append(timeout)
            self.reloj += 20
            pagina = {"searchResults": [_fila(f"AC{i}-2019.doc") for i in range(10)], "numOfResults": 40}
            return TestRed.respuesta(json.dumps({"data": {"getSearchResult": pagina}}).encode())
        antes = cs.cliente.cortes_por_tiempo
        with mock.patch.object(red.urllib.request, "urlopen", side_effect=buscador):
            inf = verificacion.verificar("Según la SC9999-2018. Sentencia T-760 de 2008.",
                                         indice=cc.Indice(FILAS), hasta=1030.0)
        por_clave = {h.clave: h for h in inf.hallazgos}
        for clave in ("SC9999-2018", "T-760/2008"):
            self.assertEqual(por_clave[clave].estado, Estado.SIN_CONFIRMAR, clave)
            self.assertEqual(por_clave[clave].motivo, verificacion.MOTIVO_SIN_TIEMPO, clave)
        self.assertTrue(inf.cortado_por_tiempo)
        self.assertEqual(esperas, [30.0, 10.0], "cada intento espera solo lo que queda del plazo")
        self.assertEqual(cs.cliente.cortes_por_tiempo - antes, 1)
        self.assertEqual(cs.cliente.fallos_seguidos, 0, "quedarse sin plazo no es culpa del servidor")
        self.assertIsNone(cs.cliente.hasta, "al acabar, el cliente vuelve a no tener plazo")
        self.assertFalse(cs.ruta_busqueda("SC", "9999", 2018).exists(), "una búsqueda a medias no se guarda")

    def test_si_la_nulidad_no_cabe_en_el_plazo(self):
        """Está en el índice, pero lo que faltaba por mirar no se miró: sale como las que no se alcanzaron."""
        antes = cc.cliente.cortes_por_tiempo
        with mock.patch.object(cc, "nota_de_nulidad", _nota_de_nulidad_real), \
                mock.patch.object(red.urllib.request, "urlopen") as urlopen:
            inf = verificacion.verificar("Sentencia T-760 de 2008.", indice=cc.Indice(FILAS),
                                         hasta=self.reloj + red.MINIMO_INTENTO / 2)
        h = inf.hallazgos[0]
        self.assertEqual((h.estado, h.motivo), (Estado.SIN_CONFIRMAR, verificacion.MOTIVO_SIN_TIEMPO))
        self.assertEqual(h.observaciones, [], "no un «no se pudo comprobar»: se acabó el tiempo")
        self.assertTrue(inf.cortado_por_tiempo)
        urlopen.assert_not_called()
        self.assertEqual(cc.cliente.cortes_por_tiempo - antes, 1)


# =============================================================== informe
class TestInforme(unittest.TestCase):
    def test_html_escapa_y_ordena(self):
        indice = cc.Indice(FILAS)
        with mock.patch.object(cc, "texto_relatoria", _relatoria_simulada({"T-999/19": None})):
            inf = verificacion.verificar(
                "T-760 de 2008 y T-999 de 2019 <script>", documento="a<b>.docx", indice=indice)
        html = informe.a_html(inf)
        self.assertIn("a&lt;b&gt;.docx", html)
        self.assertNotIn("<script>", html)
        self.assertLess(html.index("T-999"), html.index("T-760"),
                        "lo que no existe va antes que lo verificado")

    def test_json(self):
        indice = cc.Indice(FILAS)
        inf = verificacion.verificar("T-760 de 2008", indice=indice, relatoria=False)
        d = informe.a_json(inf)
        self.assertEqual(d["resumen"]["verificada"], 1)
        self.assertEqual(d["hallazgos"][0]["providencia"]["id"], "T-760/08")


# ===================================================== integridad del fuente
class TestIntegridadDelFuente(unittest.TestCase):
    """Un \\b que se come el shell compila y deja el regex muerto sin avisar."""

    def test_no_hay_caracteres_de_control(self):
        malos = []
        for ruta in RAIZ.rglob("*.py"):
            if any(p in {".git", "__pycache__", ".venv", ".cache"} for p in ruta.parts):
                continue
            datos = ruta.read_bytes()
            for b in set(datos):
                if b < 0x20 and b not in (0x09, 0x0A, 0x0D):
                    malos.append(f"{ruta.name}: 0x{b:02X}")
        self.assertEqual(malos, [])


# ========================================================= contra la fuente
@unittest.skipUnless(os.environ.get("COTEJO_RED") == "1", "sin red (COTEJO_RED=1)")
class TestFuenteReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.indice = cc.Indice.cargar()

    def test_indice_completo(self):
        self.assertGreater(len(self.indice), 29000)

    def test_sentencias_conocidas(self):
        for tipo, num, anio in [("T", "406", 1992), ("C", "355", 2006),
                                ("T", "760", 2008), ("SU", "111", 1997)]:
            self.assertIsNotNone(self.indice.buscar(tipo, num, anio), f"{tipo}-{num}/{anio}")

    def test_relatoria(self):
        self.assertIsNotNone(cc.texto_relatoria("T", "406", 1992))
        self.assertIsNone(cc.texto_relatoria("T", "999", 2019))

    def test_sentencia_anulada(self):
        self.assertIn("823", _nota_de_nulidad_real("SU", "163", 2023))
        self.assertIsNone(_nota_de_nulidad_real("T", "406", 1992))

    def test_corte_suprema(self):
        b = cs.buscar("SC", "5686", 2018)
        self.assertEqual(b.providencia.ponentes, ["Margarita Cabello Blanco"])
        self.assertEqual(cs.buscar("SP", "22", 2019).providencia.id, "SP022-2019")
        self.assertIsNotNone(cs.buscar("AP", "7408", 2015).providencia,
                             "se encabeza «AP – 7408 - 2015»")
        b = cs.buscar("SC", "99999", 2018)
        self.assertIsNone(b.providencia)
        self.assertTrue(b.completa)
        self.assertTrue(cs.buscador_fiable())

    def test_texto_corte_suprema(self):
        t = cs.texto(cs.buscar("SC", "5686", 2018).providencia)
        self.assertFalse(t.de_pdf)
        self.assertIn("Magistrada ponente", t.texto[:500])


if __name__ == "__main__":
    unittest.main(verbosity=2)
