"""Acceso a servidores públicos con cortesía y sin falsos «no existe».

Cada fuente oficial tiene su propio `Cliente`, con su pausa y su
cortacircuitos, porque que un servidor nos corte no dice nada de los otros.

Por qué existe este módulo: tras unas 800 peticiones en una tarde de
evaluación, el sitio de la Corte Constitucional dejó de responder a esta
máquina —ni la portada—, cortando la conexión TLS al instante. Si eso le pasa
a un abogado, se queda sin acceso a la relatoría por culpa de la herramienta.
De ahí las tres reglas:

1. Pausa entre peticiones al mismo servidor.
2. Reintentos con espera creciente para lo transitorio: cortes de conexión,
   tiempos de espera, 429 y 5xx.
3. Cortacircuitos: tras varios fallos seguidos, dejar de insistir durante un
   rato. Quien llama recibe `ServicioNoDisponible` y tiene que tratarlo como
   «sin confirmar», nunca como «no existe».

Un 403 es un bloqueo, no un error de la petición: es lo que responde un
cortafuegos que ha vetado nuestros rangos de direcciones (el riesgo nuevo al
consultar desde AWS). Se trata como un fallo del servicio, sin reintentar.

Y un plazo (`Cliente.hasta`): quien tiene un tiempo máximo, como la demo
pública, no puede esperar a que acabe una providencia para mirar el reloj. Una
sola de la Corte Suprema son hasta veinte búsquedas, y con respuestas lentas
pero correctas pasaba del tiempo de la Lambda, que muere sin responder nada.

Un cortafuegos también puede responder 200 con una página de desafío. Para
`pedir()` eso es un éxito; solo quien la lee sabe que no es lo que pidió, y lo
avisa con `respuesta_irreconocible()` (ver allí).

El plazo también vale mientras llega el cuerpo: el tiempo de espera de urlopen
acota cada recepción, no la respuesta entera, y una página de 3,4 MB que llega
despacio pero sin pararse no lo agotaba nunca (la C-355/06, que la nulidad de
las páginas de Word llegó a bajar entera). Por eso se lee por trozos, mirando
el reloj entre uno y otro, y con un tope de tamaño.
"""
from __future__ import annotations

import http.client
import io
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CABECERA = {"User-Agent": "Cotejo/0.1 (verificacion de citas juridicas)"}


def dir_cache() -> Path:
    base = os.environ.get("COTEJO_CACHE")
    ruta = Path(base) if base else Path(__file__).resolve().parents[2] / ".cache"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


class ServicioNoDisponible(Exception):
    """El servidor no responde. NO quiere decir que lo buscado no exista."""


class SinTiempo(ServicioNoDisponible):
    """Se acabó el plazo de quien pregunta antes de tener la respuesta.

    Tampoco dice nada de lo buscado, y tampoco del servidor: no cuenta para el
    cortacircuitos. Si contara, tres consultas que llegan al final de su plazo
    dejarían a todas las demás quince minutos sin la relatoría.
    """


# Lo mínimo que tiene que quedar del plazo para empezar un intento. Con menos,
# la respuesta casi seguro no llega a tiempo, y una petición que se abandona
# solo le da trabajo al servidor.
MINIMO_INTENTO = 1.0

# El cuerpo se lee por trozos de 64 KB, con una sola recepción del socket por
# trozo (read1): entre trozos se mira el plazo. Con read(), un trozo esperaba a
# llenarse, y a 1 KB/s eran 64 s sin mirar el reloj.
TROZO = 64 * 1024
# Ninguna página de la relatoría se acerca: la C-355/06, de las más grandes, son
# 3,4 MB. Lo que pasa de aquí no es una sentencia, y bajarlo entero es tiempo y
# memoria de la Lambda (y, con la cuenta por transferencia, dinero).
TOPE_RESPUESTA = 16 * 1024 * 1024


class _Desmedida(Exception):
    """La respuesta pasa de TOPE_RESPUESTA (solo dentro de pedir)."""


class _Redirigida(Exception):
    """urllib siguió una redirección a otra dirección (solo dentro de pedir)."""


def _es_tiempo_agotado(exc: BaseException) -> bool:
    # El que salta al conectar, urlopen lo envuelve en un URLError; el de la
    # lectura llega tal cual
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def _por_leer(r) -> int | None:
    """Bytes que el Content-Length dice que faltan (http.client los descuenta al leer)."""
    largo = getattr(r, "length", None)
    return largo if isinstance(largo, int) and not isinstance(largo, bool) else None


