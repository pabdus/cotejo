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
NO devuelve un 404, sino un 200 con una página vacía (desde septiembre de
2026, la carcasa de la aplicación del sitio). Hay que reconocer esa página, o
toda sentencia inventada pasaría por real. Y otra: las sentencias anuladas
siguen en el índice; solo la relatoría lo dice.

Formato de los identificadores en el índice, medido sobre las 29.474 filas:
tipo + guion + número con ceros hasta tres cifras + barra + año con dos:
"T-012/92", "T-1025/02", "SU-111/97". Hay además sentencias «bis» con letra,
como "T-042A/25". No hay duplicados.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import documentos
from . import red
from .red import CABECERA

API_DATOS = "https://www.datos.gov.co/resource/v2k4-2t8s.json"
RELATORIA = "https://www.corteconstitucional.gov.co/relatoria/{anio}/{archivo}.htm"

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
    return red.dir_cache()


def _hoy() -> dt.date:
    """La fecha de hoy, aparte para que las pruebas la fijen.

    En la Lambda es la de UTC, unas horas por delante de Colombia: a
    `Indice.quiza_reciente` le da, como mucho, un día más de margen, nunca uno menos.
    """
    return dt.date.today()


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
# Sentencias recientes que el índice aún no recoge (DISEÑO §1d). El índice de
# datos.gov.co va con semanas de retraso, y la relatoría tarda semanas o meses
# en publicar algunas C y SU: una sentencia real de hace un mes puede faltar en
# las dos fuentes. Con el índice recortado al 12-08-2026, la C-273/26 y la
# SU-261/26, reales, salían «no existe» (reproducido en la revisión).
#
# Lo dictado hasta 60 días antes de la última fecha del índice se da por
# asentado: si ahí falta un número, la ausencia significa algo. Por encima, un
# número solo es imposible si pasa de lo que da el ritmo de la Corte, con margen
# (el doble, y 20 números más), hasta hoy.
_DIAS_SIN_ASENTAR = 60
_MARGEN_RITMO = 2
_MARGEN_NUMEROS = 20
# Sentencias por día, como mínimo, para un año con pocos datos. Era 0,1: con un
# índice que deja de actualizarse (la descarga semanal falla y se conserva el
# anterior), el año siguiente casi no tiene filas y la C-038/26 o la SU-040/26,
# reales, salían «no existe» (revisión 4). El ritmo de la Corte no ha bajado de
# 443 al año (1,21 al día, en 2021) desde 2015; 1,0 solo ensancha ventanas.
_RITMO_MINIMO = 1.0


