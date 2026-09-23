"""¿Aparece de verdad este pasaje en la sentencia?

Es el fallo más sutil de un escrito hecho con IA, y el más peligroso: la
sentencia existe, el número es correcto, y lo que se le atribuye entre
comillas no lo dijo nunca. Comprobar solo la existencia no lo detecta.

Cómo se compara
---------------
Los abogados no copian letra por letra. Recortan con «(...)» o «[…]», cambian
una tilde, añaden una coma. Por eso:

1. Se normalizan los dos textos: sin tildes, en minúsculas, solo letras y
   cifras. Así «Constitución,» y «constitucion» son lo mismo.
2. El pasaje se parte por las elisiones y cada trozo se busca por separado.
   Todos tienen que aparecer: si uno no está, el pasaje no es fiel.
3. Primero se busca tal cual. Si no está, se localizan las zonas de la
   sentencia que comparten trigramas de palabras con el trozo, y en cada una
   se alinea palabra a palabra para contar exactamente qué cambia. Buscar en
   fuerza bruta sobre una sentencia de constitucionalidad —hay alguna de
   285.000 palabras— no es viable.

Por qué palabra a palabra y no por parecido de caracteres: la primera versión
usaba rapidfuzz y daba por buena, con un 96,7 %, una cita en la que «también
es» se había cambiado por «no es». En derecho eso no es una errata, es decir
lo contrario. Ahora «coincide» exige todas las palabras, y cualquier
diferencia se enseña tal cual: lo que pone el escrito frente a lo que dice la
sentencia.

Se devuelve también el pasaje real más parecido, en su forma original: en el
informe es lo más útil, porque le enseña al abogado qué dice de verdad la
sentencia en ese sitio.
"""
from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from dataclasses import dataclass, field

# «Coincide» exige todas las palabras. Por debajo de este porcentaje de
# palabras iguales ya no es la misma frase mal copiada: es otra frase.
UMBRAL_DIFIERE = 70.0
MIN_PALABRAS_FRAGMENTO = 4
_TRIGRAMA = 3
_CANDIDATAS = 8
_MARGEN = 8                 # palabras de holgura al alinear

# Elisiones —«(...)», «[…]»— e inserciones editoriales entre corchetes,
# «[la Corte]», que la convención permite y no son del texto citado. Las dos
# parten el pasaje en trozos que se buscan por separado.
_RE_ELISION = re.compile(
    r"\(\s*(?:\.\s*){3}\)|\[\s*(?:\.\s*){3}\]|\(\s*…\s*\)|\[\s*…\s*\]|…|\.{3,}"
    r"|\[[^\]\n]{1,60}\]"
)


# ------------------------------------------------------------- normalización
@dataclass
class TextoNormalizado:
    """Texto normalizado con el mapa de vuelta a posiciones del original."""
    original: str
    normal: str
    mapa: list[int]                         # índice normal -> índice original
    palabras: list[str] = field(default_factory=list)
    inicios: list[int] = field(default_factory=list)   # de cada palabra, en `normal`
    _indice: dict[tuple, list[int]] | None = None

    def indice_trigramas(self) -> dict[tuple, list[int]]:
        if self._indice is None:
            idx: dict[tuple, list[int]] = {}
            p = self.palabras
            for i in range(len(p) - _TRIGRAMA + 1):
                idx.setdefault(tuple(p[i:i + _TRIGRAMA]), []).append(i)
            self._indice = idx
        return self._indice

    def fragmento_original(self, palabra_ini: int, palabra_fin: int) -> str:
        """Texto original que corresponde a un rango de palabras."""
        palabra_fin = min(palabra_fin, len(self.palabras))
        if not self.palabras or palabra_fin <= palabra_ini:
            return ""
        a = self.inicios[palabra_ini]
        ultima = palabra_fin - 1
        b = self.inicios[ultima] + len(self.palabras[ultima]) - 1
        return self.original[self.mapa[a]:self.mapa[b] + 1]


def normalizar(texto: str) -> TextoNormalizado:
    salida: list[str] = []
    mapa: list[int] = []
    for i, ch in enumerate(texto):
        base = unicodedata.normalize("NFKD", ch)
        for b in base:
            if unicodedata.combining(b):
                continue
            b = b.lower()
            if b.isalnum():
                salida.append(b)
                mapa.append(i)
            elif salida and salida[-1] != " ":
                salida.append(" ")
                mapa.append(i)
    if salida and salida[-1] == " ":
        salida.pop()
        mapa.pop()
    normal = "".join(salida)
    palabras, inicios, pos = [], [], 0
    for w in normal.split(" "):
        if w:
            palabras.append(w)
            inicios.append(pos)
        pos += len(w) + 1
    return TextoNormalizado(texto, normal, mapa, palabras, inicios)


def fragmentos(pasaje: str) -> list[str]:
    """Trozos del pasaje separados por elisiones, descartando los muy cortos."""
    trozos = [t.strip(" ,;:.") for t in _RE_ELISION.split(pasaje)]
    return [t for t in trozos if len(t.split()) >= MIN_PALABRAS_FRAGMENTO]


