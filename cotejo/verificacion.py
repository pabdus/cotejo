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


class Estado(str, Enum):
    VERIFICADA = "verificada"
    CON_OBSERVACIONES = "con_observaciones"
    NO_EXISTE = "no_existe"
    SIN_CONFIRMAR = "sin_confirmar"
    NO_VERIFICABLE = "no_verificable"


ETIQUETA = {
    Estado.VERIFICADA: "Verificada",
    Estado.CON_OBSERVACIONES: "Existe, con observaciones",
    Estado.NO_EXISTE: "No existe",
    Estado.SIN_CONFIRMAR: "Sin confirmar",
    Estado.NO_VERIFICABLE: "No verificable",
}


@dataclass
class Hallazgo:
    """Resultado para una providencia, agrupando todas las veces que se cita."""
    clave: str
    estado: Estado
    citas: list[Cita]
    providencia: cc.Providencia | None = None
    fuente: str | None = None             # "indice" | "relatoria"
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
                if h.estado in (Estado.NO_EXISTE, Estado.CON_OBSERVACIONES)]


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
                  cache_textos: dict) -> Hallazgo:
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
            return Hallazgo(clave, Estado.SIN_CONFIRMAR, grupo,
                            posibles=indice.vecinas(c0.tipo, c0.numero, c0.anio),
                            motivo=f"no está en el índice oficial y la relatoría no respondió ({fallo_red})")
        else:
            return Hallazgo(clave, Estado.NO_EXISTE, grupo,
                            posibles=indice.vecinas(c0.tipo, c0.numero, c0.anio),
                            motivo="no figura en el índice oficial de la Corte ni en su relatoría")

    h = Hallazgo(clave, Estado.VERIFICADA, grupo, providencia=prov, fuente=fuente)

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
            norm = cache_textos.get(("norm", clave))
            if norm is None:
                norm = comparacion.normalizar(texto_sent)
                cache_textos[("norm", clave)] = norm
            vistos = set()
            for ct in todos:
                if ct.texto in vistos:
                    continue
                vistos.add(ct.texto)
                r = comparacion.buscar_pasaje(ct.texto, norm)
                h.pasajes.append(r)
                if r.estado == "no_aparece":
                    h.observaciones.append(
                        "Un pasaje entre comillas atribuido a esta sentencia no aparece en ella."
                    )
                elif r.estado == "difiere":
                    h.observaciones.append(
                        "Un pasaje entre comillas no coincide literalmente con la sentencia: "
                        + describir_diferencias(r.diferencias) + "."
                    )

    if h.observaciones:
        h.estado = Estado.CON_OBSERVACIONES
    return h


def verificar(texto: str, documento: str = "escrito", indice: cc.Indice | None = None,
              relatoria: bool = True, pasajes: bool = True) -> Informe:
    """Extrae y verifica todas las citas del texto."""
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
                                           pasajes, cache_textos))
        else:
            hallazgos.append(Hallazgo(clave, Estado.NO_VERIFICABLE, grupo,
                                      motivo="fuente todavía no cubierta"))

    hallazgos.sort(key=lambda h: h.primera.inicio)
    return Informe(documento, hallazgos, len(texto.split()))