class Indice:
    """Índice local de providencias de la Corte Constitucional."""

    def __init__(self, filas: list[dict] | None = None):
        self._por_id: dict[str, Providencia] = {}
        self.actualizado: float | None = None
        # (tipo, fecha, número) por año y la última fecha, para quiza_reciente;
        # se calcula la primera vez que hace falta
        self._resumen: tuple[dict[int, list[tuple[str, dt.date | None, int]]], dt.date | None] | None = None
        if filas is not None:
            self._cargar_filas(filas)

    # -- construcción --------------------------------------------------
    def _cargar_filas(self, filas: list[dict]) -> None:
        self._resumen = None
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

    def _por_anio(self) -> tuple[dict[int, list[tuple[str, dt.date | None, int]]], dt.date | None]:
        if self._resumen is None:
            anios: dict[int, list[tuple[str, dt.date | None, int]]] = {}
            fin = None
            for p in self._por_id.values():
                try:
                    fecha = dt.date.fromisoformat(p.fecha) if p.fecha else None
                except ValueError:
                    fecha = None
                fin = fecha if fecha and (fin is None or fecha > fin) else fin
                anios.setdefault(p.anio, []).append((p.tipo, fecha, int(re.match(r"\d+", p.numero).group())))
            self._resumen = (anios, fin)
        return self._resumen

    def quiza_reciente(self, tipo: str, numero: str, anio: int, hoy: dt.date | None = None) -> str | None:
        """¿Puede ser una sentencia real que aún no está en ninguna fuente? El motivo, o None.

        Para una providencia que no está en el índice ni en la relatoría. None
        quiere decir que su ausencia significa algo: un año ya asentado, un
        número de un tramo que el índice ya recoge entero, o uno imposible para
        la fecha (la T-981/26 inventada sigue siendo «no existe»). La regla, en
        DISEÑO §1d; dos cosas se le añaden, las dos hacia el lado seguro:

        - Se mira también con los números de los tres tipos juntos. Desde 2023,
          al menos, la Corte numera las T, las C y las SU en una sola serie
          (ningún número se repite entre tipos en 2023-2026). Con los números de
          su tipo solo, una SU recién dictada se medía con el ritmo de las SU,
          una cada diez días, y la SU-261/26 de la revisión seguía dando «no
          existe» (la C-273/26 también). Basta con que una de las dos cuentas dé
          motivo.
        - El ritmo es el mayor entre el del año y el del año anterior: a
          principios de año el índice trae pocas (la vacancia judicial va hasta
          mediados de enero), y una sentencia real de febrero se tomaba por
          imposible.
        Con eso, en el índice real recortado cada dos semanas de 2024 a 2026 y
        con «hoy» 30, 60 y 120 días después, ninguna de las 19.482 sentencias
        reales que faltaban en el recorte se quedó sin motivo; con la regla
        literal, 5.372 (r4_literal.py, en el scratchpad de la cuarta ronda). El
        precio: con el índice de hoy (hasta el 31-08-2026) y «hoy» 20-10-2026,
        un número inventado de 2026 entre el 202 y el 534 sale «no encontrada»
        en vez de «no existe» (el mayor real es el 280).
        """
        anios, fin = self._por_anio()
        if fin is None:
            # Un índice sin fechas no dice hasta dónde llega: nada en él está asentado
            return ("no figura en el índice oficial, que no dice hasta qué fecha llega, ni en la "
                    "relatoría; puede ser una sentencia reciente que aún no se ha publicado. "
                    "Compruébela antes de radicar")
        hoy = hoy or _hoy()
        anio4 = anio_completo(anio)
        if anio4 < fin.year - 1 or anio4 > hoy.year:
            return None
        m = re.match(r"0*(\d+)", str(numero).strip())
        if not m:
            return None
        n, tipo = int(m.group(1)), tipo.upper()
        asentado = fin - dt.timedelta(days=_DIAS_SIN_ASENTAR)
        # Para un año ya cerrado, los días cuentan hasta su 31 de diciembre: nada
        # dictado después lleva su número (en las 29.474 filas, el año de la fecha
        # es siempre el del número). Contados hasta hoy, una T-700/25 inventada
        # salía «no encontrada» en octubre de 2026, y la ventana de 2025 seguía
        # creciendo cada día (revisión 4).
        hasta_dia = min(hoy, dt.date(anio4, 12, 31))
        dias = max(0, (hasta_dia - max(dt.date(anio4, 1, 1), asentado)).days)
        dias_del_anio = (min(fin, dt.date(anio4, 12, 31)) - dt.date(anio4, 1, 1)).days + 1
        for cuenta in (lambda t: t == tipo, lambda t: True):
            del_anio = [(f, x) for t, f, x in anios.get(anio4, ()) if cuenta(t)]
            umbral = max((x for f, x in del_anio if f is not None and f <= asentado), default=0)
            if n <= umbral:
                continue
            ritmo = len(del_anio) / dias_del_anio if del_anio and anio4 <= fin.year else 0.0
            anterior = sum(1 for t, _, _ in anios.get(anio4 - 1, ()) if cuenta(t))
            ritmo = max(ritmo, anterior / 365, _RITMO_MINIMO)
            if n <= umbral + _MARGEN_RITMO * ritmo * dias + _MARGEN_NUMEROS:
                return (f"no figura en el índice oficial, que llega hasta el {fin.isoformat()}, ni en "
                        "la relatoría; puede ser una sentencia reciente que aún no se ha publicado. "
                        "Compruébela antes de radicar")
        return None


