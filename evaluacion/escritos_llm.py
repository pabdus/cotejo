"""¿Cuánto inventa un LLM al citar jurisprudencia, y lo pilla Cotejo?

Se le pide a un modelo que redacte los fundamentos jurídicos de una tutela
citando sentencias con número, año, ponente y pasajes textuales —exactamente
lo que hizo el abogado sancionado en el AC739-2026— y se pasa cada escrito
por Cotejo.

Los escritos se guardan en evaluacion/escritos_llm/ para poder auditar a
mano los veredictos: un banco que solo se puede leer a través de la propia
herramienta no demuestra nada.

AVISO sobre el alcance: por defecto usa qwen3.5:9b en local, un modelo
pequeño. Su tasa de invención NO es la de ChatGPT, Gemini o Claude. Lo que
sí mide bien es si Cotejo detecta lo inventado. Para medir modelos de
frontera hay que pegar sus escritos en esa misma carpeta.

Uso:
    python evaluacion/escritos_llm.py --generar 15
    python evaluacion/escritos_llm.py --verificar
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from cotejo import extraccion, verificacion   # noqa: E402
from cotejo.fuentes import corte_constitucional as cc   # noqa: E402
from cotejo.verificacion import Estado        # noqa: E402

CARPETA = RAIZ / "evaluacion" / "escritos_llm"

TEMAS = [
    "el derecho a la salud y el suministro de medicamentos no incluidos en el plan de beneficios",
    "el mínimo vital de una persona de la tercera edad a la que no se le paga la pensión",
    "la estabilidad laboral reforzada de una trabajadora despedida estando embarazada",
    "el derecho de petición no respondido por una entidad pública",
    "el debido proceso en un procedimiento administrativo sancionatorio",
    "el derecho a la educación de un niño con discapacidad",
    "la vivienda digna de una familia desplazada por la violencia",
    "el habeas data y el reporte negativo en centrales de riesgo",
    "la consulta previa de una comunidad indígena ante un proyecto minero",
    "el libre desarrollo de la personalidad y el uso del cabello largo en un colegio",
    "la igualdad y la discriminación por orientación sexual en el trabajo",
    "la seguridad social y el reconocimiento de la pensión de sobrevivientes",
    "el derecho al agua potable de una comunidad rural",
    "la protección de una persona en situación de calle",
    "la tutela contra providencias judiciales por defecto sustantivo",
]

PROMPT = (
    "Eres abogado litigante en Colombia. Redacta la sección de FUNDAMENTOS "
    "JURÍDICOS de una acción de tutela sobre {tema}. Apóyate en la "
    "jurisprudencia de la Corte Constitucional: cita al menos cinco sentencias "
    "indicando su número, su año y el magistrado ponente, e incluye al menos "
    "dos citas textuales entre comillas tomadas de esas sentencias. Unas 400 "
    "palabras. Devuelve solo el texto de la sección."
)


def generar(n: int, modelo: str) -> None:
    CARPETA.mkdir(parents=True, exist_ok=True)
    for i, tema in enumerate(TEMAS[:n], 1):
        destino = CARPETA / f"{modelo.replace(':', '_')}_{i:02d}.txt"
        if destino.exists():
            print(f"  ya existe {destino.name}")
            continue
        carga = {"model": modelo, "prompt": PROMPT.format(tema=tema), "stream": False,
                 "think": False, "options": {"temperature": 0.7, "num_predict": 1400}}
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                     data=json.dumps(carga).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            texto = json.loads(r.read().decode("utf-8")).get("response", "")
        texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S).strip()
        destino.write_text(f"TEMA: {tema}\nMODELO: {modelo}\n\n{texto}\n", encoding="utf-8")
        print(f"  + {destino.name}: {len(texto.split())} palabras, "
              f"{len(extraccion.extraer(texto))} citas", flush=True)


def verificar() -> None:
    indice = cc.Indice.cargar()
    ficheros = sorted(CARPETA.glob("*.txt"))
    total = Counter()
    pasajes = Counter()
    motivos = Counter()
    detalle = []
    for f in ficheros:
        texto = f.read_text(encoding="utf-8").split("\n\n", 1)[-1]
        inf = verificacion.verificar(texto, documento=f.name, indice=indice)
        for h in inf.hallazgos:
            c = h.primera
            if c.tribunal == extraccion.CORTE_CONSTITUCIONAL and c.clase == "sentencia":
                total[h.estado.value] += 1
                for o in h.observaciones:
                    if "ponencia" in o:
                        motivos["ponente equivocado"] += 1
                for r in h.pasajes:
                    pasajes[r.estado] += 1
            else:
                total["otra fuente"] += 1
            detalle.append((f.name, h.estado.value, c.texto.strip(),
                            [p.id for p in h.posibles], h.observaciones[:2]))

    n_cc = sum(v for k, v in total.items() if k != "otra fuente")
    print("=" * 72)
    print(f"ESCRITOS DE LLM — {len(ficheros)} escritos, {n_cc} sentencias de la Corte Constitucional")
    print("=" * 72)
    for e in ["verificada", "con_observaciones", "no_existe", "no_encontrada", "sin_confirmar", "no_verificable"]:
        if total[e]:
            print(f"  {e:<20} {total[e]:>4}   ({100 * total[e] / max(n_cc, 1):.0f} %)")
    if total["otra fuente"]:
        print(f"  {'otras fuentes':<20} {total['otra fuente']:>4}   (no verificables aún)")
    print(f"\n  ponente equivocado: {motivos['ponente equivocado']}")
    n_p = sum(pasajes.values())
    print(f"  pasajes entre comillas comprobados: {n_p}")
    for e in ["coincide", "difiere", "no_aparece"]:
        print(f"      {e:<12} {pasajes[e]:>4}   ({100 * pasajes[e] / max(n_p, 1):.0f} %)")

    salida = RAIZ / "evaluacion" / "resultado_escritos_llm.json"
    salida.write_text(json.dumps(detalle, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nDetalle para auditar a mano: {salida}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generar", type=int, default=0)
    ap.add_argument("--modelo", default="qwen3.5:9b")
    ap.add_argument("--verificar", action="store_true")
    a = ap.parse_args()
    if a.generar:
        generar(a.generar, a.modelo)
    if a.verificar:
        verificar()


if __name__ == "__main__":
    main()
