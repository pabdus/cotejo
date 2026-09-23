"""Fuente oficial: Corte Suprema de Justicia de Colombia.

La Corte Suprema no publica un índice en datos abiertos como la Constitucional.
Lo que tiene es el buscador de su relatoría (consultaprovidencias.cortesuprema.
gov.co), que por debajo habla con un servicio GraphQL público y sin clave:

    POST https://consultaprovidenciasbk.cortesuprema.gov.co/api
    getSearchResult(searchQuery: {...})   búsqueda, 10 resultados por página
    getContentSearch(previewDocument: {...})   texto completo de un documento

Cosas que hay que saber, todas comprobadas en vivo:

- La búsqueda «exacta» es de TEXTO COMPLETO, no de título. Buscar
  «SP022-2019» devuelve primero un auto que la cita y después la sentencia. Así
  que se filtra por título y se pagina: una ausencia solo cuenta si se han
  visto TODOS los resultados.
- El título empieza por el código y sigue con otra cosa: «SC5686-2018
  [2004-00042-01].pdf», «SP022-2019(47228).doc», «STC15558-2021.docx».
- El número puede ir con ceros delante («SP022-2019») y la búsqueda exacta no
  los ignora: «SP22-2019» no encuentra nada. Se prueban las dos formas.
- El título NO se busca: solo el texto. Si la providencia escribe su código
  con espacios («AP – 7408 - 2015»), buscar «AP7408-2015» no la encuentra, y
  hay que buscar la frase «AP 7408 2015».
- NO ESTÁ TODO. De 71 providencias citadas por la propia Corte, tres reales
  no aparecían (tutelas y autos de la Sala Civil). Por eso una ausencia aquí
  nunca es «no existe»: es «no encontrada».
- Cada providencia sale varias veces: en .pdf, en .doc o .docx, y a veces
  duplicada en una carpeta sin ponente. El ponente sale de la ruta
  («Dra. Margarita Cabello Blanco»). Hay providencias registradas bajo dos
  magistrados distintos (STP11082-2021).
- El texto de los PDF puede ser un OCR ilegible; el de los .doc y .docx es
  limpio. Se prefiere siempre el documento de Word.
- La base se sube con meses de retraso: en septiembre de 2026, lo último de
  la Sala Civil era de finales de abril. El auto que motivó este proyecto,
  el AC739-2026, no estaba todavía.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import html
import json
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import red

API = "https://consultaprovidenciasbk.cortesuprema.gov.co/api"
BUSCADOR = "https://consultaprovidencias.cortesuprema.gov.co/"

# Servidor distinto del de la Constitucional y, en las pruebas, más rápido
# (0,2 s por consulta). Aun así: un segundo entre peticiones.
PAUSA = float(os.environ.get("COTEJO_PAUSA_CSJ", "1.0"))
cliente = red.Cliente("El buscador de providencias de la Corte Suprema", PAUSA)

# Prefijo del código -> sala en el buscador. Las tutelas tienen su propia sala
# de búsqueda, sea cual sea la sala que las decidió.
SALAS = {
    "SC": "CIVIL", "AC": "CIVIL",
    "SL": "LABORAL", "AL": "LABORAL",
    "SP": "PENAL", "AP": "PENAL", "AHP": "PENAL",
    "STC": "TUTELAS", "ATC": "TUTELAS",
    "STL": "TUTELAS", "ATL": "TUTELAS",
    "STP": "TUTELAS", "ATP": "TUTELAS",
}
PREFIJOS = tuple(sorted(SALAS, key=len, reverse=True))

POR_PAGINA = 10
# Páginas que se miran como mucho por cada forma del código. Si hay más y no
# apareció, la ausencia no se puede afirmar y queda «sin confirmar».
MAX_PAGINAS = 5

_CADUCIDAD_AUSENCIA = 24 * 3600


# ----------------------------------------------------------------- modelo
@dataclass
class ProvidenciaCSJ:
    id: str                          # "SC5686-2018", tal como la registra la Corte
    prefijo: str
    numero: str
    anio: int
    sala: str                        # sala del buscador: CIVIL, LABORAL, PENAL, TUTELAS
    clase: str | None                # SENTENCIA | AUTO
    ponentes: list[str] = field(default_factory=list)
    documentos: list[str] = field(default_factory=list)   # rutas, Word primero
    fecha: str | None = None         # no la da el buscador con fiabilidad

    @property
    def ponente(self) -> str | None:
        return "; ".join(self.ponentes) or None

    @property
    def url(self) -> str:
        """Enlace al visor del documento, o a la búsqueda si no se puede formar.

        El visor de la Corte recibe la ruta en base64 tal como la hace btoa()
        (bytes latin-1) y sin escapar: el enrutador no decodifica «%3D». Una
        «/» dentro del base64 rompería la ruta, y entonces se enlaza la búsqueda.
        """
        if self.documentos:
            try:
                clave = base64.b64encode(self.documentos[0].encode("latin-1")).decode("ascii")
            except UnicodeEncodeError:
                clave = ""
            if clave and "/" not in clave:
                return f"{BUSCADOR}#/visualizador/{clave}/{self.sala}/{urllib.parse.quote(self.id)}"
        return url_busqueda(self.id, self.sala)

    def a_dict(self) -> dict:
        d = asdict(self)
        d["ponente"] = self.ponente
        d["url"] = self.url
        return d


@dataclass
class Busqueda:
    providencia: ProvidenciaCSJ | None
    completa: bool          # se vieron todos los resultados de la consulta
    total: int              # resultados que dijo tener el buscador


def url_busqueda(texto: str, sala: str) -> str:
    """La búsqueda ya hecha en el buscador de la Corte, para comprobar a mano."""
    return f"{BUSCADOR}#/resultados-busqueda/%22{urllib.parse.quote(texto)}%22/{sala.capitalize()}"


def codigo(prefijo: str, numero: str | int, anio: int) -> str:
    """Forma canónica para agrupar y mostrar: sin ceros delante."""
    return f"{prefijo.upper()}{int(numero)}-{anio}"


def _variantes(prefijo: str, numero: str | int, anio: int) -> list[str]:
    """Formas en que una providencia puede escribir su propio código.

    El buscador trocea por guiones, así que «SC5686-2018» encuentra lo que
    dice «SC5686-2018». Pero el AP7408-2015 se encabeza «AP – 7408 - 2015», que
    son tres palabras sueltas, y solo lo encuentra la frase «AP 7408 2015».
    """
    n = int(numero)
    formas = [f"{prefijo}{n}-{anio}", f"{prefijo}{n:03d}-{anio}",
              f"{prefijo} {n} {anio}", f"{prefijo} {n:03d} {anio}"]
    return list(dict.fromkeys(formas))


def _patron_titulo(prefijo: str, numero: str | int, anio: int) -> re.Pattern:
    return re.compile(rf"^\s*{prefijo}\s*-?\s*0*{int(numero)}\s*-\s*{anio}(?!\d)", re.IGNORECASE)


def limpiar_ponente(doctor: str | None) -> str | None:
    if not doctor:
        return None
    nombre = re.sub(r"^\s*(?:Dra?|Doctora?)\.?\s*", "", doctor.strip())
    if not nombre or nombre.lower().startswith("conjuec"):
        return None
    return nombre


def _preferencia(ruta: str) -> int:
    ext = ruta.rsplit(".", 1)[-1].lower()
    return {"docx": 0, "doc": 1, "rtf": 2, "pdf": 3}.get(ext, 4)


# ------------------------------------------------------------------ consultas
def _graphql(consulta: str) -> dict:
    cuerpo = cliente.pedir(API, datos=json.dumps({"query": consulta}).encode("utf-8"),
                           cabeceras={"Content-Type": "application/json"})
    if not cuerpo:
        raise red.ServicioNoDisponible("el buscador de la Corte Suprema respondió vacío")
    datos = json.loads(cuerpo.decode("utf-8"))
    if datos.get("errors"):
        raise red.ServicioNoDisponible(f"error del buscador: {datos['errors'][0].get('message')}")
    return datos["data"]


def _cadena(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _buscar_pagina(texto: str, sala: str, anio: int | None, inicio: int) -> tuple[list[dict], int]:
    q = (
        "{ getSearchResult(searchQuery:{ query: %s typeOfQuery: %s start: %d isExact: true "
        'magistrate: "" year: %s autoSentencia: "" order: "" roomTutelas: "" addedQueries: [] }) '
        "{ searchResults { title onlinePath doctor ano autoSentencia } numOfResults } }"
    ) % (_cadena(texto), _cadena(sala), inicio, _cadena(str(anio) if anio else ""))
    r = _graphql(q)["getSearchResult"]
    return r.get("searchResults") or [], int(r.get("numOfResults") or 0)


def _consultar(prefijo: str, numero: str | int, anio: int) -> Busqueda:
    sala = SALAS[prefijo]
    patron = _patron_titulo(prefijo, numero, anio)
    completa, total_max = True, 0
    for texto in _variantes(prefijo, numero, anio):
        hallados, vista_entera, total = _recorrer(texto, sala, anio, patron)
        total_max = max(total_max, total)
        if hallados:
            return Busqueda(_armar(prefijo, anio, sala, hallados), True, total_max)
        completa = completa and vista_entera
    return Busqueda(None, completa, total_max)


def _recorrer(texto: str, sala: str, anio: int, patron: re.Pattern) -> tuple[list[dict], bool, int]:
    """Busca la providencia entre los resultados de una consulta.

    Los resultados vienen más o menos de lo último subido a lo primero, y las
    providencias que citan a otra se suben después que ella: la citada suele
    quedar al FINAL. El AP2118-2020 salió en la posición 149 de 155; el
    AP2235-2020, en la 50 de 63. Así que se mira la primera página (por si hay
    pocos resultados) y después las últimas, hacia atrás.

    Devuelve (filas que son la providencia, si se vio todo, total).
    """
    paginas: dict[int, list[dict]] = {}
    primera, total = _buscar_pagina(texto, sala, anio, 0)
    paginas[0] = primera

    def pagina(p: int) -> list[dict]:
        if p not in paginas:
            paginas[p] = _buscar_pagina(texto, sala, anio, p * POR_PAGINA)[0]
        return paginas[p]

    def suyas(filas: list[dict]) -> list[int]:
        return [i for i, r in enumerate(filas) if patron.match(r.get("title") or "")]

    n_paginas = max(1, -(-total // POR_PAGINA))
    orden = [0] + list(range(n_paginas - 1, 0, -1))
    for p in orden[:MAX_PAGINAS]:
        filas = pagina(p)
        idx = suyas(filas)
        if idx:
            hallados = [filas[i] for i in idx]
            # Las versiones (.doc, .pdf) van juntas, pero pueden partirse
            # entre dos páginas
            if idx[0] == 0 and p > 0:
                hallados += [pagina(p - 1)[i] for i in suyas(pagina(p - 1))]
            if idx[-1] == len(filas) - 1 and p + 1 < n_paginas:
                hallados += [pagina(p + 1)[i] for i in suyas(pagina(p + 1))]
            return hallados, True, total
    return [], len(orden) <= MAX_PAGINAS, total


def _armar(prefijo: str, anio: int, sala: str, filas: list[dict]) -> ProvidenciaCSJ:
    patron_id = re.compile(rf"^\s*({prefijo}\s*-?\s*\d+\s*-\s*\d{{4}})", re.IGNORECASE)
    m = patron_id.match(filas[0]["title"])
    ident = re.sub(r"\s+", "", m.group(1)).upper() if m else filas[0]["title"]
    ponentes: list[str] = []
    for f in filas:
        p = limpiar_ponente(f.get("doctor"))
        if p and not any(p.lower() == q.lower() for q in ponentes):
            ponentes.append(p)
    clases = {(f.get("autoSentencia") or "").upper() for f in filas} - {""}
    rutas = sorted({f["onlinePath"] for f in filas if f.get("onlinePath")}, key=_preferencia)
    num = re.search(r"(\d+)\s*-\s*\d{4}$", ident)
    return ProvidenciaCSJ(
        id=ident, prefijo=prefijo, numero=num.group(1) if num else "", anio=anio, sala=sala,
        clase=clases.pop() if len(clases) == 1 else None,
        ponentes=ponentes, documentos=rutas,
    )


# ---------------------------------------------------------------- cobertura
def anio_cubierto(anio: int, hoy: dt.date | None = None) -> bool:
    """¿Ha pasado tiempo suficiente para que estén subidas las de ese año?

    En septiembre de 2026 lo último de la Sala Civil era de finales de abril:
    unos cinco meses de retraso. Un año se da por completo a partir del 1 de
    julio del siguiente.
    """
    hoy = hoy or dt.date.today()
    return hoy >= dt.date(anio + 1, 7, 1)


# Canario, como con la relatoría: antes de leer una ausencia, comprobar que el
# buscador encuentra una providencia que seguro está. Si un día cambia su
# formato de respuesta o su índice, todo saldría «no encontrada».
CANARIO = ("SC", "5686", 2018)
_CANARIO_VALIDEZ = 3600
_canario_ok_hasta = 0.0


def buscador_fiable() -> bool:
    global _canario_ok_hasta
    if time.time() < _canario_ok_hasta:
        return True
    if _consultar(*CANARIO).providencia is not None:
        _canario_ok_hasta = time.time() + _CANARIO_VALIDEZ
        return True
    return False


# -------------------------------------------------------------------- caché
def _carpeta(nombre: str) -> Path:
    ruta = red.dir_cache() / "corte_suprema" / nombre
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def buscar(prefijo: str, numero: str | int, anio: int) -> Busqueda:
    """¿Está la providencia en la base de la Corte Suprema?

    Lanza red.ServicioNoDisponible si no se pudo preguntar. Una búsqueda
    incompleta (demasiados resultados) vuelve con completa=False: quien llama
    no puede leer ahí una ausencia.
    """
    prefijo = prefijo.upper()
    if prefijo not in SALAS:
        raise ValueError(f"prefijo de la Corte Suprema desconocido: {prefijo}")
    ruta = _carpeta("busquedas") / f"{codigo(prefijo, numero, anio)}.json"
    if ruta.exists():
        guardado = json.loads(ruta.read_text(encoding="utf-8"))
        if guardado["providencia"] or time.time() - ruta.stat().st_mtime < _CADUCIDAD_AUSENCIA:
            prov = ProvidenciaCSJ(**{k: v for k, v in guardado["providencia"].items()
                                     if k not in ("ponente", "url")}) if guardado["providencia"] else None
            return Busqueda(prov, guardado["completa"], guardado["total"])

    b = _consultar(prefijo, numero, anio)
    if b.completa:     # una búsqueda a medias no se guarda: se repetirá
        ruta.write_text(json.dumps({
            "providencia": b.providencia.a_dict() if b.providencia else None,
            "completa": b.completa, "total": b.total,
        }, ensure_ascii=False), encoding="utf-8")
    return b


# -------------------------------------------------------------------- texto
def _de_html(fragmento: str) -> str:
    t = re.sub(r"<\s*(?:br|/p)\s*/?>", "\n", fragmento, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


@dataclass
class Texto:
    texto: str
    documento: str      # ruta del documento del que sale

    @property
    def de_pdf(self) -> bool:
        return self.documento.lower().endswith(".pdf")


def texto(prov: ProvidenciaCSJ) -> Texto | None:
    """Texto completo, del documento de Word si lo hay.

    Si solo hay PDF, se devuelve igual, marcado: el OCR puede ser malo, y
    quien compara no puede leer un «no aparece» en ese texto como una prueba.
    """
    if not prov.documentos:
        return None
    documento = prov.documentos[0]
    ruta = _carpeta("textos") / f"{prov.id}.txt.gz"
    if ruta.exists():
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            guardado = json.load(f)
        if guardado["documento"] == documento:
            return Texto(guardado["texto"], documento)

    q = ("{ getContentSearch(previewDocument:{ id: %s room: %s text: %s }) { contentText } }"
         % (_cadena(documento), _cadena(prov.sala), _cadena(prov.id)))
    crudo = (_graphql(q).get("getContentSearch") or {}).get("contentText") or ""
    plano = _de_html(crudo)
    if len(plano.split()) < 50:
        return None
    with gzip.open(ruta, "wt", encoding="utf-8") as f:
        json.dump({"texto": plano, "documento": documento}, f, ensure_ascii=False)
    return Texto(plano, documento)