# --------------------------------------------------------------- relatoría
_PALABRAS_PAGINA_VACIA = 400    # la página de "no existe" tiene unas 105
# Una ausencia se recuerda solo un día: una sentencia recién dictada puede
# tardar en subirse a la relatoría, y no se la puede dar por inexistente
# para siempre porque no estuviera el martes.
_CADUCIDAD_AUSENCIA = 24 * 3600

# Pausa, reintentos y cortacircuitos: ver red.py, que nació de que este
# servidor dejara de respondernos.
cliente = red.Cliente("La relatoría de la Corte Constitucional", PAUSA_RELATORIA)

# Nombre histórico: verificacion.py y las pruebas lo usan.
RelatoriaNoDisponible = red.ServicioNoDisponible


def relatoria_disponible() -> bool:
    return cliente.disponible()


def _descargar_pagina(url: str) -> bytes:
    return cliente.pedir(url)


# Canario. Una ausencia en la relatoría solo vale si la relatoría sigue
# sirviendo sentencias. En septiembre de 2026 el sitio se rehízo y ahora
# responde a cualquier dirección desconocida con la carcasa vacía de la
# aplicación (200, sin texto). Si un día cambia el esquema de direcciones,
# TODAS las consultas darían esa carcasa, y cada sentencia que aún no esté en
# el índice saldría como «no existe». Así que antes de dar por buena una
# ausencia se comprueba que una sentencia que seguro existe se sigue pudiendo
# leer, y la comprobación vale dos minutos. Antes valía una hora: un desafío de
# cortafuegos servido dentro de esa hora se tomaba por la respuesta de la
# Corte, se guardaba como ausencia y el espejo de la demo lo repartía a todos
# sus contenedores durante un día (reproducido). Cuesta una petición más cada
# dos minutos, y solo cuando hay una ausencia que creer.
CANARIO = ("T", "406", 1992)
_CANARIO_VALIDEZ = 120
_canario_ok_hasta = 0.0


def relatoria_fiable() -> bool:
    global _canario_ok_hasta
    if time.time() < _canario_ok_hasta:
        return True
    crudo = _descargar_pagina(url_relatoria(*CANARIO))
    if _parece_la_sentencia(documentos._de_html(crudo) if crudo else "", CANARIO[0], CANARIO[1]):
        cliente.respuesta_valida()
        _canario_ok_hasta = time.time() + _CANARIO_VALIDEZ
        return True
    # Donde tiene que estar una sentencia que seguro existe llegó otra cosa,
    # con 200: para el cortacircuitos es una respuesta rara más. Si no contara,
    # un sitio que cambió de formato o que desafía a todo seguiría recibiendo
    # las peticiones de cada consulta.
    cliente.respuesta_irreconocible()
    return False


# La carcasa. Desde septiembre de 2026 la relatoría responde a cualquier
# dirección que no conoce con la aplicación del sitio vacía: 8.607 bytes, un
# <app-root> sin nada dentro y el título «CORTE CONSTITUCIONAL DE COLOMBIA» (el
# texto lo pondría después el JavaScript). Antes se la reconocía solo por no
# tener ninguna palabra, pero un desafío de cortafuegos hecho de <script>
# tampoco tiene ninguna, y dentro de los dos minutos del canario salía «no
# existe» (reproducido en la revisión; el espejo de la demo lo repartía a todos
# sus contenedores). Si el sitio cambia su carcasa, las inventadas salen «sin
# confirmar», que es lo seguro, y la sonda de la demo lo dice.
_RE_TITULO = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _es_carcasa(crudo: bytes) -> bool:
    """¿Es la página que la relatoría sirve para lo que no tiene? Por su firma."""
    if not crudo or b"<app-root" not in crudo.lower():
        return False
    titulo = _RE_TITULO.search(crudo)
    if not titulo or b"CORTE CONSTITUCIONAL" not in b" ".join(titulo.group(1).split()).upper():
        return False
    return not documentos._de_html(crudo).split()


