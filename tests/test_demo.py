"""Pruebas de la demo pública (demo/): la Lambda, su cuota y su espejo en S3.

Ejecutar con:  python -m unittest discover -s tests -v

Ninguna toca la red ni AWS. Las fuentes se simulan por debajo de red.Cliente
(urlopen), para que corra el núcleo de verdad con sus cachés, y S3 y DynamoDB
con clientes falsos. Un guardia en urlopen hace fallar cualquier prueba que
intente salir a la red sin haberla simulado.

Lo que más se vigila, porque es lo que la demo añade al riesgo de Cotejo:
  - que un bloqueo de la Corte a AWS acabe en «no existe» o en la caché,
  - que el espejo deje sin bajar algo que el núcleo lee (y la demo vuelva a
    pedírselo a la Corte en cada consulta, sin que nada falle a la vista),
  - que el espejo reparta a otros contenedores una ausencia, o pise en S3 una
    respuesta positiva (un texto, una nota de nulidad) con una negativa,
  - que la cuota se pueda saltar, o que deje rastro de la IP,
  - que el texto o la IP acaben en los registros.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import email.message
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RAIZ), str(RAIZ / "demo")]

# lambda_function fija COTEJO_CACHE=/tmp/cotejo-cache al importarse, que es lo
# que necesita en AWS. En el proceso de pruebas eso cambiaría la caché de todas
# las demás: con COTEJO_RED=1, TestFuenteReal descargaría el índice entero.
_CACHE_PREVIA = os.environ.get("COTEJO_CACHE")
import cuota  # noqa: E402
import espejo  # noqa: E402
import lambda_function as lf  # noqa: E402
import exportar_ejemplos  # noqa: E402     (también fijan COTEJO_CACHE, a .cache)
import local  # noqa: E402

if _CACHE_PREVIA is None:
    os.environ.pop("COTEJO_CACHE", None)
else:
    os.environ["COTEJO_CACHE"] = _CACHE_PREVIA

from cotejo import extraccion, verificacion  # noqa: E402
from cotejo.fuentes import corte_constitucional as cc  # noqa: E402
from cotejo.fuentes import corte_suprema as cs  # noqa: E402
from cotejo.fuentes import red  # noqa: E402

COLOMBIA = dt.timezone(dt.timedelta(hours=-5))
BUCKET = "cotejo-demo-pruebas"
IP = "203.0.113.7"

# «Hoy» fijo, como en test_cotejo: que una ausencia sea «no existe» o «no
# encontrada» depende de la fecha (cc.Indice.quiza_reciente, DISEÑO §1d), y con
# el reloj del sistema la T-999/2019 de estas pruebas cambiaría de estado con los
# años sin que cambiara nada del código.
HOY = dt.date(2026, 10, 20)
_parche_hoy = mock.patch.object(cc, "_hoy", return_value=HOY)


def setUpModule():
    _parche_hoy.start()


def tearDownModule():
    _parche_hoy.stop()

# ------------------------------------------------------------ guardia de red
_URLOPEN_REAL = urllib.request.urlopen
_salidas_a_la_red: list[str] = []


def _sin_red(req, *args, **kwargs):
    _salidas_a_la_red.append(getattr(req, "full_url", str(req)))
    raise urllib.error.URLError("las pruebas de la demo no salen a la red")


# ------------------------------------------------------------- las fuentes
FILAS = [
    {"sentencia": "T-406/92", "fecha_sentencia": "1992-06-05T00:00:00.000",
     "magistrado_a": "Ciro Angarita Barón"},
    {"sentencia": "SU-111/97", "fecha_sentencia": "1997-03-06T00:00:00.000",
     "magistrado_a": "Eduardo Cifuentes Muñoz"},
    {"sentencia": "T-760/08", "fecha_sentencia": "2008-07-31T00:00:00.000",
     "magistrado_a": "Manuel José Cepeda Espinosa"},
]

# Con la firma de la carcasa real (DISEÑO §1c): sin <app-root> y el título, una
# página sin palabras ya no se lee como ausencia
CARCASA = (b"<html><head><title>CORTE CONSTITUCIONAL DE COLOMBIA</title></head>"
           b"<body><app-root></app-root></body></html>")
# El desafío de la revisión: un cortafuegos de JavaScript, sin una sola palabra
# tras _de_html (descarta head, script y noscript), igual que la carcasa
DESAFIO_SCRIPT = (b"<html><head><title>Un momento</title><script>window.awsWafCookieDomainList=[];"
                  b"challenge()</script></head><body><script src='/challenge.js'></script>"
                  b"<noscript>Habilite JavaScript para continuar</noscript></body></html>")
DATOS = Path(__file__).resolve().parent / "datos"


def _dato(nombre: str) -> bytes:
    """Bytes reales de la relatoría guardados en tests/datos (ver test_cotejo)."""
    return gzip.decompress((DATOS / nombre).read_bytes())
RELLENO = " ".join(["consideraciones"] * 450)
PASAJE_T406 = ("De lo dicho se deriva la idea de que el juez, en el Estado social de derecho "
               "también es un portador de la visión institucional del interés general")
PASAJE_SC5686 = ("estas llamadas actividades peligrosas llevan en sí la posibilidad inminente de "
                 "causar con su ejercicio daños a terceros")
PASAJE_SP022 = ("la prueba indiciaria exige que el hecho indicador esté plenamente demostrado "
                "dentro del proceso penal")

# Por nombre de archivo en la relatoría: las SU van sin guion
SENTENCIAS = {
    "T-406-92": f"Sentencia T-406/92. {PASAJE_T406}. {RELLENO}",
    "SU111-97": f"Sentencia SU-111/97. {RELLENO}",
    "T-760-08": f"Sentencia T-760/08. {RELLENO}",
}


def _doc_csj(titulo: str, sala: str, anio: int, doctor: str, texto: str) -> dict:
    return {"title": titulo, "onlinePath": f"/var/www/html/Index/{sala}/{anio}/{doctor}/{titulo}",
            "doctor": doctor, "ano": anio, "autoSentencia": "SENTENCIA", "texto": texto}


# El buscador de la Corte Suprema busca en el TEXTO (ESTADO.md, 11): la SP022-2019
# se escribe con ceros, y buscar «SP22-2019» no la encuentra
DOCUMENTOS_CSJ = [
    _doc_csj("SC5686-2018 [2004-00042-01].doc", "CIVIL", 2018, "Dra. Margarita Cabello Blanco",
             f"SC5686-2018. {PASAJE_SC5686}, aunque se desarrollen con suma prudencia. {RELLENO}"),
    _doc_csj("SP022-2019(47228).doc", "PENAL", 2019, "Dr. Eyder Patiño Cabrera",
             f"SP022-2019. Como se ha dicho, {PASAJE_SP022}. {RELLENO}"),
]


class _Respuesta:
    def __init__(self, cuerpo: bytes):
        self.cuerpo = cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.cuerpo


def _cadena_tras(campo: str, consulta: str) -> str:
    return json.loads(re.search(campo + r': ("(?:[^"\\]|\\.)*")', consulta).group(1))


class FuentesSimuladas:
    """La relatoría y el buscador de la Corte Suprema, en lugar de urlopen.

    Por debajo de red.Cliente: así corren de verdad la pausa, el contador de
    peticiones, el tratamiento del 403 y las cachés del núcleo.

    `desafio`: lo que un cortafuegos sirve con 200 a toda la relatoría, en vez
    de la página pedida. `publicadas`: sentencias de más, solo para esta
    instancia (lo que la relatoría publicó después, visto por otro contenedor).
    """

    def __init__(self, bloquear_relatoria: bool = False, desafio: bytes | None = None,
                 publicadas: dict[str, str] | None = None):
        self.bloquear_relatoria = bloquear_relatoria
        self.desafio = desafio
        self.publicadas = {**SENTENCIAS, **(publicadas or {})}
        self.relatoria: list[str] = []
        self.suprema: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if "corteconstitucional.gov.co" in url:
            self.relatoria.append(url)
            if self.bloquear_relatoria:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if self.desafio is not None:
                return _Respuesta(self.desafio)
            texto = self.publicadas.get(url.rsplit("/", 1)[-1].removesuffix(".htm"))
            return _Respuesta(f"<html><body><p>{texto}</p></body></html>".encode() if texto else CARCASA)
        if url == cs.API:
            self.suprema.append(url)
            consulta = json.loads(req.data)["query"]
            if "getSearchResult" in consulta:
                buscado = _cadena_tras("query", consulta)
                inicio = int(re.search(r"start: (\d+)", consulta).group(1))
                hallados = [{k: v for k, v in d.items() if k != "texto"}
                            for d in DOCUMENTOS_CSJ if buscado in d["texto"]]
                datos = {"getSearchResult": {"searchResults": hallados[inicio:inicio + 10],
                                             "numOfResults": len(hallados)}}
            else:
                ruta = _cadena_tras("id", consulta)
                texto = next(d["texto"] for d in DOCUMENTOS_CSJ if d["onlinePath"] == ruta)
                datos = {"getContentSearch": {"contentText": f"<p>{texto}</p>"}}
            return _Respuesta(json.dumps({"data": datos}).encode())
        raise AssertionError(f"dirección no simulada: {url}")


def _fuentes(simuladas: FuentesSimuladas):
    return mock.patch.object(red.urllib.request, "urlopen", simuladas)


# ------------------------------------------------------------------ AWS falso
class ErrorAWS(Exception):
    """Como botocore.exceptions.ClientError: el código va en response['Error']['Code']."""

    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.response = {"Error": {"Code": codigo, "Message": codigo}}


def _ahora_s3(hace: float = 0.0) -> dt.datetime:
    # S3 da LastModified con precisión de segundos
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=hace)).replace(microsecond=0)


class S3Falso:
    def __init__(self, falla: bool = False):
        self.objetos: dict[str, tuple[bytes, dt.datetime]] = {}
        self.bajadas: list[str] = []
        self.subidas: list[str] = []
        self.falla = falla
        # Clave → código de error que da S3 al pedirla (AccessDenied, InternalError…)
        self.errores: dict[str, str] = {}

    def poner(self, clave: str, datos: bytes, hace: float = 0.0) -> None:
        self.objetos[clave] = (datos, _ahora_s3(hace))

    def get_object(self, *, Bucket, Key):
        assert Bucket == BUCKET
        if self.falla:
            raise ConnectionError("S3 no responde")      # como EndpointConnectionError
        self.bajadas.append(Key)
        if Key in self.errores:
            raise ErrorAWS(self.errores[Key])
        if Key not in self.objetos:
            raise ErrorAWS("NoSuchKey")
        datos, fecha = self.objetos[Key]
        return {"Body": io.BytesIO(datos), "LastModified": fecha}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        assert Bucket == BUCKET
        if self.falla:
            raise ConnectionError("S3 no responde")
        self.subidas.append(Key)
        self.objetos[Key] = (bytes(Body), _ahora_s3())
        return {}


class TablaQueFalla:
    def update_item(self, **kwargs):
        raise ErrorAWS("ProvisionedThroughputExceededException")


class TablaQueFallaAlSumar(cuota.TablaEnMemoria):
    """Concede la cuota, pero falla al sumar las peticiones medidas."""

    def update_item(self, **kwargs):
        if kwargs["UpdateExpression"] == cuota.SUMA:
            raise ErrorAWS("ProvisionedThroughputExceededException")
        return super().update_item(**kwargs)


class Contexto:
    """El context de Lambda: lo único que usa la función es el tiempo que queda."""

    def __init__(self, ms: int, orden: list[str] | None = None):
        self.ms = ms
        self.orden = orden if orden is not None else []

    def get_remaining_time_in_millis(self) -> int:
        self.orden.append("contexto")
        return self.ms


# ------------------------------------------------------------------- eventos
def evento(metodo: str = "POST", ruta: str = "/verificar", cuerpo=None, ip: str = IP,
           en_base64: bool = False) -> dict:
    """Un evento de Function URL (payload 2.0)."""
    if isinstance(cuerpo, dict):
        cuerpo = json.dumps(cuerpo, ensure_ascii=False)
    if en_base64 and cuerpo is not None:
        cuerpo = base64.b64encode(cuerpo.encode("utf-8")).decode("ascii")
    return {"version": "2.0", "routeKey": "$default", "rawPath": ruta, "rawQueryString": "",
            "headers": {"content-type": "application/json"},
            "requestContext": {"http": {"method": metodo, "path": ruta, "protocol": "HTTP/1.1",
                                        "sourceIp": ip, "userAgent": "pruebas"}},
            "body": cuerpo, "isBase64Encoded": en_base64}


def cuerpo(respuesta: dict) -> dict:
    return json.loads(respuesta["body"])


def estados(respuesta: dict) -> dict[str, str]:
    return {h["clave"]: h["estado"] for h in cuerpo(respuesta)["informe"]["hallazgos"]}


# ===================================================================== base
class BaseDemo(unittest.TestCase):
    """Caché en un directorio temporal, AWS falso y la red vigilada."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cotejo-demo-"))
        self.cache = self.tmp / "cache"
        self.entorno = mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.cache)})
        self.entorno.start()
        for variable in ("BUCKET", "TABLA_CUOTA", "SAL_VISITANTE", "MAX_CONSULTAS_IP",
                         "MAX_CONSULTAS_DIA", "MAX_CARACTERES", "PLAZO_SEGUNDOS", "MAX_CITAS",
                         "MAX_PROVIDENCIAS", "MAX_PETICIONES_DIA", "AWS_LAMBDA_FUNCTION_NAME"):
            os.environ.pop(variable, None)
        self.sueno = mock.patch.object(red.time, "sleep")
        self.sueno.start()
        del _salidas_a_la_red[:]
        self.guardia = mock.patch.object(urllib.request, "urlopen", _sin_red)
        self.guardia.start()
        # Los registros de la Lambda van a stdout (CloudWatch); aquí, a un búfer
        self.registros = contextlib.redirect_stdout(io.StringIO())
        self.registros.__enter__()
        # configurar() les cambia el tiempo de espera y los reintentos: se
        # devuelven al terminar, para no alterar las pruebas del núcleo
        self.clientes = [(c, c.tiempo_espera, c.reintentos) for c in (cc.cliente, cs.cliente)]
        self.reiniciar_contenedor()
        self.s3 = S3Falso()
        self.tabla = cuota.TablaEnMemoria()
        self.configurar()

    def tearDown(self):
        self.registros.__exit__(None, None, None)
        self.guardia.stop()
        self.sueno.stop()
        self.entorno.stop()
        for cliente, espera, reintentos in self.clientes:
            cliente.tiempo_espera, cliente.reintentos = espera, reintentos
        self.reiniciar_contenedor()
        lf._listo = False
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(_salidas_a_la_red, [], "una prueba salió a la red sin simularla")

    @staticmethod
    def reiniciar_contenedor() -> None:
        """Lo que se pierde cuando Lambda recicla el contenedor: cortacircuitos y canarios."""
        cc.cliente.reiniciar()
        cs.cliente.reiniciar()
        cc._canario_ok_hasta = 0.0
        cs._canario_ok_hasta = 0.0

    def configurar(self, s3="falso", ddb="falso", indice="filas") -> None:
        """Como el arranque en frío. s3=None o ddb=None: sin ese servicio (y sin su nombre,
        o configurar() iría a crear el cliente de verdad con boto3)."""
        s3 = self.s3 if isinstance(s3, str) else s3
        ddb = self.tabla if isinstance(ddb, str) else ddb
        lf.configurar(s3=s3, ddb=ddb, bucket=BUCKET if s3 is not None else "",
                      tabla="cotejo-demo-cuota" if ddb is not None else "", sal="sal-de-prueba",
                      indice=cc.Indice(FILAS) if isinstance(indice, str) else indice)

    def verificar(self, texto: str, ip: str = IP, fuentes: FuentesSimuladas | None = None,
                  contexto: Contexto | None = None) -> dict:
        with _fuentes(fuentes or FuentesSimuladas()):
            return lf.lambda_handler(evento(cuerpo={"texto": texto}, ip=ip), contexto)

    def hoy(self) -> str:
        return cuota.fecha_colombia(time.time())

    def contador(self, clave: str) -> int | None:
        elemento = self.tabla.elementos.get(clave)
        return int(elemento["n"]["N"]) if elemento else None

    def escritos(self) -> set[str]:
        """Lo que hay en la caché en disco, por ruta relativa."""
        return set(espejo.foto(self.cache))


