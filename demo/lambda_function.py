"""Demo pública «Cotejo en vivo»: una Lambda con Function URL (contrato en DISEÑO.md §2).

Rutas (eventos de Function URL, payload 2.0):
    GET  /            salud: tamaño del índice, hasta cuándo llega, límites
    POST /verificar   {"texto": "..."}  ->  el informe en JSON

Eventos directos, sin requestContext.http (una Function URL nunca los produce):
    {"accion": "actualizar_indice"}   lo lanza EventBridge cada lunes
    {"accion": "sonda"}               a mano: ¿responden las fuentes a una IP de AWS?

El texto se procesa en memoria y no se guarda en ningún sitio. Los registros
llevan conteos y tiempos, nunca texto ni IP (DISEÑO, principio 4): un escrito
antes de radicarse es información confidencial del cliente de un abogado, y un
registro de CloudWatch vive 14 días donde no lo controla quien pegó el texto.

En el paquete todo va plano en la raíz del zip (este módulo, espejo.py,
cuota.py y el paquete cotejo/), de ahí los «import espejo» sin prefijo.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import os
import time
import urllib.parse
import urllib.request

# Antes de importar cotejo. La caché por defecto va junto al paquete, que en
# la Lambda es /var/task, de solo lectura: el primer guardado daría
# PermissionError. /tmp es lo único escribible.
os.environ.setdefault("COTEJO_CACHE", "/tmp/cotejo-cache")

from cotejo import documentos, extraccion, informe, verificacion  # noqa: E402
from cotejo.fuentes import corte_constitucional as cc  # noqa: E402
from cotejo.fuentes import corte_suprema as cs  # noqa: E402
from cotejo.fuentes import red  # noqa: E402

import cuota  # noqa: E402
import espejo  # noqa: E402

CLAVE_INDICE = "indice/corte_constitucional_indice.json.gz"
DOCUMENTO = "Texto pegado"

# 5.000 caracteres en JSON, escapados, no llegan a 64 KB. Se mira antes de
# decodificar nada: un cuerpo de 6 MB (lo que admite una Function URL) se
# rechaza sin gastar en él memoria ni CPU.
MAX_CUERPO = 64 * 1024

# Una actualización semanal que trae menos del 95 % de las providencias del
# índice anterior es una descarga a medias (una página que falló, un límite de
# la API), no una Corte que borró sentencias. Con un índice recortado, las
# sentencias que faltan se irían a preguntar una a una a la relatoría.
UMBRAL_INDICE = 0.95

# Clientes de las fuentes en la Lambda. Los 60 s de serie, con tres intentos,
# son tres minutos por petición contra una relatoría lenta: más que el timeout
# de la función. Con 12 s y dos intentos, una providencia atascada cuesta como
# mucho medio minuto, y el plazo de verificar() corta las siguientes.
TIEMPO_ESPERA = 12
REINTENTOS = 2

# Lo que se reserva de la invocación para después de verificar: subir el espejo
# (tiempos de S3 de 3 + 10 s, con reintentos) y responder. Sin esto, con el
# plazo medido desde que empieza verificar() y no desde que empezó la
# invocación, lo que tardaran la cuota y el espejo se comía ese margen, y una
# Lambda que muere por timeout no responde nada ni sube lo que ya preguntó.
MARGEN_RESPUESTA = 25

# Valores por defecto de los topes (DISEÑO §2); en AWS los fija Terraform.
# exportar_ejemplos.py usa el mismo MAX_PROVIDENCIAS, para que el informe
# guardado de un ejemplo sea el que daría «Verificar en vivo».
MAX_CITAS = 40
MAX_PROVIDENCIAS = 15
MAX_PETICIONES_DIA = 600

CABECERAS = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # La respuesta lleva trozos del texto pegado (cómo se citó, los pasajes): ni
    # el navegador ni un proxy intermedio tienen por qué guardarla.
    "Cache-Control": "no-store",
    # Es una API JSON: si alguien abre la URL en el navegador, nada que ejecutar
    # ni que enmarcar.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

MENSAJES = {
    "mal_formada": "La solicitud no es un JSON válido con el campo «texto».",
    "texto_vacio": "Pega un texto con citas para verificarlo.",
    "demasiadas_citas": ("El texto cita {n} providencias distintas; la demo verifica como mucho "
                         "{maximo} por texto. Pártelo en trozos más pequeños."),
    "limite_ip": ("Ya hiciste las {maximo} verificaciones de hoy en esta demo. "
                  "Se renuevan a medianoche, hora de Colombia."),
    "interno": "Algo falló al verificar el texto. Inténtalo de nuevo más tarde.",
}

# limite_global, por cuál de los dos topes del día (DISEÑO §2, «Respuesta de
# limite_global»). Antes era un solo mensaje y la página decía «su tope de 100
# consultas» también cuando lo agotado era el presupuesto de peticiones: con 15
# sentencias por consulta, el 429 llegaba en la 35.ª (reproducido).
MENSAJES_LIMITE_GLOBAL = {
    "consultas": ("La demo llegó hoy a su tope de {maximo} verificaciones entre todos los "
                  "visitantes. Se renueva a medianoche, hora de Colombia."),
    "peticiones": ("La demo ya hizo hoy las {maximo} peticiones a las fuentes oficiales que se "
                   "permite al día, para no saturar a la Corte. Se renueva a medianoche, hora de Colombia."),
}

# ------------------------------------------------------------------ estado
# Vive lo que vive el contenedor: los clientes de AWS, el índice leído de S3 en
# el arranque en frío y, dentro de cotejo, el cortacircuitos de cada fuente.
_listo = False
_s3 = None
_bucket = ""
_espejo: espejo.Espejo | None = None
_cuota: cuota.Cuota | None = None
_indice: cc.Indice | None = None
_max_caracteres = 5000
_max_ip = 3
_plazo = 60.0
_max_citas = MAX_CITAS
_max_providencias = MAX_PROVIDENCIAS


def _cliente_aws(servicio: str):
    # Perezoso: boto3 viene con el runtime de Lambda, pero el .venv de Cotejo no
    # lo tiene, y las pruebas y local.py inyectan clientes falsos.
    import boto3
    from botocore.config import Config
    # Tiempos cortos: un S3 o un DynamoDB que no responden no pueden comerse el
    # plazo de la consulta. Con los de serie (60 s, con reintentos), una caída
    # de S3 se llevaría el minuto entero en el espejo, antes de preguntar nada.
    return boto3.client(servicio, config=Config(
        connect_timeout=3, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"}))


def configurar(s3=None, ddb=None, *, bucket: str | None = None, tabla: str | None = None,
               sal: str | None = None, indice: cc.Indice | None = None) -> None:
    """Arranque: clientes de AWS, límites y clientes HTTP de las fuentes.

    Sin argumentos lo toma todo del entorno, que es lo que pasa en AWS. local.py
    y las pruebas le pasan clientes falsos y, si quieren, el índice ya hecho.
    """
    global _listo, _s3, _bucket, _espejo, _cuota, _indice, _max_caracteres, _max_ip, _plazo
    global _max_citas, _max_providencias
    entorno = os.environ
    bucket = entorno.get("BUCKET", "") if bucket is None else bucket
    tabla = entorno.get("TABLA_CUOTA", "") if tabla is None else tabla
    sal = entorno.get("SAL_VISITANTE", "") if sal is None else sal
    max_ip = int(entorno.get("MAX_CONSULTAS_IP", "3"))
    max_dia = int(entorno.get("MAX_CONSULTAS_DIA", "100"))
    max_caracteres = int(entorno.get("MAX_CARACTERES", "5000"))
    plazo = float(entorno.get("PLAZO_SEGUNDOS", "60"))
    max_citas = int(entorno.get("MAX_CITAS", str(MAX_CITAS)))
    max_providencias = int(entorno.get("MAX_PROVIDENCIAS", str(MAX_PROVIDENCIAS)))
    max_peticiones = int(entorno.get("MAX_PETICIONES_DIA", str(MAX_PETICIONES_DIA)))

    if s3 is None and bucket:
        s3 = _cliente_aws("s3")
    if ddb is None and tabla:
        ddb = _cliente_aws("dynamodb")
    _s3, _bucket = (s3, bucket) if s3 is not None and bucket else (None, "")
    _espejo = espejo.Espejo(_s3, _bucket) if _s3 is not None else None
    _cuota = (cuota.Cuota(ddb, tabla, sal, max_ip, max_dia, max_peticiones=max_peticiones)
              if ddb is not None and tabla else None)
    _indice = indice
    _max_caracteres, _max_ip, _plazo = max_caracteres, max_ip, plazo
    _max_citas, _max_providencias = max_citas, max_providencias
    for cliente in (cc.cliente, cs.cliente):
        cliente.tiempo_espera = TIEMPO_ESPERA
        cliente.reintentos = REINTENTOS
    _listo = True


def _registrar(datos: dict) -> None:
    """Una línea JSON en CloudWatch. Solo conteos y tiempos (principio 4)."""
    print(json.dumps(datos, ensure_ascii=False), flush=True)


def _respuesta(codigo: int, cuerpo: dict, extra: dict | None = None) -> dict:
    return {"statusCode": codigo, "headers": {**CABECERAS, **(extra or {})},
            "body": json.dumps(cuerpo, ensure_ascii=False)}


def _error(http: int, codigo: str, mensaje: str | None = None, extra: dict | None = None,
           campos: dict | None = None) -> dict:
    """{"error", "mensaje"} y, con `campos`, lo que el código lleve de más (el "limite" de limite_global)."""
    return _respuesta(http, {"error": codigo, "mensaje": mensaje or MENSAJES[codigo], **(campos or {})},
                      extra)


# ------------------------------------------------------------------- índice
def indice_actual() -> cc.Indice:
    """El índice del contenedor; en el arranque en frío, de S3 (o de .cache en local)."""
    global _indice
    if _indice is None:
        _indice = _cargar_indice()
    return _indice


def _cargar_indice() -> cc.Indice:
    if _s3 is not None:
        r = _s3.get_object(Bucket=_bucket, Key=CLAVE_INDICE)
        filas = json.loads(gzip.decompress(r["Body"].read()).decode("utf-8"))
        indice = cc.Indice(filas)
        # Lo que la página enseña como «índice actualizado»: cuándo se subió,
        # no cuándo arrancó este contenedor
        indice.actualizado = r["LastModified"].timestamp()
        return indice
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        # cc.Indice.cargar() lo descargaría entero de datos.gov.co, y en mitad de
        # una consulta: un par de MB y hasta tres minutos, desde una IP de AWS
        # que ese servidor puede estar bloqueando
        raise RuntimeError("en AWS el índice se lee de S3, y falta BUCKET")
    return cc.Indice.cargar()


# ----------------------------------------------------------------- informe
def cuerpo_informe(inf: verificacion.Informe, *, segundos: float, restantes: int | None,
                   indice: cc.Indice, peticiones: dict) -> dict:
    """El 200 de POST /verificar (DISEÑO §2).

    Función aparte y pura porque exportar_ejemplos.py la usa para los ejemplos
    precargados de la página: tienen que tener la forma de la API, o la página
    necesitaría dos formas de dibujar el mismo informe.
    """
    return {
        "informe": informe.a_json(inf),
        "segundos": round(segundos, 2),
        "restantes": restantes,
        "indice": {"providencias": len(indice), "hasta": indice.fecha_mas_reciente},
        "peticiones": {"relatoria": peticiones["relatoria"],
                       "corte_suprema": peticiones["corte_suprema"]},
        # informe.a_json no los lleva: los deciden el plazo y el tope de la
        # demo, no Cotejo. La página explica el corte con ellos.
        "cortado_por_tiempo": inf.cortado_por_tiempo,
        "cortado_por_tope": inf.cortado_por_tope,
    }


def _salud() -> dict:
    ind = indice_actual()
    actualizado = (dt.datetime.fromtimestamp(ind.actualizado, dt.timezone.utc).isoformat(timespec="seconds")
                   if ind.actualizado else None)
    return _respuesta(200, {
        "servicio": "cotejo-demo", "estado": "ok", "providencias": len(ind),
        "indice_hasta": ind.fecha_mas_reciente, "indice_actualizado": actualizado,
        "max_caracteres": _max_caracteres, "max_consultas_ip": _max_ip,
    })


# --------------------------------------------------------- POST /verificar
class _Rechazo(Exception):
    def __init__(self, http: int, codigo: str, mensaje: str | None = None):
        super().__init__(codigo)
        self.http, self.codigo, self.mensaje = http, codigo, mensaje


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _leer_texto(event: dict) -> str:
    """Pasos 1 y 2 del orden de DISEÑO §2: tamaño, base64, JSON; vacío y largo."""
    cuerpo = event.get("body") or ""
    if not isinstance(cuerpo, str) or len(cuerpo) > MAX_CUERPO or len(cuerpo.encode("utf-8")) > MAX_CUERPO:
        raise _Rechazo(400, "mal_formada")
    if event.get("isBase64Encoded"):
        try:
            cuerpo = base64.b64decode(cuerpo, validate=True).decode("utf-8")
        except ValueError:                      # binascii.Error y UnicodeDecodeError lo son
            raise _Rechazo(400, "mal_formada") from None
    try:
        datos = json.loads(cuerpo)
    except (ValueError, RecursionError):        # «[[[[…» anidado 30.000 veces cabe en 64 KB
        raise _Rechazo(400, "mal_formada") from None
    if not isinstance(datos, dict):
        raise _Rechazo(400, "mal_formada")
    texto = datos.get("texto")
    if texto is None:
        texto = ""
    if not isinstance(texto, str):
        raise _Rechazo(400, "mal_formada")
    if not texto.strip():
        raise _Rechazo(422, "texto_vacio")
    if len(texto) > _max_caracteres:
        raise _Rechazo(413, "texto_largo",
                       f"El texto tiene {_miles(len(texto))} caracteres; "
                       f"el máximo de la demo es {_miles(_max_caracteres)}.")
    return texto


def _plazo_de(context) -> float:
    """Segundos que se le dan a verificar() (paso 4 de DISEÑO §2).

    PLAZO_SEGUNDOS, o menos si a la invocación no le queda para tanto más el
    margen de responder. context es None en local.py y en las pruebas: ahí no
    hay timeout que respetar y vale el plazo de serie.
    """
    restante = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(restante):
        return _plazo
    return min(_plazo, restante() / 1000 - MARGEN_RESPUESTA)


def _medir(antes: tuple[int, int]) -> dict:
    return {"relatoria": cc.cliente.peticiones - antes[0],
            "corte_suprema": cs.cliente.peticiones - antes[1]}


def _sumar_peticiones(peticiones: dict) -> None:
    """Paso 8: las peticiones medidas, al presupuesto del día. Nunca lanza.

    La consulta ya está hecha y su informe vale: un fallo aquí se registra y se
    responde igual. El tope ya se comprobó antes de preguntar.
    """
    if _cuota is None:
        return
    try:
        _cuota.sumar_peticiones(sum(peticiones.values()))
    except Exception as exc:
        tipo = str(exc) if isinstance(exc, cuota.TablaNoDisponible) else type(exc).__name__
        _registrar({"evento": "cuota_fallo", "operacion": "sumar_peticiones", "tipo": tipo})


def _verificar(event: dict, ip: str, context=None) -> dict:
    texto = _leer_texto(event)
    t0 = time.monotonic()
    citas = extraccion.extraer(texto)

    # Paso 3. Antes que nada más: en un texto de 5.000 caracteres caben cientos
    # de citas, y la ruta gratuita (las no verificables) no tenía tope. «T-1/1985
    # T-2/1985…» daba 464 hallazgos y 165 KB de respuesta por 4,9 KB de
    # petición, sin cuota (reproducido). Se cuentan como verificar(): por
    # providencia, no por mención.
    distintas = len(verificacion._agrupar(citas))
    if distintas > _max_citas:
        raise _Rechazo(422, "demasiadas_citas", MENSAJES["demasiadas_citas"].format(
            n=_miles(distintas), maximo=_miles(_max_citas)))

    indice = indice_actual()
    antes = (cc.cliente.peticiones, cs.cliente.peticiones)

    # Misma regla con la que verificar() decide qué le pregunta a una fuente:
    # si se copiara aquí y divergiera, un texto gastaría cuota sin consultar
    # nada, o consultaría sin gastarla.
    consulta = any(verificacion._necesita_red(c, True) for c in citas)
    if not consulta:
        # Gratis, y con relatoria=False por si acaso: si esa regla y verificar()
        # discreparan, lo que saldría es «no se consultó», no una petición a la
        # Corte que nadie pagó. Para lo no verificable el resultado es el mismo.
        inf = verificacion.verificar(texto, documento=DOCUMENTO, indice=indice, relatoria=False)
        restantes, evento = None, "sin_consultas"
    else:
        if _cuota is None:
            raise RuntimeError("sin tabla de cuota")     # sin límite no se sirve
        # Paso 4, antes de la cuota y del espejo: lo que tarden DynamoDB y S3
        # sale del plazo, no del margen para responder
        hasta = time.monotonic() + _plazo_de(context)
        restantes = _cuota.consumir(ip)                     # paso 5
        # Paso 6, con el mismo tope que verificar(): lo que pase de él sale «sin
        # confirmar» sin abrir la caché, y bajarlo solo gastaría plazo
        foto = (_espejo.preparar(citas, max_consultadas=_max_providencias)
                if _espejo else None)
        try:
            inf = verificacion.verificar(texto, documento=DOCUMENTO, indice=indice,     # paso 7
                                         hasta=hasta, max_consultadas=_max_providencias)
        finally:
            # Paso 8, también si verificar falló: lo que alcanzó a guardar vale,
            # y lo que alcanzó a preguntar cuenta
            if _espejo is not None and foto is not None:
                _espejo.subir(foto)
            _sumar_peticiones(_medir(antes))
        evento = "verificado"

    peticiones = _medir(antes)
    segundos = time.monotonic() - t0
    _registrar({"evento": evento, "citas": len(inf.hallazgos),                   # paso 9
                "estados": {e.value: inf.contar(e) for e in verificacion.Estado if inf.contar(e)},
                "segundos": round(segundos, 2), "peticiones": peticiones,
                "cortado_por_tiempo": inf.cortado_por_tiempo,
                "cortado_por_tope": inf.cortado_por_tope})
    return _respuesta(200, cuerpo_informe(inf, segundos=segundos, restantes=restantes,
                                          indice=indice, peticiones=peticiones))


def _post_verificar(event: dict, ip: str, context=None) -> dict:
    try:
        return _verificar(event, ip, context)
    except _Rechazo as r:
        _registrar({"evento": "rechazado", "error": r.codigo})
        return _error(r.http, r.codigo, r.mensaje)
    except cuota.LimiteIP as e:
        _registrar({"evento": "limitado", "limite": "ip"})
        return _error(429, "limite_ip", MENSAJES["limite_ip"].format(maximo=e.maximo),
                      {"Retry-After": str(e.reintentar_en)})
    except cuota.LimiteGlobal as e:
        # Cuál de los dos topes se agotó va en el cuerpo, y la página explica
        # cada uno: el de consultas lo agotan los visitantes; el de peticiones,
        # pocas consultas con muchas providencias. El registro dice lo mismo.
        _registrar({"evento": "limitado", "limite": e.de})
        return _error(429, "limite_global", MENSAJES_LIMITE_GLOBAL[e.de].format(maximo=_miles(e.maximo)),
                      {"Retry-After": str(e.reintentar_en)}, campos={"limite": e.de})
    except Exception as exc:
        # Solo el tipo: el mensaje de una excepción puede citar el texto pegado
        _registrar({"evento": "error", "fase": "verificar", "tipo": type(exc).__name__})
        return _error(500, "interno")


# ------------------------------------------------------- acciones directas
def _actualizar_indice() -> dict:
    """Descarga el índice de datos.gov.co y lo sube a S3, si no viene recortado."""
    global _indice
    t0 = time.monotonic()
    salida: dict = {"accion": "actualizar_indice", "ok": False}
    if _s3 is None:
        salida["motivo"] = "sin bucket: en local el índice lo renueva cc.Indice.cargar()"
    else:
        try:
            # Sin el índice anterior no hay con qué medir el nuevo: no se sube
            anterior = len(indice_actual())
            filas = cc.Indice._descargar()
            nuevo = cc.Indice(filas)
            salida.update(providencias=len(nuevo), anterior=anterior, hasta=nuevo.fecha_mas_reciente)
            if len(nuevo) < UMBRAL_INDICE * anterior:
                salida["motivo"] = (f"la descarga trae {len(nuevo)} providencias y el índice tenía "
                                    f"{anterior}: se conserva el anterior")
            else:
                # El mismo formato que .cache/: una lista de filas en JSON, en gzip
                datos = gzip.compress(json.dumps(filas, ensure_ascii=False).encode("utf-8"))
                _s3.put_object(Bucket=_bucket, Key=CLAVE_INDICE, Body=datos,
                               ContentType="application/gzip")
                nuevo.actualizado = time.time()
                _indice = nuevo
                salida["ok"] = True
        except Exception as exc:
            salida["motivo"] = f"{type(exc).__name__}: {exc}"[:300]
    salida["segundos"] = round(time.monotonic() - t0, 2)
    _registrar({"evento": "indice", **salida})
    return salida


class _Codigos(urllib.request.BaseHandler):
    """Apunta el código HTTP de cada respuesta, para la sonda.

    red.Cliente.pedir solo devuelve el cuerpo, y en la sonda el código es la
    mitad del diagnóstico: un 206 dice que la relatoría respeta Range (la nota
    de nulidad lee 30 KB, no la sentencia entera) y un 403, que un cortafuegos
    veta a AWS.
    """
    handler_order = 100     # antes de HTTPErrorProcessor (1000), que hace del 403 una excepción

    def __init__(self):
        self.ultimo: int | None = None

    def http_response(self, request, response):
        self.ultimo = getattr(response, "status", None) or getattr(response, "code", None)
        return response

    https_response = http_response


def _palabras(cuerpo: bytes) -> tuple[str, int]:
    texto = documentos._de_html(cuerpo) if cuerpo else ""
    return texto, len(texto.split())


def _comprobar_portada(codigo, cuerpo: bytes) -> tuple[bool, str]:
    if b"corte constitucional" in cuerpo.lower():
        return True, "la portada de la Corte"
    return False, "no menciona a la Corte Constitucional: ¿una página de bloqueo?"


def _comprobar_sentencia(tipo: str, numero: str, ventana: int | None = None):
    """Con `ventana`, además, que la relatoría respete Range y no mande más de esos bytes."""
    def comprobar(codigo, cuerpo: bytes) -> tuple[bool, str]:
        texto, n = _palabras(cuerpo)
        es = cc._parece_la_sentencia(texto, tipo, numero)
        detalle = f"{n} palabras; " + ("es la sentencia" if es else "no parece la sentencia")
        if ventana is None:
            return es, detalle
        # Sin Range, cada nota de nulidad bajaría la sentencia entera (la T-760/08
        # pasa del MB) en vez de 30 KB
        parcial = codigo == 206 and len(cuerpo) <= ventana
        return es and parcial, detalle + ("; respeta Range" if parcial else "; NO respeta Range")
    return comprobar


def _comprobar_carcasa(codigo, cuerpo: bytes) -> tuple[bool, str]:
    # Cotejo solo lee como ausencia la carcasa reconocida por su firma
    # (cc._es_carcasa: sin palabras, con <app-root> y el título de la Corte;
    # DISEÑO §1c y §1d). Antes la sonda daba por buena cualquier página sin
    # palabras, y un desafío hecho solo de <script> también lo es: pasaba la
    # sonda y luego el núcleo no creía ninguna ausencia. Si el sitio cambia su
    # carcasa, las inventadas salen «sin confirmar», que es lo seguro, y esta
    # prueba es la que lo dice.
    if codigo == 404:
        # Antes valía: era «lo que Cotejo también lee como ausencia». Ya no lo
        # lee así: la relatoría responde la carcasa a lo que no tiene, nunca un
        # 404, y uno viene de algo que está entre medias. Que la sonda lo diera
        # por bueno escondía justo lo que tiene que contar: desde AWS, las
        # inventadas saldrían «sin confirmar».
        return False, ("un 404 donde la relatoría responde su carcasa: algo entre medias "
                       "(¿un cortafuegos?) responde por ella, y Cotejo no lo cree como ausencia")
    if cc._es_carcasa(cuerpo):
        return True, "la carcasa de la aplicación, como para cualquier dirección desconocida"
    _, n = _palabras(cuerpo)
    if n:
        return False, f"{n} palabras donde se esperaba la carcasa: ¿una página de desafío?"
    return False, ("sin palabras pero sin la firma de la carcasa (<app-root> y el título de la "
                   "Corte): ¿un desafío hecho de <script>, o el sitio cambió su carcasa?")


def _comprobar_datos(codigo, cuerpo: bytes) -> tuple[bool, str]:
    filas = json.loads(cuerpo.decode("utf-8"))
    if not isinstance(filas, list):
        return False, f"se esperaba una lista y llegó {type(filas).__name__}"
    ids = [str(f.get("sentencia")) for f in filas if isinstance(f, dict) and f.get("sentencia")]
    return len(ids) == 2, ", ".join(ids) or "filas sin «sentencia»"


def _comprobar_suprema(codigo, cuerpo: bytes) -> tuple[bool, str]:
    datos = json.loads(cuerpo.decode("utf-8"))
    r = ((datos.get("data") or {}).get("getSearchResult") or {}) if isinstance(datos, dict) else {}
    patron = cs._patron_titulo(*cs.CANARIO)
    titulos = [x.get("title") or "" for x in r.get("searchResults") or [] if isinstance(x, dict)]
    hallado = next((t for t in titulos if patron.match(t)), None)
    return hallado is not None, (f"{r.get('numOfResults')} resultados; "
                                 + (hallado if hallado else "ninguno es la SC5686-2018"))


def _probar(codigos: _Codigos, nombre: str, cliente: red.Cliente, url: str, comprobar,
            datos: bytes | None = None, cabeceras: dict | None = None) -> dict:
    # La pausa de cortesía se espera aquí y no dentro de pedir(), para que los
    # segundos midan la respuesta del servidor y no los 1,5 s de cortesía
    espera = cliente.pausa - (time.time() - cliente.ultima)
    if espera > 0:
        time.sleep(espera)
    codigos.ultimo = None
    t0 = time.monotonic()
    cuerpo, error = None, None
    try:
        cuerpo = cliente.pedir(url, datos=datos, cabeceras=cabeceras)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
    segundos = round(time.monotonic() - t0, 2)
    ok, detalle = False, None
    if cuerpo is not None:
        try:
            ok, detalle = comprobar(codigos.ultimo, cuerpo)
        except Exception as exc:                # un JSON roto también es un diagnóstico
            ok, detalle = False, f"respuesta inesperada: {type(exc).__name__}"
    prueba: dict = {"nombre": nombre, "ok": ok}
    if codigos.ultimo is not None:
        prueba["codigo"] = codigos.ultimo
    if error is not None:
        prueba["error"] = error
    prueba.update(bytes=len(cuerpo or b""), segundos=segundos, detalle=detalle)
    return prueba


def _sonda() -> dict:
    """¿Responden las fuentes a una IP de AWS, y con lo que Cotejo espera? (DISEÑO §5)

    Clientes propios, con la pausa de cada servidor y sin reintentos: se mide
    una petición, y el cortacircuitos de las consultas no se entera. Si la
    relatoría falla tres veces seguidas, el cortacircuitos de la sonda corta la
    cuarta, que es lo cortés.
    """
    t0 = time.monotonic()
    relatoria = red.Cliente("sonda: relatoría", cc.PAUSA_RELATORIA, reintentos=1, tiempo_espera=TIEMPO_ESPERA)
    datos_gov = red.Cliente("sonda: datos.gov.co", 1.0, reintentos=1, tiempo_espera=TIEMPO_ESPERA)
    suprema = red.Cliente("sonda: Corte Suprema", cs.PAUSA, reintentos=1, tiempo_espera=TIEMPO_ESPERA)
    prefijo, numero, anio = cs.CANARIO
    consulta = (
        "{ getSearchResult(searchQuery:{ query: %s typeOfQuery: %s start: 0 isExact: true "
        'magistrate: "" year: %s autoSentencia: "" order: "" roomTutelas: "" addedQueries: [] }) '
        "{ searchResults { title } numOfResults } }"
    ) % (json.dumps(cs.codigo(prefijo, numero, anio)), json.dumps(cs.SALAS[prefijo]), json.dumps(str(anio)))

    codigos = _Codigos()
    # urlopen usa el abridor instalado: así se ve el código sin tocar red.py
    urllib.request.install_opener(urllib.request.build_opener(codigos))
    try:
        pruebas = [
            _probar(codigos, "portada_constitucional", relatoria,
                    "https://www.corteconstitucional.gov.co/", _comprobar_portada),
            _probar(codigos, "relatoria_T-406-92", relatoria, cc.url_relatoria("T", "406", 1992),
                    _comprobar_sentencia("T", "406")),
            _probar(codigos, "relatoria_inexistente", relatoria, cc.url_relatoria("T", "999", 1992),
                    _comprobar_carcasa),
            _probar(codigos, "relatoria_T-760-08_range", relatoria, cc.url_relatoria("T", "760", 2008),
                    _comprobar_sentencia("T", "760", ventana=cc._BYTES_CABECERA + 1),
                    cabeceras={"Range": f"bytes=0-{cc._BYTES_CABECERA}"}),
            # La segunda ventana de la nulidad (DISEÑO §1d): una página de Word con
            # 150 KB de <style> antes del texto (3,4 MB entera). Si con 300 KB deja
            # de reconocerse, cada nulidad de una de esas baja la página entera.
            _probar(codigos, "relatoria_C-355-06_range_300k", relatoria, cc.url_relatoria("C", "355", 2006),
                    _comprobar_sentencia("C", "355", ventana=cc._BYTES_VENTANA),
                    cabeceras={"Range": f"bytes=0-{cc._BYTES_VENTANA - 1}"}),
            _probar(codigos, "datos_gov_dos_filas", datos_gov,
                    cc.API_DATOS + "?" + urllib.parse.urlencode({"$limit": "2"}), _comprobar_datos),
            _probar(codigos, "corte_suprema_SC5686-2018", suprema, cs.API, _comprobar_suprema,
                    datos=json.dumps({"query": consulta}).encode("utf-8"),
                    cabeceras={"Content-Type": "application/json"}),
        ]
    finally:
        urllib.request.install_opener(None)     # vuelve el de serie
    salida = {"accion": "sonda", "ok": all(p["ok"] for p in pruebas), "pruebas": pruebas,
              "segundos": round(time.monotonic() - t0, 2)}
    _registrar({"evento": "sonda", **salida})
    return salida


def _accion_directa(event: dict) -> dict:
    accion = event.get("accion")
    if accion == "actualizar_indice":
        return _actualizar_indice()
    if accion == "sonda":
        return _sonda()
    return {"accion": accion if isinstance(accion, str) else None, "ok": False,
            "motivo": "acción desconocida"}


# ------------------------------------------------------------------ handler
def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    contexto = event.get("requestContext")
    # Las acciones solo con un evento directo: uno que no traiga requestContext.http.
    # Una Function URL siempre lo trae, así que un POST público con
    # {"accion": "sonda"} en el cuerpo, o con «accion» en el propio evento, nunca
    # llega aquí y no pone a la demo a pedirle cosas a la Corte saltándose la cuota.
    url = isinstance(contexto, dict) and "http" in contexto

    if not _listo:
        try:
            configurar()
        except Exception as exc:
            _registrar({"evento": "error", "fase": "arranque", "tipo": type(exc).__name__})
            return _error(500, "interno") if url else {"ok": False, "motivo": type(exc).__name__}

    if not url:
        return _accion_directa(event)

    http = contexto["http"] if isinstance(contexto["http"], dict) else {}
    metodo = str(http.get("method") or "").upper()
    ruta = event.get("rawPath") or http.get("path") or "/"
    if metodo == "GET" and ruta == "/":
        try:
            return _salud()
        except Exception as exc:
            _registrar({"evento": "error", "fase": "salud", "tipo": type(exc).__name__})
            return _error(500, "interno")
    if metodo == "POST" and ruta == "/verificar":
        return _post_verificar(event, str(http.get("sourceIp") or ""), context)
    return _respuesta(404, {"error": "ruta"})