def _es_ausencia(crudo: bytes) -> bool:
    """La respuesta de «no la tengo»: solo la carcasa, reconocida por su firma.

    Un 404 no lo es. La relatoría no lo da nunca para lo que no tiene (responde
    la carcasa: comprobado en cinco direcciones), así que un 404 viene de algo
    que está entre medias —un cortafuegos, una caché— y no dice nada de la
    Corte. Antes valía, y como pedir() tira el cuerpo de los 404, una página de
    bloqueo servida con 404 daba «no existe» aunque el canario se hubiera
    pedido en la misma consulta: un cortafuegos selectivo deja pasar la
    T-406/92 (reproducido en la revisión). Tampoco un 200 vacío, que puede ser
    cualquier cosa.
    """
    return _es_carcasa(crudo)


def _motivo_raro(crudo: bytes, por_defecto: str) -> str:
    """Por qué no se cree una respuesta: el 404 se dice como lo que es."""
    if not crudo and cliente.ultimo_codigo == 404:
        return ("la relatoría respondió 404, y para lo que no tiene responde con su página vacía, "
                "nunca con un 404: algo entre medias puede estar bloqueando esta conexión")
    return por_defecto


# Lo que puede ser una respuesta de la relatoría (ver _clasificar)
_SENTENCIA, _NOTA, _AUSENCIA, _RARA = "sentencia", "nota", "ausencia", "rara"


def _clasificar(crudo: bytes, tipo: str, numero: str) -> tuple[str, str, str | None]:
    """(clase, texto, nota) de una página de la relatoría.

    - _NOTA: la página de la sentencia sustituida por una NOTA DE RELATORÍA de
      nulidad. Puede ser corta: no se le exige parecer la sentencia (y si lo
      parece, es una nulidad parcial: el texto sigue en su sitio).
    - _SENTENCIA: la sentencia, sin nota.
    - _AUSENCIA: la carcasa.
    - _RARA: cualquier otra cosa, con texto o sin él: un desafío de
      cortafuegos, una redirección, un 200 vacío, un 404. Nunca se lee como
      ausencia.
    """
    texto = documentos._de_html(crudo) if crudo else ""
    nota = _nota_de_nulidad_en(texto)
    if nota:
        return _NOTA, texto, nota
    if _parece_la_sentencia(texto, tipo, numero):
        return _SENTENCIA, texto, None
    return (_AUSENCIA if _es_ausencia(crudo) else _RARA), texto, None


# Sentencias anuladas. La relatoría no publica una lista: lo que hace es
# sustituir la página de la sentencia por una NOTA DE RELATORÍA y mover el
# texto a /relatoria/Anuladas/. Así está la SU-163/23 (anulada por el Auto 823
# de 2024), que el índice oficial sigue listando como una sentencia más. Citar
# una sentencia anulada como vigente es justo lo que un verificador tiene que
# ver. Basta leer el principio de la página: se pide con Range (salvo las
# páginas de Word con una cabecera enorme; ver nota_de_nulidad).
_RE_NOTA = re.compile(r"NOTA DE RELATOR[IÍ]A", re.IGNORECASE)
# Range: bytes=0-30000. Basta para casi todas: la nota va al principio
_BYTES_CABECERA = 30_000
# Segunda ventana (Range: bytes=0-299999), para las páginas de Word con más de
# 30 KB de <style> antes del texto. Medido: la C-355/06 (</head> en el byte
# 152.575), la SU-214/16 y la SU-1073/12 (con su nota) se reconocen con 300 KB.
# Antes, tras la primera ventana se pedía la página entera: 3,4 MB la C-355/06,
# aunque la cita no tuviera pasajes.
_BYTES_VENTANA = 300_000
# Lo que caduca es el «no anulada»: una sentencia puede anularse un año después
# (la SU-163/23 lo fue en 2024). Una nota de nulidad no caduca.
_CADUCIDAD_NOTA = 30 * 24 * 3600
_RE_PARRAFO = re.compile(r"\n\s*\n")


