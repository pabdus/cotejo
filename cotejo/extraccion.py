"""Extracción de citas jurisprudenciales de un escrito en español.

Qué reconoce
------------
Providencias de la Corte Constitucional en todas las formas habituales:

    Sentencia T-406 de 1992          T-406/92          SU-111 de 1997
    sentencia No. C-355 de 2006      T-1025 de 2002    T-042A de 2025
    sentencias T-123, T-456 y T-789 de 2010     <- el año se comparte

Con su magistrado ponente si viene al lado ("M.P. Ciro Angarita Barón") y con
las citas textuales entre comillas que se le atribuyen.

Y detecta, sin poder verificarlas todavía, las citas de otras fuentes: autos de
la Corte Constitucional, providencias de la Corte Suprema (SC5686-2018,
AC739-2026...), radicados de 23 dígitos y menciones a sentencias por fecha del
Consejo de Estado o la Corte Suprema. Se detectan para que el informe diga
claramente qué NO se ha comprobado. Un verificador que calla lo que no mira es
peor que ninguno: el abogado cree que está todo revisado.

Lo que NO es una cita
---------------------
El número de expediente de una tutela tiene la misma letra: "Expediente
T-7.123.456". Es el caso más frecuente de falso positivo en un escrito de
tutela, y se descarta: una sentencia nunca pasa de cuatro cifras ni lleva
puntos de millar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------- modelo
CORTE_CONSTITUCIONAL = "Corte Constitucional"
CORTE_SUPREMA = "Corte Suprema de Justicia"
CONSEJO_ESTADO = "Consejo de Estado"
SIN_IDENTIFICAR = "Sin identificar"


@dataclass
class CitaTextual:
    """Un pasaje entre comillas que el escrito atribuye a una providencia."""
    texto: str
    inicio: int
    fin: int


@dataclass
class Cita:
    tribunal: str
    clase: str                      # sentencia | auto | codigo | radicado | mencion
    texto: str                      # como aparece en el escrito
    inicio: int
    fin: int
    tipo: str | None = None         # T, C, SU
    numero: str | None = None
    anio: int | None = None
    anio_heredado: bool = False     # "T-123, T-456 y T-789 de 2010"
    ponente: str | None = None
    citas_textuales: list[CitaTextual] = field(default_factory=list)
    motivo_no_verificable: str | None = None

    @property
    def verificable(self) -> bool:
        return self.motivo_no_verificable is None

    @property
    def clave(self) -> str:
        """Identidad de la providencia, para agrupar citas repetidas."""
        if self.tipo and self.numero and self.anio:
            return f"{self.tipo}-{self.numero.upper()}/{self.anio}"
        return re.sub(r"\s+", " ", self.texto.strip())


# ------------------------------------------------------------ Corte Constitucional
_ANIO = r"(?P<anio>(?:19|20)\d{2}|\d{2})"
# "de" en cualquier caja: los escritos judiciales vienen a menudo en mayúsculas
_SEP_ANIO = r"(?:\s*/\s*|\s+(?i:del?)\s+(?:(?i:año)\s+)?)"

# Tipo, guion, número de 1 a 4 cifras con letra opcional. La mirada adelante
# descarta "T-7.123.456" (expediente con puntos de millar) y "T-7123456".
_RE_PROVIDENCIA = re.compile(
    r"(?<![\w-])(?P<tipo>SU|T|C)\s?[-–—]\s?(?P<num>\d{1,4})(?P<letra>[A-Za-z])?"
    r"(?![\d\w])(?!\.\d)"
    r"(?:" + _SEP_ANIO + _ANIO + r"(?!\d))?",
)

# Separadores que mantienen una enumeración: "T-123, T-456 y T-789 de 2010"
_RE_ENUMERACION = re.compile(r"^\s*(?:,|;|,?\s+[ye])\s*(?:la\s+|las\s+)?$", re.IGNORECASE)

# Una cita sin año solo se admite si viene anunciada como providencia
_RE_ANUNCIO = re.compile(r"(?:sentencias?|fallos?|providencias?)\s+(?:No\.?\s*)?$", re.IGNORECASE)
_RE_EXPEDIENTE_ANTES = re.compile(r"(?:expediente|exp\.|radicado|rad\.)\s*(?:No\.?\s*)?$", re.IGNORECASE)

_RE_AUTO = re.compile(
    r"\bAuto\s+(?:No\.?\s*)?(?:A\s*[-–]\s*)?(?P<num>\d{1,4})(?P<letra>[A-Za-z])?"
    + _SEP_ANIO + _ANIO + r"(?!\d)",
    re.IGNORECASE,
)

# -------------------------------------------------------------- Corte Suprema
# SC5686-2018, AC739-2026, STC1234-2021, SL1234-2019... Tipo de providencia,
# número y año de cuatro cifras unidos por guion.
_RE_CODIGO_CSJ = re.compile(
    r"\b(?P<codigo>SC|SP|SL|STC|STP|STL|STH|AC|AP|AL|ATC|ATP|ATL|AHP)\s?"
    r"(?P<num>\d{1,5})\s?[-–]\s?(?P<anio>(?:19|20)\d{2})\b"
)

# Radicado de 23 dígitos: 11001-02-03-000-2019-01234-00
_RE_RADICADO = re.compile(
    r"\b\d{5}\s?-\s?\d{2}\s?-\s?\d{2}\s?-\s?\d{3}\s?-\s?\d{4}\s?-\s?\d{5}\s?-\s?\d{2}\b"
)

_MESES = r"(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
_RE_MENCION_FECHA = re.compile(
    r"(?P<tribunal>Corte Suprema de Justicia|Consejo de Estado|CSJ)"
    r"[^.;\n]{0,160}?\bsentencias?\s+(?:del?\s+)?\d{1,2}\s+de\s+" + _MESES + r"\s+de\s+(?:19|20)\d{2}",
    re.IGNORECASE,
)

# ----------------------------------------------------------------- ponente
_NOMBRE = (
    r"[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+"
    r"(?:\s+(?:de\s+la\s+|de\s+los\s+|de\s+las\s+|del\s+|de\s+|y\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+){1,5}"
)
_RE_PONENTE = re.compile(
    r"(?:M\.\s?P\.?|MP\.?|[Mm]agistrad[oa]\s+[Pp]onente|[Mm]agistrad[oa]\s+[Ss]ustanciador[a]?)"
    r"\s*[:.]?\s*(?:(?:el|la)\s+)?(?:(?:Dr|Dra|Doctor|Doctora)\.?\s+)?(?P<nombre>" + _NOMBRE + r")"
)
_DISTANCIA_PONENTE = 160

# --------------------------------------------------------------- comillas
_RE_COMILLAS = re.compile(
    r"“(?P<a>[^”]{20,}?)”|«(?P<b>[^»]{20,}?)»|\"(?P<c>[^\"]{20,}?)\"",
    re.S,
)
MIN_PALABRAS_CITA_TEXTUAL = 8
_ANTES_MAX = 700        # la cita suele anunciar el pasaje: "la Corte señaló: «…»"
_DESPUES_MAX = 150      # o cerrarlo: "«…» (Sentencia T-406 de 1992)"
_RE_SALTO_PARRAFO = re.compile(r"\n\s*\n")


# ================================================================== extracción
def _anio_valido(anio: int) -> bool:
    return 1900 <= anio <= 2100


def _anio4(txt: str) -> int:
    n = int(txt)
    if n >= 100:
        return n
    return 1900 + n if n >= 92 else 2000 + n


def _providencias_cc(texto: str) -> list[Cita]:
    """Providencias T, C y SU, incluido el año compartido en enumeraciones."""
    brutas: list[tuple[re.Match, int | None]] = []
    for m in _RE_PROVIDENCIA.finditer(texto):
        anio = _anio4(m.group("anio")) if m.group("anio") else None
        brutas.append((m, anio))

    # Año compartido: de atrás hacia delante, cada cita sin año hereda el de la
    # siguiente si están en la misma enumeración.
    anios: list[int | None] = [a for _, a in brutas]
    heredado = [False] * len(brutas)
    for i in range(len(brutas) - 2, -1, -1):
        if anios[i] is None and anios[i + 1] is not None:
            hueco = texto[brutas[i][0].end():brutas[i + 1][0].start()]
            if _RE_ENUMERACION.match(hueco):
                anios[i] = anios[i + 1]
                heredado[i] = True

    citas: list[Cita] = []
    for (m, _), anio, her in zip(brutas, anios, heredado):
        antes = texto[max(0, m.start() - 40):m.start()]
        if _RE_EXPEDIENTE_ANTES.search(antes):
            continue                    # "Expediente T-7123": no es una sentencia
        numero = m.group("num") + (m.group("letra") or "")
        motivo = None
        if anio is None:
            if not _RE_ANUNCIO.search(antes):
                continue                # "C-19" suelto: no hay nada que citar
            motivo = "la cita no indica el año de la providencia"
        elif not _anio_valido(anio):
            continue
        elif anio < 1992:
            motivo = ("anterior a 1992: no puede ser de la Corte Constitucional; "
                      "quizá de la antigua Sala Constitucional de la Corte Suprema")
        fin = m.end()
        citas.append(Cita(
            tribunal=CORTE_CONSTITUCIONAL, clase="sentencia",
            texto=texto[m.start():fin], inicio=m.start(), fin=fin,
            tipo=m.group("tipo").upper(), numero=numero.upper(), anio=anio,
            anio_heredado=her, motivo_no_verificable=motivo,
        ))
    return citas


def _otras(texto: str, ocupado: list[tuple[int, int]]) -> list[Cita]:
    """Citas de fuentes que aún no se verifican: se listan, no se ocultan."""
    def libre(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in ocupado)

    citas: list[Cita] = []
    for m in _RE_AUTO.finditer(texto):
        if libre(m.start(), m.end()):
            citas.append(Cita(
                CORTE_CONSTITUCIONAL, "auto", m.group(0), m.start(), m.end(),
                numero=(m.group("num") + (m.group("letra") or "")).upper(),
                anio=_anio4(m.group("anio")),
                motivo_no_verificable="los autos no están en el índice abierto de la Corte",
            ))
            ocupado.append((m.start(), m.end()))
    for m in _RE_CODIGO_CSJ.finditer(texto):
        if libre(m.start(), m.end()):
            citas.append(Cita(
                CORTE_SUPREMA, "codigo", m.group(0), m.start(), m.end(),
                tipo=m.group("codigo"), numero=m.group("num"), anio=int(m.group("anio")),
                motivo_no_verificable="la Corte Suprema no publica un índice abierto consultable",
            ))
            ocupado.append((m.start(), m.end()))
    for m in _RE_RADICADO.finditer(texto):
        if libre(m.start(), m.end()):
            citas.append(Cita(
                SIN_IDENTIFICAR, "radicado", m.group(0), m.start(), m.end(),
                motivo_no_verificable="un radicado identifica un proceso, no una providencia concreta",
            ))
            ocupado.append((m.start(), m.end()))
    for m in _RE_MENCION_FECHA.finditer(texto):
        if libre(m.start(), m.end()):
            trib = m.group("tribunal")
            trib = CORTE_SUPREMA if trib.upper() in ("CSJ", "CORTE SUPREMA DE JUSTICIA") else CONSEJO_ESTADO
            citas.append(Cita(
                trib, "mencion", m.group(0), m.start(), m.end(),
                motivo_no_verificable=f"verificación de {trib} todavía no disponible",
            ))
            ocupado.append((m.start(), m.end()))
    return citas


def _asignar_ponentes(texto: str, citas: list[Cita]) -> None:
    orden = sorted(citas, key=lambda c: c.inicio)
    for i, c in enumerate(orden):
        tope = orden[i + 1].inicio if i + 1 < len(orden) else len(texto)
        ventana = texto[c.fin:min(tope, c.fin + _DISTANCIA_PONENTE)]
        m = _RE_PONENTE.search(ventana)
        if m:
            c.ponente = m.group("nombre").strip()


def _asignar_citas_textuales(texto: str, citas: list[Cita]) -> None:
    """Cada pasaje entre comillas se atribuye a la cita más cercana.

    Se prefiere la que va delante ("la Corte señaló: «…»"), que es la forma más
    común, y puede estar en el párrafo anterior ("En la misma providencia la
    Corte precisó: «…»"). La que va DETRÁS solo cuenta si está en el mismo
    párrafo y pegada al pasaje, que es como se cierra una cita: "«…»
    (Sentencia T-406 de 1992)". Sin esa condición, un pasaje de la T-760/08
    se atribuía a la T-406/92 solo porque esta abría el párrafo siguiente.

    Se atribuye a la más cercana de CUALQUIER tribunal: un pasaje que sigue a
    una cita de la Corte Suprema no es de la Corte Constitucional aunque haya
    una antes.
    """
    for m in _RE_COMILLAS.finditer(texto):
        pasaje = next(g for g in m.groups() if g)
        if len(pasaje.split()) < MIN_PALABRAS_CITA_TEXTUAL:
            continue
        mejor, mejor_dist = None, None
        for c in citas:
            if c.fin <= m.start():
                dist = m.start() - c.fin
                if dist > _ANTES_MAX:
                    continue
                peso = dist
            elif c.inicio >= m.end():
                dist = c.inicio - m.end()
                if dist > _DESPUES_MAX or _RE_SALTO_PARRAFO.search(texto, m.end(), c.inicio):
                    continue
                peso = dist * 1.5
            else:
                continue            # la cita está dentro del pasaje
            if mejor_dist is None or peso < mejor_dist:
                mejor, mejor_dist = c, peso
        if mejor is not None:
            mejor.citas_textuales.append(CitaTextual(pasaje.strip(), m.start(), m.end()))


def extraer(texto: str) -> list[Cita]:
    """Todas las citas del escrito, en orden de aparición."""
    citas = _providencias_cc(texto)
    ocupado = [(c.inicio, c.fin) for c in citas]
    citas += _otras(texto, ocupado)
    citas.sort(key=lambda c: c.inicio)
    _asignar_ponentes(texto, citas)
    _asignar_citas_textuales(texto, citas)
    return citas
