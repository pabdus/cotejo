"""Verificación de las citas de un escrito contra la fuente oficial.

La regla que manda sobre todas las demás: un falso «no existe» ante un
abogado es grave. Le hace perder tiempo, le hace dudar de un escrito correcto
y, si lo reporta a la contraparte, le deja en mal lugar. Por eso:

- Una providencia solo se declara INEXISTENTE si falta en el índice oficial Y
  la relatoría confirma que no la tiene. Dos fuentes, no una.
- Si alguna de las dos no responde, el resultado es SIN CONFIRMAR, nunca
  «no existe».
- Lo que no se puede verificar se dice con todas las letras (NO VERIFICABLE),
  con el motivo. Callarlo haría creer que el escrito está revisado entero.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from rapidfuzz import fuzz

from . import comparacion, extraccion
from .extraccion import Cita
from .fuentes import corte_constitucional as cc
from .fuentes import corte_suprema as cs


class Estado(str, Enum):
    VERIFICADA = "verificada"
    CON_OBSERVACIONES = "con_observaciones"
    NO_EXISTE = "no_existe"
    # No está en una fuente que se sabe incompleta (el buscador de la Corte
    # Suprema). Pide comprobación, pero no afirma que no exista: medido, a esa
    # fuente le falta una de cada veinte providencias reales.
    NO_ENCONTRADA = "no_encontrada"
    SIN_CONFIRMAR = "sin_confirmar"
    NO_VERIFICABLE = "no_verificable"


ETIQUETA = {
    Estado.VERIFICADA: "Verificada",
    Estado.CON_OBSERVACIONES: "Existe, con observaciones",
    Estado.NO_EXISTE: "No existe",
    Estado.NO_ENCONTRADA: "No encontrada",
    Estado.SIN_CONFIRMAR: "Sin confirmar",
    Estado.NO_VERIFICABLE: "No verificable",
}


@dataclass
class Hallazgo:
    """Resultado para una providencia, agrupando todas las veces que se cita."""
    clave: str
    estado: Estado
    citas: list[Cita]
    providencia: cc.Providencia | cs.ProvidenciaCSJ | None = None
    fuente: str | None = None             # "indice" | "relatoria" | "corte_suprema"
    observaciones: list[str] = field(default_factory=list)
    pasajes: list[comparacion.ResultadoPasaje] = field(default_factory=list)
    posibles: list[cc.Providencia] = field(default_factory=list)
    motivo: str | None = None

    @property
    def primera(self) -> Cita:
        return self.citas[0]


@dataclass
class Informe:
    documento: str
    hallazgos: list[Hallazgo]
    palabras: int

    def contar(self, estado: Estado) -> int:
        return sum(1 for h in self.hallazgos if h.estado == estado)

    @property
    def problemas(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos
                if h.estado in (Estado.NO_EXISTE, Estado.NO_ENCONTRADA, Estado.CON_OBSERVACIONES)]


# ------------------------------------------------------------------ ponentes
_PARTICULAS = {"de", "la", "del", "los", "las", "y"}


def _tokens_nombre(nombre: str) -> list[str]:
    base = unicodedata.normalize("NFKD", nombre)
    base = "".join(c for c in base if not unicodedata.combining(c)).lower()
    return [t for t in base.replace(".", " ").split() if t not in _PARTICULAS]


def mismo_ponente(citado: str, registrado: str | None) -> bool | None:
    """¿El ponente que cita el escrito es el que consta? None si no se sabe.

    Se admite que el escrito abrevie: «Ciro Angarita» por «Ciro Angarita
    Barón». Lo que no se admite es un apellido que no está.
    """
    if not registrado:
        return None
    a = _tokens_nombre(citado)
    if not a:
        return None
    # Hay providencias con dos ponentes («Clara Inés Vargas Hernández; Jaime
    # Araujo Rentería», la C-355/06). Se compara con cada uno por separado:
    # juntarlos daría por buena una mezcla como «Clara Araujo».
    for uno in re.split(r"\s*;\s*|\s+y\s+(?=[A-ZÁÉÍÓÚÑ])", registrado):
        b = _tokens_nombre(uno)
        if b and (set(a) <= set(b) or fuzz.token_set_ratio(" ".join(a), " ".join(b)) >= 90):
            return True
    return False


def describir_diferencias(difs: list[tuple[str, str]], maximo: int = 4) -> str:
    """«no» donde la sentencia dice «también»; sobra «siempre»; falta «no»."""
    partes = []
    for escrito, sentencia in difs[:maximo]:
        if escrito and sentencia:
            partes.append(f"el escrito dice «{escrito}» donde la sentencia dice «{sentencia}»")
        elif escrito:
            partes.append(f"el escrito añade «{escrito}», que no está en la sentencia")
        else:
            partes.append(f"el escrito omite «{sentencia}» sin marcar la elisión")
    if len(difs) > maximo:
        partes.append(f"y {len(difs) - maximo} diferencia(s) más")
    return "; ".join(partes)


# ------------------------------------------------------------ verificación
def _agrupar(citas: list[Cita]) -> dict[str, list[Cita]]:
    grupos: dict[str, list[Cita]] = {}
    for c in citas:
        grupos.setdefault(c.clave, []).append(c)
    return grupos


def _verificar_cc(clave: str, grupo: list[Cita], indice: cc.Indice,
                  relatoria: bool, pasajes: bool,
                  cache_textos: dict, nulidad: bool = True) -> Hallazgo:
    c0 = grupo[0]
    prov = indice.buscar(c0.tipo, c0.numero, c0.anio)
    fuente = "indice" if prov else None
    texto_sent: str | None = None
    fallo_red: str | None = None

    def obtener_texto() -> str | None:
        nonlocal fallo_red
        if clave in cache_textos:
            return cache_textos[clave]
        try:
            t = cc.texto_relatoria(c0.tipo, c0.numero, c0.anio)
        except Exception as exc:              # red caída, servidor con errores
            fallo_red = f"{type(exc).__name__}: {exc}"
            t = None
        cache_textos[clave] = t
        return t

    # -- existencia, con doble comprobación ------------------------------
    if prov is None:
        if not relatoria:
            return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo,
                            posibles=indice.vecinas(c0.tipo, c0.numero, c0.anio),
                            motivo="no está en el índice oficial y no se consultó la relatoría")
        texto_sent = obtener_texto()
        if texto_sent:
            # Existe, pero el índice aún no la recoge: es reciente
            prov = cc.Providencia(
                id=cc.id_oficial(c0.tipo, c0.numero, c0.anio), tipo=c0.tipo,
                numero=cc.normalizar_numero(c0.numero), anio=cc.anio_completo(c0.anio),
                fecha=None, ponente=None, sala=None, proceso=None,
            )
            fuente = "relatoria"
        elif fallo_red:
            # No es «no existe»: la segunda fuente no contestó. Pero decir
            # hasta dónde llega el índice le da al abogado con qué juzgar:
            # una sentencia de 2019 que no está en un índice que llega a 2026
            # es otra cosa que una de la semana pasada.
            hasta = indice.fecha_mas_reciente
            cobertura = f" (el índice llega hasta el {hasta})" if hasta else ""
            return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo,
                            posibles=indice.vecinas(c0.tipo, c0.numero, c0.anio),
                            motivo=(f"no figura en el índice oficial{cobertura}, y la relatoría "
                                    f"no respondió para confirmarlo: {fallo_red}"))
        else:
            return Hallazgo(clave, Estado.NO_EXISTE, grupo,
                            posibles=indice.vecinas(c0.tipo, c0.numero, c0.anio),
                            motivo="no figura en el índice oficial de la Corte ni en su relatoría")

    h = Hallazgo(clave, Estado.VERIFICADA, grupo, providencia=prov, fuente=fuente)

    # -- ¿anulada? -------------------------------------------------------
    # El índice oficial sigue listando las sentencias anuladas; solo la
    # relatoría lo dice (ver cc.nota_de_nulidad).
    if relatoria and nulidad:
        try:
            nota = cc.nota_de_nulidad(c0.tipo, c0.numero, c0.anio)
        except Exception:
            nota = None     # no se pudo mirar; lo demás sigue valiendo
        if nota:
            h.observaciones.append(f"La sentencia fue ANULADA. Nota de la relatoría: {nota}")

    # -- ponente ---------------------------------------------------------
    for c in grupo:
        if c.ponente:
            igual = mismo_ponente(c.ponente, prov.ponente)
            if igual is False:
                h.observaciones.append(
                    f"El escrito atribuye la ponencia a {c.ponente}; "
                    f"según el registro oficial fue {prov.ponente}."
                )

    # -- pasajes entre comillas -----------------------------------------
    todos = [ct for c in grupo for ct in c.citas_textuales]
    if todos and pasajes:
        texto_sent = texto_sent or obtener_texto()
        if not texto_sent:
            h.observaciones.append(
                "No se pudo obtener el texto de la sentencia para comprobar "
                f"{len(todos)} pasaje(s) citado(s)."
                + (f" ({fallo_red})" if fallo_red else "")
            )
        else:
            _cotejar_pasajes(h, todos, texto_sent, clave, cache_textos)

    if h.observaciones:
        h.estado = Estado.CON_OBSERVACIONES
    return h


def _cotejar_pasajes(h: Hallazgo, todos: list, texto_sent: str, clave: str,
                     cache_textos: dict, fiable: bool = True) -> None:
    """Busca cada pasaje citado en el texto de la providencia.

    Con fiable=False (el texto sale del OCR de un PDF) solo se cree lo que
    coincide: una palabra mal reconocida haría que un pasaje fiel pareciera
    alterado, y eso es acusar al escrito de algo que no hizo.
    """
    norm = cache_textos.get(("norm", clave))
    if norm is None:
        norm = comparacion.normalizar(texto_sent)
        cache_textos[("norm", clave)] = norm
    vistos = set()
    sin_comprobar = 0
    for ct in todos:
        if ct.texto in vistos:
            continue
        vistos.add(ct.texto)
        r = comparacion.buscar_pasaje(ct.texto, norm)
        if not fiable and r.estado != "coincide":
            sin_comprobar += 1
            continue
        h.pasajes.append(r)
        if r.estado == "no_aparece":
            h.observaciones.append(
                "Un pasaje entre comillas atribuido a esta providencia no aparece en ella."
            )
        elif r.estado == "difiere":
            h.observaciones.append(
                "Un pasaje entre comillas no coincide literalmente con la providencia: "
                + describir_diferencias(r.diferencias) + "."
            )
    if sin_comprobar:
        h.observaciones.append(
            f"{sin_comprobar} pasaje(s) sin comprobar: la providencia solo está publicada "
            "en PDF escaneado y su texto no es fiable para comparar palabra a palabra."
        )


# ---------------------------------------------------------- Corte Suprema
def _verificar_csj(clave: str, grupo: list[Cita], consultar: bool, pasajes: bool,
                   cache_textos: dict) -> Hallazgo:
    c0 = grupo[0]
    if c0.tipo not in cs.SALAS:
        return Hallazgo(clave, Estado.NO_VERIFICABLE, grupo,
                        motivo=f"el tipo de providencia {c0.tipo} de la Corte Suprema aún no se cubre")
    if not consultar:
        return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo,
                        motivo="no se consultó el buscador de la Corte Suprema")
    try:
        b = cs.buscar(c0.tipo, c0.numero, c0.anio)
    except Exception as exc:
        return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo,
                        motivo=f"el buscador de la Corte Suprema no respondió: {exc}")

    if b.providencia is None:
        return _ausencia_csj(clave, grupo, b)

    prov = b.providencia
    h = Hallazgo(clave, Estado.VERIFICADA, grupo, providencia=prov, fuente="corte_suprema")

    for c in grupo:
        if c.ponente:
            if mismo_ponente(c.ponente, prov.ponente) is False:
                h.observaciones.append(
                    f"El escrito atribuye la ponencia a {c.ponente}; "
                    f"según el registro de la Corte Suprema fue {prov.ponente}."
                )

    todos = [ct for c in grupo for ct in c.citas_textuales]
    if todos and pasajes:
        try:
            t = cs.texto(prov)
            fallo = None
        except Exception as exc:
            t, fallo = None, f"{type(exc).__name__}: {exc}"
        if t is None:
            h.observaciones.append(
                "No se pudo obtener el texto de la providencia para comprobar "
                f"{len(todos)} pasaje(s) citado(s)." + (f" ({fallo})" if fallo else ""))
        else:
            _cotejar_pasajes(h, todos, t.texto, clave, cache_textos, fiable=not t.de_pdf)

    if h.observaciones:
        h.estado = Estado.CON_OBSERVACIONES
    return h


def _ausencia_csj(clave: str, grupo: list[Cita], b: "cs.Busqueda") -> Hallazgo:
    """Qué significa que el buscador no la encuentre.

    Nunca «no existe»: la Corte Suprema no tiene una segunda fuente con la que
    confirmar, y su buscador no tiene todo. En una muestra de 71 providencias
    citadas por la propia Corte, tres reales no estaban (STC529-2016,
    AC3390-2019, STC10418-2020). Declararlas inexistentes sería acusar en
    falso a uno de cada veinte escritos correctos.
    """
    c0 = grupo[0]
    if not b.completa:
        return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo, motivo=(
            f"el buscador de la Corte Suprema devolvió {b.total} documentos que la mencionan y "
            "no se pudieron revisar todos"))
    try:
        fiable = cs.buscador_fiable()
    except Exception:
        fiable = False
    if not fiable:
        return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo, motivo=(
            "no figura en el buscador de la Corte Suprema, pero el buscador tampoco encuentra "
            "una providencia que seguro existe: puede estar fallando"))
    if not cs.anio_cubierto(c0.anio):
        return Hallazgo(clave, Estado.NO_ENCONTRADA, grupo, motivo=(
            "no figura en el buscador de la Corte Suprema; la Corte sube sus providencias con "
            f"meses de retraso y las de {c0.anio} aún no están todas. Compruébela antes de radicar"))
    return Hallazgo(clave, Estado.NO_ENCONTRADA, grupo, motivo=(
        "no figura en el buscador de providencias de la Corte Suprema. Ese buscador no tiene "
        "todas las providencias (le falta alrededor de una de cada veinte), así que no se puede "
        "afirmar que no exista, pero sí hay que comprobarla antes de radicar"))


def verificar(texto: str, documento: str = "escrito", indice: cc.Indice | None = None,
              relatoria: bool = True, pasajes: bool = True, nulidad: bool = True) -> Informe:
    """Extrae y verifica todas las citas del texto.

    nulidad=False se salta la consulta de si cada sentencia fue anulada (una
    petición por sentencia): solo para los bancos de evaluación.
    """
    indice = indice if indice is not None else cc.Indice.cargar()
    citas = extraccion.extraer(texto)
    cache_textos: dict = {}
    hallazgos: list[Hallazgo] = []

    for clave, grupo in _agrupar(citas).items():
        c0 = grupo[0]
        if not c0.verificable:
            hallazgos.append(Hallazgo(clave, Estado.NO_VERIFICABLE, grupo,
                                      motivo=c0.motivo_no_verificable))
        elif c0.tribunal == extraccion.CORTE_CONSTITUCIONAL and c0.clase == "sentencia":
            hallazgos.append(_verificar_cc(clave, grupo, indice, relatoria,
                                           pasajes, cache_textos, nulidad))
        elif c0.tribunal == extraccion.CORTE_SUPREMA and c0.clase == "codigo":
            hallazgos.append(_verificar_csj(clave, grupo, relatoria, pasajes, cache_textos))
        else:
            hallazgos.append(Hallazgo(clave, Estado.NO_VERIFICABLE, grupo,
                                      motivo="fuente todavía no cubierta"))

    hallazgos.sort(key=lambda h: h.primera.inicio)
    return Informe(documento, hallazgos, len(texto.split()))
