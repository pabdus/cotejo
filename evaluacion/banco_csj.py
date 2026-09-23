"""¿Cuánto vale una ausencia en el buscador de la Corte Suprema?

La Corte Constitucional tiene dos fuentes (índice y relatoría) y Cotejo solo
declara inexistente lo que falta en las dos. La Corte Suprema tiene una sola:
su buscador. Antes de dejar que una ausencia ahí se lea como «no existe» hay
que medir cuántas providencias REALES no encuentra.

De dónde salen citas reales con certeza: de las propias providencias de la
Corte Suprema, que citan su jurisprudencia por código («SC5686-2018»). Se
toman documentos al azar de cada sala y año, se extraen los códigos que citan
y se busca cada uno. Lo que no se encuentre es, o un hueco de la base, o una
errata de la propia Corte al citar; las dos cosas le pasarían igual al
escrito de un abogado, así que la cifra es la que importa.

    python evaluacion/banco_csj.py --semilla 7 --documentos 40 --codigos 200

Resultado en evaluacion/resultado_banco_csj.txt. Cortés con el servidor: una
petición cada 1,5 s y todo en caché.
"""
from __future__ import annotations

import argparse
import collections
import os
import random
import sys
from pathlib import Path

os.environ.setdefault("COTEJO_PAUSA_CSJ", "1.5")
RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from cotejo import extraccion                     # noqa: E402
from cotejo.fuentes import corte_suprema as cs    # noqa: E402

SALAS_SEMILLA = {"CIVIL": "casación", "LABORAL": "casación", "PENAL": "casación",
                 "TUTELAS": "tutela"}


def _listar(consulta: str, sala: str, anio: int, inicio: int) -> list[dict]:
    q = (
        "{ getSearchResult(searchQuery:{ query: %s typeOfQuery: %s start: %d isExact: false "
        'magistrate: "" year: %s autoSentencia: "" order: "" roomTutelas: "" addedQueries: [] }) '
        "{ searchResults { title onlinePath doctor ano autoSentencia } numOfResults } }"
    ) % (cs._cadena(consulta), cs._cadena(sala), inicio, cs._cadena(str(anio)))
    return cs._graphql(q)["getSearchResult"]["searchResults"] or []


def documentos_semilla(azar: random.Random, n: int) -> list[cs.ProvidenciaCSJ]:
    """Providencias con código y en Word, repartidas por sala y año."""
    salida: list[cs.ProvidenciaCSJ] = []
    combinaciones = [(s, a) for s in SALAS_SEMILLA for a in range(2016, 2026)]
    azar.shuffle(combinaciones)
    for sala, anio in combinaciones:
        if len(salida) >= n:
            break
        filas = _listar(SALAS_SEMILLA[sala], sala, anio, azar.randrange(0, 200))
        filas = [f for f in filas if f["onlinePath"].lower().endswith((".doc", ".docx"))]
        for f in filas:
            m = extraccion._RE_CODIGO_CSJ.match(f["title"])
            if m and m.group("codigo") in cs.SALAS:
                salida.append(cs.ProvidenciaCSJ(
                    id=m.group(0), prefijo=m.group("codigo"), numero=extraccion.numero_csj(m),
                    anio=int(m.group("anio")), sala=sala, clase=None,
                    documentos=[f["onlinePath"]]))
                break
    return salida


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--semilla", type=int, default=7)
    ap.add_argument("--documentos", type=int, default=40)
    ap.add_argument("--codigos", type=int, default=200)
    args = ap.parse_args()
    azar = random.Random(args.semilla)

    semillas = documentos_semilla(azar, args.documentos)
    print(f"{len(semillas)} documentos semilla", flush=True)

    citados: dict[str, tuple[str, str, int]] = {}
    origen: dict[str, str] = {}
    for p in semillas:
        t = cs.texto(p)
        if not t:
            continue
        for m in extraccion._RE_CODIGO_CSJ.finditer(t.texto):
            pre, num, anio = m.group("codigo"), extraccion.numero_csj(m), int(m.group("anio"))
            clave = cs.codigo(pre, num, anio)
            if pre in cs.SALAS and clave != cs.codigo(p.prefijo, p.numero, p.anio):
                citados.setdefault(clave, (pre, num, anio))
                origen.setdefault(clave, p.id)
    print(f"{len(citados)} códigos distintos citados", flush=True)

    claves = sorted(citados)
    azar.shuffle(claves)
    muestra = claves[:args.codigos]

    filas = []
    for i, clave in enumerate(muestra, 1):
        pre, num, anio = citados[clave]
        try:
            b = cs.buscar(pre, num, anio)
            estado = "encontrada" if b.providencia else ("ausente" if b.completa else "incompleta")
        except cs.red.ServicioNoDisponible as exc:
            estado = f"sin_respuesta ({exc})"
        filas.append((clave, pre, anio, estado, origen[clave]))
        if i % 20 == 0:
            print(f"  {i}/{len(muestra)}", flush=True)

    # -- informe -------------------------------------------------------
    lineas = [f"Banco Corte Suprema — semilla {args.semilla}",
              f"{len(semillas)} documentos semilla, {len(citados)} códigos citados, "
              f"{len(muestra)} buscados", ""]
    total = collections.Counter(f[3].split(" ")[0] for f in filas)
    lineas.append("Total: " + ", ".join(f"{k} {v}" for k, v in total.most_common()))
    lineas.append("")
    for nombre, clave_grupo in (("Por década/año", lambda f: str(f[2]) if f[2] >= 2014 else "antes de 2014"),
                                ("Por prefijo", lambda f: f[1])):
        lineas.append(nombre)
        grupos = collections.defaultdict(collections.Counter)
        for f in filas:
            grupos[clave_grupo(f)][f[3].split(" ")[0]] += 1
        for g in sorted(grupos):
            c = grupos[g]
            n = sum(c.values())
            lineas.append(f"  {g:>14}: {c['encontrada']}/{n} encontradas"
                          + (f", {c['ausente']} ausentes" if c['ausente'] else "")
                          + (f", {c['incompleta']} incompletas" if c['incompleta'] else "")
                          + (f", {c['sin_respuesta']} sin respuesta" if c['sin_respuesta'] else ""))
        lineas.append("")
    lineas.append("Ausentes (citadas por la propia Corte y no encontradas):")
    for f in filas:
        if f[3] != "encontrada":
            lineas.append(f"  {f[0]:<16} {f[3]:<12} citada en {f[4]}")

    salida = RAIZ / "evaluacion" / f"resultado_banco_csj_semilla{args.semilla}.txt"
    salida.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    print("\n".join(lineas))


if __name__ == "__main__":
    main()
