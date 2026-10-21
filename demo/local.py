"""La Lambda de la demo en local, sin AWS: para probar la página contra ella.

    .venv/Scripts/python demo/local.py              # http://127.0.0.1:8011
    .venv/Scripts/python demo/pagina/vista.py       # y la página en http://localhost:8765/lab/cotejo.html?api=http://127.0.0.1:8011/

Cada petición se convierte en el evento que manda una Function URL (payload
2.0) y se le pasa a `lambda_handler` tal cual, con un contexto que cuenta el
tiempo que le queda como lo haría el de Lambda (180 s, el timeout de la
función), así que lo que se prueba es el mismo código que corre en AWS,
también el plazo y los topes (MAX_CITAS, MAX_PROVIDENCIAS, MAX_PETICIONES_DIA,
del entorno o los de serie). Cambian tres cosas:

- La cuota va en memoria (`cuota.TablaEnMemoria`, con la semántica de
  DynamoDB, también la del presupuesto de peticiones) y con una sal nueva en
  cada arranque: reiniciar el servidor renueva las consultas del día.
- No hay S3: ni espejo ni índice de S3. El índice sale de .cache con
  `cc.Indice.cargar()`, que lo descarga de datos.gov.co si tiene más de una
  semana; se carga al arrancar, antes de atender nada.
- Las cabeceras CORS las pone este servidor para http://localhost:8765. En
  AWS las pone la Function URL, y la función no debe ponerlas: duplicadas, el
  navegador rechaza la respuesta.

Atiende de una en una, como la función en AWS con su concurrencia reservada 1
(DISEÑO §2): una petición que llega mientras otra se verifica recibe en el acto
un 429 sin cuerpo nuestro, como el que da la Function URL sin invocar la
función, y la página dice que otra persona está verificando. Antes el servidor
era de un solo hilo y la segunda esperaba en cola a que acabara la primera: en
local nunca se veía ese 429. El handler y los clientes de las fuentes (pausa,
cortacircuitos) no están hechos para hilos, y nunca corren dos a la vez: los
hilos solo sirven para poder contestar ese 429. Si el 429 de AWS trae las
cabeceras CORS no está comprobado; este las pone, para que la página lo lea.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RAIZ), str(RAIZ / "demo")]
# Antes de importar la Lambda, que si no fija /tmp/cotejo-cache: en local se
# usa la caché de siempre, la que ya tiene casi todo y no molesta a la Corte
os.environ.setdefault("COTEJO_CACHE", str(RAIZ / ".cache"))

import cuota  # noqa: E402
import lambda_function  # noqa: E402

PUERTO = 8011
ORIGEN_PAGINA = "http://localhost:8765"
MAX_CUERPO = 6 * 1024 * 1024        # lo que admite una Function URL
TIMEOUT_LAMBDA = 180                # timeout_segundos de infra/variables.tf

# La única «instancia» de la función: quien no la consigue al momento recibe el
# 429. Sin «error» en el cuerpo, que es como la página distingue el 429 de la
# Function URL (no se gastó nada) de los nuestros (limite_ip, limite_global).
TURNO = threading.Lock()
OCUPADA = b'{"Message": "Rate Exceeded."}'


class Contexto:
    """Lo que la Lambda usa del objeto context: cuánto le queda a la invocación.

    Sin él, el handler toma el plazo de serie y nunca recorta por el timeout:
    en local no se vería el paso 4 de DISEÑO §2.
    """

    def __init__(self, timeout: float = TIMEOUT_LAMBDA):
        self.fin = time.monotonic() + timeout

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int((self.fin - time.monotonic()) * 1000))


class Manejador(http.server.BaseHTTPRequestHandler):
    server_version = "cotejo-local"

    def _cors(self) -> dict:
        # Como la Function URL: solo para el origen admitido, y sin comodín
        if self.headers.get("Origin") != ORIGEN_PAGINA:
            return {}
        return {"Access-Control-Allow-Origin": ORIGEN_PAGINA, "Vary": "Origin",
                # Sin esto fetch no deja leer el Retry-After de un limite_ip
                "Access-Control-Expose-Headers": "retry-after"}

    def _evento(self, metodo: str, cuerpo: bytes) -> dict:
        partes = urllib.parse.urlsplit(self.path)
        try:
            texto, en_base64 = (cuerpo.decode("utf-8") if cuerpo else None), False
        except UnicodeDecodeError:
            # La Function URL manda en base64 lo que no es texto
            texto, en_base64 = base64.b64encode(cuerpo).decode("ascii"), True
        ahora = time.time()
        return {
            "version": "2.0", "routeKey": "$default",
            "rawPath": partes.path or "/", "rawQueryString": partes.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "requestContext": {
                "accountId": "anonymous", "apiId": "local",
                "domainName": f"127.0.0.1:{self.server.server_address[1]}",
                "http": {"method": metodo, "path": partes.path or "/", "protocol": self.request_version,
                         "sourceIp": self.client_address[0], "userAgent": self.headers.get("User-Agent", "")},
                "requestId": secrets.token_hex(8), "routeKey": "$default", "stage": "$default",
                "timeEpoch": int(ahora * 1000),
            },
            "body": texto, "isBase64Encoded": en_base64,
        }

    def _enviar(self, codigo: int, cabeceras: dict, cuerpo: bytes) -> None:
        self.send_response(codigo)
        for k, v in {**cabeceras, **self._cors()}.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _atender(self, metodo: str) -> None:
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > MAX_CUERPO:
            # La Function URL corta antes de invocar la función
            self._enviar(413, {"Content-Type": "application/json"}, b'{"message":"Request Entity Too Large"}')
            return
        cuerpo = self.rfile.read(largo) if largo else b""
        # Cualquier invocación ocupa el turno, también GET / (salud): en AWS
        # también cuenta para la concurrencia
        if not TURNO.acquire(blocking=False):
            self._enviar(429, {"Content-Type": "application/json"}, OCUPADA)
            return
        try:
            r = lambda_function.lambda_handler(self._evento(metodo, cuerpo), Contexto())
        finally:
            TURNO.release()
        self._enviar(r["statusCode"], r.get("headers") or {}, (r.get("body") or "").encode("utf-8"))

    def do_GET(self) -> None:
        self._atender("GET")

    def do_POST(self) -> None:
        self._atender("POST")

    def do_OPTIONS(self) -> None:
        # El preflight lo contesta la Function URL sin invocar la función
        cabeceras = {}
        if self.headers.get("Origin") == ORIGEN_PAGINA:
            cabeceras = {"Access-Control-Allow-Methods": "GET, POST",
                         "Access-Control-Allow-Headers": "content-type",
                         "Access-Control-Max-Age": "7200"}
        self._enviar(204, cabeceras, b"")

    def log_message(self, formato: str, *args) -> None:
        # Sin la IP del cliente, como en AWS (principio 4), aunque aquí sea 127.0.0.1
        sys.stderr.write("local · " + (formato % args) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--puerto", type=int, default=PUERTO)
    a = p.parse_args()

    # Sin S3 aunque el entorno tenga BUCKET: en local no se toca AWS
    lambda_function.configurar(s3=None, ddb=cuota.TablaEnMemoria(), bucket="",
                               tabla="cuota-local", sal=secrets.token_hex(16))
    indice = lambda_function.indice_actual()
    print(f"Índice: {len(indice)} providencias, hasta {indice.fecha_mas_reciente}", flush=True)
    print(f"Topes: {lambda_function._max_citas} providencias distintas por texto, "
          f"{lambda_function._max_providencias} consultadas, "
          f"{lambda_function._cuota.max_peticiones} peticiones a las fuentes al día", flush=True)
    # Con hilos para poder contestar el 429 de la concurrencia 1 (ver TURNO)
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", a.puerto), Manejador)
    print(f"Lambda local en http://127.0.0.1:{a.puerto} (CORS para {ORIGEN_PAGINA}; "
          "una verificación a la vez, la segunda recibe 429)", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
