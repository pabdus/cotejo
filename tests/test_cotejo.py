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

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from cotejo import comparacion, extraccion, informe, verificacion   # noqa: E402
from cotejo.fuentes import corte_constitucional as cc               # noqa: E402
from cotejo.verificacion import Estado                               # noqa: E402


def claves(texto: str) -> list[str]:
    return [c.clave for c in extraccion.extraer(texto)]


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
        self.assertTrue(all(not c.verificable for c in citas))
        self.assertEqual({c.clase for c in citas}, {"codigo", "radicado", "auto", "mencion"})

    def test_ponente(self):
        c = extraccion.extraer("Sentencia T-406 de 1992 (M.P. Ciro Angarita Barón).")[0]
        self.assertEqual(c.ponente, "Ciro Angarita Barón")
        c = extraccion.extraer("T-760 de 2008, Magistrado Ponente: Manuel José Cepeda Espinosa")[0]
        self.assertEqual(c.ponente, "Manuel José Cepeda Espinosa")

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
        inf = self.verificar("Sentencia T-760 de 2009.", textos={"T-760/09": None})
        h = inf.hallazgos[0]
        self.assertEqual(h.estado, Estado.NO_EXISTE)
        self.assertEqual([p.id for p in h.posibles], ["T-760/08", "T-760/10"])

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
        inf = self.verificar("Según el AC739-2026.")
        self.assertEqual(inf.hallazgos[0].estado, Estado.NO_VERIFICABLE)

    def test_citas_repetidas_se_agrupan(self):
        inf = self.verificar("La T-760 de 2008 y otra vez la Sentencia T-760/08.")
        self.assertEqual(len(inf.hallazgos), 1)
        self.assertEqual(len(inf.hallazgos[0].citas), 2)


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

    def test_reconoce_la_pagina_de_no_encontrado(self):
        """La relatoría responde 200 también para lo que no existe."""
        self.assertFalse(cc._parece_la_sentencia("CORTE CONSTITUCIONAL " * 50, "T", "999"))
        self.assertTrue(cc._parece_la_sentencia(
            "Sentencia T-406/92 " + "texto " * 600, "T", "406"))
        self.assertFalse(cc._parece_la_sentencia(
            "Sentencia T-407/92 " + "texto " * 600, "T", "406"),
            "una página larga que no menciona el número no cuenta")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
