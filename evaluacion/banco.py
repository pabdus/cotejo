"""Banco de evaluación controlado: ¿acierta Cotejo cuando la respuesta se sabe?

Dos pruebas, con la etiqueta correcta conocida por construcción:

1. EXISTENCIA. Sentencias reales sacadas al azar del índice oficial y
   sentencias inventadas (tipo, número y año plausibles que no están en el
   índice). Cada una se escribe en una forma distinta —«Sentencia T-406 de
   1992», «T-406/92», «SENTENCIA No. T-406/1992»...— y se mete en una frase,
   así que se mide a la vez la extracción y la verificación.

   Una inventada que la relatoría SÍ tiene no es un fallo de Cotejo: es un
   hueco del índice oficial, y se cuenta aparte.

2. PASAJES. De sentencias reales se toma una frase del cuerpo y se cita:
     literal ........ tal cual                              -> coincide
     ruido .......... sin tildes, en minúsculas, sin comas  -> coincide
     elision ........ con un tramo cambiado por «(...)»      -> coincide
     negacion ....... con un «no» insertado                  -> difiere
     sustitucion .... con una palabra cambiada por otra      -> difiere
     omision ........ con una palabra quitada sin marcar     -> difiere
     cruzado ........ una frase de OTRA sentencia            -> no_aparece
     barajado ....... las mismas palabras, desordenadas      -> no_aparece

Uso:  python evaluacion/banco.py --existencia 300 --pasajes 40
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from cotejo import comparacion, extraccion, verificacion   # noqa: E402
from cotejo.fuentes import corte_constitucional as cc      # noqa: E402
from cotejo.verificacion import Estado                     # noqa: E402


# ------------------------------------------------------------- formas de citar
def forma(tipo: str, numero: str, anio: int, rng: random.Random) -> str:
    n = numero.lstrip("0") or "0"
    if rng.random() < 0.5:
        n = numero                          # a veces con los ceros delante
    a2, a4 = anio % 100, anio
    opciones = [
        f"Sentencia {tipo}-{n} de {a4}",
        f"sentencia {tipo}-{n} de {a4}",
        f"{tipo}-{n}/{a2:02d}",
        f"la {tipo}-{n} de {a4}",
        f"Sentencia No. {tipo}-{n}/{a4}",
        f"SENTENCIA {tipo}-{n} DE {a4}",
        f"Corte Constitucional, Sentencia {tipo}-{n} del {a4}",
    ]
    return rng.choice(opciones)


PLANTILLAS = [
    "Así lo reconoció la Corte en {c}, al estudiar el caso concreto.",
    "Este criterio fue fijado por {c} y reiterado después.",
    "Como se advirtió en {c}, la protección procede de manera excepcional.",
    "La jurisprudencia ({c}) ha sido constante en este punto.",
]


# ------------------------------------------------------------------ existencia
def banco_existencia(indice: cc.Indice, n: int, rng: random.Random):
    ids = sorted(indice._por_id)
    reales = rng.sample(ids, n)

    # Rango de números por tipo y año, para inventar sentencias plausibles
    maximo: dict[tuple[str, int], int] = defaultdict(int)
    for p in indice._por_id.values():
        num = int(re.match(r"\d+", p.numero).group(0))
        maximo[(p.tipo, p.anio)] = max(maximo[(p.tipo, p.anio)], num)
    claves_ta = list(maximo)
    inventadas = []
    while len(inventadas) < n:
        tipo, anio = rng.choice(claves_ta)
        num = rng.randint(1, maximo[(tipo, anio)] + 40)
        if indice.buscar(tipo, str(num), anio) is None:
            inventadas.append((tipo, str(num), anio))

    casos = []
    for ident in reales:
        p = indice._por_id[ident]
        casos.append(("real", p.tipo, p.numero, p.anio))
    for tipo, num, anio in inventadas:
        casos.append(("inventada", tipo, num, anio))
    rng.shuffle(casos)
    return casos


def evaluar_existencia(indice: cc.Indice, casos, rng: random.Random) -> None:
    matriz = Counter()
    fallos_extraccion = []
    huecos_indice = []
    t0 = time.time()
    for i, (etiqueta, tipo, num, anio) in enumerate(casos, 1):
        cita = forma(tipo, num, anio, rng)
        frase = rng.choice(PLANTILLAS).format(c=cita)
        inf = verificacion.verificar(frase, indice=indice, pasajes=False, nulidad=False)
        verif = [h for h in inf.hallazgos if h.primera.tribunal == extraccion.CORTE_CONSTITUCIONAL]
        esperado = cc.id_oficial(tipo, num, anio)
        encontrado = [h for h in verif
                      if h.primera.tipo and cc.id_oficial(h.primera.tipo, h.primera.numero,
                                                          h.primera.anio) == esperado]
        if not encontrado:
            fallos_extraccion.append((etiqueta, cita, [h.clave for h in inf.hallazgos]))
            matriz[(etiqueta, "no_extraida")] += 1
            continue
        h = encontrado[0]
        if etiqueta == "inventada" and h.estado == Estado.VERIFICADA and h.fuente == "relatoria":
            huecos_indice.append(esperado)
        matriz[(etiqueta, h.estado.value)] += 1
        if i % 50 == 0:
            print(f"  {i}/{len(casos)}  ({time.time() - t0:.0f} s)", flush=True)

    print()
    print("=" * 72)
    print("EXISTENCIA — %d casos (%d reales, %d inventadas)" % (
        len(casos), sum(1 for c in casos if c[0] == "real"),
        sum(1 for c in casos if c[0] == "inventada")))
    print("=" * 72)
    estados = ["verificada", "con_observaciones", "no_existe", "sin_confirmar",
               "no_verificable", "no_extraida"]
    print("%-11s" % "" + "".join("%13s" % e[:12] for e in estados))
    for et in ("real", "inventada"):
        print("%-11s" % et + "".join("%13d" % matriz[(et, e)] for e in estados))

    reales = sum(matriz[("real", e)] for e in estados)
    inv = sum(matriz[("inventada", e)] for e in estados) - len(huecos_indice)
    falso_no_existe = matriz[("real", "no_existe")]
    detectadas = matriz[("inventada", "no_existe")]
    print()
    print(f"Reales dadas por inexistentes (lo grave): {falso_no_existe} de {reales}")
    print(f"Inventadas detectadas: {detectadas} de {inv} "
          f"({100 * detectadas / max(inv, 1):.1f} %)")
    print(f"Huecos del índice oficial (inventadas que la relatoría sí tiene): {len(huecos_indice)}"
          + (f" -> {huecos_indice[:5]}" if huecos_indice else ""))
    if fallos_extraccion:
        print(f"\nNo extraídas ({len(fallos_extraccion)}):")
        for f in fallos_extraccion[:10]:
            print("   ", f)


# -------------------------------------------------------------------- pasajes
_RE_FRASE = re.compile(r"(?<=[.;:])\s+(?=[A-ZÁÉÍÓÚÑ«“\"])")
_VERBOS_NEGABLES = re.compile(r"\b(es|son|debe|deben|puede|pueden|tiene|tienen|procede|constituye|existe)\b")


def frases_de(texto: str) -> list[str]:
    """Frases del cuerpo, de longitud citable, lejos de cabeceras y tablas."""
    cuerpo = texto[len(texto) // 8: -len(texto) // 8]
    salida = []
    for f in _RE_FRASE.split(" ".join(cuerpo.split())):
        w = f.split()
        if not (18 <= len(w) <= 40):
            continue
        letras = sum(ch.isalpha() for ch in f)
        if letras < 0.75 * len(f) or sum(ch.isupper() for ch in f) > 0.15 * letras:
            continue
        if "[" in f or "http" in f:
            continue
        salida.append(f.strip())
    return salida


def variantes(frase: str, otra: str, vocab: list[str], rng: random.Random) -> dict[str, str]:
    w = frase.split()
    v = {"literal": frase}

    ruido = frase.lower().replace(",", "")
    for a, b in zip("áéíóú", "aeiou"):
        ruido = ruido.replace(a, b)
    v["ruido"] = ruido

    a = len(w) // 3
    b = a + max(3, len(w) // 4)
    v["elision"] = " ".join(w[:a] + ["(...)"] + w[b:])

    m = _VERBOS_NEGABLES.search(frase)
    if m and not frase[:m.start()].rstrip().endswith(" no"):
        v["negacion"] = frase[:m.start()] + "no " + frase[m.start():]
    else:
        k = rng.randint(2, len(w) - 3)
        v["negacion"] = " ".join(w[:k] + ["no"] + w[k:])

    candidatos = [i for i in range(2, len(w) - 2) if len(w[i]) >= 5 and w[i].isalpha()]
    if candidatos and vocab:
        i = rng.choice(candidatos)
        nueva = rng.choice([x for x in vocab if x.lower() != w[i].lower()] or vocab)
        v["sustitucion"] = " ".join(w[:i] + [nueva] + w[i + 1:])

    candidatos = [i for i in range(2, len(w) - 2) if len(w[i]) >= 4]
    if candidatos:
        i = rng.choice(candidatos)
        v["omision"] = " ".join(w[:i] + w[i + 1:])

    v["cruzado"] = otra
    barajada = w[:]
    rng.shuffle(barajada)
    v["barajado"] = " ".join(barajada)
    return v


ESPERADO = {"literal": "coincide", "ruido": "coincide", "elision": "coincide",
            "negacion": "difiere", "sustitucion": "difiere", "omision": "difiere",
            "cruzado": "no_aparece", "barajado": "no_aparece"}


def evaluar_pasajes(indice: cc.Indice, n: int, rng: random.Random,
                    tipos: tuple[str, ...] = cc.TIPOS) -> None:
    # Sentencias de tamaño manejable, de todas las épocas
    ids = [i for i, p in indice._por_id.items() if p.anio >= 1995 and p.tipo in tipos]
    elegidas, textos = [], {}
    # Se cuenta lo que se descarta y por qué: durante la primera evaluación
    # las SU se descartaron TODAS por no tener texto, y nadie lo vio.
    descartes = Counter()
    print(f"Descargando {n} sentencias de la relatoría (1 petición por segundo)...")
    for ident in rng.sample(ids, min(len(ids), n * 3)):
        if len(elegidas) >= n:
            break
        p = indice._por_id[ident]
        t = cc.texto_relatoria(p.tipo, p.numero, p.anio)
        if not t:
            descartes[f"{p.tipo} sin texto en la relatoría"] += 1
            continue
        if len(t.split()) > 120_000:
            descartes[f"{p.tipo} de más de 120.000 palabras"] += 1
            continue
        frs = frases_de(t)
        if len(frs) < 5:
            descartes[f"{p.tipo} con menos de 5 frases utilizables"] += 1
            continue
        elegidas.append(p)
        textos[p.id] = (t, frs)
    print("Elegidas por tipo: " + ", ".join(f"{k} {v}" for k, v in
                                             Counter(p.tipo for p in elegidas).most_common()))
    print("Descartadas: " + (", ".join(f"{k}: {v}" for k, v in descartes.most_common()) or "ninguna"))

    matriz = Counter()
    ejemplos_fallo = []
    for idx, p in enumerate(elegidas):
        texto, frs = textos[p.id]
        frase = rng.choice(frs)
        otra_id = elegidas[(idx + 1) % len(elegidas)].id
        otra = rng.choice(textos[otra_id][1])
        norm_prop = comparacion.normalizar(texto)
        # Una frase "cruzada" que por casualidad también está en esta
        # sentencia (fórmulas repetidas) no sirve de caso negativo
        if comparacion.normalizar(otra).normal in norm_prop.normal:
            otra = "la tutela no procede contra decisiones de ningún tribunal del país bajo circunstancia alguna"
        vocab = [x for x in texto.split() if len(x) >= 6 and x.isalpha()][:4000]
        for nombre, cita in variantes(frase, otra, vocab, rng).items():
            escrito = (f"En la Sentencia {p.tipo}-{p.numero.lstrip('0')} de {p.anio}, "
                       f"la Corte sostuvo: “{cita}”.")
            inf = verificacion.verificar(escrito, indice=indice, nulidad=False)
            h = inf.hallazgos[0]
            obtenido = h.pasajes[0].estado if h.pasajes else "sin_pasaje"
            matriz[(nombre, obtenido)] += 1
            if obtenido != ESPERADO[nombre] and len(ejemplos_fallo) < 12:
                ejemplos_fallo.append((nombre, p.id, obtenido,
                                       h.pasajes[0].puntuacion if h.pasajes else None,
                                       cita[:120]))

    print()
    print("=" * 72)
    print(f"PASAJES — {len(elegidas)} sentencias × {len(ESPERADO)} variantes")
    print("=" * 72)
    columnas = ["coincide", "difiere", "no_aparece", "sin_pasaje", "demasiado_corto"]
    print("%-12s %-11s" % ("variante", "esperado") + "".join("%12s" % c[:11] for c in columnas) + "   acierto")
    total_ok = total = 0
    for nombre, esp in ESPERADO.items():
        fila = [matriz[(nombre, c)] for c in columnas]
        n_fila = sum(fila)
        ok = matriz[(nombre, esp)]
        total_ok += ok
        total += n_fila
        print("%-12s %-11s" % (nombre, esp) + "".join("%12d" % x for x in fila)
              + "   %5.1f %%" % (100 * ok / max(n_fila, 1)))
    print(f"\nAcierto global: {total_ok}/{total} ({100 * total_ok / max(total, 1):.1f} %)")
    peligrosos = sum(matriz[(v, "coincide")] for v in ("negacion", "sustitucion", "omision",
                                                         "cruzado", "barajado"))
    print(f"Alteraciones dadas por buenas (lo grave): {peligrosos}")
    if ejemplos_fallo:
        print("\nFallos, para mirarlos uno a uno:")
        for e in ejemplos_fallo:
            print("   ", e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--existencia", type=int, default=300)
    ap.add_argument("--pasajes", type=int, default=40)
    ap.add_argument("--semilla", type=int, default=7)
    ap.add_argument("--tipos", default="T,C,SU", help="p. ej. SU para medir solo unificación")
    args = ap.parse_args()
    rng = random.Random(args.semilla)
    indice = cc.Indice.cargar()
    print(f"Índice: {len(indice)} providencias\n")
    if args.pasajes:
        evaluar_pasajes(indice, args.pasajes, rng, tuple(args.tipos.split(",")))
    if args.existencia:
        print(f"\nPreparando {2 * args.existencia} casos de existencia...")
        casos = banco_existencia(indice, args.existencia, rng)
        evaluar_existencia(indice, casos, rng)


if __name__ == "__main__":
    main()