def _nota_de_nulidad_en(texto: str) -> str | None:
    """La NOTA DE RELATORÍA si habla de nulidad; None si no hay nota o es de otra cosa.

    «nulidad» se busca solo en el párrafo de la nota, no detrás. Antes se miraban
    los 900 caracteres que la siguen, y ahí llegan los descriptores de la
    sentencia, que pueden hablar de cualquier nulidad: la nota real de
    anonimización de la T-760/08 puesta delante de la C-355/06 («…-No origina la
    nulidad…») daba «La sentencia fue ANULADA», y una anulación guardada no
    caduca. La relatoría añade notas así años después (la T-760/08 en 2015, la
    SU-214/23 en 2025). Si el párrafo tiene menos de 4 palabras, la nota va como
    encabezado suelto y se toma el siguiente.

    Se miran todas las notas del principio, no solo la primera: con una nota de
    anonimización delante de la de nulidad (la relatoría las añade años
    después), la nulidad parcial de la SU-1073/12 dejaba de verse y salía
    «verificada» (revisión 4).
    """
    for m in _RE_NOTA.finditer(texto[:6000]):
        nota = _parrafo_de_nota(texto[m.end():m.end() + 6000])
        if nota and re.search(r"\bnulidad\b", nota, re.IGNORECASE):
            return nota
    return None


def _parrafo_de_nota(resto: str) -> str | None:
    """El párrafo que sigue a un «NOTA DE RELATORÍA», en una línea."""
    # «NOTA DE RELATORIA: Mediante…» (la SU-1073/12) dejaba los dos puntos delante
    resto = resto.lstrip(":;.,-–—* \t")
    parrafos = [p for p in _RE_PARRAFO.split(resto) if p.strip()]
    if not parrafos:
        return None
    parrafo = parrafos[1] if len(parrafos[0].split()) < 4 and len(parrafos) > 1 else parrafos[0]
    # Sin líneas en blanco el párrafo sería el resto de la página: se acota como antes
    nota = re.split(r"REP[ÚU]BLICA DE COLOMBIA|Si desea conocer", parrafo[:900], maxsplit=1)[0]
    return " ".join(nota.split()).lstrip(":;.,-–—* ")


def nulidad_parcial(nota: str) -> bool:
    """¿La nota anula solo una parte de la sentencia?

    La SU-1073/12 perdió dos numerales de su parte resolutiva y sigue en lo
    demás («se declara la nulidad parcial de la presente providencia»): decir
    «ANULADA» a secas exagera. Se mira solo la frase de la nota que habla de la
    nulidad, porque detrás vienen los descriptores de la sentencia, que pueden
    hablar de cualquier nulidad. Ante la duda, no es parcial: exagerar una
    anulación es menos grave que quitarle importancia.
    """
    frase = re.search(r"[^.]*\bnulidad\b[^.]*", nota, re.IGNORECASE)
    return bool(frase and re.search(r"\bparcial(?:mente)?\b", frase.group(0), re.IGNORECASE))


def ruta_nota(tipo: str, numero: str, anio: int) -> Path:
    """Dónde se guarda la respuesta de `nota_de_nulidad`.

    Pública porque la demo replica la caché en S3 y tiene que saber de
    antemano qué archivos va a leer cada consulta.
    """
    carpeta = _dir_cache() / "relatoria" / "notas"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"{tipo.upper()}-{normalizar_numero(numero)}-{anio_completo(anio)}.json"