# ============================================================== rutas básicas
class TestRutas(BaseDemo):
    def test_salud(self):
        r = lf.lambda_handler(evento("GET", "/"), None)
        self.assertEqual(r["statusCode"], 200)
        d = cuerpo(r)
        self.assertEqual((d["servicio"], d["estado"], d["providencias"]), ("cotejo-demo", "ok", 3))
        self.assertEqual(d["indice_hasta"], "2008-07-31")
        self.assertEqual((d["max_caracteres"], d["max_consultas_ip"]), (5000, 3))
        self.assertIn("indice_actualizado", d)

    def test_cabeceras_de_seguridad(self):
        h = lf.lambda_handler(evento("GET", "/"), None)["headers"]
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertTrue(h["Content-Type"].startswith("application/json"))
        self.assertFalse(any(k.lower().startswith("access-control") for k in h),
                         "el CORS lo pone la Function URL: duplicado, el navegador lo rechaza")

    def test_otra_ruta(self):
        for metodo, ruta in (("GET", "/verificar"), ("POST", "/"), ("GET", "/x"), ("OPTIONS", "/")):
            r = lf.lambda_handler(evento(metodo, ruta, cuerpo={"texto": "Sentencia T-406 de 1992."}), None)
            self.assertEqual((r["statusCode"], cuerpo(r)), (404, {"error": "ruta"}), f"{metodo} {ruta}")
        self.assertEqual(self.tabla.elementos, {})

    def test_forma_del_200(self):
        """La misma que DISEÑO §2: la página y exportar_ejemplos.py dependen de ella."""
        r = self.verificar("La Sentencia T-406 de 1992 y el Auto 092 de 2008.")
        self.assertEqual(r["statusCode"], 200)
        d = cuerpo(r)
        self.assertEqual(set(d), {"informe", "segundos", "restantes", "indice", "peticiones",
                                  "cortado_por_tiempo", "cortado_por_tope"})
        self.assertEqual(d["indice"], {"providencias": 3, "hasta": "2008-07-31"})
        self.assertEqual(set(d["peticiones"]), {"relatoria", "corte_suprema"})
        self.assertIsInstance(d["segundos"], float)
        self.assertEqual(d["restantes"], 2)
        self.assertIs(d["cortado_por_tiempo"], False)
        self.assertIs(d["cortado_por_tope"], False)
        self.assertEqual(d["informe"]["documento"], "Texto pegado")
        self.assertEqual(estados(r), {"T-406/1992": "verificada", "Auto 092 de 2008": "no_verificable"})


# ================================================================ validación
class TestValidacion(BaseDemo):
    def post(self, **kw) -> dict:
        return lf.lambda_handler(evento(**kw), None)

    def assertError(self, r: dict, http: int, codigo: str) -> None:
        self.assertEqual(r["statusCode"], http)
        d = cuerpo(r)
        self.assertEqual(d["error"], codigo)
        self.assertTrue(d["mensaje"])

    def test_vacio(self):
        self.assertError(self.post(cuerpo={"texto": ""}), 422, "texto_vacio")
        self.assertError(self.post(cuerpo={"texto": "  \n\t "}), 422, "texto_vacio")
        self.assertError(self.post(cuerpo={}), 422, "texto_vacio")

    def test_largo(self):
        r = self.post(cuerpo={"texto": "a" * 5001})
        self.assertError(r, 413, "texto_largo")
        self.assertIn("5.000", cuerpo(r)["mensaje"])
        self.assertEqual(self.post(cuerpo={"texto": "a" * 5000})["statusCode"], 200,
                         "5.000 justos sí")

    def test_json_roto(self):
        for malo in ('{"texto": "sin cerrar', "", "[1, 2]", '"texto"', '{"texto": 5}',
                     "[" * 20000 + "]" * 20000):
            self.assertError(self.post(cuerpo=malo), 400, "mal_formada")

    def test_cuerpo_enorme(self):
        """Se rechaza antes de decodificar: una Function URL admite hasta 6 MB."""
        enorme = json.dumps({"texto": "a" * 4000, "relleno": "x" * 70_000})
        self.assertError(self.post(cuerpo=enorme), 400, "mal_formada")
        # Y en bytes, no en caracteres: 40.000 «ñ» son 80 KB de UTF-8
        self.assertError(self.post(cuerpo=json.dumps({"texto": "a", "x": "ñ" * 40_000},
                                                     ensure_ascii=False)), 400, "mal_formada")

    def test_base64(self):
        r = self.post(cuerpo={"texto": "Auto 092 de 2008, nada que consultar."}, en_base64=True)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(estados(r), {"Auto 092 de 2008": "no_verificable"})
        roto = evento(cuerpo="esto no es base64 !!!")
        roto["isBase64Encoded"] = True
        self.assertError(lf.lambda_handler(roto, None), 400, "mal_formada")
        no_utf8 = evento(cuerpo=base64.b64encode(b"\xff\xfe\x00").decode())
        no_utf8["isBase64Encoded"] = True
        self.assertError(lf.lambda_handler(no_utf8, None), 400, "mal_formada")

    def test_ninguna_validacion_gasta_cuota_ni_pregunta(self):
        self.post(cuerpo={"texto": ""})
        self.post(cuerpo={"texto": "Sentencia T-406 de 1992. " + "a" * 5000})
        self.post(cuerpo="{roto")
        self.assertEqual(self.tabla.elementos, {})
        self.assertEqual(self.s3.bajadas, [])


# ===================================================================== cuota
class TestCuota(BaseDemo):
    TEXTO = "Sentencia T-760 de 2008."

    def test_sin_citas_consultables_no_gasta(self):
        """Lo no verificable se lista gratis: no le pide nada a la Corte."""
        texto = ("Según el Consejo de Estado, en sentencia del 3 de marzo de 2020, el Auto 092 de "
                 "2008, la STH45-2020 y la sentencia T-760 sin más, rad. 11001-02-03-000-2019-01234-00.")
        fuentes = FuentesSimuladas()
        for _ in range(5):
            r = self.verificar(texto, fuentes=fuentes)
            self.assertEqual(r["statusCode"], 200)
        d = cuerpo(r)
        self.assertTrue(d["informe"]["hallazgos"])
        self.assertEqual(set(estados(r).values()), {"no_verificable"})
        self.assertIsNone(d["restantes"])
        self.assertEqual(d["peticiones"], {"relatoria": 0, "corte_suprema": 0})
        self.assertEqual(self.tabla.elementos, {})
        self.assertEqual((fuentes.relatoria, fuentes.suprema, self.s3.bajadas), ([], [], []))
        self.assertEqual(self.verificar("Un texto sin ninguna cita.")["statusCode"], 200)
        self.assertEqual(self.tabla.elementos, {})

    def test_tres_por_ip_y_la_cuarta_429(self):
        restantes = [cuerpo(self.verificar(self.TEXTO))["restantes"] for _ in range(3)]
        self.assertEqual(restantes, [2, 1, 0])
        fuentes = FuentesSimuladas()
        r = self.verificar(self.TEXTO, fuentes=fuentes)
        self.assertEqual(r["statusCode"], 429)
        self.assertEqual(cuerpo(r)["error"], "limite_ip")
        espera = int(r["headers"]["Retry-After"])
        self.assertTrue(0 < espera <= 86400)
        self.assertAlmostEqual(espera, cuota.segundos_hasta_medianoche(time.time()), delta=5)
        self.assertEqual(fuentes.relatoria, [], "la cuarta no le pide nada a la Corte")
        hoy = cuota.fecha_colombia(time.time())
        self.assertEqual(self.tabla.elementos[f"dia#{hoy}"]["n"]["N"], "3",
                         "la IP va primero: la que insiste no se come el cupo de los demás")

    def test_otra_ip_va_aparte(self):
        for _ in range(3):
            self.verificar(self.TEXTO, ip="198.51.100.1")
        self.assertEqual(self.verificar(self.TEXTO, ip="198.51.100.1")["statusCode"], 429)
        r = self.verificar(self.TEXTO, ip="198.51.100.2")
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(cuerpo(r)["restantes"], 2)

    def test_tope_global(self):
        os.environ["MAX_CONSULTAS_DIA"] = "2"
        self.configurar()
        self.assertEqual(self.verificar(self.TEXTO, ip="198.51.100.1")["statusCode"], 200)
        self.assertEqual(self.verificar(self.TEXTO, ip="198.51.100.2")["statusCode"], 200)
        r = self.verificar(self.TEXTO, ip="198.51.100.3")
        self.assertEqual(r["statusCode"], 429)
        d = cuerpo(r)
        self.assertEqual(d["error"], "limite_global")
        # Cuál de los dos topes (DISEÑO §2): la página explica cada uno
        self.assertEqual(d["limite"], "consultas")
        self.assertEqual(set(d), {"error", "mensaje", "limite"})
        self.assertIn("2 verificaciones", d["mensaje"], "el tope que hay, no un número fijo")
        self.assertIn("Retry-After", r["headers"])

    def test_la_tabla_no_guarda_la_ip(self):
        r = self.verificar(self.TEXTO, ip="198.51.100.77")
        claves = sorted(self.tabla.elementos)
        self.assertEqual(len(claves), 3)
        hoy = self.hoy()
        self.assertEqual(claves[0], f"dia#{hoy}")
        self.assertRegex(claves[1], rf"^ip#{hoy}#[0-9a-f]{{32}}$")
        self.assertEqual(claves[2], f"pet#{hoy}")
        self.assertEqual(self.contador(f"pet#{hoy}"), sum(cuerpo(r)["peticiones"].values()),
                         "el presupuesto lleva las peticiones medidas")
        volcado = json.dumps(self.tabla.elementos)
        for trozo in ("198.51.100.77", "198.51.100", "51.100.77"):
            self.assertNotIn(trozo, volcado)

    def test_ttl_de_dos_dias_como_mucho(self):
        self.verificar(self.TEXTO)
        ahora = time.time()
        for elemento in self.tabla.elementos.values():
            restante = int(elemento["expira"]["N"]) - ahora
            self.assertLessEqual(restante, 2 * 86400)
            self.assertGreater(restante, 2 * 86400 - 60)

    def test_la_tabla_caida_no_deja_pasar(self):
        """Sin límite no se sirve: una caída de DynamoDB no abre la puerta a la Corte."""
        self.configurar(ddb=TablaQueFalla())
        fuentes = FuentesSimuladas()
        r = self.verificar(self.TEXTO, fuentes=fuentes)
        self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (500, "interno"))
        self.assertEqual(fuentes.relatoria, [])

    def test_sin_tabla_no_se_sirve(self):
        self.configurar(ddb=None)
        r = self.verificar(self.TEXTO)
        self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (500, "interno"))

    # ---------------------------------------------------- IPv6 (revisión)
    def test_cien_ipv6_de_una_64_son_un_solo_visitante(self):
        """La reproducción del revisor: 100 direcciones de una /64 se comían las 100 del día."""
        ips = [f"2001:db8:1234:5678:{i:x}:{i * 7919 % 65536:x}:{i * 104729 % 65536:x}:{i:x}"
               for i in range(1, 101)]
        self.assertEqual(len(set(ips)), 100)
        fuentes = FuentesSimuladas()
        respuestas = [self.verificar(self.TEXTO, ip=ip, fuentes=fuentes) for ip in ips]
        self.assertEqual([r["statusCode"] for r in respuestas[:4]], [200, 200, 200, 429])
        self.assertEqual({cuerpo(r)["error"] for r in respuestas[3:]}, {"limite_ip"},
                         "de la 4.ª en adelante, todas limite_ip; ninguna limite_global")
        self.assertEqual(self.contador(f"dia#{self.hoy()}"), 3, "el total solo gastó tres")
        r = self.verificar(self.TEXTO, ip="2001:db8:1234:5679::1")
        self.assertEqual((r["statusCode"], cuerpo(r)["restantes"]), (200, 2), "otra /64 va aparte")

    def test_una_ipv4_mapeada_es_su_ipv4(self):
        self.assertEqual(cuerpo(self.verificar(self.TEXTO, ip="198.51.100.7"))["restantes"], 2)
        self.assertEqual(cuerpo(self.verificar(self.TEXTO, ip="::ffff:198.51.100.7"))["restantes"], 1)
        self.assertEqual(cuerpo(self.verificar(self.TEXTO, ip="198.51.100.7"))["restantes"], 0)
        self.assertEqual(self.verificar(self.TEXTO, ip="::ffff:c633:6407")["statusCode"], 429,
                         "la misma, escrita en hexadecimal")

    # ------------------------------------------ caché negativa (revisión)
    def test_tras_un_limite_ip_no_se_escribe_mas_en_la_tabla(self):
        for _ in range(4):
            self.verificar(self.TEXTO)
        escrituras = list(self.tabla.escrituras)
        for _ in range(20):
            r = self.verificar(self.TEXTO)
            self.assertEqual(cuerpo(r)["error"], "limite_ip")
            self.assertAlmostEqual(int(r["headers"]["Retry-After"]),
                                   cuota.segundos_hasta_medianoche(time.time()), delta=5)
        self.assertEqual(self.tabla.escrituras, escrituras,
                         "DynamoDB cobra la escritura aunque falle la condición")

    # --------------------------------- presupuesto de peticiones (revisión)
    def test_presupuesto_de_peticiones_agotado_da_limite_global(self):
        os.environ["MAX_PETICIONES_DIA"] = "2"
        self.configurar()
        primera = FuentesSimuladas()
        r = self.verificar("Sentencia T-999 de 2019.", ip="198.51.100.1", fuentes=primera)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(cuerpo(r)["peticiones"]["relatoria"], 2, "la sentencia y el canario")
        self.assertEqual(self.contador(f"pet#{self.hoy()}"), 2)

        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            segunda = FuentesSimuladas()
            escrituras = len(self.tabla.escrituras)
            r = self.verificar("Sentencia T-406 de 1992.", ip="198.51.100.2", fuentes=segunda)
            self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (429, "limite_global"))
            # La reproducción del revisor: el 429 se atribuía a «100 consultas» con
            # solo 34 hechas. Ahora dice que lo agotado son las peticiones
            self.assertEqual(cuerpo(r)["limite"], "peticiones")
            self.assertNotIn("verificaciones", cuerpo(r)["mensaje"])
            self.assertIn("Retry-After", r["headers"])
            self.assertEqual(segunda.relatoria, [])
            self.assertEqual(len(self.tabla.escrituras), escrituras,
                             "este contenedor ya sabe que se agotó: ni escribe ni le gasta la consulta")

            # Un contenedor nuevo no lo sabe: se lo dice la condición de DynamoDB
            self.reiniciar_contenedor()
            self.configurar()
            tercera = FuentesSimuladas()
            r = self.verificar("Sentencia T-406 de 1992.", ip="198.51.100.3", fuentes=tercera)
            self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (429, "limite_global"))
            self.assertEqual(cuerpo(r)["limite"], "peticiones", "también cuando lo dice DynamoDB")
            self.assertEqual(tercera.relatoria, [])
        registros = [json.loads(linea) for linea in salida.getvalue().splitlines()]
        self.assertEqual([x.get("limite") for x in registros if x["evento"] == "limitado"],
                         ["peticiones", "peticiones"], "en el registro se ve cuál de los topes fue")

    def test_lo_que_sale_de_la_cache_no_suma(self):
        self.verificar(self.TEXTO)
        antes = len(self.tabla.escrituras)
        r = self.verificar(self.TEXTO)
        self.assertEqual(cuerpo(r)["peticiones"], {"relatoria": 0, "corte_suprema": 0})
        hoy = self.hoy()
        self.assertEqual([k.split("#")[0] for k in self.tabla.escrituras[antes:]], ["ip", "dia", "pet"],
                         "la comprobación del presupuesto, y ninguna suma")
        self.assertEqual(self.contador(f"pet#{hoy}"), 1)

    def test_si_falla_la_suma_se_responde_igual(self):
        """El tope ya se comprobó antes de preguntar; el informe obtenido vale."""
        self.configurar(ddb=TablaQueFallaAlSumar())
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            r = self.verificar(self.TEXTO)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(estados(r), {"T-760/2008": "verificada"})
        fallos = [json.loads(x) for x in salida.getvalue().splitlines() if "cuota_fallo" in x]
        self.assertEqual(fallos, [{"evento": "cuota_fallo", "operacion": "sumar_peticiones",
                                   "tipo": "ErrorAWS"}])


