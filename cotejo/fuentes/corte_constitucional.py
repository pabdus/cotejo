"""Fuente oficial: Corte Constitucional de Colombia.

Dos accesos, los dos públicos y sin clave, comprobados en vivo:

1. El índice de datos abiertos del Gobierno (datos.gov.co, recurso v2k4-2t8s):
   29.474 providencias —tutelas, constitucionalidad y unificación— desde el 25
   de febrero de 1992 hasta la última publicada, con número, fecha, magistrado
   ponente y sala. Se descarga entero una vez (un par de MB) y se consulta en
   local: así verificar un escrito con veinte citas no son veinte peticiones.

2. La relatoría (corteconstitucional.gov.co/relatoria/AAAA/T-406-92.htm), que
   tiene el TEXTO COMPLETO de cada sentencia. Sirve para dos cosas: comprobar
   que un pasaje citado aparece de verdad en la sentencia, y confirmar la
   existencia de una providencia cuando el índice no la tiene todavía.

Una cosa que hay que saber de la relatoría: para una sentencia que no existe
NO devuelve un 404, sino un 200 con una página casi vacía (unas cien palabras
de cabecera del sitio). Hay que reconocer esa página, o toda sentencia
inventada pasaría por real.

Formato de los identificadores en el índice, medido sobre las 29.474 filas:
tipo + guion + número con ceros hasta tres cifras + barra + año con dos:
"T-012/92", "T-1025/02", "SU-111/97". Hay además sentencias «bis» con letra,
como "T-042A/25". No hay duplicados.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import documentos

API_DATOS = "https://www.datos.gov.co/resource/v2k4-2t8s.json"
RELATORIA = "https://www.corteconstitucional.gov.co/relatoria/{anio}/{archivo}.htm"
CABECERA = {"User-Agent": "Cotejo/0.1 (verificacion de citas juridicas)"}

# La Corte empezó a dictar sentencias en 1992: un año anterior en una cita
# "T-..." es un error, o una referencia a la antigua Sala Constitucional de la
# Corte Suprema, que es otra fuente.
PRIMER_ANIO = 1992
TIPOS = ("T", "C", "SU")

# Se refresca el índice cada semana. Lo que se haya publicado después lo cubre
# la comprobación contra la relatoría, que va siempre al día.
CADUCIDAD_INDICE = 7 * 24 * 3600

# Cortesía con un servidor público. Con una petición por segundo sostenida
# durante una evaluación, el sitio acabó cortando la conexión a esta máquina.
# Para un escrito normal —diez o veinte citas— 1,5 s no se nota.
PAUSA_RELATORIA = float(os.environ.get("COTEJO_PAUSA", "1.5"))


def _dir_cache() -> Path:
    base = os.environ.get("COTEJO_CACHE")
    ruta = Path(base) if base else Path(__file__).resolve().parents[2] / ".cache"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


# ----------------------------------------------------------------- modelo
@dataclass
class Providencia:
    id: str                 # "T-406/92", formato del índice oficial
    tipo: str               # T, C, SU
    numero: str             # "406", "042A"
    anio: int               # 1992
    fecha: str | None       # "1992-06-05"
    ponente: str | None
    sala: str | None
    proceso: str | None

    @property
    def url(self) -> str:
        return url_relatoria(self.tipo, self.numero, self.anio)

    def a_dict(self) -> dict:
        d = asdict(self)
        d["url"] = self.url
        return d


def anio_completo(anio: int) -> int:
    """Año de dos cifras a cuatro. La Corte existe desde 1992."""
    if anio >= 100:
        return anio
    return 1900 + anio if anio >= 92 else 2000 + anio


def normalizar_numero(numero: str) -> str:
    """'42' -> '042', '42a' -> '042A', '1025' -> '1025'."""
    m = re.fullmatch(r"0*(\d{1,4})([A-Za-z]?)", numero.strip())
    if not m:
        raise ValueError(f"número de providencia no reconocible: {numero!r}")
    return m.group(1).zfill(3) + m.group(2).upper()


def id_oficial(tipo: str, numero: str, anio: int) -> str:
    """Identificador tal como aparece en el índice: 'T-406/92'."""
    return f"{tipo.upper()}-{normalizar_numero(numero)}/{anio_completo(anio) % 100:02d}"


def urls_relatoria(tipo: str, numero: str, anio: int) -> list[str]:
    """URLs posibles en la relatoría, la más probable primero.

    Las tutelas y las de constitucionalidad llevan guion entre tipo y número
    (T-406-92.htm, c-214-21.htm); las de unificación NO (SU022-23.htm). Se
    descubrió porque las 280 sentencias SU consultadas durante la evaluación
    habían devuelto todas la página de «no existe». Se prueba la otra forma
    como respaldo, por si la relatoría no es consistente en años antiguos.
    """
    anio4 = anio_completo(anio)
    tipo, num, yy = tipo.upper(), normalizar_numero(numero), f"{anio4 % 100:02d}"
    con_guion = RELATORIA.format(anio=anio4, archivo=f"{tipo}-{num}-{yy}")
    sin_guion = RELATORIA.format(anio=anio4, archivo=f"{tipo}{num}-{yy}")
    # El respaldo solo para las SU: para T y C la forma con guion está
    # comprobada de 1992 a 2025, y probar dos URLs por cada sentencia
    # inventada duplica la carga sobre un servidor que ya nos cortó una vez.
    return [sin_guion, con_guion] if tipo == "SU" else [con_guion]


def url_relatoria(tipo: str, numero: str, anio: int) -> str:
    return urls_relatoria(tipo, numero, anio)[0]


# ------------------------------------------------------------------ índice
class Indice:
    """Índice local de providencias de la Corte Constitucional."""

    def __init__(self, filas: list[dict] | None = None):
        self._por_id: dict[str, Providencia] = {}
        self.actualizado: float | None = None
        if filas is not None:
            self._cargar_filas(filas)

    # -- construcción --------------------------------------------------
    def _cargar_filas(self, filas: list[dict]) -> None:
        for f in filas:
            ident = (f.get("sentencia") or "").strip().upper()
            m = re.fullmatch(r"(SU|T|C)-(\d{1,4}[A-Z]?)/(\d{2})", ident)
            if not m:
                continue
            fecha = (f.get("fecha_sentencia") or "")[:10] or None
            self._por_id[ident] = Providencia(
                id=ident, tipo=m.group(1), numero=m.group(2),
                anio=anio_completo(int(m.group(3))), fecha=fecha,
                ponente=(f.get("magistrado_a") or "").strip() or None,
                sala=(f.get("sala") or "").strip() or None,
                proceso=(f.get("proceso") or "").strip() or None,
            )

    @classmethod
    def cargar(cls, forzar_descarga: bool = False) -> "Indice":
        """Índice desde la caché si está fresco; si no, lo descarga entero."""
        ruta = _dir_cache() / "corte_constitucional_indice.json.gz"
        if not forzar_descarga and ruta.exists():
            edad = time.time() - ruta.stat().st_mtime
            if edad < CADUCIDAD_INDICE:
                with gzip.open(ruta, "rt", encoding="utf-8") as f:
                    indice = cls(json.load(f))
                indice.actualizado = ruta.stat().st_mtime
                return indice
        filas = cls._descargar()
        with gzip.open(ruta, "wt", encoding="utf-8") as f:
            json.dump(filas, f, ensure_ascii=False)
        indice = cls(filas)
        indice.actualizado = time.time()
        return indice

    @staticmethod
    def _descargar() -> list[dict]:
        filas: list[dict] = []
        offset, tanda = 0, 50000
        while True:
            params = {"$limit": str(tanda), "$offset": str(offset), "$order": "sentencia"}
            url = API_DATOS + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers=CABECERA)
            with urllib.request.urlopen(req, timeout=180) as r:
                lote = json.loads(r.read().decode("utf-8"))
            filas += lote
            if len(lote) < tanda:
                return filas
            offset += tanda

    # -- consulta ------------------------------------------------------
    def __len__(self) -> int:
        return len(self._por_id)

    @property
    def fecha_mas_reciente(self) -> str | None:
        """Hasta dónde llega el índice: sirve para leer una ausencia en él."""
        fechas = [p.fecha for p in self._por_id.values() if p.fecha]
        return max(fechas) if fechas else None

    def buscar(self, tipo: str, numero: str, anio: int) -> Providencia | None:
        try:
            return self._por_id.get(id_oficial(tipo, numero, anio))
        except ValueError:
            return None

    def vecinas(self, tipo: str, numero: str, anio: int, margen: int = 2) -> list[Providencia]:
        """Mismo tipo y número en años cercanos: la confusión de año más típica."""
        anio4 = anio_completo(anio)
        salida = []
        for a in range(anio4 - margen, anio4 + margen + 1):
            if a == anio4 or a < PRIMER_ANIO:
                continue
            p = self.buscar(tipo, numero, a)
            if p:
                salida.append(p)
        return salida


# --------------------------------------------------------------- relatoría
_ultima_peticion = 0.0
_PALABRAS_PAGINA_VACIA = 400    # la página de "no existe" tiene unas 105
# Una ausencia se recuerda solo un día: una sentencia recién dictada puede
# tardar en subirse a la relatoría, y no se la puede dar por inexistente
# para siempre porque no estuviera el martes.
_CADUCIDAD_AUSENCIA = 24 * 3600

# Reintentos y cortacircuitos. Tras unas 800 peticiones en una tarde, el sitio
# de la Corte dejó de responder a esta máquina —ni la portada—, cortando la
# conexión TLS. Si eso le pasa a un abogado, pierde el acceso a la relatoría
# por culpa de la herramienta. Así que: reintentar lo transitorio con espera
# creciente, y si falla varias veces seguidas, DEJAR de insistir durante un
# rato y marcar como «sin confirmar» lo que dependa de ella.
_REINTENTOS = 3
_FALLOS_PARA_CORTAR = 3
_CORTE_SEGUNDOS = 15 * 60
_fallos_seguidos = 0
_cortado_hasta = 0.0


class RelatoriaNoDisponible(Exception):
    """La relatoría no responde; no quiere decir que la sentencia no exista."""


def relatoria_disponible() -> bool:
    return time.time() >= _cortado_hasta


def _descargar_pagina(url: str) -> bytes:
    """GET con reintentos. Un 404 es una respuesta; un corte, un error."""
    global _ultima_peticion, _fallos_seguidos, _cortado_hasta
    if not relatoria_disponible():
        raise RelatoriaNoDisponible(
            "la relatoría dejó de responder hace poco; se espera antes de insistir")

    ultimo: Exception | None = None
    for intento in range(_REINTENTOS):
        espera = PAUSA_RELATORIA * (2 ** intento) - (time.time() - _ultima_peticion)
        if espera > 0:
            time.sleep(espera)
        _ultima_peticion = time.time()
        try:
            req = urllib.request.Request(url, headers=CABECERA)
            with urllib.request.urlopen(req, timeout=60) as r:
                datos = r.read()
            _fallos_seguidos = 0
            return datos
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                _fallos_seguidos = 0
                return b""
            if exc.code < 500 and exc.code != 429:
                raise
            ultimo = exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            ultimo = exc

    _fallos_seguidos += 1
    if _fallos_seguidos >= _FALLOS_PARA_CORTAR:
        _cortado_hasta = time.time() + _CORTE_SEGUNDOS
    raise RelatoriaNoDisponible(f"{type(ultimo).__name__}: {ultimo}")


def _ruta_texto(tipo: str, numero: str, anio: int) -> Path:
    nombre = f"{tipo.upper()}-{normalizar_numero(numero)}-{anio_completo(anio)}.txt.gz"
    carpeta = _dir_cache() / "relatoria"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / nombre


def _parece_la_sentencia(texto: str, tipo: str, numero: str) -> bool:
    """¿Es la sentencia, o la página casi vacía que sirve para lo que no existe?"""
    if len(texto.split()) < _PALABRAS_PAGINA_VACIA:
        return False
    num = normalizar_numero(numero)
    sin_ceros = num.lstrip("0") or "0"
    # El número tiene que aparecer en el cuerpo, con o sin ceros delante y con
    # cualquier separador o ninguno: «T-406/92», pero también «SU.214/16» y
    # «SU214-23», que es como escriben las de unificación.
    patron = rf"\b{tipo}\s*[-–.]?\s*0*{re.escape(sin_ceros)}(?!\d)"
    return re.search(patron, texto, re.IGNORECASE) is not None


def texto_relatoria(tipo: str, numero: str, anio: int) -> str | None:
    """Texto completo de la sentencia, o None si la relatoría no la tiene.

    Se guarda en caché comprimido: una sentencia de constitucionalidad con sus
    salvamentos puede pasar de las 280.000 palabras.

    Lanza RelatoriaNoDisponible si no se pudo preguntar. Quien llama tiene que
    distinguir «la relatoría dice que no existe» (None) de «no se pudo
    preguntar» (excepción): confundirlas produce falsos «no existe».
    """
    ruta = _ruta_texto(tipo, numero, anio)
    if ruta.exists():
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            contenido = f.read()
        if contenido:
            return contenido
        if time.time() - ruta.stat().st_mtime < _CADUCIDAD_AUSENCIA:
            return None
        # Ausencia caducada: se vuelve a preguntar

    texto = ""
    for url in urls_relatoria(tipo, numero, anio):
        crudo = _descargar_pagina(url)
        candidato = documentos._de_html(crudo) if crudo else ""
        if _parece_la_sentencia(candidato, tipo, numero):
            texto = candidato
            break
    # Se cachea también la ausencia, como cadena vacía, para no repetir
    with gzip.open(ruta, "wt", encoding="utf-8") as f:
        f.write(texto)
    return texto or None