class Cliente:
    def __init__(self, nombre: str, pausa: float, reintentos: int = 3,
                 fallos_para_cortar: int = 3, corte_segundos: float = 15 * 60,
                 tiempo_espera: float = 60):
        self.nombre = nombre
        self.pausa = pausa
        self.reintentos = reintentos
        self.fallos_para_cortar = fallos_para_cortar
        self.corte_segundos = corte_segundos
        self.tiempo_espera = tiempo_espera
        self.ultima = 0.0
        self.fallos_seguidos = 0
        self.cortado_hasta = 0.0
        # Intentos de red reales, reintentos incluidos; lo que sale de la caché
        # o del cortacircuitos no cuenta. Sirve para medir cuánto le pide cada
        # consulta a cada servidor: la demo pública tiene que ser cortés también
        # en el agregado, y eso solo se sabe contando.
        self.peticiones = 0
        # Plazo en time.monotonic(), o None. Lo fija verificar() mientras dura.
        self.hasta: float | None = None
        # Veces que pedir() se rindió por el plazo: verificar() lo compara antes
        # y después de cada providencia para saber si la respuesta quedó a medias.
        self.cortes_por_tiempo = 0
        # Respuestas con 200 que quien llama no reconoce, seguidas (ver
        # respuesta_irreconocible). pedir() no lo reinicia: cada desafío llega
        # con 200, y el contador de fallos se volvía a poner a 0 en cada uno.
        self.raras_seguidas = 0
        # Código HTTP de la última respuesta de pedir(), o None si no la hubo.
        # pedir() devuelve b"" tanto para un 404 como para un 200 sin cuerpo, y
        # solo el primero dice «no la tengo»: un 200 vacío puede ser cualquier
        # cosa, también un bloqueo.
        self.ultimo_codigo: int | None = None

    def disponible(self) -> bool:
        return time.time() >= self.cortado_hasta

    def reiniciar(self) -> None:
        self.fallos_seguidos, self.raras_seguidas, self.cortado_hasta = 0, 0, 0.0

    def _fallo(self) -> None:
        self.fallos_seguidos += 1
        if self.fallos_seguidos >= self.fallos_para_cortar:
            self.cortado_hasta = time.time() + self.corte_segundos

    def respuesta_irreconocible(self) -> None:
        """El servidor respondió 200, pero con algo que no es lo que se pidió.

        Una página de desafío de un cortafuegos llega con 200: pedir() la cuenta
        como un éxito y pone a 0 los fallos. Con un desafío persistente el
        cortacircuitos no se abría nunca, y cada consulta de la demo volvía a
        gastar sus peticiones contra una puerta que ya estaba filtrando, hasta
        agotar el presupuesto del día. Quien lee la respuesta y no la reconoce
        llama a esto, y al llegar a `fallos_para_cortar` se corta igual que con
        los fallos de red.
        """
        self.raras_seguidas += 1
        if self.raras_seguidas >= self.fallos_para_cortar:
            self.cortado_hasta = time.time() + self.corte_segundos

    def respuesta_valida(self) -> None:
        """Quien llama reconoció la respuesta: lo raro deja de ir seguido."""
        self.raras_seguidas = 0

    def _sin_tiempo(self, para: str) -> SinTiempo:
        self.cortes_por_tiempo += 1
        return SinTiempo(f"{self.nombre}: no quedaba tiempo para {para} antes del plazo")

    def _leer(self, r) -> bytes:
        """El cuerpo, por trozos: el plazo se mira entre uno y otro, con tope de tamaño.

        Cada trozo es una sola recepción del socket (read1), y el tiempo de
        espera del intento acota cada una: del plazo se puede pasar como mucho
        eso (12 s en la Lambda, dentro de los 25 que se guarda para responder),
        y no lo que tarde en llegar la página entera.

        Por trozos solo se lee un flujo binario con búfer, como la respuesta de
        urlopen (http.client.HTTPResponse lo es). Lo demás —los dobles de las
        pruebas, que solo tienen read()— se lee de una vez. Antes se miraba si
        había read1, y un MagicMock lo tiene: devolvía un objeto de longitud 0
        que nunca era b"", y la batería se quedaba colgada en este bucle.
        """
        falta = _por_leer(r)
        if falta is not None and falta > TOPE_RESPUESTA:
            raise _Desmedida()          # lo anuncia el Content-Length: ni se empieza
        if not isinstance(r, io.BufferedIOBase):
            cuerpo = r.read()
            if len(cuerpo) > TOPE_RESPUESTA:
                raise _Desmedida()
            return cuerpo
        trozos, total = [], 0
        while True:
            if _por_leer(r) == 0:
                break                   # llegó todo lo que anunció: no se tira por el reloj
            if self.hasta is not None and time.monotonic() >= self.hasta:
                raise self._sin_tiempo("recibir la respuesta")
            trozo = r.read1(TROZO)
            if not trozo:
                break
            total += len(trozo)
            if total > TOPE_RESPUESTA:
                raise _Desmedida()
            trozos.append(trozo)
        cuerpo = b"".join(trozos)
        # read() lanza IncompleteRead si el servidor corta antes del
        # Content-Length; read1() devuelve b"" sin más, y el cuerpo cortado
        # pasaría por entero
        falta = _por_leer(r)
        if falta:
            raise http.client.IncompleteRead(cuerpo, falta)
        return cuerpo

    def pedir(self, url: str, datos: bytes | None = None,
              cabeceras: dict | None = None) -> bytes:
        """GET, o POST si hay `datos`. Un 404 devuelve b"" y deja `ultimo_codigo` en 404.

        Qué significa un 404 lo decide quien llama: la relatoría, por ejemplo,
        nunca lo da para lo que no tiene, así que allí no es una ausencia.

        Con `hasta`, lanza SinTiempo antes de una pausa o de un intento que no
        caben en el plazo, y también entre dos trozos del cuerpo; el tiempo de
        espera de cada intento se recorta a lo que queda. Una respuesta de más
        de TOPE_RESPUESTA cuenta como un fallo y no se reintenta.
        """
        self.ultimo_codigo = None
        if not self.disponible():
            raise ServicioNoDisponible(
                f"{self.nombre} dejó de responder hace poco; se espera antes de insistir")

        ultimo: Exception | None = None
        for intento in range(self.reintentos):
            espera = max(0.0, self.pausa * (2 ** intento) - (time.time() - self.ultima))
            # Antes de dormir: una pausa tras la que ya no cabe un intento es
            # tiempo perdido, y el plazo se pasaría esperando
            if self.hasta is not None and time.monotonic() + espera + MINIMO_INTENTO > self.hasta:
                raise self._sin_tiempo("la pausa y un intento" if espera else "un intento")
            if espera > 0:
                time.sleep(espera)
            limite = self.tiempo_espera
            if self.hasta is not None:
                queda = self.hasta - time.monotonic()
                if queda < MINIMO_INTENTO:
                    raise self._sin_tiempo("un intento")
                limite = min(limite, queda)
            recortado = limite < self.tiempo_espera
            self.ultima = time.time()
            try:
                req = urllib.request.Request(
                    url, data=datos, headers={**CABECERA, **(cabeceras or {})},
                    method="POST" if datos is not None else "GET")
                self.peticiones += 1
                with urllib.request.urlopen(req, timeout=limite) as r:
                    codigo = getattr(r, "status", None)
                    final = getattr(r, "url", None)
                    if isinstance(final, str) and final != url:
                        raise _Redirigida(final)
                    cuerpo = self._leer(r)
                self.ultimo_codigo = codigo if isinstance(codigo, int) else None
                self.fallos_seguidos = 0
                return cuerpo
            except _Redirigida as exc:
                # Ninguna de las fuentes redirige lo que se le pide (medido desde
                # AWS: la relatoría responde 200 o 206 en la dirección pedida, y la
                # carcasa para lo que no tiene). Una redirección viene de un
                # intermediario y su destino no es lo pedido. urllib la sigue por
                # su cuenta, sin plazo, sin tope de tamaño y sin que el salto se
                # cuente (revisión 4): se cuenta aquí, y es un fallo del servicio.
                # El cuerpo del 3xx lo lee urllib antes de llegar aquí; eso queda
                # fuera del plazo, acotado por el tiempo máximo de la Lambda.
                self.peticiones += 1
                self._fallo()
                raise ServicioNoDisponible(
                    f"{self.nombre} redirigió la petición a otra dirección ({exc}): puede "
                    "estar bloqueando esta conexión") from None
            except _Desmedida:
                # No es transitorio: la misma dirección volvería a mandar lo
                # mismo. Cuenta para el cortacircuitos, como un bloqueo
                self._fallo()
                raise ServicioNoDisponible(
                    f"{self.nombre} respondió más de {TOPE_RESPUESTA // (1024 * 1024)} MB, "
                    "que no es una página que se espere de ella") from None
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self.ultimo_codigo = 404
                    self.fallos_seguidos = 0
                    return b""
                if exc.code == 403:
                    # Antes salía como HTTPError sin contar: tres bloqueos seguidos
                    # no abrían el cortacircuitos y se seguía llamando a una puerta
                    # cerrada. Reintentar tampoco sirve: el veto no es transitorio.
                    self._fallo()
                    raise ServicioNoDisponible(
                        f"{self.nombre} rechazó la petición (HTTP 403): puede estar "
                        "bloqueando esta conexión") from exc
                if exc.code < 500 and exc.code != 429:
                    raise
                ultimo = exc
            # http.client.HTTPException: un cuerpo cortado antes de su
            # Content-Length (IncompleteRead) o una línea de estado rota no son
            # OSError. Antes salían sin reintentar y sin contar, y un servidor
            # que cerrara las conexiones a mitad nunca abría el cortacircuitos.
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError,
                    http.client.HTTPException) as exc:
                if recortado and _es_tiempo_agotado(exc):
                    # Se agotó la espera que se recortó al plazo: con la
                    # completa, quizá habría respondido. No es culpa suya.
                    raise self._sin_tiempo("recibir la respuesta") from exc
                ultimo = exc

        self._fallo()
        raise ServicioNoDisponible(f"{self.nombre}: {type(ultimo).__name__}: {ultimo}")