class TestHuella(unittest.TestCase):
    """La cuota por dentro, con el reloj en la mano."""

    @staticmethod
    def instante(y, m, d, h, mi=0) -> float:
        return dt.datetime(y, m, d, h, mi, tzinfo=COLOMBIA).timestamp()

    def test_el_dia_es_el_de_colombia(self):
        # 03:00 UTC del 21 son las 22:00 del 20 en Bogotá
        utc = dt.datetime(2026, 10, 21, 3, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(cuota.fecha_colombia(utc), "2026-10-20")
        self.assertEqual(cuota.segundos_hasta_medianoche(utc), 7200)

    def test_la_clave_cambia_de_un_dia_a_otro(self):
        """Ni con la tabla delante se pueden enlazar dos días de la misma IP."""
        reloj = [self.instante(2026, 10, 20, 23, 59)]
        tabla = cuota.TablaEnMemoria()
        c = cuota.Cuota(tabla, "t", "sal", max_ip=3, max_dia=100, reloj=lambda: reloj[0])
        for _ in range(3):
            c.consumir(IP)
        with self.assertRaises(cuota.LimiteIP) as ctx:
            c.consumir(IP)
        self.assertEqual(ctx.exception.reintentar_en, 60)
        reloj[0] = self.instante(2026, 10, 21, 0, 1)
        self.assertEqual(c.consumir(IP), 2, "a medianoche se renueva")
        de_ip = sorted(k for k in tabla.elementos if k.startswith("ip#"))
        self.assertEqual(len(de_ip), 2)
        huellas = [k.rsplit("#", 1)[1] for k in de_ip]
        self.assertNotEqual(huellas[0], huellas[1], "no cambia solo la fecha: también el HMAC")

    def test_huella(self):
        self.assertEqual(len(cuota.huella("sal", "2026-10-20", IP)), 32)
        self.assertNotEqual(cuota.huella("sal", "2026-10-20", IP), cuota.huella("otra", "2026-10-20", IP),
                            "sin la sal no se puede recalcular")

    def test_sin_sal_no_arranca(self):
        with self.assertRaises(ValueError):
            cuota.Cuota(cuota.TablaEnMemoria(), "t", "", 3, 100)

    def test_la_tabla_en_memoria_solo_emula_lo_suyo(self):
        with self.assertRaises(ValueError):
            cuota.TablaEnMemoria().update_item(
                TableName="t", Key={"pk": {"S": "x"}}, UpdateExpression="SET n = :uno",
                ConditionExpression=cuota.CONDICION, ExpressionAttributeValues={})
        with self.assertRaises(ValueError, msg="la suma va sin condición, y la cuota con ella"):
            cuota.TablaEnMemoria().update_item(
                TableName="t", Key={"pk": {"S": "x"}}, UpdateExpression=cuota.ACTUALIZACION,
                ExpressionAttributeValues={":uno": {"N": "1"}, ":exp": {"N": "0"}})
        with self.assertRaises(ValueError, msg="DynamoDB rechaza un valor que no se usa"):
            cuota.TablaEnMemoria().update_item(
                TableName="t", Key={"pk": {"S": "x"}}, UpdateExpression=cuota.SUMA,
                ExpressionAttributeValues={":peticiones": {"N": "3"}, ":exp": {"N": "0"},
                                           ":max": {"N": "600"}})

    def test_normalizar_ip(self):
        casos = {
            "198.51.100.7": "198.51.100.7",
            "::ffff:198.51.100.7": "198.51.100.7",
            "2001:db8:1234:5678:abcd:ef01:2345:6789": "2001:db8:1234:5678::/64",
            "2001:DB8:1234:5678::1": "2001:db8:1234:5678::/64",
            "2001:db8:1234:5679::1": "2001:db8:1234:5679::/64",
            "fe80::1%eth0": "fe80::/64",
            "": "",
            "no es una IP": "no es una IP",
        }
        for ip, esperado in casos.items():
            self.assertEqual(cuota.normalizar_ip(ip), esperado, ip)

    def cuota_con_reloj(self, **topes) -> tuple[cuota.Cuota, cuota.TablaEnMemoria, list[float]]:
        reloj = [self.instante(2026, 10, 20, 10)]
        tabla = cuota.TablaEnMemoria()
        topes = {"max_ip": 3, "max_dia": 100, **topes}
        return cuota.Cuota(tabla, "t", "sal", reloj=lambda: reloj[0], **topes), tabla, reloj

    def test_tras_un_limite_ip_no_hay_mas_escrituras_ese_dia_y_si_al_siguiente(self):
        """La reproducción del revisor: 1.000 POST de una IP daban 1.003 UpdateItem."""
        c, tabla, reloj = self.cuota_con_reloj()
        for _ in range(3):
            c.consumir(IP)
        with self.assertRaises(cuota.LimiteIP):
            c.consumir(IP)
        escrituras = len(tabla.escrituras)
        self.assertEqual(escrituras, 3 * 3 + 1, "ip, día y presupuesto por consulta, y el intento fallido")
        for _ in range(1000):
            with self.assertRaises(cuota.LimiteIP):
                c.consumir(IP)
        self.assertEqual(len(tabla.escrituras), escrituras)
        reloj[0] += 3600
        with self.assertRaises(cuota.LimiteIP) as ctx:
            c.consumir(IP)
        self.assertEqual(ctx.exception.reintentar_en, 13 * 3600, "el Retry-After se recalcula")
        self.assertNotIn(IP, repr(vars(c)), "en memoria van huellas, nunca la IP")
        self.assertEqual(c.consumir("198.51.100.9"), 2, "las demás IP siguen pasando")

        reloj[0] = self.instante(2026, 10, 21, 0, 1)
        self.assertEqual(c.consumir(IP), 2, "a medianoche se renueva")
        self.assertGreater(len(tabla.escrituras), escrituras + 3)
        self.assertEqual(c._ips_agotadas, set(), "y lo del día anterior se olvida")

    def test_tras_un_limite_global_tampoco(self):
        c, tabla, reloj = self.cuota_con_reloj(max_dia=2)
        c.consumir("198.51.100.1")
        c.consumir("198.51.100.2")
        with self.assertRaises(cuota.LimiteGlobal):
            c.consumir("198.51.100.3")
        escrituras = len(tabla.escrituras)
        for i in range(10, 60):
            with self.assertRaises(cuota.LimiteGlobal) as ctx:
                c.consumir(f"198.51.100.{i}")
        self.assertEqual(ctx.exception.de, "consultas")
        self.assertEqual(len(tabla.escrituras), escrituras)
        reloj[0] = self.instante(2026, 10, 21, 8)
        self.assertEqual(c.consumir("198.51.100.3"), 2)

    def test_presupuesto_de_peticiones(self):
        c, tabla, reloj = self.cuota_con_reloj(max_peticiones=10)
        c.consumir("198.51.100.1")
        c.sumar_peticiones(4)
        c.sumar_peticiones(0)                   # nada que sumar: ni escribe
        c.consumir("198.51.100.2")
        c.sumar_peticiones(7)                   # 11: se pasa en lo que hizo la última, no más
        self.assertEqual(tabla.elementos["pet#2026-10-20"]["n"]["N"], "11")
        self.assertEqual(sum(k == "pet#2026-10-20" for k in tabla.escrituras), 4,
                         "dos comprobaciones y dos sumas")
        escrituras = len(tabla.escrituras)
        with self.assertRaises(cuota.LimiteGlobal) as ctx:
            c.consumir("198.51.100.3")
        self.assertEqual((ctx.exception.de, ctx.exception.maximo), ("peticiones", 10))
        self.assertEqual(len(tabla.escrituras), escrituras, "este contenedor lo apuntó al sumar")

        otro = cuota.Cuota(tabla, "t", "sal", 3, 100, max_peticiones=10, reloj=lambda: reloj[0])
        with self.assertRaises(cuota.LimiteGlobal) as ctx:
            otro.consumir("198.51.100.3")
        self.assertEqual(ctx.exception.de, "peticiones", "otro contenedor lo sabe por DynamoDB")
        self.assertEqual(tabla.elementos["pet#2026-10-20"]["n"]["N"], "11", "comprobar no gasta")

        reloj[0] = self.instante(2026, 10, 21, 8)
        self.assertEqual(c.consumir("198.51.100.3"), 2, "el presupuesto también es por día")


# ==================================================================== espejo
class TestEspejo(BaseDemo):
    def test_baja_con_el_mtime_de_s3_y_sube_solo_lo_cambiado(self):
        # Un gzip de verdad: uno que no se puede leer cuenta como ausencia y ya no
        # se baja (ver test_lo_que_no_se_puede_leer_no_se_reparte)
        texto = gzip.compress("texto de la T-406/92".encode())
        self.s3.poner("cache/relatoria/T-406-1992.txt.gz", texto, hace=10 * 86400)
        self.s3.poner("cache/relatoria/notas/T-406-1992.json", b'{"nota": null}', hace=3 * 86400)
        e = espejo.Espejo(self.s3, BUCKET)
        foto = e.preparar(extraccion.extraer("Sentencia T-406 de 1992."))

        local = self.cache / "relatoria" / "T-406-1992.txt.gz"
        self.assertEqual(local.read_bytes(), texto)
        for clave, (_, fecha) in self.s3.objetos.items():
            ruta = self.cache / clave.removeprefix("cache/")
            self.assertEqual(ruta.stat().st_mtime, fecha.timestamp(),
                             "las caducidades del núcleo se miden desde que se guardó, no desde que se bajó")
        self.assertEqual(set(foto), {"relatoria/T-406-1992.txt.gz", "relatoria/notas/T-406-1992.json"})

        (self.cache / "relatoria" / "notas" / "T-406-1992.json").write_text('{"nota": "otra"}')
        (self.cache / "relatoria" / "T-760-2008.txt.gz").write_bytes(gzip.compress(b"nueva"))
        self.assertEqual(e.subir(foto), 2)
        self.assertEqual(sorted(self.s3.subidas), ["cache/relatoria/T-760-2008.txt.gz",
                                                   "cache/relatoria/notas/T-406-1992.json"])

    def test_lo_que_ya_esta_en_disco_no_se_vuelve_a_bajar(self):
        self.s3.poner("cache/relatoria/T-406-1992.txt.gz", b"de s3")
        local = cc.ruta_texto("T", "406", 1992)
        local.write_bytes(b"local")
        espejo.Espejo(self.s3, BUCKET).preparar(extraccion.extraer("Sentencia T-406 de 1992."))
        self.assertEqual(local.read_bytes(), b"local")
        self.assertNotIn("cache/relatoria/T-406-1992.txt.gz", self.s3.bajadas)

    def test_el_texto_de_la_suprema_va_por_el_id_de_la_busqueda(self):
        """La cita «SP22-2019» se registra «SP022-2019», y su texto va con ese nombre."""
        busqueda = {"providencia": {"id": "SP022-2019"}, "completa": True, "total": 1}
        self.s3.poner("cache/corte_suprema/busquedas/SP22-2019.json", json.dumps(busqueda).encode())
        self.s3.poner("cache/corte_suprema/textos/SP022-2019.txt.gz", b"texto")
        espejo.Espejo(self.s3, BUCKET).preparar(extraccion.extraer("La SP22-2019."))
        self.assertTrue(cs.ruta_texto("SP022-2019").exists())

    def test_un_id_raro_no_se_sale_de_la_cache(self):
        busqueda = {"providencia": {"id": "../../fuera"}, "completa": True, "total": 1}
        cs.ruta_busqueda("SC", "1", 2018).write_text(json.dumps(busqueda))
        rutas = espejo.previsibles(extraccion.extraer("La SC1-2018."))
        self.assertEqual(rutas, [cs.ruta_busqueda("SC", "1", 2018)])

    def test_un_fallo_de_s3_no_rompe_la_consulta(self):
        self.s3.falla = True
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            # Con un pasaje, para que haya algo que subir: el texto de la T-406/92.
            # Las negativas (el «no anulada», la ausencia) ni se intentan: con la
            # bajada a medias no se sabe qué hay en S3
            r = self.verificar(f"En la Sentencia T-406 de 1992 la Corte dijo: “{PASAJE_T406}”.\n\n"
                               "La Sentencia T-999 de 2019 no existe.")
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(estados(r), {"T-406/1992": "verificada", "T-999/2019": "no_existe"})
        registros = [json.loads(linea) for linea in salida.getvalue().splitlines()]
        fallos = [x for x in registros if x["evento"] == "espejo_fallo"]
        self.assertEqual([x["operacion"] for x in fallos], ["bajar", "subir"])
        self.assertEqual(len(self.s3.bajadas), 0)

    def test_un_access_denied_en_una_clave_no_impide_bajar_las_demas(self):
        """Con ListBucket limitado por prefijo, una clave ausente puede dar AccessDenied.

        Antes eso cortaba la bajada entera: las notas de nulidad que sí estaban en
        el bucket no se bajaban y se le volvían a pedir a la relatoría.
        """
        self.s3.errores["cache/relatoria/T-406-1992.txt.gz"] = "AccessDenied"   # la primera clave
        for nota in ("T-406-1992", "T-760-2008"):
            self.s3.poner(f"cache/relatoria/notas/{nota}.json", b'{"nota": null}', hace=3600)
        fuentes = FuentesSimuladas()
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            r = self.verificar("Sentencia T-406 de 1992. Sentencia T-760 de 2008.", fuentes=fuentes)
        self.assertEqual(estados(r), {"T-406/1992": "verificada", "T-760/2008": "verificada"})
        self.assertEqual(fuentes.relatoria, [], "las dos notas salieron de S3")
        self.assertEqual(self.s3.bajadas[0], "cache/relatoria/T-406-1992.txt.gz")
        self.assertIn("cache/relatoria/notas/T-760-2008.json", self.s3.bajadas)
        registros = [json.loads(linea) for linea in salida.getvalue().splitlines()]
        self.assertIn({"evento": "espejo_denegado", "claves": 1}, registros, "lo descartado se cuenta")
        self.assertNotIn("espejo_fallo", {x["evento"] for x in registros})

    def test_otro_error_de_s3_deja_de_bajar(self):
        """S3 caído o con errores: se registra y no se insiste con cada clave."""
        self.s3.errores["cache/relatoria/T-406-1992.txt.gz"] = "InternalError"
        self.s3.poner("cache/relatoria/notas/T-406-1992.json", b'{"nota": null}')
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            espejo.Espejo(self.s3, BUCKET).preparar(
                extraccion.extraer("Sentencia T-406 de 1992. Sentencia T-760 de 2008."))
        self.assertEqual(self.s3.bajadas, ["cache/relatoria/T-406-1992.txt.gz"])
        self.assertEqual([json.loads(x) for x in salida.getvalue().splitlines()],
                         [{"evento": "espejo_fallo", "operacion": "bajar", "tipo": "ErrorAWS",
                           "codigo": "InternalError"}])

    def test_una_ausencia_en_s3_no_se_cree_ni_se_renueva(self):
        """Las ausencias de la relatoría valen un día, y solo en el disco de quien preguntó.

        Antes una ausencia de S3 de hace una hora se creía sin preguntar, y la que
        se volvía a preguntar se subía para renovar su día. Si la había dejado un
        desafío, cada contenedor nuevo daba «no existe» sin una sola petición.
        """
        clave = "cache/relatoria/T-999-2019.txt.gz"
        vieja = gzip.compress(b"")
        self.s3.poner(clave, vieja, hace=3600)
        fuentes = FuentesSimuladas()
        r = self.verificar("Sentencia T-999 de 2019.", fuentes=fuentes)
        self.assertEqual(estados(r), {"T-999/2019": "no_existe"})
        self.assertEqual([u.rsplit("/", 1)[-1] for u in fuentes.relatoria], ["T-999-19.htm", "T-406-92.htm"],
                         "se volvió a preguntar, con el canario")
        self.assertIn("relatoria/T-999-2019.txt.gz", self.escritos(), "la ausencia vale en este disco")
        self.assertNotIn(clave, self.s3.subidas)
        self.assertEqual(self.s3.objetos[clave][0], vieja)


# ======================================================= contrato del espejo
class TestContratoDelEspejo(BaseDemo):
    """Todo lo que el núcleo escribe en la caché está entre lo que el espejo predice.

    Si un día el núcleo guardara en otra ruta, esa entrada nunca se bajaría de
    S3 y la demo volvería a pedírsela a la Corte en cada arranque en frío, sin
    que nada fallara a la vista. Esta prueba es la que lo ve.
    """

    # Un párrafo por pasaje: una cita que va detrás del pasaje y en el mismo
    # párrafo se lo quedaría (ESTADO.md, 4), y entonces no se pediría el texto
    TEXTO = (
        f"En la Sentencia T-406 de 1992 (M.P. Ciro Angarita Barón) la Corte dijo: “{PASAJE_T406}”.\n\n"
        "La SU-111 de 1997 lo reiteró. La Sentencia T-999 de 2019 no existe.\n\n"
        f"En materia civil, la SC5686-2018 reiteró que “{PASAJE_SC5686}”.\n\n"
        f"Y en penal, la SP22-2019 sostuvo que “{PASAJE_SP022}”.\n\n"
        "La SC9999-2018 no aparece. Y el Auto 092 de 2008 y la STH45-2020 no se consultan."
    )
    ESPERADO = {"T-406/1992": "verificada", "SU-111/1997": "verificada", "T-999/2019": "no_existe",
                "SC5686-2018": "verificada", "SP22-2019": "verificada", "SC9999-2018": "no_encontrada",
                "Auto 092 de 2008": "no_verificable", "STH45-2020": "no_verificable"}

    def previstas(self) -> set[str]:
        return {p.relative_to(red.dir_cache()).as_posix()
                for p in espejo.previsibles(extraccion.extraer(self.TEXTO))}

    def test_en_el_nucleo(self):
        with _fuentes(FuentesSimuladas()):
            inf = verificacion.verificar(self.TEXTO, indice=cc.Indice(FILAS))
        self.assertEqual({h.clave: h.estado.value for h in inf.hallazgos}, self.ESPERADO)
        escritos = self.escritos()
        self.assertTrue({"relatoria/T-406-1992.txt.gz", "relatoria/notas/T-406-1992.json",
                         "relatoria/notas/SU-111-1997.json", "relatoria/T-999-2019.txt.gz",
                         "corte_suprema/busquedas/SC5686-2018.json",
                         "corte_suprema/textos/SC5686-2018.txt.gz",
                         "corte_suprema/busquedas/SP22-2019.json",
                         "corte_suprema/textos/SP022-2019.txt.gz",
                         "corte_suprema/busquedas/SC9999-2018.json"} <= escritos,
                        "la prueba no es vacía: el núcleo escribió de todas las clases")
        self.assertEqual(escritos - self.previstas(), set())

    def test_a_traves_de_la_lambda(self):
        """Y lo que una consulta sube, un contenedor nuevo lo baja: no se vuelve a preguntar."""
        fuentes = FuentesSimuladas()
        r = self.verificar(self.TEXTO, fuentes=fuentes)
        self.assertEqual(estados(r), self.ESPERADO)
        self.assertEqual(cuerpo(r)["peticiones"], {"relatoria": len(fuentes.relatoria),
                                                   "corte_suprema": len(fuentes.suprema)})
        self.assertGreater(len(fuentes.relatoria), 0)
        subidas = {k.removeprefix("cache/") for k in self.s3.subidas}
        # Todo, salvo la ausencia de la relatoría: esa no se reparte (DISEÑO §2)
        ausencia = "relatoria/T-999-2019.txt.gz"
        self.assertIn(ausencia, self.escritos())
        self.assertEqual(subidas, self.escritos() - {ausencia})
        self.assertEqual(subidas - self.previstas(), set())

        # Contenedor nuevo: /tmp vacío, canarios y cortacircuitos de cero
        shutil.rmtree(self.cache)
        self.reiniciar_contenedor()
        del self.s3.subidas[:]
        otra_vez = FuentesSimuladas()
        r = self.verificar(self.TEXTO, fuentes=otra_vez)
        self.assertEqual(estados(r), self.ESPERADO)
        # De la relatoría solo se repite la inventada, con su canario: lo demás
        # (textos y notas) salió de S3
        self.assertEqual([u.rsplit("/", 1)[-1] for u in otra_vez.relatoria], ["T-999-19.htm", "T-406-92.htm"])
        # De la Corte Suprema, el canario del buscador, que vive en memoria: una
        # ausencia allí no se cree sin él (ESTADO.md, 6b)
        self.assertEqual(cuerpo(r)["peticiones"], {"relatoria": 2, "corte_suprema": 1})
        self.assertEqual(self.s3.subidas, [], "lo que solo se leyó no se vuelve a subir")

    def test_con_tope_lo_escrito_sigue_entre_lo_previsto(self):
        """preparar(max_consultadas=N) baja menos: lo que el núcleo lee tiene que seguir dentro."""
        citas = extraccion.extraer(self.TEXTO)
        for n in range(1, 7):
            shutil.rmtree(self.cache, ignore_errors=True)
            self.reiniciar_contenedor()
            with _fuentes(FuentesSimuladas()):
                verificacion.verificar(self.TEXTO, indice=cc.Indice(FILAS), max_consultadas=n)
            previstas = {p.relative_to(red.dir_cache()).as_posix()
                         for p in espejo.previsibles(citas, max_consultadas=n)}
            self.assertTrue(self.escritos(), n)
            self.assertEqual(self.escritos() - previstas, set(), f"max_consultadas={n}")


# ================================== lo que el espejo no reparte (tercera ronda)
class TestAusenciasNoSeReparten(BaseDemo):
    """El error de un contenedor no llega a los demás por S3 (DISEÑO §2, «Espejo»)."""

    PUBLICADA = {"T-980-26": f"Sentencia T-980/26. {RELLENO}"}

    def contenedor_nuevo(self) -> None:
        """Otro contenedor: /tmp vacío, canarios y cortacircuitos de cero; el mismo S3."""
        shutil.rmtree(self.cache, ignore_errors=True)
        self.reiniciar_contenedor()
        self.configurar()

    def test_la_reproduccion_del_revisor(self):
        """Desafío solo de <script> tras un canario vigente, por lambda_handler.

        Antes la T-980/26 salía no_existe con una sola petición (el canario de hace
        segundos seguía vigente), se subía su ausencia, y un contenedor B arrancado
        en frío daba no_existe a la T-980/26 ya publicada sin preguntar nada.
        """
        a = FuentesSimuladas()
        r = self.verificar("Sentencia T-981 de 2026.", fuentes=a)
        self.assertEqual(estados(r), {"T-981/2026": "no_existe"}, "una inventada de verdad: carcasa y canario")
        self.assertEqual(len(a.relatoria), 2)
        self.assertIn("relatoria/T-981-2026.txt.gz", self.escritos(), "la ausencia vale en el disco de A")
        self.assertNotIn("cache/relatoria/T-981-2026.txt.gz", self.s3.objetos, "y solo ahí")

        desafio = FuentesSimuladas(desafio=DESAFIO_SCRIPT)
        r = self.verificar("Sentencia T-980 de 2026.", ip="198.51.100.9", fuentes=desafio)
        self.assertEqual(estados(r), {"T-980/2026": "sin_confirmar"})
        self.assertEqual(len(desafio.relatoria), 1, "el canario seguía vigente: no se volvió a pedir")
        self.assertNotIn("relatoria/T-980-2026.txt.gz", self.escritos())
        self.assertEqual([k for k in self.s3.objetos if "T-98" in k], [], "S3 no recibe ninguna ausencia")

        self.contenedor_nuevo()
        b = FuentesSimuladas(publicadas=self.PUBLICADA)
        r = self.verificar("Sentencia T-980 de 2026. Sentencia T-981 de 2026.", ip="192.0.2.44", fuentes=b)
        self.assertEqual(estados(r), {"T-980/2026": "verificada", "T-981/2026": "no_existe"})
        pedidas = [u.rsplit("/", 1)[-1] for u in b.relatoria]
        self.assertIn("T-980-26.htm", pedidas, "B pregunta: de S3 no le llegó nada")
        self.assertIn("T-981-26.htm", pedidas, "tampoco la ausencia legítima de A")

    def test_si_el_nucleo_creyera_el_desafio_tampoco_se_reparte(self):
        """La salvaguarda es del espejo, no solo del núcleo: con el fallo de antes, simulado."""
        self.verificar("Sentencia T-981 de 2026.")              # canario vigente
        # El núcleo de la segunda ronda: «cero palabras» bastaba para ser la carcasa
        sin_palabras = lambda crudo: not cc.documentos._de_html(crudo).split()  # noqa: E731
        with mock.patch.object(cc, "_es_carcasa", sin_palabras):
            r = self.verificar("Sentencia T-980 de 2026.", ip="198.51.100.9",
                               fuentes=FuentesSimuladas(desafio=DESAFIO_SCRIPT))
        self.assertEqual(estados(r), {"T-980/2026": "no_existe"}, "el fallo, en este contenedor")
        self.assertEqual([k for k in self.s3.objetos if "T-98" in k], [])

        self.contenedor_nuevo()
        b = FuentesSimuladas(publicadas=self.PUBLICADA)
        r = self.verificar("Sentencia T-980 de 2026.", ip="192.0.2.44", fuentes=b)
        self.assertEqual(estados(r), {"T-980/2026": "verificada"}, "en B no: preguntó")
        self.assertEqual([u.rsplit("/", 1)[-1] for u in b.relatoria], ["T-980-26.htm"])

    def test_un_texto_en_s3_nunca_se_sustituye_por_una_ausencia(self):
        """El agravante de la revisión: la bajada del texto falló y el núcleo leyó una ausencia.

        Antes S3 tenía el texto de la T-982/26 (7.219 bytes), su bajada falló con
        InternalError y quedó sustituido por un objeto vacío.
        """
        clave = "cache/relatoria/T-982-2026.txt.gz"
        texto = gzip.compress(f"Sentencia T-982/26. {RELLENO}".encode())
        self.s3.poner(clave, texto, hace=86400)
        self.s3.errores[clave] = "InternalError"
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            # La relatoría da la carcasa para la T-982/26 (un tropiezo del sitio) y
            # sirve el canario: el núcleo cree la ausencia, con razón según lo que ve
            r = self.verificar("Sentencia T-982 de 2026.")
        self.assertEqual(estados(r), {"T-982/2026": "no_existe"})
        self.assertIn('"espejo_fallo"', salida.getvalue())
        self.assertNotIn(clave, self.s3.subidas)
        self.assertEqual(self.s3.objetos[clave][0], texto)

    def test_lo_que_no_se_puede_leer_no_se_reparte(self):
        """Un .txt.gz roto en S3 no se baja: el núcleo lo leería como un gzip roto cada vez."""
        self.s3.poner("cache/relatoria/T-406-1992.txt.gz", b"\x1f\x8b roto")
        espejo.Espejo(self.s3, BUCKET).preparar(extraccion.extraer("Sentencia T-406 de 1992."))
        self.assertFalse(cc.ruta_texto("T", "406", 1992).exists())

    def test_que_es_una_respuesta_negativa(self):
        ruta = cc.ruta_texto("T", "999", 2019)
        with gzip.open(ruta, "wt", encoding="utf-8") as f:     # como la escribe el núcleo
            f.write("")
        ausencia = ruta.read_bytes()
        casos = [
            ("relatoria/T-999-2019.txt.gz", ausencia, True),
            ("relatoria/T-999-2019.txt.gz", gzip.compress(b""), True),
            ("relatoria/T-406-1992.txt.gz", gzip.compress("Sentencia T-406/92".encode()), False),
            ("relatoria/T-406-1992.txt.gz", b"\x1f\x8b roto", True),
            ("relatoria/T-406-1992.txt.gz", b"", True),
            ("relatoria/notas/T-406-1992.json", b'{"nota": null}', True),
            ("relatoria/notas/SU-163-2023.json", '{"nota": "… declaró la nulidad …"}'.encode(), False),
            ("relatoria/notas/T-406-1992.json", b'{"nota": ', True),
            ("corte_suprema/busquedas/SC9999-2018.json",
             b'{"providencia": null, "completa": true, "total": 0}', True),
            ("corte_suprema/busquedas/SC5686-2018.json",
             b'{"providencia": {"id": "SC5686-2018"}, "completa": true, "total": 3}', False),
            ("corte_suprema/textos/SC5686-2018.txt.gz", gzip.compress(b'{"texto": "x"}'), False),
        ]
        for rel, datos, esperado in casos:
            self.assertEqual(espejo.negativa(rel, datos), esperado, rel)
        self.assertTrue(espejo.ausencia_relatoria("relatoria/T-999-2019.txt.gz", ausencia))
        self.assertFalse(espejo.ausencia_relatoria("relatoria/notas/T-999-2019.json", b'{"nota": null}'),
                         "un «no anulada» no es una ausencia: se sigue repartiendo")
        self.assertFalse(espejo.ausencia_relatoria("corte_suprema/busquedas/SC9-2018.json", b"{}"))


class TestRecientesSinPublicar(BaseDemo):
    """Una C real de hace semanas que aún no está en ninguna fuente (DISEÑO §1d), por lambda_handler.

    Con el índice real recortado al 12-08 (tests/datos), la C-273/26 del 27-08
    salía «no existe» por la Lambda (revisión 3). Ahora «no encontrada», que pide
    comprobarla; su ausencia vale un día en este disco y no sube a S3, como
    cualquier otra.
    """

    def test_no_encontrada_con_su_motivo_y_la_ausencia_no_se_reparte(self):
        filas = [f for f in json.loads(_dato("indice_2025_2026.json.gz")) if f["fecha_sentencia"][:10] <= "2026-08-12"]
        self.configurar(indice=cc.Indice(filas))
        fuentes = FuentesSimuladas()
        r = self.verificar("La Sentencia C-273 de 2026 y la T-981 de 2026.", fuentes=fuentes)
        self.assertEqual(estados(r), {"C-273/2026": "no_encontrada", "T-981/2026": "no_existe"})
        motivos = {h["clave"]: h["motivo"] for h in cuerpo(r)["informe"]["hallazgos"]}
        self.assertEqual(motivos["C-273/2026"], (
            "no figura en el índice oficial, que llega hasta el 2026-08-12, ni en la relatoría; puede ser una "
            "sentencia reciente que aún no se ha publicado. Compruébela antes de radicar"))
        self.assertEqual(cuerpo(r)["indice"]["hasta"], "2026-08-12")
        self.assertEqual([u.rsplit("/", 1)[-1] for u in fuentes.relatoria],
                         ["C-273-26.htm", "T-406-92.htm", "T-981-26.htm"])
        self.assertIn("relatoria/C-273-2026.txt.gz", self.escritos())
        self.assertEqual([k for k in self.s3.objetos if "C-273" in k or "T-981" in k], [],
                         "ninguna de las dos ausencias sube a S3")


class TestNegativasNoPisanPositivas(BaseDemo):
    """Un «no anulada» o una búsqueda vacía no sustituyen en S3 lo que puede ser una positiva."""

    CLAVE = "cache/relatoria/notas/T-760-2008.json"
    NOTA = json.dumps({"nota": "Mediante Auto 999 de 2024 la Sala Plena declaró la nulidad de "
                               "la Sentencia T-760 de 2008."}, ensure_ascii=False).encode()

    def assertIntacta(self) -> None:
        self.assertNotIn(self.CLAVE, self.s3.subidas)
        self.assertEqual(self.s3.objetos[self.CLAVE][0], self.NOTA, "la nota sigue en S3")

    def test_si_la_bajada_fallo_no_se_sube_ninguna(self):
        self.s3.poner(self.CLAVE, self.NOTA, hace=86400)
        self.s3.errores[self.CLAVE] = "InternalError"
        with contextlib.redirect_stdout(io.StringIO()):
            r = self.verificar("Sentencia T-760 de 2008.")
        # Lo que ve este contenedor: la página sin nota (en la simulación); la
        # guarda en su disco, pero no la reparte por encima de la de S3
        self.assertEqual(estados(r), {"T-760/2008": "verificada"})
        self.assertIsNone(json.loads(cc.ruta_nota("T", "760", 2008).read_text())["nota"])
        self.assertIntacta()
        self.assertIn("cache/relatoria/T-760-2008.txt.gz", self.s3.bajadas, "la otra clave sí se miró")

    def test_si_la_bajada_fallo_a_mitad_tampoco_las_de_antes(self):
        """Ni siquiera donde S3 dijo NoSuchKey antes de fallar: de un S3 que falla a mitad no
        se da nada por cierto, y una negativa de menos solo cuesta una petición en otro contenedor."""
        self.s3.errores["cache/relatoria/T-406-1992.txt.gz"] = "InternalError"
        with contextlib.redirect_stdout(io.StringIO()):
            r = self.verificar("Sentencia T-760 de 2008. Sentencia T-406 de 1992.")
        self.assertEqual(estados(r), {"T-760/2008": "verificada", "T-406/1992": "verificada"})
        self.assertEqual(self.s3.bajadas[:2], ["cache/relatoria/T-760-2008.txt.gz", self.CLAVE],
                         "las de la T-760/08 respondieron NoSuchKey antes del fallo")
        self.assertEqual(self.s3.subidas, [])

    def test_con_access_denied_tampoco(self):
        """Para bajar, una clave que no se deja leer es una que no está; para pisarla, no."""
        self.s3.poner(self.CLAVE, self.NOTA, hace=86400)
        self.s3.errores[self.CLAVE] = "AccessDenied"
        with contextlib.redirect_stdout(io.StringIO()):
            self.verificar("Sentencia T-760 de 2008.")
        self.assertIntacta()

    def test_donde_s3_no_tenia_nada_si_se_sube(self):
        """El «no anulada» se sigue repartiendo: ahorra una petición por sentencia y contenedor."""
        self.verificar("Sentencia T-760 de 2008.")
        self.assertIn(self.CLAVE, self.s3.subidas)
        self.assertEqual(json.loads(self.s3.objetos[self.CLAVE][0]), {"nota": None})

    def test_sobre_otra_negativa_si(self):
        """Un «no anulada» caducado que S3 aún no borró (su ciclo de vida va con retraso) se renueva."""
        self.s3.poner(self.CLAVE, b'{"nota": null}', hace=31 * 86400)
        fuentes = FuentesSimuladas()
        self.verificar("Sentencia T-760 de 2008.", fuentes=fuentes)
        self.assertEqual(len(fuentes.relatoria), 1, "caducado: se volvió a preguntar")
        self.assertIn(self.CLAVE, self.s3.subidas)

    def test_una_positiva_siempre_sube(self):
        """Al revés no hay peligro: una nota de nulidad sustituye a un «no anulada»."""
        self.s3.poner(self.CLAVE, b'{"nota": null}', hace=86400)
        self.s3.errores["cache/relatoria/T-760-2008.txt.gz"] = "InternalError"     # bajada a medias
        e = espejo.Espejo(self.s3, BUCKET)
        with contextlib.redirect_stdout(io.StringIO()):
            foto = e.preparar(extraccion.extraer("Sentencia T-760 de 2008."))
        cc.ruta_nota("T", "760", 2008).write_bytes(self.NOTA)
        self.assertEqual(e.subir(foto), 1)
        self.assertEqual(self.s3.objetos[self.CLAVE][0], self.NOTA)

    def test_una_busqueda_sin_la_providencia_no_pisa_una_que_la_tiene(self):
        clave = "cache/corte_suprema/busquedas/SC5686-2018.json"
        hallada = json.dumps({"providencia": {"id": "SC5686-2018"}, "completa": True, "total": 3}).encode()
        self.s3.poner(clave, hallada)
        self.s3.errores[clave] = "InternalError"
        e = espejo.Espejo(self.s3, BUCKET)
        with contextlib.redirect_stdout(io.StringIO()):
            foto = e.preparar(extraccion.extraer("La SC5686-2018."))
        cs.ruta_busqueda("SC", "5686", 2018).write_text(
            json.dumps({"providencia": None, "completa": True, "total": 0}), encoding="utf-8")
        self.assertEqual(e.subir(foto), 0)
        self.assertEqual(self.s3.objetos[clave][0], hallada)

    def test_sin_preparar_no_sube_ninguna_negativa(self):
        e = espejo.Espejo(self.s3, BUCKET)
        cc.ruta_nota("T", "760", 2008).write_text('{"nota": null}', encoding="utf-8")
        cc.ruta_texto("T", "406", 1992).write_bytes(gzip.compress(b"texto"))
        self.assertEqual(e.subir({}), 1)
        self.assertEqual(self.s3.subidas, ["cache/relatoria/T-406-1992.txt.gz"])


class TestNotasDeNulidadEnS3(BaseDemo):
    """Una nota de nulidad no caduca: tampoco en S3, donde el ciclo de vida borra cache/ a los 30 días."""

    CLAVE = "cache/relatoria/notas/SU-163-2023.json"
    NOTA = json.dumps({"nota": "Mediante Auto 823 de 2024 la Sala Plena declaró la nulidad de la "
                               "Sentencia SU-163 de 2023."}, ensure_ascii=False).encode()
    TEXTO = "La Sentencia SU-163 de 2023 dijo algo."

    def setUp(self):
        super().setUp()
        filas = FILAS + [{"sentencia": "SU-163/23", "fecha_sentencia": "2023-05-11T00:00:00.000",
                          "magistrado_a": "Ponente de Prueba"}]
        self.configurar(indice=cc.Indice(filas))

    def con_la_relatoria_bloqueada(self) -> dict:
        bloqueada = FuentesSimuladas(bloquear_relatoria=True)
        r = self.verificar(self.TEXTO, fuentes=bloqueada)
        self.assertEqual(bloqueada.relatoria, [], "la nota sale del espejo: nada que preguntar")
        return cuerpo(r)["informe"]["hallazgos"][0]

    def test_una_de_mas_de_20_dias_se_vuelve_a_subir(self):
        """La reproducción del revisor: con la relatoría en 403, la nota de S3 es lo único que lo dice.

        Antes nada la volvía a subir; al día 30 el ciclo de vida la borraba y la
        SU-163/23 pasaba de «ANULADA» a «no se pudo comprobar si fue anulada».
        """
        self.s3.poner(self.CLAVE, self.NOTA, hace=25 * 86400)
        h = self.con_la_relatoria_bloqueada()
        self.assertEqual(h["estado"], "con_observaciones")
        self.assertTrue(h["observaciones"][0].startswith("La sentencia fue ANULADA. "), h["observaciones"])
        self.assertEqual(self.s3.subidas, [self.CLAVE])
        datos, fecha = self.s3.objetos[self.CLAVE]
        self.assertEqual(datos, self.NOTA)
        self.assertLess(_ahora_s3() - fecha, dt.timedelta(minutes=1), "el ciclo de vida vuelve a contar desde hoy")

        # Otro contenedor la baja recién subida: no hace falta subirla otra vez
        shutil.rmtree(self.cache)
        self.reiniciar_contenedor()
        del self.s3.subidas[:]
        h = self.con_la_relatoria_bloqueada()
        self.assertTrue(h["observaciones"][0].startswith("La sentencia fue ANULADA. "))
        self.assertEqual(self.s3.subidas, [])

    def test_una_de_menos_de_20_dias_no(self):
        self.s3.poner(self.CLAVE, self.NOTA, hace=19 * 86400)
        self.con_la_relatoria_bloqueada()
        self.assertEqual(self.s3.subidas, [])
        self.assertEqual(cc.ruta_nota("SU", "163", 2023).stat().st_mtime,
                         self.s3.objetos[self.CLAVE][1].timestamp())

    def test_un_no_anulada_viejo_no_se_renueva(self):
        """El «no anulada» caduca a los 30 días a propósito: renovarlo escondería una anulación posterior."""
        clave = "cache/relatoria/notas/T-760-2008.json"
        self.s3.poner(clave, b'{"nota": null}', hace=25 * 86400)
        fuentes = FuentesSimuladas()
        r = self.verificar("Sentencia T-760 de 2008.", fuentes=fuentes)
        self.assertEqual(estados(r), {"T-760/2008": "verificada"})
        self.assertEqual((fuentes.relatoria, self.s3.subidas), ([], []))
        self.assertEqual(cc.ruta_nota("T", "760", 2008).stat().st_mtime, self.s3.objetos[clave][1].timestamp(),
                         "conserva el LastModified: caduca cuando le toca")


class TestPrepararConTope(BaseDemo):
    """preparar(max_consultadas=N) baja solo lo de las N providencias que verificar() consultará."""

    # El auto no pregunta nada y la T-0406 es la misma providencia que la T-406:
    # ninguno de los dos ocupa un hueco del tope
    TEXTO = ("Sentencia T-406 de 1992. El Auto 092 de 2008. Sentencia T-0406 de 1992. "
             "Sentencia T-760 de 2008. La SC5686-2018. Sentencia SU-111 de 1997.")

    def test_en_el_orden_de_verificar(self):
        citas = extraccion.extraer(self.TEXTO)
        e = espejo.Espejo(self.s3, BUCKET)
        e.preparar(citas, max_consultadas=2)
        self.assertEqual(self.s3.bajadas, ["cache/relatoria/T-406-1992.txt.gz", "cache/relatoria/notas/T-406-1992.json",
                                           "cache/relatoria/T-760-2008.txt.gz", "cache/relatoria/notas/T-760-2008.json"])
        del self.s3.bajadas[:]
        e.preparar(citas, max_consultadas=3)
        self.assertEqual(self.s3.bajadas[-1], "cache/corte_suprema/busquedas/SC5686-2018.json")
        del self.s3.bajadas[:]
        e.preparar(citas)
        self.assertEqual(len(self.s3.bajadas), 7, "sin tope, las de las cuatro")

    def test_por_la_lambda_con_el_mismo_tope_que_verificar(self):
        os.environ["MAX_PROVIDENCIAS"] = "1"
        self.configurar()
        r = self.verificar("Sentencia T-406 de 1992. Sentencia T-760 de 2008.")
        self.assertEqual(estados(r), {"T-406/1992": "verificada", "T-760/2008": "sin_confirmar"})
        self.assertEqual(self.s3.bajadas, ["cache/relatoria/T-406-1992.txt.gz",
                                           "cache/relatoria/notas/T-406-1992.json"])


# ============================================================ bloqueo y plazo
class TestBloqueoYPlazo(BaseDemo):
    def test_un_403_desde_aws_nunca_da_no_existe(self):
        """El riesgo nuevo en AWS: que la relatoría vete los rangos de AWS (DISEÑO, principio 1)."""
        fuentes = FuentesSimuladas(bloquear_relatoria=True)
        r = self.verificar("Sentencia T-999 de 2019. Sentencia T-406 de 1992.", fuentes=fuentes)
        self.assertEqual(r["statusCode"], 200)
        hallazgos = {h["clave"]: h for h in cuerpo(r)["informe"]["hallazgos"]}
        self.assertEqual(hallazgos["T-999/2019"]["estado"], "sin_confirmar")
        self.assertIn("403", hallazgos["T-999/2019"]["motivo"])
        self.assertNotIn("no_existe", estados(r).values())
        # Ni el bloqueo ni la nota sin leer se guardan como respuesta de la Corte
        self.assertEqual(self.escritos(), set())
        self.assertEqual(self.s3.subidas, [])
        self.assertEqual(cuerpo(r)["peticiones"]["relatoria"], len(fuentes.relatoria))
        self.assertEqual(len(fuentes.relatoria), 2, "un bloqueo no se reintenta")

    def test_plazo_agotado(self):
        """Lo que no se alcanza a consultar se lista como «sin confirmar», nunca se omite."""
        os.environ["PLAZO_SEGUNDOS"] = "0"
        self.configurar()
        fuentes = FuentesSimuladas()
        r = self.verificar("Sentencia T-406 de 1992. La SC5686-2018. El Auto 092 de 2008.", fuentes=fuentes)
        d = cuerpo(r)
        self.assertIs(d["cortado_por_tiempo"], True)
        hallazgos = {h["clave"]: h for h in d["informe"]["hallazgos"]}
        for clave in ("T-406/1992", "SC5686-2018"):
            self.assertEqual(hallazgos[clave]["estado"], "sin_confirmar")
            self.assertEqual(hallazgos[clave]["motivo"], verificacion.MOTIVO_SIN_TIEMPO)
        self.assertEqual(hallazgos["Auto 092 de 2008"]["estado"], "no_verificable")
        self.assertEqual((fuentes.relatoria, fuentes.suprema), ([], []))
        self.assertEqual(d["peticiones"], {"relatoria": 0, "corte_suprema": 0})

    def test_clientes_de_la_lambda(self):
        self.assertEqual((cc.cliente.tiempo_espera, cc.cliente.reintentos), (12, 2))
        self.assertEqual((cs.cliente.tiempo_espera, cs.cliente.reintentos), (12, 2))


# ===================================================== topes por texto (revisión)
class TestTopes(BaseDemo):
    NO_VERIFICABLES = " ".join(f"T-{i}/1985" for i in range(1, 465))
    INVENTADAS = ", ".join(f"T-{9000 + i}/15" for i in range(446))

    def test_464_no_verificables_dan_demasiadas_citas(self):
        """La reproducción del revisor: 4,9 KB de petición devolvían 165 KB, gratis y sin tope."""
        self.assertLessEqual(len(self.NO_VERIFICABLES), 5000)
        fuentes = FuentesSimuladas()
        r = self.verificar(self.NO_VERIFICABLES, fuentes=fuentes)
        self.assertEqual(r["statusCode"], 422)
        d = cuerpo(r)
        self.assertEqual(d["error"], "demasiadas_citas")
        self.assertIn("464", d["mensaje"])
        self.assertIn("40", d["mensaje"])
        self.assertLess(len(r["body"]), 500, "la respuesta ya no crece con las citas")
        self.assertEqual((self.tabla.elementos, self.s3.bajadas), ({}, []))
        self.assertEqual((fuentes.relatoria, fuentes.suprema), ([], []))

    def test_se_cuentan_providencias_no_menciones(self):
        cuarenta = " ".join(f"T-{i}/1985" for i in range(1, 41))
        r = self.verificar(cuarenta + " " + cuarenta)
        self.assertEqual(r["statusCode"], 200, "40 distintas, aunque se nombren dos veces, caben")
        self.assertEqual(len(estados(r)), 40)
        self.assertIsNone(cuerpo(r)["restantes"], "y siguen sin gastar cuota")
        r = self.verificar(cuarenta + " T-41/1985")
        self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (422, "demasiadas_citas"))

    def test_max_citas_del_entorno(self):
        os.environ["MAX_CITAS"] = "2"
        self.configurar()
        fuentes = FuentesSimuladas()
        r = self.verificar("Sentencia T-406 de 1992. Sentencia T-760 de 2008. Sentencia SU-111 de 1997.",
                           fuentes=fuentes)
        self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (422, "demasiadas_citas"))
        self.assertEqual(fuentes.relatoria, [], "se rechaza antes de la cuota y de preguntar")
        self.assertEqual(self.tabla.elementos, {})

    def test_446_inventadas_consultan_como_mucho_15(self):
        """La reproducción del revisor: 446 inventadas hacían 41 peticiones con una sola consulta."""
        self.assertLessEqual(len(self.INVENTADAS), 5000)
        # Con los topes de serie ni se llega a verificar: son más de 40 providencias
        fuentes = FuentesSimuladas()
        r = self.verificar(self.INVENTADAS, fuentes=fuentes)
        self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (422, "demasiadas_citas"))
        self.assertEqual(fuentes.relatoria, [])

        # Y si MAX_CITAS no lo parara, MAX_PROVIDENCIAS sí
        os.environ["MAX_CITAS"] = "1000"
        self.configurar()
        fuentes = FuentesSimuladas()
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            r = self.verificar(self.INVENTADAS, fuentes=fuentes)
        self.assertEqual(r["statusCode"], 200)
        d = cuerpo(r)
        self.assertIs(d["cortado_por_tope"], True)
        self.assertIs(d["cortado_por_tiempo"], False)
        self.assertEqual(d["restantes"], 2, "una sola consulta")
        hallazgos = d["informe"]["hallazgos"]
        self.assertEqual(len(hallazgos), 446, "lo que no se consulta se lista, no se omite")
        consultadas = [h for h in hallazgos if h["estado"] == "no_existe"]
        topadas = [h for h in hallazgos if h["estado"] == "sin_confirmar"]
        self.assertEqual((len(consultadas), len(topadas)), (15, 431))
        self.assertEqual({h["motivo"] for h in topadas}, {verificacion.MOTIVO_TOPE.format(n=15)})
        self.assertEqual(len(fuentes.relatoria), 16, "15 sentencias y el canario")
        self.assertEqual(d["peticiones"], {"relatoria": 16, "corte_suprema": 0})
        self.assertEqual(self.contador(f"pet#{self.hoy()}"), 16)
        registro = next(json.loads(x) for x in salida.getvalue().splitlines() if '"verificado"' in x)
        self.assertIs(registro["cortado_por_tope"], True)

    def test_max_providencias_del_entorno(self):
        os.environ["MAX_PROVIDENCIAS"] = "1"
        self.configurar()
        r = self.verificar("Sentencia T-406 de 1992. Sentencia T-760 de 2008. El Auto 092 de 2008.")
        d = cuerpo(r)
        self.assertEqual(estados(r), {"T-406/1992": "verificada", "T-760/2008": "sin_confirmar",
                                      "Auto 092 de 2008": "no_verificable"})
        motivo = next(h["motivo"] for h in d["informe"]["hallazgos"] if h["clave"] == "T-760/2008")
        self.assertEqual(motivo, verificacion.MOTIVO_TOPE.format(n=1))
        self.assertIs(d["cortado_por_tope"], True)


