"""Salidas del verificador: terminal, JSON e informe HTML imprimible.

El informe es el producto. Lo lee un abogado con prisa, así que el orden es:
primero lo que puede costarle una sanción (citas que no existen o que dicen
otra cosa), después lo verificado y al final lo que no se pudo comprobar, que
se muestra siempre: esconderlo haría creer que el escrito está revisado
entero.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re

from .verificacion import ETIQUETA, Estado, Hallazgo, Informe

ORDEN = [Estado.NO_EXISTE, Estado.CON_OBSERVACIONES, Estado.SIN_CONFIRMAR,
         Estado.VERIFICADA, Estado.NO_VERIFICABLE]


def _ordenados(inf: Informe) -> list[Hallazgo]:
    return sorted(inf.hallazgos, key=lambda h: (ORDEN.index(h.estado), h.primera.inicio))


def _plano(texto: str, maximo: int | None = None) -> str:
    """Una sola línea, sin los saltos que trae el texto de la relatoría."""
    t = " ".join(texto.split())
    if maximo and len(t) > maximo:
        t = t[:maximo].rstrip() + "…"
    return t


# ------------------------------------------------------------------ terminal
def a_texto(inf: Informe) -> str:
    lineas = [f"Cotejo · {inf.documento}", "=" * 72]
    total = len(inf.hallazgos)
    lineas.append(f"{total} providencias citadas en {inf.palabras} palabras")
    for e in ORDEN:
        n = inf.contar(e)
        if n:
            lineas.append(f"  {ETIQUETA[e]:<28} {n}")
    lineas.append("")
    for h in _ordenados(inf):
        c = h.primera
        veces = f" (citada {len(h.citas)} veces)" if len(h.citas) > 1 else ""
        lineas.append(f"[{ETIQUETA[h.estado]}] {c.texto.strip()}{veces}")
        if h.providencia:
            p = h.providencia
            datos = [x for x in (p.fecha, p.ponente and f"M.P. {p.ponente}") if x]
            if datos:
                lineas.append("    " + " · ".join(datos))
        if h.motivo:
            lineas.append(f"    {h.motivo}")
        for o in h.observaciones:
            lineas.append(f"    ! {o}")
        for r in h.pasajes:
            if r.estado == "coincide":
                continue
            lineas.append(f"    pasaje citado ({r.puntuacion:.0f} % de las palabras coinciden): "
                          f"«{_plano(r.pasaje, 110)}»")
            if r.estado == "no_aparece":
                # El fragmento que peor encaja es el que hay que enseñar
                peor = min(r.fragmentos, key=lambda f: f.puntuacion, default=None)
                if peor and peor.mas_parecido:
                    lineas.append(f"    lo más parecido en la sentencia: «{_plano(peor.mas_parecido, 110)}»")
        if h.posibles:
            sug = ", ".join(p.id for p in h.posibles)
            lineas.append(f"    ¿quizá quería citar {sug}?")
        lineas.append("")
    return "\n".join(lineas)


# ---------------------------------------------------------------------- JSON
def a_json(inf: Informe) -> dict:
    return {
        "documento": inf.documento,
        "palabras": inf.palabras,
        "resumen": {e.value: inf.contar(e) for e in Estado},
        "hallazgos": [
            {
                "clave": h.clave,
                "estado": h.estado.value,
                "citado_como": [c.texto for c in h.citas],
                "tribunal": h.primera.tribunal,
                "providencia": h.providencia.a_dict() if h.providencia else None,
                "fuente": h.fuente,
                "motivo": h.motivo,
                "observaciones": h.observaciones,
                "ponentes_citados": [c.ponente for c in h.citas if c.ponente],
                "pasajes": [
                    {
                        "pasaje": r.pasaje,
                        "estado": r.estado,
                        "puntuacion": r.puntuacion,
                        "fragmentos": [
                            {"fragmento": f.fragmento, "puntuacion": f.puntuacion,
                             "mas_parecido": _plano(f.mas_parecido),
                             "diferencias": [{"escrito": e, "sentencia": s}
                                             for e, s in f.diferencias]}
                            for f in r.fragmentos
                        ],
                    }
                    for r in h.pasajes
                ],
                "posibles": [p.id for p in h.posibles],
            }
            for h in _ordenados(inf)
        ],
    }


def a_json_texto(inf: Informe) -> str:
    return json.dumps(a_json(inf), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------- HTML
_E = html.escape

_ESTILO = """
:root{--papel:#FBFAF7;--tinta:#1C1B18;--suave:#5E5A52;--linea:#DDD8CE;--caja:#FFFFFF;
--rojo:#9B2C22;--rojo-f:#F6E4E0;--ambar:#8A5A0B;--ambar-f:#F8EDD8;--verde:#2F6B45;
--verde-f:#E3F0E7;--gris:#6B6B6B;--gris-f:#EEEDEA;--acento:#23395B}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--papel:#141413;
--tinta:#ECE9E2;--suave:#A8A398;--linea:#34322D;--caja:#1C1B19;--rojo:#E5897D;
--rojo-f:#3A1D19;--ambar:#E0B465;--ambar-f:#34280F;--verde:#86C39C;--verde-f:#16291D;
--gris:#A9A9A9;--gris-f:#262523;--acento:#9DB6DA}}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);
font:16px/1.6 "Source Serif 4",Georgia,"Times New Roman",serif}
.hoja{max-width:860px;margin:0 auto;padding:40px 24px 80px}
header{border-bottom:2px solid var(--tinta);padding-bottom:18px;margin-bottom:28px}
.marca{font:600 12px/1 "IBM Plex Sans",system-ui,sans-serif;letter-spacing:.16em;
text-transform:uppercase;color:var(--acento)}
h1{font-size:1.75rem;line-height:1.2;margin:10px 0 6px;text-wrap:balance}
.meta{font:13px/1.5 "IBM Plex Sans",system-ui,sans-serif;color:var(--suave)}
.resumen{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;
margin:0 0 30px}
.cifra{background:var(--caja);border:1px solid var(--linea);border-radius:4px;padding:12px 14px}
.cifra b{display:block;font:600 1.6rem/1.1 "IBM Plex Sans",system-ui,sans-serif;
font-variant-numeric:tabular-nums}
.cifra span{font:12px/1.3 "IBM Plex Sans",system-ui,sans-serif;color:var(--suave)}
.cifra.no_existe b{color:var(--rojo)}.cifra.con_observaciones b{color:var(--ambar)}
.cifra.verificada b{color:var(--verde)}
h2{font:600 13px/1 "IBM Plex Sans",system-ui,sans-serif;letter-spacing:.12em;
text-transform:uppercase;color:var(--suave);margin:34px 0 12px}
.cita{background:var(--caja);border:1px solid var(--linea);border-left-width:4px;
border-radius:4px;padding:14px 18px;margin:0 0 12px;break-inside:avoid}
.cita.no_existe{border-left-color:var(--rojo)}.cita.con_observaciones{border-left-color:var(--ambar)}
.cita.verificada{border-left-color:var(--verde)}.cita.sin_confirmar,.cita.no_verificable{border-left-color:var(--gris)}
.cab{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:baseline}
.cab strong{font-size:1.08rem}
.chip{font:600 11px/1 "IBM Plex Sans",system-ui,sans-serif;letter-spacing:.06em;
text-transform:uppercase;padding:4px 7px;border-radius:3px}
.chip.no_existe{background:var(--rojo-f);color:var(--rojo)}
.chip.con_observaciones{background:var(--ambar-f);color:var(--ambar)}
.chip.verificada{background:var(--verde-f);color:var(--verde)}
.chip.sin_confirmar,.chip.no_verificable{background:var(--gris-f);color:var(--gris)}
.datos{font:13px/1.5 "IBM Plex Sans",system-ui,sans-serif;color:var(--suave);margin-top:4px}
.datos a{color:var(--acento)}
.obs{margin:8px 0 0;padding-left:18px}.obs li{margin:3px 0}
.pasaje{margin:10px 0 0;font-size:.95rem}
.pasaje .et{font:600 11px/1 "IBM Plex Sans",system-ui,sans-serif;letter-spacing:.08em;
text-transform:uppercase;color:var(--suave);display:block;margin-bottom:3px}
blockquote{margin:0 0 8px;padding:6px 12px;border-left:2px solid var(--linea);color:var(--tinta)}
blockquote.real{border-left-color:var(--verde)}
mark{background:var(--ambar-f);color:var(--ambar);font-weight:700;padding:0 2px;border-radius:2px}
blockquote.real mark{background:var(--verde-f);color:var(--verde)}
.nota{font:13px/1.55 "IBM Plex Sans",system-ui,sans-serif;color:var(--suave);
border-top:1px solid var(--linea);margin-top:36px;padding-top:14px}
@media print{body{background:#fff}.cita{border-color:#bbb}}
"""


def _resaltar(texto: str, palabras: list[str]) -> str:
    """Escapa el texto y marca la primera aparición de cada tramo distinto.

    Es solo ayuda visual: la diferencia exacta ya va escrita en la
    observación, así que si un tramo aparece dos veces y se marca el que no
    era, no se pierde información.
    """
    marcas: list[tuple[int, int]] = []
    for p in palabras:
        p = p.strip()
        if not p:
            continue
        m = re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", texto, re.IGNORECASE)
        if m and all(m.end() <= a or m.start() >= b for a, b in marcas):
            marcas.append((m.start(), m.end()))
    salida, pos = [], 0
    for a, b in sorted(marcas):
        salida.append(_E(texto[pos:a]))
        salida.append(f"<mark>{_E(texto[a:b])}</mark>")
        pos = b
    salida.append(_E(texto[pos:]))
    return "".join(salida)


def _tarjeta(h: Hallazgo) -> str:
    c = h.primera
    partes = [f'<article class="cita {h.estado.value}">', '<div class="cab">',
              f"<strong>{_E(c.texto.strip())}</strong>",
              f'<span class="chip {h.estado.value}">{_E(ETIQUETA[h.estado])}</span>']
    if len(h.citas) > 1:
        partes.append(f'<span class="datos">citada {len(h.citas)} veces</span>')
    partes.append("</div>")

    datos = []
    if h.providencia:
        p = h.providencia
        if p.fecha:
            datos.append(_E(p.fecha))
        if p.ponente:
            datos.append("M.P. " + _E(p.ponente))
        datos.append(f'<a href="{_E(p.url)}">texto en la relatoría</a>')
        if h.fuente == "relatoria":
            datos.append("aún no figura en el índice oficial")
    elif c.tribunal:
        datos.append(_E(c.tribunal))
    if datos:
        partes.append(f'<div class="datos">{" · ".join(datos)}</div>')

    notas = ([h.motivo] if h.motivo else []) + h.observaciones
    if h.posibles:
        notas.append("¿Quizá quería citar " + ", ".join(p.id for p in h.posibles) + "?")
    if notas:
        partes.append('<ul class="obs">' + "".join(f"<li>{_E(n)}</li>" for n in notas) + "</ul>")

    for r in h.pasajes:
        if r.estado == "coincide":
            partes.append('<div class="pasaje"><span class="et">Pasaje citado · aparece literal en la sentencia</span>'
                          f"<blockquote class=\"real\">{_E(_plano(r.pasaje))}</blockquote></div>")
            continue
        peor = min(r.fragmentos, key=lambda f: f.puntuacion, default=None)
        difiere = r.estado == "difiere"
        etiqueta = "no coincide literalmente" if difiere else "no aparece en la sentencia"
        citado = (_resaltar(_plano(r.pasaje), [e for e, _ in r.diferencias])
                  if difiere else _E(_plano(r.pasaje)))
        partes.append('<div class="pasaje">'
                      f'<span class="et">Pasaje citado · {_E(etiqueta)} · '
                      f'{r.puntuacion:.0f} % de las palabras</span>'
                      f"<blockquote>{citado}</blockquote>")
        if peor and peor.mas_parecido:
            titulo = ("Lo que dice la sentencia en ese punto" if difiere
                      else "Lo más parecido que dice la sentencia")
            real = (_resaltar(_plano(peor.mas_parecido), [s for _, s in peor.diferencias])
                    if difiere else _E(_plano(peor.mas_parecido)))
            partes.append(f'<span class="et">{titulo}</span>'
                          f'<blockquote class="real">{real}</blockquote>')
        partes.append("</div>")
    partes.append("</article>")
    return "".join(partes)


def a_html(inf: Informe) -> str:
    fecha = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    grupos = [
        ("Requieren atención", [Estado.NO_EXISTE, Estado.CON_OBSERVACIONES, Estado.SIN_CONFIRMAR]),
        ("Verificadas", [Estado.VERIFICADA]),
        ("No verificadas en esta versión", [Estado.NO_VERIFICABLE]),
    ]
    cifras = "".join(
        f'<div class="cifra {e.value}"><b>{inf.contar(e)}</b><span>{_E(ETIQUETA[e])}</span></div>'
        for e in ORDEN
    )
    cuerpo = []
    for titulo, estados in grupos:
        hs = [h for h in _ordenados(inf) if h.estado in estados]
        if hs:
            cuerpo.append(f"<h2>{_E(titulo)} · {len(hs)}</h2>")
            cuerpo += [_tarjeta(h) for h in hs]
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cotejo · {_E(inf.documento)}</title>
<style>{_ESTILO}</style></head>
<body><main class="hoja">
<header><div class="marca">Cotejo · verificación de citas</div>
<h1>{_E(inf.documento)}</h1>
<div class="meta">{len(inf.hallazgos)} providencias citadas · {inf.palabras} palabras · generado el {fecha}</div>
</header>
<section class="resumen">{cifras}</section>
{''.join(cuerpo)}
<p class="nota">Cotejo contrasta cada cita con el índice oficial de providencias de la Corte
Constitucional (datos.gov.co) y con el texto publicado en su relatoría. Una providencia solo se
declara inexistente cuando falta en las dos fuentes. Los pasajes entre comillas se buscan en el
texto completo de la sentencia, tolerando tildes, puntuación y elisiones. Este informe ayuda a
cumplir el deber de verificación, pero no sustituye la lectura de las providencias por parte de
quien firma el escrito.</p>
</main></body></html>"""