def _guardar_nota(ruta: Path, nota: str | None) -> None:
    """Guarda la respuesta de la nulidad. Un «no anulada» nunca pisa una nota.

    Ahora la escriben dos: nota_de_nulidad y, de paso, texto_relatoria. Una
    anulación no se deshace; si alguna vez se leyó una nota, borrarla con un
    «no anulada» haría pasar por vigente una sentencia anulada.
    """
    if nota is None and ruta.exists():
        try:
            if json.loads(ruta.read_text(encoding="utf-8")).get("nota"):
                return
        except (ValueError, OSError, AttributeError):
            pass                        # ilegible: se sustituye
    ruta.write_text(json.dumps({"nota": nota}, ensure_ascii=False), encoding="utf-8")


def nota_de_nulidad(tipo: str, numero: str, anio: int) -> str | None:
    """La nota de la relatoría si la sentencia fue anulada; None si no lo fue.

    Lanza RelatoriaNoDisponible si no se pudo saber: si no se pudo preguntar,
    o si la relatoría respondió algo que no es ni la nota ni la sentencia. En
    esos casos no se guarda nada.
    """
    ruta = ruta_nota(tipo, numero, anio)
    if ruta.exists():
        guardada = json.loads(ruta.read_text(encoding="utf-8"))["nota"]
        # Una anulación no se deshace. Antes la nota caducaba a los 30 días
        # como el «no anulada», y bastaba con que la relatoría no respondiera
        # al renovarla para que la sentencia anulada pasara por vigente.
        if guardada or time.time() - ruta.stat().st_mtime < _CADUCIDAD_NOTA:
            return guardada

    rara: str | None = None
    for url in urls_relatoria(tipo, numero, anio):
        crudo = cliente.pedir(url, cabeceras={"Range": f"bytes=0-{_BYTES_CABECERA}"})
        clase, _, nota = _clasificar(crudo, tipo, numero)
        # Llenó la ventana entera y no se reconoce nada: no es un bloqueo, es
        # una página de Word con más de 30 KB de <style> antes del texto (la
        # C-355/06, la SU-214/16; la SU-1073/12, con su nota de nulidad detrás).
        # Con 30 KB les quedaba una palabra: salían «con observaciones» por un
        # bloqueo que no existía, no se guardaban nunca y la nota no se veía.
        # Se mira una ventana de 300 KB, y solo si también se llena sin
        # reconocerse, la página entera. Si el servidor no hubiera respetado
        # Range, ya estaría entera: no llena la ventana justa y no se repite.
        if clase == _RARA and len(crudo) == _BYTES_CABECERA + 1:
            crudo = cliente.pedir(url, cabeceras={"Range": f"bytes=0-{_BYTES_VENTANA - 1}"})
            clase, _, nota = _clasificar(crudo, tipo, numero)
            if clase == _RARA and len(crudo) == _BYTES_VENTANA:
                crudo = _descargar_pagina(url)
                clase, _, nota = _clasificar(crudo, tipo, numero)
        # Solo vale una respuesta que se reconoce: la nota o la sentencia.
        # Antes bastaba con que hubiera texto, y la página de desafío de un
        # cortafuegos tiene texto: se habría guardado como «no anulada»
        # durante 30 días, escondiendo una nulidad todo ese tiempo.
        if clase in (_NOTA, _SENTENCIA):
            cliente.respuesta_valida()
            _guardar_nota(ruta, nota)
            return nota
        if clase == _RARA:
            cliente.respuesta_irreconocible()
            rara = rara or _motivo_raro(crudo, (
                "la relatoría respondió una página que no es ni la nota de nulidad ni la "
                "sentencia: puede estar bloqueando esta conexión"))
        # La carcasa: se prueba la otra dirección. No cuenta para el
        # cortacircuitos: que la relatoría no tenga la página no es culpa suya.
    # No se sabe, y se dice. Antes se devolvía None, lo mismo que «no
    # anulada», y quien llama no podía distinguirlo: con un desafío, la
    # SU-163/23 (anulada) salía verificada sin ningún aviso.
    if rara:
        raise RelatoriaNoDisponible(rara)
    raise RelatoriaNoDisponible("la relatoría no tiene publicada su página")