# ============================================== plazo de la invocación (revisión)
class TestPlazoDeLaInvocacion(BaseDemo):
    TEXTO = "Sentencia T-760 de 2008."

    def plazo_dado(self, contexto: Contexto | None) -> float:
        """Los segundos que la Lambda le dio a verificar()."""
        real = verificacion.verificar
        visto: dict = {}

        def espia(*args, **kwargs):
            visto.update(kwargs, llamada=time.monotonic())
            return real(*args, **kwargs)

        with mock.patch.object(lf.verificacion, "verificar", espia):
            r = self.verificar(self.TEXTO, contexto=contexto)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(visto["max_consultadas"], 15)
        return visto["hasta"] - visto["llamada"]

    def test_el_plazo_descuenta_lo_que_queda_de_la_invocacion(self):
        self.assertAlmostEqual(self.plazo_dado(None), 60, delta=1, msg="sin context (local, pruebas)")
        self.assertAlmostEqual(self.plazo_dado(Contexto(180_000)), 60, delta=1)
        self.assertAlmostEqual(self.plazo_dado(Contexto(50_000)), 25, delta=1,
                               msg="25 s se reservan para subir el espejo y responder")

    def test_el_plazo_se_toma_antes_de_la_cuota_y_del_espejo(self):
        orden: list[str] = []
        consumir, preparar = lf._cuota.consumir, lf._espejo.preparar

        def cuota_(ip):
            orden.append("cuota")
            return consumir(ip)

        def espejo_(citas, **kw):
            orden.append("espejo")
            return preparar(citas, **kw)

        with mock.patch.object(lf._cuota, "consumir", cuota_), \
                mock.patch.object(lf._espejo, "preparar", espejo_):
            self.verificar(self.TEXTO, contexto=Contexto(180_000, orden))
        self.assertEqual(orden, ["contexto", "cuota", "espejo"])

    def test_sin_tiempo_para_responder_no_se_pregunta(self):
        fuentes = FuentesSimuladas()
        r = self.verificar(self.TEXTO, fuentes=fuentes, contexto=Contexto(20_000))
        d = cuerpo(r)
        self.assertIs(d["cortado_por_tiempo"], True)
        self.assertEqual(estados(r), {"T-760/2008": "sin_confirmar"})
        self.assertEqual(d["informe"]["hallazgos"][0]["motivo"], verificacion.MOTIVO_SIN_TIEMPO)
        self.assertEqual(fuentes.relatoria, [])

    def test_el_contexto_de_local_imita_al_de_lambda(self):
        contexto = local.Contexto(timeout=180)
        self.assertGreater(contexto.get_remaining_time_in_millis(), 179_000)
        self.assertEqual(self.verificar(self.TEXTO, contexto=contexto)["statusCode"], 200)
        self.assertEqual(local.Contexto(timeout=0).get_remaining_time_in_millis(), 0)


