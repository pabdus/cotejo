"""Verifica los tres ejemplos de la demo y escribe el JSON que precarga la página.

    .venv/Scripts/python demo/exportar_ejemplos.py
    .venv/Scripts/python demo/exportar_ejemplos.py --permitir-sucio   # solo en desarrollo
    .venv/Scripts/python demo/exportar_ejemplos.py --comprobar        # ¿la forma de hoy? sin red

Usa las fuentes reales, a través de la caché local (.cache), que ya tiene casi
todo: lo que falte se le pide a la Corte con el cliente cortés de siempre.
Escribe demo/pagina/ejemplos.json con la forma de DISEÑO.md §4; la página lo
sirve en /data/cotejo/ejemplos.json.

Sale con error, y sin escribir nada, si algún veredicto no es el que espera
demo/ejemplos/ejemplos.json. Los ejemplos se enseñan como «verificado el
<fecha>» y su descripción promete un resultado («cinco sentencias que no
existen»). Si la relatoría estuviera caída al exportar, las inventadas
saldrían «sin confirmar» y la página contradiría su propia descripción; mejor
no publicar que publicar eso. Y lo mismo con una cita de más o de menos: si
la extracción cambia, la descripción ya no cuenta lo que se ve.

Por lo mismo, se niega si cotejo/ o demo/ tienen cambios sin commit (salvo
demo/pagina/, donde está lo que este script escribe). La página enseña el
commit como procedencia comprobable («commit <hash> de Cotejo»), y el primer
ejemplos.json decía cd8cd43, un commit sin demo/ ni el código que produjo
esos informes: quien hiciera checkout de ese hash no podía reproducirlos.
Con --permitir-sucio exporta igual y lo dice en el commit («<hash>-sucio»).

Y como el archivo se genera y se copia a mano, puede quedarse atrás del código:
el de cd8cd43 seguía con 6 claves en cada «respuesta» cuando el 200 de POST
/verificar ya tenía 7 (le faltaba cortado_por_tope). --comprobar, y una prueba
de tests/test_demo.py, comparan su forma con la de cuerpo_informe de hoy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RAIZ), str(RAIZ / "demo")]
# Antes de importar la Lambda, que si no fija /tmp/cotejo-cache
os.environ.setdefault("COTEJO_CACHE", str(RAIZ / ".cache"))

import lambda_function  # noqa: E402
from cotejo import verificacion  # noqa: E402
from cotejo.fuentes import corte_constitucional as cc  # noqa: E402
from cotejo.fuentes import corte_suprema as cs  # noqa: E402

EJEMPLOS = RAIZ / "demo" / "ejemplos"
SALIDA = RAIZ / "demo" / "pagina" / "ejemplos.json"
# Lo que se vigila, y lo que no: la página es la que recibe ejemplos.json, y si
# contara, la propia salida dejaría el árbol sucio para la siguiente exportación
VIGILADO = ("cotejo", "demo")
SIN_VIGILAR = ("demo/pagina/",)


def _commit() -> str:
    r = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True, timeout=30, check=True)
    return r.stdout.strip()


def _porcelana() -> str:
    # -z: rutas sin comillas ni escapes (git escribiría «"demo/DISE\303\221O.md"»).
    # --untracked-files=all: si no, una carpeta sin seguimiento sale entera
    # («?? demo/») y no se sabría si lo nuevo está solo en demo/pagina/.
    r = subprocess.run(["git", "-C", str(RAIZ), "status", "--porcelain=v1", "-z",
                        "--untracked-files=all", "--", *VIGILADO],
                       capture_output=True, timeout=30, check=True)
    return r.stdout.decode("utf-8", "replace")


def cambios_sin_commit(porcelana: str) -> list[str]:
    """Las rutas con cambios de una salida de `git status --porcelain=v1 -z`, salvo demo/pagina/."""
    entradas = porcelana.split("\0")
    rutas: list[str] = []
    i = 0
    while i < len(entradas):
        entrada = entradas[i]
        i += 1
        if len(entrada) < 4:
            continue
        estado, ruta = entrada[:2], entrada[3:]
        rutas.append(ruta)
        if "R" in estado or "C" in estado:
            # Con -z, el origen de un renombrado o una copia va en la entrada
            # siguiente; también cuenta: sacar algo de cotejo/ es cambiarlo
            if i < len(entradas):
                rutas.append(entradas[i])
                i += 1
    return sorted({r for r in rutas if not r.startswith(SIN_VIGILAR)})


def _estado_git() -> tuple[str, list[str]]:
    return _commit(), cambios_sin_commit(_porcelana())


def _hoy_en_colombia() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date().isoformat()


# ------------------------------------------------------------------ forma
CLAVES = {"generado", "commit", "ejemplos"}
CLAVES_EJEMPLO = {"id", "titulo", "descripcion", "texto", "respuesta"}
REGENERAR = ("Regenera demo/pagina/ejemplos.json: .venv/Scripts/python demo/exportar_ejemplos.py "
             "(tras el commit; en desarrollo, con --permitir-sucio).")


def respuesta_de_referencia() -> dict:
    """Un 200 de POST /verificar de hoy, hecho sin red: la forma con la que comparar."""
    indice = cc.Indice([])
    inf = verificacion.verificar("El Auto 092 de 2008.", documento=lambda_function.DOCUMENTO,
                                 indice=indice, relatoria=False)
    return lambda_function.cuerpo_informe(inf, segundos=0.0, restantes=None, indice=indice,
                                          peticiones={"relatoria": 0, "corte_suprema": 0})


def _comparar(referencia, real, donde: str) -> list[str]:
    """Claves que faltan o sobran, en cada objeto que la referencia trae como objeto.

    Las listas se comparan elemento a elemento con el primero de la referencia
    (un hallazgo tiene siempre las mismas claves). Lo que la referencia trae
    vacío o nulo (pasajes, la providencia de una no verificable) no se mira.
    """
    if isinstance(referencia, dict):
        if not isinstance(real, dict):
            return [f"{donde}: se esperaba un objeto"]
        salida = []
        faltan, sobran = sorted(set(referencia) - set(real)), sorted(set(real) - set(referencia))
        if faltan or sobran:
            salida.append(f"{donde}: " + "; ".join(
                ([f"faltan {', '.join(faltan)}"] if faltan else [])
                + ([f"sobran {', '.join(sobran)}"] if sobran else [])))
        for clave in sorted(set(referencia) & set(real)):
            salida += _comparar(referencia[clave], real[clave], f"{donde}.{clave}")
        return salida
    if isinstance(referencia, list) and referencia and isinstance(real, list):
        return [d for i, x in enumerate(real) for d in _comparar(referencia[0], x, f"{donde}[{i}]")]
    return []


def diferencias_de_forma(datos) -> list[str]:
    """Lo que no cuadra entre un ejemplos.json y la forma de DISEÑO §4 de hoy; [] si nada."""
    if not isinstance(datos, dict):
        return ["ejemplos.json: se esperaba un objeto"]
    salida = _comparar(dict.fromkeys(CLAVES), datos, "ejemplos.json")
    ejemplos = datos.get("ejemplos")
    if not isinstance(ejemplos, list) or not ejemplos:
        return salida + ["ejemplos.json: «ejemplos» tiene que ser una lista con ejemplos"]
    referencia = {**dict.fromkeys(CLAVES_EJEMPLO), "respuesta": respuesta_de_referencia()}
    for i, ejemplo in enumerate(ejemplos):
        nombre = ejemplo.get("id", i) if isinstance(ejemplo, dict) else i
        salida += _comparar(referencia, ejemplo, f"ejemplos[{nombre}]")
    return salida


def comprobar() -> int:
    """--comprobar: ¿tiene SALIDA la forma de hoy? Sin red ni git."""
    try:
        datos = json.loads(SALIDA.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"No se pudo leer {SALIDA}: {type(exc).__name__}: {exc}\n{REGENERAR}", file=sys.stderr)
        return 1
    diferencias = diferencias_de_forma(datos)
    if diferencias:
        print(f"{SALIDA} está desfasado:", *diferencias, sep="\n  ", file=sys.stderr)
        print(REGENERAR, file=sys.stderr)
        return 1
    print(f"{SALIDA}: forma al día (commit {datos.get('commit')}, generado {datos.get('generado')})")
    return 0


def exportar(permitir_sucio: bool = False) -> int:
    # Antes de verificar nada: negarse después de pedirle los ejemplos a la
    # Corte sería gastarle peticiones para nada
    try:
        estado = _estado_git()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"No se exporta: no se pudo consultar git ({type(exc).__name__}: {exc}).", file=sys.stderr)
        return 1
    commit, sucios = estado
    if sucios and not permitir_sucio:
        print(f"No se exporta: {len(sucios)} archivo(s) de cotejo/ o demo/ con cambios sin commit. "
              f"El informe diría que es del commit {commit}, y no lo sería. Haz commit y vuelve a "
              "exportar (o --permitir-sucio, solo en desarrollo).", *sucios, sep="\n  ", file=sys.stderr)
        return 1
    if sucios:
        commit += "-sucio"
        print(f"Aviso: {len(sucios)} archivo(s) con cambios sin commit; se exporta como {commit}.",
              file=sys.stderr)

    indice = cc.Indice.cargar()
    meta = json.loads((EJEMPLOS / "ejemplos.json").read_text(encoding="utf-8"))
    ejemplos, errores = [], []
    for m in meta:
        texto = (EJEMPLOS / m["archivo"]).read_text(encoding="utf-8")
        antes = (cc.cliente.peticiones, cs.cliente.peticiones)
        t0 = time.monotonic()
        # Sin plazo: el ejemplo tiene que salir entero. Y con el mismo nombre de
        # documento y el mismo tope de providencias que la API, para que
        # «Verificar en vivo» dé lo mismo (si el tope cortara algo, el veredicto
        # no sería el esperado y no se escribiría)
        inf = verificacion.verificar(texto, documento=lambda_function.DOCUMENTO, indice=indice,
                                     max_consultadas=lambda_function.MAX_PROVIDENCIAS)
        peticiones = {"relatoria": cc.cliente.peticiones - antes[0],
                      "corte_suprema": cs.cliente.peticiones - antes[1]}
        respuesta = lambda_function.cuerpo_informe(
            inf, segundos=time.monotonic() - t0, restantes=None, indice=indice, peticiones=peticiones)

        obtenido = {h.clave: h.estado.value for h in inf.hallazgos}
        esperado = m["esperado"]
        print(f"{m['id']} ({peticiones['relatoria']} peticiones a la relatoría, "
              f"{peticiones['corte_suprema']} al buscador de la Corte Suprema)")
        for clave in sorted(set(obtenido) | set(esperado)):
            sale, debe = obtenido.get(clave, "(no se extrajo)"), esperado.get(clave, "(no esperada)")
            marca = "ok " if sale == debe else "MAL"
            print(f"  {marca} {clave:<14} {sale}" + ("" if sale == debe else f"   (se esperaba {debe})"))
            if sale != debe:
                errores.append(f"{m['id']}: {clave} salió {sale}, se esperaba {debe}")

        ejemplos.append({"id": m["id"], "titulo": m["titulo"], "descripcion": m["descripcion"],
                         "texto": texto, "respuesta": respuesta})

    if errores:
        print(f"\nNo se escribe {SALIDA.name}: {len(errores)} veredicto(s) distintos de lo esperado.",
              *errores, sep="\n  ", file=sys.stderr)
        return 1
    # Verificar lleva minutos si hay que pedir algo: si entre tanto cambió el
    # commit o el árbol, los informes ya no son de lo que se vio al empezar
    try:
        cambio = _estado_git() != estado
    except (OSError, subprocess.SubprocessError):
        cambio = True
    if cambio:
        print(f"\nNo se escribe {SALIDA.name}: el commit o los archivos de cotejo/ y demo/ "
              "cambiaron mientras se verificaba. Vuelve a exportar.", file=sys.stderr)
        return 1
    datos = {"generado": _hoy_en_colombia(), "commit": commit, "ejemplos": ejemplos}
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(datos, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\nEscrito {SALIDA} ({len(ejemplos)} ejemplos, commit {datos['commit']}, "
          f"índice de {len(indice)} providencias hasta {indice.fecha_mas_reciente})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--permitir-sucio", action="store_true",
                   help="solo en desarrollo: exporta aunque cotejo/ o demo/ tengan cambios sin "
                        "commit, y lo marca en el commit como <hash>-sucio")
    p.add_argument("--comprobar", action="store_true",
                   help="no exporta: dice si demo/pagina/ejemplos.json tiene la forma del 200 de "
                        "POST /verificar de hoy (sin red)")
    a = p.parse_args(argv)
    if a.comprobar:
        return comprobar()
    return exportar(permitir_sucio=a.permitir_sucio)


if __name__ == "__main__":
    sys.exit(main())
