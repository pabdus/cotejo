"""Verificador que corre tras cada edicion de un fichero del proyecto.

Nace de dos problemas concretos y repetidos.

El primero, en Python: al escribir codigo a traves del shell las secuencias
de escape se corrompen. Un "\\b" acaba siendo el caracter 0x08, un "\\n" se
convierte en un salto de linea real que parte el literal, y "\\1" pasa a ser
0x01. El literal roto es un SyntaxError que solo aparece al ejecutar; peor
aun, un 0x08 dentro de una cadena NO es error de sintaxis: el fichero
compila, el programa arranca, y el regex simplemente no coincide nunca.

El segundo, en JavaScript: un listener sobre un elemento que no existe lanza
un TypeError que aborta el resto de la funcion de arranque. Paso de verdad
en este proyecto y dejo la interfaz medio muerta con aspecto de estar entera.
Por eso aqui se comprueba que todo id que usa el JS exista en el HTML.

Recibe por stdin el JSON del hook y no depende de jq (que no esta instalado
en esta maquina) ni de escapes de shell, que es justo lo que falla.
"""
from __future__ import annotations

import io
import json
import os
import py_compile
import re
import sys
import tempfile

# Caracteres de control que nunca deberian aparecer en un fuente. Se permiten
# tabulador (9), salto de linea (10) y retorno de carro (13).
CONTROL_PROHIBIDOS = set(range(0, 9)) | {11, 12} | set(range(14, 32))

NOMBRES = {
    0: "NUL", 1: "SOH (suele venir de un \\1 mal escapado)", 2: "STX",
    3: "ETX", 4: "EOT", 5: "ENQ", 6: "ACK", 7: "BEL (de un \\a)",
    8: "BACKSPACE (suele venir de un \\b mal escapado)", 11: "VT",
    12: "FF", 27: "ESC",
}

EXTENSIONES = (".py", ".js", ".html", ".css", ".json")


def salir_limpio() -> None:
    sys.exit(0)


def avisar(mensaje: str) -> None:
    """Devuelve el aviso al modelo y lo muestra al usuario."""
    # ensure_ascii a proposito: la consola de Windows usa cp1252 y un acento
    # sin escapar puede llegar corrupto a quien lea esta salida.
    print(json.dumps({
        "decision": "block",
        "reason": mensaje,
        "systemMessage": mensaje.split("\n")[0],
    }, ensure_ascii=True))
    sys.exit(0)


def revisar_control(contenido: str, nombre: str) -> None:
    """Caracteres de control: compilan sin quejarse y rompen los regex."""
    hallados: dict[int, int] = {}
    for i, ch in enumerate(contenido):
        cp = ord(ch)
        if cp in CONTROL_PROHIBIDOS and cp not in hallados:
            hallados[cp] = contenido[:i].count("\n") + 1

    if hallados:
        detalle = "; ".join(
            "U+%04X %s en la línea %d" % (cp, NOMBRES.get(cp, ""), linea)
            for cp, linea in sorted(hallados.items())
        )
        avisar(
            "CARACTERES DE CONTROL en %s: %s.\n"
            "El fichero puede compilar igual, pero un 0x08 dentro de una "
            "cadena rompe el regex en silencio. Reescribe esa línea con Write "
            "o Edit, no por el shell." % (nombre, detalle)
        )


def revisar_python(ruta: str, nombre: str) -> None:
    cache = os.path.join(tempfile.gettempdir(), "verificar_hook.pyc")
    try:
        py_compile.compile(ruta, cfile=cache, doraise=True)
    except py_compile.PyCompileError as exc:
        avisar("SINTAXIS ROTA en %s:\n%s" % (nombre, exc.msg.strip()))
    except Exception:
        pass
    finally:
        try:
            os.remove(cache)
        except OSError:
            pass


def revisar_json(contenido: str, nombre: str) -> None:
    try:
        json.loads(contenido)
    except json.JSONDecodeError as exc:
        avisar("JSON INVÁLIDO en %s: %s (línea %d)" % (nombre, exc.msg, exc.lineno))


_RE_ID_JS = re.compile(r"""\$\(\s*["']#([\w-]+)["']\s*\)""")
_RE_ID_QS = re.compile(r"""querySelector\(\s*["']#([\w-]+)["']\s*\)""")
_RE_ID_GET = re.compile(r"""getElementById\(\s*["']([\w-]+)["']\s*\)""")
_RE_ID_HTML = re.compile(r"""\bid=["']([^"']+)["']""")


def revisar_js_contra_html(ruta: str, contenido: str, nombre: str) -> None:
    """Todo id que use el JS tiene que existir en el HTML del proyecto.

    Un listener sobre null lanza TypeError y se lleva por delante el resto
    de la funcion de arranque: la pagina se queda a medias con aspecto de
    estar entera.
    """
    # Se busca en la carpeta del JS, en la de al lado y en la del proyecto:
    # lo habitual es static/app.js junto a static/index.html, pero tambien
    # js/app.js con el HTML un nivel mas arriba.
    carpeta = os.path.dirname(os.path.abspath(ruta))
    raiz = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(carpeta)
    htmls: list[str] = []
    for base in dict.fromkeys([carpeta, os.path.dirname(carpeta), raiz]):
        try:
            htmls += [
                os.path.join(base, f)
                for f in os.listdir(base or ".")
                if f.endswith((".html", ".htm"))
            ]
        except OSError:
            continue
    if not htmls:
        # Sin HTML no hay nada contra lo que contrastar
        return

    disponibles: set[str] = set()
    for h in htmls:
        try:
            disponibles |= set(_RE_ID_HTML.findall(
                io.open(h, encoding="utf-8", errors="replace").read()))
        except OSError:
            continue

    usados: set[str] = set()
    for patron in (_RE_ID_JS, _RE_ID_QS, _RE_ID_GET):
        usados |= set(patron.findall(contenido))

    ausentes = sorted(usados - disponibles)
    if ausentes:
        avisar(
            "IDS QUE NO EXISTEN en el HTML, usados por %s: %s.\n"
            "Un addEventListener sobre null lanza TypeError y aborta el resto "
            "de la función: la página se queda a medias pareciendo entera."
            % (nombre, ", ".join(ausentes))
        )


def main() -> None:
    try:
        datos = json.load(sys.stdin)
    except Exception:
        salir_limpio()

    entrada = datos.get("tool_input") or {}
    respuesta = datos.get("tool_response") or {}
    ruta = entrada.get("file_path") or respuesta.get("filePath") or ""

    if not ruta.endswith(EXTENSIONES) or not os.path.isfile(ruta):
        salir_limpio()

    try:
        contenido = io.open(ruta, encoding="utf-8", errors="replace").read()
    except OSError:
        salir_limpio()

    nombre = os.path.basename(ruta)
    revisar_control(contenido, nombre)

    if ruta.endswith(".py"):
        revisar_python(ruta, nombre)
    elif ruta.endswith(".json"):
        revisar_json(contenido, nombre)
    elif ruta.endswith(".js"):
        revisar_js_contra_html(ruta, contenido, nombre)

    salir_limpio()


if __name__ == "__main__":
    main()