# ==================================================================== índice
def _filas(n: int) -> list[dict]:
    return [{"sentencia": f"T-{i:03d}/20", "fecha_sentencia": "2020-01-01T00:00:00.000",
             "magistrado_a": "Ponente de Prueba"} for i in range(1, n + 1)]


def _gz(filas: list[dict]) -> bytes:
    return gzip.compress(json.dumps(filas).encode("utf-8"))


class TestIndice(BaseDemo):
    CLAVE = lf.CLAVE_INDICE

    def setUp(self):
        super().setUp()
        self.s3.poner(self.CLAVE, _gz(_filas(100)), hace=8 * 86400)
        self.configurar(indice=None)        # arranque en frío: el índice sale de S3

    def actualizar(self, filas):
        descargar = (mock.patch.object(cc.Indice, "_descargar", side_effect=filas)
                     if isinstance(filas, Exception)
                     else mock.patch.object(cc.Indice, "_descargar", return_value=filas))
        with descargar:
            return lf.lambda_handler({"accion": "actualizar_indice"}, None)

    def providencias(self) -> int:
        return cuerpo(lf.lambda_handler(evento("GET", "/"), None))["providencias"]

    def test_el_indice_sale_de_s3_con_su_fecha(self):
        d = cuerpo(lf.lambda_handler(evento("GET", "/"), None))
        self.assertEqual(d["providencias"], 100)
        subido = self.s3.objetos[self.CLAVE][1]
        self.assertEqual(dt.datetime.fromisoformat(d["indice_actualizado"]), subido)

    def test_respeta_el_95_por_ciento(self):
        antes = self.s3.objetos[self.CLAVE]
        r = self.actualizar(_filas(94))
        self.assertFalse(r["ok"])
        self.assertIn("94", r["motivo"])
        self.assertEqual(self.s3.objetos[self.CLAVE], antes, "el de S3 no se toca")
        self.assertEqual(self.providencias(), 100)

        r = self.actualizar(_filas(95))
        self.assertTrue(r["ok"], "el 95 % justo vale")
        self.assertEqual(self.providencias(), 95, "el contenedor pasa a usar el nuevo")
        with gzip.open(io.BytesIO(self.s3.objetos[self.CLAVE][0]), "rt", encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 95, "y en S3, con el formato de .cache")

    def test_si_datos_gov_bloquea_se_conserva_el_anterior(self):
        r = self.actualizar(urllib.error.HTTPError(cc.API_DATOS, 403, "Forbidden", {}, None))
        self.assertFalse(r["ok"])
        self.assertEqual(self.s3.subidas, [])
        self.assertEqual(self.providencias(), 100)

    def test_una_consulta_nunca_descarga_el_indice(self):
        with mock.patch.object(cc.Indice, "_descargar") as descargar, \
                mock.patch.object(cc.Indice, "cargar") as cargar:
            self.verificar("Sentencia T-001 de 2020.")
            os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "cotejo-demo"
            self.configurar(s3=None, indice=None)
            r = self.verificar("Sentencia T-001 de 2020.")
        self.assertEqual((r["statusCode"], cuerpo(r)["error"]), (500, "interno"),
                         "en AWS sin BUCKET no hay índice, y no se baja de datos.gov.co")
        descargar.assert_not_called()
        cargar.assert_not_called()