def _url_anulada(url: str) -> str:
    return re.sub(r"/relatoria/\d{4}/", "/relatoria/Anuladas/", url)


def _ruta_texto(tipo: str, numero: str, anio: int) -> Path:
    nombre = f"{tipo.upper()}-{normalizar_numero(numero)}-{anio_completo(anio)}.txt.gz"
    carpeta = _dir_cache() / "relatoria"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / nombre


# Alias público, por lo mismo que `ruta_nota`: el espejo de la caché en S3
ruta_texto = _ruta_texto


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

    Una ausencia solo se cree si todas las direcciones devolvieron la carcasa
    (reconocida por su firma, ver _es_carcasa) y la relatoría acaba de servir
    el canario. Cualquier otra página que no sea la sentencia lanza: un desafío
    de cortafuegos, con texto o hecho solo de <script>, una redirección a otra
    cosa o un 404 no son la respuesta de la Corte.

    De paso deja escrita la respuesta de la nulidad (ruta_nota): ya tiene la
    página entera delante, y así nota_de_nulidad no la vuelve a pedir.
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

    texto, rara = "", None
    for url in urls_relatoria(tipo, numero, anio):
        crudo = _descargar_pagina(url)
        clase, candidato, nota = _clasificar(crudo, tipo, numero)
        if clase == _NOTA:
            cliente.respuesta_valida()
            _guardar_nota(ruta_nota(tipo, numero, anio), nota)
            # Anulada: el texto sigue publicado, en otra carpeta. La nota ya
            # dice que la sentencia existe: si allí no está, no es una ausencia
            clase_a, anulada, _ = _clasificar(_descargar_pagina(_url_anulada(url)), tipo, numero)
            if _parece_la_sentencia(anulada, tipo, numero):
                texto = anulada
                break
            if clase_a == _RARA:
                # Un desafío en /Anuladas/ no dice si allí está la sentencia:
                # no se puede dar por buena la página original en su lugar
                cliente.respuesta_irreconocible()
                rara = ("la relatoría tiene la nota de nulidad, pero donde va el texto de la "
                        "sentencia anulada respondió otra página: puede estar bloqueando esta conexión")
                continue
            if _parece_la_sentencia(candidato, tipo, numero):
                # Nulidad parcial (la SU-1073/12): /Anuladas/ no la tiene porque
                # el texto sigue en su sitio, detrás de la nota. Antes se
                # desechaba ese texto, se lanzaba y no se guardaba nada: los
                # pasajes no se comprobaban nunca y cada consulta volvía a bajar
                # unas 80.000 palabras.
                texto = candidato
                break
            rara = "la relatoría tiene la nota de nulidad, pero no el texto de la sentencia anulada"
            continue
        if clase == _SENTENCIA:
            cliente.respuesta_valida()
            _guardar_nota(ruta_nota(tipo, numero, anio), None)
            texto = candidato
            break
        if clase == _RARA:
            # Antes esto se leía como ausencia siempre que el canario siguiera
            # vigente, y un desafío de cortafuegos acababa en «no existe»,
            # guardado un día y repartido por el espejo de la demo. Un 404
            # también es esto (ver _es_ausencia).
            cliente.respuesta_irreconocible()
            if rara is None:
                rara = _motivo_raro(crudo, "la relatoría respondió una página que no es la sentencia "
                                           "ni la de «no existe»: puede estar bloqueando esta conexión")
    if not texto and rara:
        raise RelatoriaNoDisponible(rara)
    if not texto:
        if not relatoria_fiable():
            raise RelatoriaNoDisponible(
                "la relatoría no devuelve ni una sentencia que seguro existe "
                f"({CANARIO[0]}-{CANARIO[1]}/{CANARIO[2] % 100:02d}): puede haber cambiado de formato")
        cliente.respuesta_valida()      # la carcasa, con el canario vivo
    # Se cachea también la ausencia, como cadena vacía, para no repetir
    with gzip.open(ruta, "wt", encoding="utf-8") as f:
        f.write(texto)
    return texto or None
