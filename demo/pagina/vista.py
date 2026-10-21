"""Vista previa local de la página de la demo (pabloduque.dev/lab/cotejo.html).

La página es un borrador que vive aquí, fuera del repositorio del portafolio,
hasta que se copie a `site/lab/`. Para verla con la barra, las fuentes, los
tokens de color y la CSP de verdad, este servidor sirve el sitio del portafolio
tal como está en disco (solo lectura) y superpone encima los archivos del
borrador:

    /lab/cotejo.html|css|js     ->  demo/pagina/cotejo.*
    /data/cotejo/ejemplos.json  ->  demo/pagina/ejemplos.json, que escribe
                                    demo/exportar_ejemplos.py con las fuentes
                                    reales; si todavía no existe,
                                    demo/pagina/muestra.json

Uso (desde la raíz de Cotejo):

    .venv/Scripts/python demo/pagina/vista.py             # http://localhost:8765/lab/cotejo.html
    .venv/Scripts/python demo/pagina/vista.py --muestra   # rehace muestra.json y sale

Para probar «Verificar en vivo» sin AWS, con demo/local.py en marcha:
http://localhost:8765/lab/cotejo.html?api=http://127.0.0.1:8011/
Hay que abrirla como «localhost» y no como 127.0.0.1: es el origen que admite
el CORS de local.py, el mismo que admite la Function URL.

Y para probar los avisos de la página sin gastar cupo ni pedir nada a nadie,
este servidor trae una API simulada, sin red, en el mismo origen:
http://localhost:8765/lab/cotejo.html?api=http://localhost:8765/_simulada/
Verifica de verdad, con la caché local (lo que no esté en ella sale «sin
confirmar»), lleva un cupo de 3 en memoria, y responde cada error de
DISEÑO.md §2 si el texto es exactamente uno de estos:
!limite_ip  !limite_global  !interno  !mal_formada  !ruta  !demasiadas_citas
            (el código tal cual; !limite_global sin el campo "limite", como
            una Lambda anterior a ese campo)
!limite_global:consultas   !limite_global:peticiones
            limite_global con "limite" (DISEÑO.md §2, «Respuesta de limite_global»):
            el tope diario de consultas o el presupuesto diario de peticiones
!ocupado    429 sin cuerpo propio, como la Function URL cuando ya hay una
            verificación en curso (concurrencia reservada 1) o con el freno puesto
!html       502 con una página HTML en vez de JSON
!red        corta la conexión sin responder
!lento      espera 8 s y verifica el ejemplo del pasaje alterado: para ver el estado de carga
!tope       verifica 18 sentencias inventadas con el tope de 15 de la Lambda
            (MAX_PROVIDENCIAS): las 3 últimas salen «sin confirmar» por el tope.
            Necesita un Cotejo con verificar(max_consultadas=...)

Con --puerto se puede servir en otro puerto si el 8765 está ocupado, pero
entonces la página ya no es el origen que admite local.py y el navegador
bloquea sus respuestas por CORS (la API simulada sigue funcionando: es del
mismo origen).
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import gzip
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

PAGINA = Path(__file__).resolve().parent
RAIZ = PAGINA.parents[1]                      # C:\dev\Cotejo
SITIO = Path(os.environ.get("COTEJO_SITIO") or RAIZ.parent / "Portafolio" / "site")
EJEMPLOS = RAIZ / "demo" / "ejemplos"
MUESTRA = PAGINA / "muestra.json"
EXPORTADO = PAGINA / "ejemplos.json"

# La CSP de producción (infra/variables.tf del portafolio), con dos cambios
# que solo existen aquí: connect-src admite la API local de demo/local.py, y
# no lleva upgrade-insecure-requests, que convertiría la petición a
# http://127.0.0.1:8011 en https y la haría fallar. Servirla en la vista
# previa es lo que destapa a tiempo un <script> en línea o un onclick, que en
# producción se bloquearían sin más aviso que la consola.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data:; "
       "connect-src 'self' http://127.0.0.1:8011 http://localhost:8011; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


def _ejemplos_json() -> Path:
    return EXPORTADO if EXPORTADO.exists() else MUESTRA


SUPERPUESTOS = {
    "/lab/cotejo.html": lambda: PAGINA / "cotejo.html",
    "/lab/cotejo.css": lambda: PAGINA / "cotejo.css",
    "/lab/cotejo.js": lambda: PAGINA / "cotejo.js",
    "/data/cotejo/ejemplos.json": _ejemplos_json,
}


class Manejador(http.server.SimpleHTTPRequestHandler):
    # En Windows, mimetypes lee el registro, y hay equipos donde .js figura
    # como text/plain: con nosniff (que producción envía) el navegador no
    # ejecutaría el script. Los tipos del sitio se fijan aquí.
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".txt": "text/plain; charset=utf-8",
        ".xml": "application/xml",
    }

    def translate_path(self, path: str) -> str:
        ruta = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
        if ruta in SUPERPUESTOS:
            return str(SUPERPUESTOS[ruta]())
        return super().translate_path(path)

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        # Sin caché: cada recarga tiene que enseñar el borrador tal como está
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, formato: str, *args) -> None:
        sys.stderr.write("vista · " + (formato % args) + "\n")

    def do_GET(self) -> None:
        # /data/estado.json lo escribe en producción la Lambda de estado cada 15 minutos; en disco no existe, y su
        # 404 ensuciaría la consola de la vista previa. Sin historial, LAB.demoDown dice «no está caída».
        if (urllib.parse.urlsplit(self.path).path == "/data/estado.json"
                and not (SITIO / "data" / "estado.json").exists()):
            self._json(200, {"actualizado": None, "historial": [], "demos": []})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/_simulada/verificar":
            self._json(404, {"error": "ruta"})
            return
        _simular(self)

    def _json(self, codigo: int, datos: dict, cabeceras: dict | None = None) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        for nombre, valor in (cabeceras or {}).items():
            self.send_header(nombre, valor)
        self.end_headers()
        self.wfile.write(cuerpo)


# ------------------------------------------------------------- sin red
_preparado: dict = {}
_cerrojo = threading.Lock()


def _sin_red():
    """Carga Cotejo con la red bloqueada y el índice de la caché, una sola vez.

    Se bloquea la red a propósito: la caché ya tiene casi todo, la vista
    previa no tiene por qué molestar a la Corte, y si algo falta, el bloqueo
    lo convierte en «sin confirmar» (nunca en «no existe»: es justo la regla
    que la página tiene que enseñar bien) y queda anotado en `bloqueadas`.
    """
    with _cerrojo:
        if _preparado:
            return _preparado
        sys.path.insert(0, str(RAIZ))
        from cotejo import informe, verificacion
        from cotejo.fuentes import corte_constitucional as cc
        from cotejo.fuentes import corte_suprema as cs
        from cotejo.fuentes import red

        bloqueadas: list[str] = []

        def pedir_sin_red(self, url, *args, **kwargs):
            bloqueadas.append(url)
            raise red.ServicioNoDisponible(f"{self.nombre}: vista previa sin red")

        red.Cliente.pedir = pedir_sin_red

        # El índice se lee de la caché aunque tenga más de 7 días: Indice.cargar
        # lo descargaría entero de datos.gov.co, y para ver la página no hace falta.
        ruta_indice = red.dir_cache() / "corte_constitucional_indice.json.gz"
        with gzip.open(ruta_indice, "rt", encoding="utf-8") as f:
            indice = cc.Indice(json.load(f))
        indice.actualizado = ruta_indice.stat().st_mtime
        _preparado.update(informe=informe, verificacion=verificacion, cc=cc, cs=cs,
                          indice=indice, bloqueadas=bloqueadas)
        return _preparado


def _respuesta(texto: str, documento: str, max_consultadas: int | None = None) -> dict:
    """Verifica sin red y devuelve la forma del 200 de POST /verificar (DISEÑO.md §2)."""
    m = _sin_red()
    cc, cs, indice = m["cc"], m["cs"], m["indice"]
    tope = {} if max_consultadas is None else {"max_consultadas": max_consultadas}
    with _cerrojo:      # los contadores de peticiones son de módulo: una verificación cada vez
        antes_cc, antes_cs = cc.cliente.peticiones, cs.cliente.peticiones
        t0 = time.monotonic()
        inf = m["verificacion"].verificar(texto, documento=documento, indice=indice, **tope)
        segundos = round(time.monotonic() - t0, 2)
        return {
            "informe": m["informe"].a_json(inf),
            "segundos": segundos,
            "restantes": None,
            "indice": {"providencias": len(indice), "hasta": indice.fecha_mas_reciente},
            "peticiones": {"relatoria": cc.cliente.peticiones - antes_cc,
                           "corte_suprema": cs.cliente.peticiones - antes_cs},
            "cortado_por_tiempo": bool(getattr(inf, "cortado_por_tiempo", False)),
            "cortado_por_tope": bool(getattr(inf, "cortado_por_tope", False)),
        }


# ------------------------------------------------------- API simulada
_cupo_simulado = {"restantes": 3}
_MENSAJE_GLOBAL = "La demo llegó a su límite de verificaciones de hoy. Se renueva a medianoche, hora de Colombia."
_ERRORES_SIMULADOS = {
    "!limite_ip": (429, {"error": "limite_ip", "mensaje": "Se alcanzó el límite de consultas de hoy desde esta conexión."}),
    "!limite_global": (429, {"error": "limite_global", "mensaje": _MENSAJE_GLOBAL}),
    # Los dos topes que acaban en limite_global; la página dice cuál, y con el presupuesto agotado no puede hablar
    # de «100 consultas»: se habían servido 34 cuando se reprodujo
    "!limite_global:consultas": (429, {"error": "limite_global", "mensaje": _MENSAJE_GLOBAL, "limite": "consultas"}),
    "!limite_global:peticiones": (429, {"error": "limite_global", "mensaje": _MENSAJE_GLOBAL, "limite": "peticiones"}),
    "!interno": (500, {"error": "interno", "mensaje": "Error interno."}),
    "!mal_formada": (400, {"error": "mal_formada", "mensaje": "La petición no es un JSON con «texto»."}),
    "!ruta": (404, {"error": "ruta"}),
    "!demasiadas_citas": (422, {"error": "demasiadas_citas",
                                "mensaje": "El texto cita más de 40 providencias distintas; el máximo de la demo es 40."}),
    # Lo que devuelve la propia Function URL cuando se pasa la concurrencia reservada: no es un error nuestro
    "!ocupado": (429, {"Message": "Rate Exceeded."}),
}


def _hasta_medianoche() -> int:
    """Segundos hasta la medianoche de Colombia (UTC−5, sin horario de verano), como cuota.py."""
    ahora = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))
    manana = dt.datetime.combine(ahora.date() + dt.timedelta(days=1), dt.time(), ahora.tzinfo)
    return max(1, int((manana - ahora).total_seconds()))


def _simular(m: Manejador) -> None:
    largo = int(m.headers.get("Content-Length") or 0)
    if largo > 64 * 1024:
        m._json(400, {"error": "mal_formada", "mensaje": "El cuerpo pasa de 64 KB."})
        return
    try:
        texto = json.loads(m.rfile.read(largo).decode("utf-8")).get("texto")
    except (ValueError, AttributeError):
        texto = None
    if not isinstance(texto, str):
        m._json(400, {"error": "mal_formada", "mensaje": "La petición no es un JSON con «texto»."})
        return
    orden = texto.strip()
    if orden in _ERRORES_SIMULADOS:
        codigo, cuerpo = _ERRORES_SIMULADOS[orden]
        # Los dos límites diarios llevan Retry-After hasta la medianoche de Colombia, como en la Lambda; el 429 de
        # la Function URL (!ocupado) no lleva nada nuestro
        cabeceras = ({"Retry-After": str(_hasta_medianoche())}
                     if cuerpo.get("error") in ("limite_ip", "limite_global") else None)
        m._json(codigo, cuerpo, cabeceras)
        return
    if orden == "!red":
        m.close_connection = True       # sin respuesta: el navegador lo ve como un fallo de red
        return
    if orden == "!html":
        cuerpo = b"<html><body>502 Bad Gateway</body></html>"
        m.send_response(502)
        m.send_header("Content-Type", "text/html")
        m.send_header("Content-Length", str(len(cuerpo)))
        m.end_headers()
        m.wfile.write(cuerpo)
        return
    if orden == "!lento":
        time.sleep(8)
        texto = (EJEMPLOS / "pasaje_alterado.txt").read_text(encoding="utf-8")
    max_consultadas = None
    if orden == "!tope":
        # Inventadas: sin red salen «sin confirmar» igual, pero las 3 últimas con el motivo del tope, que es lo que
        # la página tiene que explicar
        texto = "Sentencias " + ", ".join(f"T-{n}/15" for n in range(9000, 9018)) + "."
        max_consultadas = 15
    if not texto.strip():
        m._json(422, {"error": "texto_vacio", "mensaje": "No hay texto que verificar."})
        return
    if len(texto) > 5000:
        m._json(413, {"error": "texto_largo", "mensaje": "El texto pasa de 5.000 caracteres."})
        return
    try:
        r = _respuesta(texto, "Texto pegado", max_consultadas)
    except TypeError:
        if max_consultadas is None:
            raise
        m._json(500, {"error": "interno", "mensaje": "Este Cotejo todavía no tiene verificar(max_consultadas=...)."})
        return
    # Como la Lambda: si no hay nada que consultar, no se gasta cupo
    if any(h["estado"] != "no_verificable" for h in r["informe"]["hallazgos"]):
        if _cupo_simulado["restantes"] <= 0:
            m._json(429, _ERRORES_SIMULADOS["!limite_ip"][1], {"Retry-After": str(_hasta_medianoche())})
            return
        _cupo_simulado["restantes"] -= 1
    r["restantes"] = _cupo_simulado["restantes"]
    m._json(200, r)


# ------------------------------------------------------------------ muestra
def _commit() -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def generar_muestra() -> dict:
    """Los tres ejemplos verificados con la caché local y sin red.

    Es una muestra para ver la página mientras demo/exportar_ejemplos.py no
    haya escrito el archivo de verdad. Avisa si algún veredicto no es el
    esperado en demo/ejemplos/ejemplos.json: sin red, una consulta que no esté
    en la caché sale «sin confirmar», y la muestra ya no enseñaría el ejemplo.
    """
    meta = json.loads((EJEMPLOS / "ejemplos.json").read_text(encoding="utf-8"))
    ejemplos = []
    for m in meta:
        texto = (EJEMPLOS / m["archivo"]).read_text(encoding="utf-8")
        r = _respuesta(texto, m["titulo"]["es"])
        obtenido = {h["clave"]: h["estado"] for h in r["informe"]["hallazgos"]}
        for clave, esperado in m.get("esperado", {}).items():
            if obtenido.get(clave) != esperado:
                print(f"AVISO {m['id']}: {clave} salió {obtenido.get(clave)}, se esperaba {esperado}",
                      file=sys.stderr)
        ejemplos.append({"id": m["id"], "titulo": m["titulo"], "descripcion": m["descripcion"],
                         "texto": texto, "respuesta": r})
    bloqueadas = _sin_red()["bloqueadas"]
    if bloqueadas:
        print(f"AVISO: {len(bloqueadas)} consulta(s) no estaban en la caché y quedaron "
              "«sin confirmar»:", *bloqueadas, sep="\n  ", file=sys.stderr)
    hoy = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date().isoformat()
    return {"generado": hoy, "commit": _commit(),
            "origen": "muestra de vista.py: caché local, sin red", "ejemplos": ejemplos}


def escribir_muestra() -> None:
    datos = generar_muestra()
    MUESTRA.write_text(json.dumps(datos, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Escrita {MUESTRA} ({len(datos['ejemplos'])} ejemplos, commit {datos['commit']})")


# -------------------------------------------------------------------- main
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--puerto", type=int, default=8765)
    p.add_argument("--muestra", action="store_true", help="rehace muestra.json y sale")
    a = p.parse_args()

    if a.muestra:
        escribir_muestra()
        return
    if not SITIO.is_dir():
        sys.exit(f"No encuentro el sitio del portafolio en {SITIO} (variable COTEJO_SITIO)")
    if not EXPORTADO.exists() and not MUESTRA.exists():
        escribir_muestra()

    manejador = functools.partial(Manejador, directory=str(SITIO))
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", a.puerto), manejador)
    print(f"Sitio: {SITIO}\nEjemplos: {_ejemplos_json().name}\n"
          f"Abre http://localhost:{a.puerto}/lab/cotejo.html", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