# ===================================================================== sonda
def _https(url: str, codigo: int, cuerpo: bytes):
    r = urllib.response.addinfourl(io.BytesIO(cuerpo), email.message.Message(), url, codigo)
    r.msg = "simulado"
    return r


class SitiosSimulados:
    """Los servidores de la sonda, por debajo de urlopen (en HTTPSHandler).

    Más abajo que FuentesSimuladas porque la sonda lee el código HTTP con un
    manejador propio del abridor de urllib, y eso también tiene que correr.
    """

    def __init__(self, bloquear_relatoria: bool = False, inexistente_404: bool = False,
                 sin_range: bool = False):
        self.bloquear_relatoria = bloquear_relatoria
        self.inexistente_404 = inexistente_404      # algo entre medias responde 404 por la relatoría
        self.sin_range = sin_range                  # la relatoría deja de respetar Range
        self.pedidas: list[str] = []

    # La C-355/06: los primeros 170 KB reales (tests/datos, con 152.575 bytes de
    # cabecera de Word) y relleno hasta los 3,4 MB que mide entera
    C355 = None

    @classmethod
    def c355(cls) -> bytes:
        if cls.C355 is None:
            parrafo = b"<p class=MsoNormal>" + b"palabra " * 120 + b"</p>\r\n"
            recortada = _dato("C-355-06.htm.gz")
            cls.C355 = recortada + parrafo * ((3_398_037 - len(recortada)) // len(parrafo) + 1)
        return cls.C355

    def _con_range(self, req, url: str, pagina: bytes):
        rango = req.get_header("Range")
        if rango and not self.sin_range:
            return _https(url, 206, pagina[:int(rango.rsplit("-", 1)[1]) + 1])
        return _https(url, 200, pagina)

    def responder(self, req):
        url = req.full_url
        self.pedidas.append(url)
        if "corteconstitucional.gov.co" in url:
            if self.bloquear_relatoria:
                return _https(url, 403, b"<html><body>Request blocked</body></html>")
            if url.endswith(".gov.co/"):
                return _https(url, 200, CARCASA.replace(b"<body>", b"<body>Corte Constitucional de Colombia"))
            if url.endswith("T-406-92.htm"):
                return _https(url, 200, f"<html><body>{SENTENCIAS['T-406-92']}</body></html>".encode())
            if url.endswith("T-760-08.htm"):
                pagina = ("<html><body>Sentencia T-760/08 " + "palabra " * 12000 + "</body></html>").encode()
                return self._con_range(req, url, pagina)
            if url.endswith("C-355-06.htm"):
                return self._con_range(req, url, self.c355())
            if self.inexistente_404:
                return _https(url, 404, b"<html><body><h1>404 Not Found</h1>Request blocked</body></html>")
            return _https(url, 200, CARCASA)
        if "datos.gov.co" in url:
            return _https(url, 200, json.dumps([{"sentencia": "C-001/92"}, {"sentencia": "C-002/92"}]).encode())
        if url == cs.API:
            r = {"data": {"getSearchResult": {"searchResults": [{"title": "SC5686-2018 [2004-00042-01].pdf"}],
                                              "numOfResults": 3}}}
            return _https(url, 200, json.dumps(r).encode())
        raise AssertionError(f"dirección no simulada: {url}")


class TestSonda(BaseDemo):
    NOMBRES = ["portada_constitucional", "relatoria_T-406-92", "relatoria_inexistente",
               "relatoria_T-760-08_range", "relatoria_C-355-06_range_300k", "datos_gov_dos_filas",
               "corte_suprema_SC5686-2018"]

    def sondear(self, sitios: SitiosSimulados) -> dict:
        def https_open(handler, req):
            return sitios.responder(req)
        # urlopen de verdad (la sonda instala su abridor), con los servidores simulados
        with mock.patch.object(urllib.request, "urlopen", _URLOPEN_REAL), \
                mock.patch.object(urllib.request.HTTPSHandler, "https_open", https_open):
            return lf.lambda_handler({"accion": "sonda"}, None)

    def test_todo_responde(self):
        r = self.sondear(SitiosSimulados())
        self.assertEqual([p["nombre"] for p in r["pruebas"]], self.NOMBRES)
        self.assertTrue(r["ok"], r["pruebas"])
        por_nombre = {p["nombre"]: p for p in r["pruebas"]}
        self.assertEqual(por_nombre["relatoria_T-760-08_range"]["codigo"], 206)
        self.assertLessEqual(por_nombre["relatoria_T-760-08_range"]["bytes"], 30_001)
        # La segunda ventana de la nulidad (DISEÑO §1d): 300 KB, no los 3,4 MB enteros
        c355 = por_nombre["relatoria_C-355-06_range_300k"]
        self.assertEqual((c355["codigo"], c355["bytes"]), (206, cc._BYTES_VENTANA))
        self.assertIn("es la sentencia; respeta Range", c355["detalle"])
        for p in r["pruebas"]:
            self.assertEqual(set(p), {"nombre", "ok", "codigo", "bytes", "segundos", "detalle"})
        self.assertIsNone(urllib.request._opener, "la sonda deja el abridor de serie")

    def test_la_c355_sin_range_no_pasa(self):
        """Sin Range, cada nulidad de una página de Word de cabecera grande bajaría 3,4 MB."""
        r = self.sondear(SitiosSimulados(sin_range=True))
        c355 = {p["nombre"]: p for p in r["pruebas"]}["relatoria_C-355-06_range_300k"]
        self.assertEqual((c355["ok"], c355["codigo"]), (False, 200))
        self.assertGreater(c355["bytes"], 3_300_000)
        self.assertIn("NO respeta Range", c355["detalle"])
        self.assertFalse(r["ok"])

    def test_un_404_donde_va_la_carcasa_no_pasa(self):
        """Antes la sonda lo daba por bueno; el núcleo ya no lo cree como ausencia (DISEÑO §1d)."""
        r = self.sondear(SitiosSimulados(inexistente_404=True))
        inexistente = {p["nombre"]: p for p in r["pruebas"]}["relatoria_inexistente"]
        self.assertEqual((inexistente["ok"], inexistente["codigo"]), (False, 404))
        self.assertIn("Cotejo no lo cree como ausencia", inexistente["detalle"])
        self.assertFalse(r["ok"])

    def test_relatoria_bloqueada(self):
        sitios = SitiosSimulados(bloquear_relatoria=True)
        r = self.sondear(sitios)
        self.assertFalse(r["ok"])
        pruebas = r["pruebas"]
        for p in pruebas[:3]:
            self.assertEqual((p["ok"], p["codigo"]), (False, 403))
            self.assertIn("403", p["error"])
        # Tres fallos seguidos abren el cortacircuitos de la sonda: la cuarta y la
        # quinta no se piden
        for p in pruebas[3:5]:
            self.assertNotIn("codigo", p)
            self.assertIn("dejó de responder", p["error"])
        self.assertEqual(sum("corteconstitucional" in u for u in sitios.pedidas), 3)
        self.assertTrue(pruebas[5]["ok"] and pruebas[6]["ok"], "las demás fuentes se miden igual")
        self.assertEqual(cc.cliente.fallos_seguidos, 0, "el cortacircuitos de las consultas no se entera")

    def test_una_pagina_de_desafio_no_pasa_por_carcasa(self):
        ok, detalle = lf._comprobar_carcasa(200, b"<html><body>Checking your browser before "
                                                 b"accessing the site</body></html>")
        self.assertFalse(ok)
        self.assertIn("palabras", detalle)

    def test_la_carcasa_se_reconoce_por_su_firma(self):
        """Lo mismo que cree el núcleo (cc._es_carcasa): si no, la sonda daría por bueno lo que él no cree.

        Antes bastaba con cero palabras, y un desafío hecho solo de <script> pasaba
        la sonda; y si el sitio cambiara su carcasa, la sonda no lo diría.
        """
        real = _dato("carcasa.htm.gz")
        self.assertEqual(len(real), 8607)
        self.assertTrue(lf._comprobar_carcasa(200, real)[0], "la carcasa real de la relatoría")
        ok, detalle = lf._comprobar_carcasa(200, DESAFIO_SCRIPT)
        self.assertFalse(ok)
        self.assertIn("firma", detalle)
        self.assertFalse(lf._comprobar_carcasa(200, real.replace(b"<app-root", b"<otra-app"))[0],
                         "una carcasa distinta: las inventadas saldrían «sin confirmar», y la sonda lo dice")
        ok, detalle = lf._comprobar_carcasa(404, b"")
        self.assertFalse(ok, "un 404 ya no es una ausencia: la relatoría responde la carcasa, nunca 404")
        self.assertIn("404", detalle)
        self.assertFalse(lf._comprobar_carcasa(200, b"")[0], "un 200 vacío no dice nada")


class TestAccionesSoloDirectas(BaseDemo):
    def test_un_post_de_url_nunca_ejecuta_una_accion(self):
        with mock.patch.object(lf, "_sonda") as sonda, \
                mock.patch.object(lf, "_actualizar_indice") as actualizar:
            for accion in ("sonda", "actualizar_indice"):
                r = lf.lambda_handler(evento(cuerpo={"accion": accion}), None)
                self.assertEqual(cuerpo(r)["error"], "texto_vacio")
                r = lf.lambda_handler(evento(ruta="/", cuerpo={"accion": accion}), None)
                self.assertEqual(r["statusCode"], 404)
                # Ni con «accion» en el propio evento: trae requestContext.http
                ev = evento("GET", "/")
                ev["accion"] = accion
                self.assertEqual(cuerpo(lf.lambda_handler(ev, None))["servicio"], "cotejo-demo")
        sonda.assert_not_called()
        actualizar.assert_not_called()

    def test_evento_directo_desconocido(self):
        self.assertFalse(lf.lambda_handler({"accion": "borrar_todo"}, None)["ok"])
        self.assertFalse(lf.lambda_handler({}, None)["ok"])


# ================================================================ registros
class TestRegistros(BaseDemo):
    """Conteos y tiempos, nunca texto ni IP (DISEÑO, principio 4)."""

    SECRETO = "Cláusula reservada del cliente Zeta-Omega"
    IP = "198.51.100.23"

    def test_ni_texto_ni_ip(self):
        texto = f"{self.SECRETO}. Sentencia T-406 de 1992. Sentencia T-999 de 2019."
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            for _ in range(4):                                  # la cuarta, 429
                self.verificar(texto, ip=self.IP)
            self.verificar(self.SECRETO, ip=self.IP)             # sin citas
            lf.lambda_handler(evento(cuerpo={"texto": self.SECRETO * 400}, ip=self.IP), None)
            lf.lambda_handler(evento(cuerpo='{"texto": "' + self.SECRETO, ip=self.IP), None)
            self.s3.falla = True
            self.configurar(ddb=cuota.TablaEnMemoria())
            self.verificar(texto, ip=self.IP)                    # S3 caído
            self.configurar(ddb=TablaQueFalla())
            self.verificar(texto, ip=self.IP)                    # DynamoDB caído
        lineas = salida.getvalue().splitlines()
        registros = [json.loads(linea) for linea in lineas]      # cada línea, un JSON
        self.assertEqual({x["evento"] for x in registros},
                         {"verificado", "limitado", "sin_consultas", "rechazado", "espejo_fallo", "error"})
        volcado = salida.getvalue()
        for prohibido in (self.IP, "Zeta-Omega", "Cláusula", "T-406", "T-999",
                          cuota.huella("sal-de-prueba", cuota.fecha_colombia(time.time()), self.IP)):
            self.assertNotIn(prohibido, volcado)
        verificado = next(x for x in registros if x["evento"] == "verificado")
        self.assertEqual(set(verificado), {"evento", "citas", "estados", "segundos", "peticiones",
                                           "cortado_por_tiempo", "cortado_por_tope"})


# ======================================================== forma de ejemplos
class TestCuerpoInforme(unittest.TestCase):
    def test_es_lo_que_exporta_la_pagina(self):
        indice = cc.Indice(FILAS)
        inf = verificacion.verificar("El Auto 092 de 2008.", indice=indice, relatoria=False)
        d = lf.cuerpo_informe(inf, segundos=1.23456, restantes=None, indice=indice,
                              peticiones={"relatoria": 0, "corte_suprema": 0})
        self.assertEqual(d["segundos"], 1.23)
        self.assertEqual(d["indice"], {"providencias": 3, "hasta": "2008-07-31"})
        self.assertEqual(d["informe"]["resumen"]["no_verificable"], 1)
        self.assertEqual((d["cortado_por_tiempo"], d["cortado_por_tope"]), (False, False))
        json.dumps(d)       # serializable tal cual


class TestFormaDeEjemplosJson(unittest.TestCase):
    """demo/pagina/ejemplos.json tiene la forma del 200 de POST /verificar de hoy (DISEÑO §4).

    Es un archivo generado que se copia a mano al portafolio, y se quedaba atrás:
    el de cd8cd43 tenía 6 claves en cada «respuesta» cuando cuerpo_informe ya
    daba 7 (faltaba cortado_por_tope). Si esta prueba falla, el mensaje dice
    cómo regenerarlo.
    """

    ARCHIVO = RAIZ / "demo" / "pagina" / "ejemplos.json"

    def test_cada_respuesta_tiene_las_claves_de_cuerpo_informe(self):
        datos = json.loads(self.ARCHIVO.read_text(encoding="utf-8"))
        diferencias = exportar_ejemplos.diferencias_de_forma(datos)
        self.assertEqual(diferencias, [], "\n".join(
            [f"{self.ARCHIVO} está desfasado:"] + diferencias + [exportar_ejemplos.REGENERAR]))
        referencia = set(exportar_ejemplos.respuesta_de_referencia())
        for ejemplo in datos["ejemplos"]:
            self.assertEqual(set(ejemplo["respuesta"]), referencia, exportar_ejemplos.REGENERAR)
        meta = json.loads((RAIZ / "demo" / "ejemplos" / "ejemplos.json").read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in datos["ejemplos"]], [m["id"] for m in meta],
                         "los ejemplos de demo/ejemplos/. " + exportar_ejemplos.REGENERAR)

    def test_la_referencia_es_cuerpo_informe(self):
        indice = cc.Indice(FILAS)
        inf = verificacion.verificar("El Auto 092 de 2008.", indice=indice, relatoria=False)
        d = lf.cuerpo_informe(inf, segundos=0.0, restantes=None, indice=indice,
                              peticiones={"relatoria": 0, "corte_suprema": 0})
        self.assertEqual(set(exportar_ejemplos.respuesta_de_referencia()), set(d))

    def test_ve_lo_que_falta_y_lo_que_sobra(self):
        """Que la comprobación no sea vacía: con la forma de cd8cd43, falla."""
        respuesta = exportar_ejemplos.respuesta_de_referencia()
        datos = {"generado": "2026-10-20", "commit": "abc1234", "ejemplos": [
            {"id": "x", "titulo": {}, "descripcion": {}, "texto": "", "respuesta": respuesta}]}
        self.assertEqual(exportar_ejemplos.diferencias_de_forma(datos), [])
        del respuesta["cortado_por_tope"]
        respuesta["informe"]["hallazgos"][0]["sobra"] = 1
        del datos["generado"]
        self.assertEqual(exportar_ejemplos.diferencias_de_forma(datos), [
            "ejemplos.json: faltan generado",
            "ejemplos[x].respuesta: faltan cortado_por_tope",
            "ejemplos[x].respuesta.informe.hallazgos[0]: sobran sobra"])

        salida = Path(tempfile.mkdtemp(prefix="cotejo-forma-")) / "ejemplos.json"
        self.addCleanup(shutil.rmtree, salida.parent, True)
        salida.write_text(json.dumps(datos), encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(exportar_ejemplos, "SALIDA", salida), contextlib.redirect_stderr(err):
            self.assertEqual(exportar_ejemplos.main(["--comprobar"]), 1)
        self.assertIn("faltan cortado_por_tope", err.getvalue())
        self.assertIn("exportar_ejemplos.py", err.getvalue(), "y cómo regenerarlo")


# ==================================================== local.py (concurrencia 1)
class TestLocalConcurrencia(unittest.TestCase):
    """local.py atiende como la función con concurrencia reservada 1 (DISEÑO §2).

    Antes era un servidor de un hilo: la segunda petición esperaba en cola, y en
    local nunca se veía el 429 con el que la Function URL rechaza a un segundo
    visitante simultáneo.
    """

    def manejador(self, enviados: list) -> "local.Manejador":
        # Sin socket: lo justo para que _atender arme el evento y responda
        m = object.__new__(local.Manejador)
        m.headers = email.message.Message()
        m.path, m.request_version, m.client_address = "/verificar", "HTTP/1.1", ("127.0.0.1", 50000)
        m.server = mock.Mock(server_address=("127.0.0.1", local.PUERTO))
        m._enviar = lambda codigo, cabeceras, cuerpo: enviados.append((codigo, cuerpo))
        return m

    def test_una_segunda_peticion_simultanea_recibe_429(self):
        dentro, seguir = threading.Event(), threading.Event()

        def lenta(evento, contexto):
            dentro.set()
            seguir.wait(5)
            return {"statusCode": 200, "headers": {}, "body": "{}"}

        primera, segunda = [], []
        with mock.patch.object(local.lambda_function, "lambda_handler", side_effect=lenta) as handler:
            hilo = threading.Thread(target=self.manejador(primera)._atender, args=("POST",))
            hilo.start()
            self.assertTrue(dentro.wait(5))
            self.manejador(segunda)._atender("GET")            # también la salud ocupa el turno
            seguir.set()
            hilo.join(5)
            self.assertEqual(handler.call_count, 1, "la segunda no invoca la función")
        self.assertEqual([c for c, _ in primera], [200])
        self.assertEqual([c for c, _ in segunda], [429])
        self.assertNotIn("error", json.loads(segunda[0][1]),
                         "sin código nuestro: la página lo lee como «otra persona está verificando»")
        self.assertTrue(local.TURNO.acquire(blocking=False), "el turno se suelta al acabar")
        local.TURNO.release()

    def test_el_turno_se_suelta_aunque_la_funcion_falle(self):
        with mock.patch.object(local.lambda_function, "lambda_handler", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                self.manejador([])._atender("POST")
        self.assertTrue(local.TURNO.acquire(blocking=False))
        local.TURNO.release()


# ================================================= exportar_ejemplos.py (revisión)
class GitFalso:
    """subprocess.run para el exportador: HEAD fijo y una salida de status por llamada."""

    def __init__(self, *porcelanas: str, commit: str = "abc1234"):
        self.porcelanas = list(porcelanas)
        self.commit = commit

    def __call__(self, args, **kwargs):
        if "rev-parse" in args:
            salida = self.commit + "\n"
        elif "status" in args:
            assert "-z" in args and "--untracked-files=all" in args, args
            # La última se repite: un árbol que no cambia da siempre lo mismo
            salida = self.porcelanas.pop(0) if len(self.porcelanas) > 1 else self.porcelanas[0]
        else:
            raise AssertionError(f"orden de git no simulada: {args}")
        return subprocess.CompletedProcess(args, 0, stdout=salida if kwargs.get("text") else salida.encode())


class TestExportar(unittest.TestCase):
    """El exportador se niega si el commit que publica no es el que produjo los informes."""

    SUCIO = " M cotejo/verificacion.py\0?? demo/DISEÑO.md\0"
    SOLO_PAGINA = " M demo/pagina/cotejo.js\0?? demo/pagina/ejemplos.json\0"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cotejo-exportar-"))
        ejemplos = self.tmp / "ejemplos"
        ejemplos.mkdir()
        # Un ejemplo que no consulta nada: el exportador corre entero sin red
        (ejemplos / "x.txt").write_text("El Auto 092 de 2008.", encoding="utf-8")
        (ejemplos / "ejemplos.json").write_text(json.dumps([{
            "id": "x", "archivo": "x.txt", "titulo": {"es": "X", "en": "X"},
            "descripcion": {"es": "x", "en": "x"}, "esperado": {"Auto 092 de 2008": "no_verificable"}}]),
            encoding="utf-8")
        self.salida = self.tmp / "pagina" / "ejemplos.json"
        for parche in (mock.patch.object(exportar_ejemplos, "EJEMPLOS", ejemplos),
                       mock.patch.object(exportar_ejemplos, "SALIDA", self.salida),
                       mock.patch.dict(os.environ, {"COTEJO_CACHE": str(self.tmp / "cache")}),
                       mock.patch.object(urllib.request, "urlopen", _sin_red)):
            parche.start()
            self.addCleanup(parche.stop)
        del _salidas_a_la_red[:]
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def exportar(self, git: GitFalso, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.object(exportar_ejemplos.subprocess, "run", git), \
                mock.patch.object(cc.Indice, "cargar", return_value=cc.Indice(FILAS)) as cargar, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            codigo = exportar_ejemplos.main(list(argv))
        self.cargar = cargar
        self.assertEqual(_salidas_a_la_red, [])
        return codigo, err.getvalue()

    def test_lee_la_porcelana(self):
        porcelana = (" M cotejo/verificacion.py\0?? demo/pagina/ejemplos.json\0 M demo/pagina/cotejo.js\0"
                     "R  demo/pagina/nuevo.py\0demo/viejo.py\0?? demo/DISEÑO.md\0")
        self.assertEqual(exportar_ejemplos.cambios_sin_commit(porcelana),
                         ["cotejo/verificacion.py", "demo/DISEÑO.md", "demo/viejo.py"],
                         "lo que sale de demo/ hacia la página también es un cambio")
        self.assertEqual(exportar_ejemplos.cambios_sin_commit(self.SOLO_PAGINA), [])
        self.assertEqual(exportar_ejemplos.cambios_sin_commit(""), [])

    def test_se_niega_con_cambios_sin_commit(self):
        codigo, err = self.exportar(GitFalso(self.SUCIO))
        self.assertEqual(codigo, 1)
        self.assertFalse(self.salida.exists())
        self.cargar.assert_not_called()          # ni índice ni peticiones a la Corte
        self.assertIn("cotejo/verificacion.py", err)
        self.assertIn("demo/DISEÑO.md", err)
        self.assertIn("abc1234", err)

    def test_los_cambios_en_la_pagina_no_cuentan(self):
        codigo, _ = self.exportar(GitFalso(self.SOLO_PAGINA))
        self.assertEqual(codigo, 0)
        datos = json.loads(self.salida.read_text(encoding="utf-8"))
        self.assertEqual(datos["commit"], "abc1234")
        respuesta = datos["ejemplos"][0]["respuesta"]
        self.assertIs(respuesta["cortado_por_tope"], False, "la forma del 200 de POST /verificar")

    def test_con_permitir_sucio_el_commit_lo_dice(self):
        codigo, err = self.exportar(GitFalso(self.SUCIO), "--permitir-sucio")
        self.assertEqual(codigo, 0)
        self.assertEqual(json.loads(self.salida.read_text(encoding="utf-8"))["commit"], "abc1234-sucio")
        self.assertIn("abc1234-sucio", err)

    def test_si_el_arbol_cambia_mientras_verifica_no_escribe(self):
        codigo, err = self.exportar(GitFalso("", self.SUCIO))
        self.assertEqual(codigo, 1)
        self.assertFalse(self.salida.exists())
        self.assertIn("cambiaron mientras se verificaba", err)
        codigo, _ = self.exportar(GitFalso(""))
        self.assertEqual(codigo, 0, "con el árbol quieto, sí")


if __name__ == "__main__":
    unittest.main(verbosity=2)
