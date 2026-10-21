"""Límite diario de la demo pública, en DynamoDB (DISEÑO.md §2, paso 5).

Cada verificación puede costarle a la Corte decenas de peticiones: la relatoría
se consulta con 1,5 s de pausa y el buscador de la Corte Suprema con 1 s, y un
escrito con diez sentencias son diez textos y diez notas de nulidad. La Corte
ya cortó la conexión a la máquina de Pablo tras unas 800 peticiones en una
tarde (ESTADO.md, 1c). Una URL pública sin tope puede llegar a eso en minutos,
y entonces el veto lo sufriría la demo, o peor, el resto de usuarios de la
relatoría desde los mismos rangos de AWS. Por eso: 3 verificaciones por IP y
día, y 100 en total, con el día de Colombia (UTC−5), que es el de quien la usa.

Privacidad (principio 4): la IP no se guarda. La clave del contador es un
HMAC-SHA256, con sal secreta, de «fecha|ip». La sal tiene que ser secreta
porque las IPv4 son pocas: con ella a la vista bastaría calcular el HMAC de las
cuatro mil millones para saber qué IP hay detrás de cada contador. Y la fecha
va dentro del hash para que ni siquiera con la tabla delante se puedan enlazar
dos días de la misma IP. El TTL de 2 días borra lo que queda.

La tabla solo se toca con UpdateItem (ADD, con condición donde hay un tope): es
una operación atómica, así que dos contenedores a la vez no pueden pasar los
dos del tope. Y no hay lectura previa, que costaría otra petición y abriría la
carrera entre leer y escribir.

Contar consultas no bastaba: una sola, con 446 sentencias inventadas, le hacía
41 peticiones a la relatoría, y 100 consultas así son unas 4.100 en un día
(revisión adversarial). Por eso hay un tercer contador, el de peticiones a las
fuentes (`pet#<fecha>`, 600 al día): antes de cada consulta se comprueba que
quede presupuesto, y después se le suman las peticiones medidas. Solo se pasa
del tope en lo que haga la consulta que estaba en vuelo al cruzarlo: una, con la
concurrencia reservada 1 (DISEÑO §2). Lo que esa consulta puede pedir no lo
acotan sus 15 providencias, sino el plazo: un código de la Corte Suprema son
hasta 4 variantes × 5 páginas, y red.Cliente deja al menos 1 s entre dos
intentos al mismo servidor, así que caben una petición por segundo de plazo y
una más por cada cambio de Corte: 60 + 15 con los valores de serie. Es la cuenta
que comprueba la validación de max_peticiones_dia en infra/variables.tf.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import math
import re
import time

COLOMBIA = dt.timezone(dt.timedelta(hours=-5))
TTL_SEGUNDOS = 2 * 24 * 3600

# Las expresiones, en un solo sitio: TablaEnMemoria comprueba que le llegan
# exactamente estas, y así un cambio aquí no puede dejar a local.py y a las
# pruebas emulando una semántica distinta de la que ve DynamoDB.
ACTUALIZACION = "ADD n :uno SET expira = :exp"
CONDICION = "attribute_not_exists(n) OR n < :max"
# El presupuesto de peticiones se comprueba sin gastarlo (ADD de cero, con la
# misma condición: falla si ya no queda) y se gasta después, sin condición,
# con lo que se midió: lo hecho, hecho está, y tiene que contar.
COMPROBACION = "ADD n :cero SET expira = :exp"
SUMA = "ADD n :peticiones SET expira = :exp"

_LARGO_HUELLA = 32      # 128 bits del HMAC: de sobra para no chocar en un día


class LimiteIP(Exception):
    """Esta IP ya hizo sus consultas de hoy."""

    def __init__(self, maximo: int, reintentar_en: int):
        super().__init__(f"límite de {maximo} consultas por IP y día")
        self.maximo = maximo
        self.reintentar_en = reintentar_en


class LimiteGlobal(Exception):
    """La demo entera llegó a su tope de hoy: de consultas o de peticiones a las fuentes."""

    def __init__(self, maximo: int, reintentar_en: int, de: str = "consultas"):
        super().__init__(f"límite de {maximo} {de} por día")
        self.maximo = maximo
        self.reintentar_en = reintentar_en
        self.de = de


class TablaNoDisponible(Exception):
    """DynamoDB falló por algo que no es el tope. Sin límite no se sirve."""


# ------------------------------------------------------------------- fechas
def fecha_colombia(ahora: float) -> str:
    """El día de Colombia de un instante: «2026-10-20»."""
    return dt.datetime.fromtimestamp(ahora, COLOMBIA).date().isoformat()


def segundos_hasta_medianoche(ahora: float) -> int:
    """Lo que falta para que se renueve el cupo: el Retry-After del 429."""
    local = dt.datetime.fromtimestamp(ahora, COLOMBIA)
    manana = (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((manana - local).total_seconds()))


def huella(sal: str, fecha: str, ip: str) -> str:
    """HMAC-SHA256 de «fecha|ip» con la sal secreta, truncado."""
    return hmac.new(sal.encode("utf-8"), f"{fecha}|{ip}".encode("utf-8"),
                    hashlib.sha256).hexdigest()[:_LARGO_HUELLA]


def normalizar_ip(ip: str) -> str:
    """Lo que cuenta como un visitante: la IPv4, o la /64 de una IPv6.

    La Function URL es de doble pila, y una conexión doméstica o un VPS con
    IPv6 tiene una /64 entera para él: con la dirección literal, 100 direcciones
    de la misma /64 se comían las 100 consultas del día sin un solo limite_ip
    (reproducido). Una IPv4 mapeada (::ffff:a.b.c.d) es esa IPv4: si no, la
    misma máquina tendría dos cupos según por dónde entrara. Lo que no se pueda
    interpretar (vacío, basura) va tal cual: cuenta como un visitante más, y no
    rompe la consulta.
    """
    try:
        a = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            return str(a.ipv4_mapped)
        # Por el entero y no por la cadena: una dirección con zona («fe80::1%eth0»)
        # no se deja convertir en red con «/64» detrás
        return str(ipaddress.IPv6Network((int(a) >> 64 << 64, 64)))
    return str(a)


def _es_condicion(exc: Exception) -> bool:
    """¿Es el «ya llegó al tope» de DynamoDB?

    Se mira el código del error y no la clase: boto3 genera la clase
    (client.exceptions.ConditionalCheckFailedException) al crear el cliente, y
    este módulo tiene que funcionar sin boto3, con la tabla en memoria.
    """
    error = (getattr(exc, "response", None) or {}).get("Error") or {}
    return error.get("Code") == "ConditionalCheckFailedException"


# -------------------------------------------------------------------- cuota
class Cuota:
    def __init__(self, ddb, tabla: str, sal: str, max_ip: int, max_dia: int,
                 max_peticiones: int = 600, reloj=time.time):
        if not sal:
            # Con la sal vacía, el HMAC de una IP lo puede calcular cualquiera
            raise ValueError("falta la sal secreta del visitante (SAL_VISITANTE)")
        self.ddb = ddb
        self.tabla = tabla
        self.sal = sal
        self.max_ip = max_ip
        self.max_dia = max_dia
        self.max_peticiones = max_peticiones
        self.reloj = reloj
        # Caché negativa, en la memoria del contenedor: las huellas y el día que
        # DynamoDB ya dio por agotados. DynamoDB cobra la escritura aunque falle
        # la condición, y sin esto cada intento de una IP que ya agotó lo suyo
        # era un UpdateItem: 1.000 POST daban 1.003 escrituras (reproducido).
        # Solo sirve para negar: conceder lo sigue decidiendo el UpdateItem
        # atómico, y cada contenedor nuevo (Lambda los recicla) deja escapar como
        # mucho una escritura de más por huella. Las claves son huellas, nunca la IP.
        self._hoy: str | None = None
        self._ips_agotadas: set[str] = set()
        self._dia_agotado: str | None = None        # "consultas" o "peticiones"

    def _al_dia(self, fecha: str) -> None:
        # Medianoche en Colombia: lo agotado ayer ya no lo está, y se vacía
        # entero, así que la memoria no crece de un día para otro
        if fecha != self._hoy:
            self._hoy, self._ips_agotadas, self._dia_agotado = fecha, set(), None

    def _global(self, de: str, reintentar: int) -> LimiteGlobal:
        if de == "peticiones":
            return LimiteGlobal(self.max_peticiones, reintentar, de="peticiones")
        return LimiteGlobal(self.max_dia, reintentar)

    def consumir(self, ip: str) -> int | None:
        """Gasta una consulta de la IP y una del total, si queda presupuesto de
        peticiones. Devuelve las que le quedan a la IP.

        Primero la IP y después el total: una IP que ya agotó lo suyo y sigue
        insistiendo no se come el cupo de los demás. Al revés, si el total o el
        presupuesto están agotados, la IP pierde una consulta que no se le
        sirvió; da igual, porque todos los contadores se renuevan a la misma
        medianoche.
        """
        ahora = self.reloj()
        fecha = fecha_colombia(ahora)
        expira = int(ahora) + TTL_SEGUNDOS
        reintentar = segundos_hasta_medianoche(ahora)
        self._al_dia(fecha)
        h = huella(self.sal, fecha, normalizar_ip(ip))
        if h in self._ips_agotadas:
            raise LimiteIP(self.max_ip, reintentar)
        if self._dia_agotado:
            raise self._global(self._dia_agotado, reintentar)
        try:
            n_ip = self._sumar(f"ip#{fecha}#{h}", ACTUALIZACION, {":uno": {"N": "1"}},
                               self.max_ip, expira, LimiteIP(self.max_ip, reintentar))
        except LimiteIP:
            self._ips_agotadas.add(h)
            raise
        try:
            self._sumar(f"dia#{fecha}", ACTUALIZACION, {":uno": {"N": "1"}},
                        self.max_dia, expira, self._global("consultas", reintentar))
            self._sumar(f"pet#{fecha}", COMPROBACION, {":cero": {"N": "0"}},
                        self.max_peticiones, expira, self._global("peticiones", reintentar))
        except LimiteGlobal as e:
            self._dia_agotado = e.de
            raise
        return None if n_ip is None else max(0, self.max_ip - n_ip)

    def sumar_peticiones(self, n: int) -> None:
        """Suma al presupuesto del día las peticiones que hizo una consulta.

        Sin condición: ya se hicieron. Si con ellas se llega al tope, este
        contenedor lo apunta y la siguiente consulta se rechaza sin escribir
        (ni gastarle al visitante una consulta que no se le iba a servir).
        Lanza TablaNoDisponible si DynamoDB falla.
        """
        if n <= 0:
            return                  # todo salió de la caché: no hay nada que sumar
        ahora = self.reloj()
        fecha = fecha_colombia(ahora)
        self._al_dia(fecha)
        total = self._sumar(f"pet#{fecha}", SUMA, {":peticiones": {"N": str(n)}},
                            None, int(ahora) + TTL_SEGUNDOS, None)
        if total is not None and total >= self.max_peticiones:
            self._dia_agotado = "peticiones"

    def _sumar(self, clave: str, expresion: str, valores: dict, maximo: int | None,
               expira: int, limite: Exception | None) -> int | None:
        """Un UpdateItem. Con `maximo`, condicional: si no cabe, lanza `limite`."""
        argumentos = dict(TableName=self.tabla, Key={"pk": {"S": clave}}, UpdateExpression=expresion,
                          ExpressionAttributeValues={**valores, ":exp": {"N": str(expira)}},
                          ReturnValues="UPDATED_NEW")
        if maximo is not None:
            argumentos["ConditionExpression"] = CONDICION
            argumentos["ExpressionAttributeValues"][":max"] = {"N": str(maximo)}
        try:
            r = self.ddb.update_item(**argumentos)
        except Exception as exc:
            if limite is not None and _es_condicion(exc):
                raise limite from None
            raise TablaNoDisponible(type(exc).__name__) from exc
        n = ((r or {}).get("Attributes") or {}).get("n", {}).get("N")
        return int(n) if n is not None else None


# --------------------------------------------------- DynamoDB en memoria
class CondicionFallida(Exception):
    """Lo que lanza DynamoDB cuando no se cumple la condición, con su mismo código."""

    def __init__(self):
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException",
                                   "Message": "The conditional request failed"}}


# Lo que TablaEnMemoria sabe emular: (actualización, condición) → la variable que suma
_EMULADAS = {(ACTUALIZACION, CONDICION): ":uno", (COMPROBACION, CONDICION): ":cero",
             (SUMA, None): ":peticiones"}


class TablaEnMemoria:
    """DynamoDB falso para demo/local.py y las pruebas, con la semántica de Cuota.

    Solo entiende las actualizaciones y la condición de este módulo, y rechaza
    cualquier otra: si Cuota cambiara de expresión, local.py y las pruebas
    fallarían en vez de seguir emulando la vieja. No borra por TTL, igual que
    DynamoDB, que tarda hasta dos días en hacerlo; como la fecha va en la clave,
    un contador caducado no cuenta para el día siguiente.

    `escrituras` apunta la clave de cada UpdateItem, también de los que fallan
    por la condición: DynamoDB los cobra igual.
    """

    def __init__(self):
        self.elementos: dict[str, dict] = {}
        self.escrituras: list[str] = []

    def update_item(self, *, TableName, Key, UpdateExpression, ExpressionAttributeValues,
                    ConditionExpression=None, ReturnValues="NONE"):
        variable = _EMULADAS.get((UpdateExpression, ConditionExpression))
        if variable is None:
            raise ValueError("TablaEnMemoria solo emula las actualizaciones de cuota.py")
        # DynamoDB rechaza (ValidationException) un valor que las expresiones no
        # usan, o uno que usan y no llega: la suma, sin condición, no puede llevar
        # :max. Sin esto, ese error solo se vería ya en AWS.
        usadas = set(re.findall(r":\w+", UpdateExpression + " " + (ConditionExpression or "")))
        if usadas != set(ExpressionAttributeValues):
            raise ValueError(f"valores {sorted(ExpressionAttributeValues)} para las variables {sorted(usadas)}")
        pk = Key["pk"]["S"]
        self.escrituras.append(pk)
        valores = ExpressionAttributeValues
        actual = self.elementos.get(pk, {})
        n = int(actual["n"]["N"]) if "n" in actual else None
        if ConditionExpression is not None and n is not None and not n < int(valores[":max"]["N"]):
            raise CondicionFallida()
        nuevo = {"pk": {"S": pk}, "n": {"N": str((n or 0) + int(valores[variable]["N"]))},
                 "expira": valores[":exp"]}
        self.elementos[pk] = nuevo
        if ReturnValues == "UPDATED_NEW":
            return {"Attributes": {"n": nuevo["n"], "expira": nuevo["expira"]}}
        return {}