# ------------------------------------------------------------------ búsqueda
@dataclass
class ResultadoFragmento:
    fragmento: str
    puntuacion: float               # % de palabras que coinciden, 0-100
    mas_parecido: str               # pasaje real de la sentencia, original
    diferencias: list[tuple[str, str]] = field(default_factory=list)
    # (lo que dice el escrito, lo que dice la sentencia), palabra a palabra

    @property
    def estado(self) -> str:
        if self.puntuacion >= 100.0:
            return "coincide"
        if self.puntuacion >= UMBRAL_DIFIERE:
            return "difiere"
        return "no_aparece"


@dataclass
class ResultadoPasaje:
    pasaje: str
    fragmentos: list[ResultadoFragmento]

    @property
    def puntuacion(self) -> float:
        return min((f.puntuacion for f in self.fragmentos), default=0.0)

    @property
    def estado(self) -> str:
        if not self.fragmentos:
            return "demasiado_corto"
        orden = {"no_aparece": 0, "difiere": 1, "coincide": 2}
        return min((f.estado for f in self.fragmentos), key=orden.__getitem__)

    @property
    def diferencias(self) -> list[tuple[str, str]]:
        return [d for f in self.fragmentos for d in f.diferencias]


def _cotejar(fn: list[str], sent: list[str], inicio: int) -> tuple[int, int, int, list]:
    """Alinea el fragmento con la sentencia palabra a palabra cerca de `inicio`.

    Devuelve (palabras cambiadas, ini, fin, operaciones). Se trabaja con
    palabras y no con caracteres a propósito: en una cita jurídica una sola
    palabra cambia el sentido. «también es» -> «no es» da un 96,7 % de
    parecido por caracteres y es exactamente lo contrario de lo que se dijo.

    Las operaciones son (i1, i2, j1, j2): palabras del fragmento y de la
    sentencia, estas en posición absoluta, que no coinciden.
    """
    n = len(fn)
    a = max(0, inicio - _MARGEN)
    b = min(len(sent), inicio + n + _MARGEN)
    ventana = sent[a:b]
    sm = difflib.SequenceMatcher(None, fn, ventana, autojunk=False)
    bloques = [bl for bl in sm.get_matching_blocks() if bl.size]
    if not bloques:
        return n, a, a, []
    ini, fin = bloques[0].b, bloques[-1].b + bloques[-1].size
    tramo = ventana[ini:fin]
    cambiadas, ops = 0, []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, fn, tramo, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        cambiadas += max(i2 - i1, j2 - j1)
        ops.append((i1, i2, a + ini + j1, a + ini + j2))
    return cambiadas, a + ini, a + fin, ops


def _buscar_fragmento(frag: str, sentencia: TextoNormalizado) -> ResultadoFragmento:
    fn = normalizar(frag)
    n = len(fn.palabras)
    if n == 0:
        return ResultadoFragmento(frag, 0.0, "")

    # 1. Tal cual, tras normalizar: coincidencia literal
    pos = sentencia.normal.find(fn.normal)
    if pos != -1:
        w = bisect.bisect_right(sentencia.inicios, pos) - 1
        return ResultadoFragmento(frag, 100.0, sentencia.fragmento_original(w, w + n))

    # 2. Zonas candidatas por trigramas compartidos, alineadas por posición
    idx = sentencia.indice_trigramas()
    votos: dict[int, int] = {}
    for j in range(n - _TRIGRAMA + 1):
        for i in idx.get(tuple(fn.palabras[j:j + _TRIGRAMA]), ()):
            inicio = i - j
            votos[inicio] = votos.get(inicio, 0) + 1

    # 3. Sin un solo trigrama en común se prueba con pares, más laxo. Un
    #    pasaje reescrito casi entero puede conservar pares sueltos.
    if not votos and n >= 2:
        pares = {(fn.palabras[k], fn.palabras[k + 1]): k for k in range(n - 1)}
        p = sentencia.palabras
        for i in range(len(p) - 1):
            k = pares.get((p[i], p[i + 1]))
            if k is not None:
                votos[i - k] = votos.get(i - k, 0) + 1
    if not votos:
        return ResultadoFragmento(frag, 0.0, "")

    # 4. De las zonas más votadas, la que menos palabras cambia
    mejor = None
    for inicio, _ in sorted(votos.items(), key=lambda kv: -kv[1])[:_CANDIDATAS]:
        r = _cotejar(fn.palabras, sentencia.palabras, inicio)
        if mejor is None or r[0] < mejor[0]:
            mejor = r
    cambiadas, a, b, ops = mejor
    puntuacion = max(0.0, 100.0 * (1 - cambiadas / n))
    # Las diferencias se enseñan con las palabras originales, tildes incluidas
    difs = [(fn.fragmento_original(i1, i2), sentencia.fragmento_original(j1, j2))
            for i1, i2, j1, j2 in ops]
    return ResultadoFragmento(frag, round(puntuacion, 1),
                              sentencia.fragmento_original(a, b), difs)


def buscar_pasaje(pasaje: str, sentencia: TextoNormalizado) -> ResultadoPasaje:
    return ResultadoPasaje(pasaje, [_buscar_fragmento(f, sentencia) for f in fragmentos(pasaje)])
