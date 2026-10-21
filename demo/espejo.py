"""Espejo en S3 de la caché en disco de Cotejo (DISEÑO.md §2, «Espejo»).

La caché de Cotejo son archivos bajo `red.dir_cache()`, y cada módulo decide
con el mtime si una entrada sigue valiendo: el texto de una sentencia y una
nota de nulidad, para siempre; un «no anulada», 30 días; una ausencia, un día.
En la Lambda esa carpeta está en /tmp y muere con el contenedor, así que sin
espejo cada arranque en frío volvería a pedirle a la Corte las mismas
sentencias. Con contenedores que se reciclan cada pocas horas, la demo le
pediría a la relatoría lo mismo una y otra vez, que es justo lo que hizo que
la Corte nos cortara la conexión (ESTADO.md, 1c).

El espejo no cambia esa lógica de caducidad, la conserva: antes de la consulta
baja de `s3://BUCKET/cache/<ruta relativa>` las entradas que esta consulta va a
leer y les pone como mtime el LastModified de S3; así un «no anulada» guardado
hace veinte días sigue caducando a los 30, y no 30 días después de bajarlo.
Después sube lo nuevo o lo cambiado. El bucket borra `cache/` a los 30 días
(ciclo de vida): las notas de nulidad, que no caducan, se vuelven a subir antes
(ver _RENOVAR_NOTA).

Lo que no se reparte (DISEÑO §2, tercera ronda):
- Las ausencias de la relatoría (un `relatoria/*.txt.gz` con el texto vacío)
  ni se suben ni se bajan. Un desafío de cortafuegos hecho solo de <script>,
  dentro de los dos minutos del canario, salía «no existe»; el espejo lo subía
  y un contenedor recién arrancado daba «no existe» a esa sentencia durante un
  día sin preguntar nada (reproducido). El núcleo ya no lo cree (la carcasa se
  reconoce por su firma), pero el error de un contenedor no tiene por qué
  repartirse a los demás: cada uno vuelve a preguntar sus inventadas (lo acota
  MAX_PROVIDENCIAS) y la ausencia vale un día solo en su disco.
- Una respuesta negativa («no anulada», una búsqueda de la Corte Suprema sin
  la providencia) donde S3 pueda tener una positiva. Solo se sube si esta
  consulta vio que en S3 no había nada, o que había otra negativa; si la
  bajada falló, no se sube ninguna. Reproducido con el texto: S3 tenía el de la
  T-982/26, su bajada falló con InternalError, el núcleo leyó un desafío como
  ausencia y el espejo sustituyó el texto por un objeto vacío. Con una nota de
  nulidad sería esconder una anulación a todos los contenedores.

Qué entradas va a leer una consulta se sabe antes de verificar, por las citas
(ver `previsibles`). Una prueba de contrato comprueba que todo lo que el núcleo
escribe está entre lo previsto: si un día el núcleo guardara en otra ruta, esa
entrada nunca se bajaría y la demo volvería a pedirla siempre, sin que nada
fallara a la vista.

Un fallo de S3 nunca rompe la consulta: se registra y se sigue sin caché. Lo
peor que puede pasar sin espejo es preguntarle a la Corte; lo peor que puede
pasar con un espejo que rompe es no responder.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import time
import zlib
from pathlib import Path

from cotejo import extraccion, verificacion
from cotejo.fuentes import corte_constitucional as cc
from cotejo.fuentes import corte_suprema as cs
from cotejo.fuentes import red

PREFIJO = "cache/"

# El índice tiene su propia clave en S3 (indice/) y en la Lambda no se escribe
# en disco; si alguna vez se escribiera, no tiene que acabar duplicado en cache/.
_INDICE = "corte_constitucional_indice.json.gz"
_PARCIAL = ".parcial"

# Lo que S3 responde para una clave que aún no está. Con s3:ListBucket es
# NoSuchKey; sin ese permiso es AccessDenied, y la política da ListBucket
# limitado a los prefijos de la demo, que no está comprobado contra AWS que
# baste para que una clave ausente dé 404. Antes un AccessDenied cortaba la
# bajada entera y la consulta volvía a pedirle a la Corte todo lo demás, aunque
# estuviera en el bucket. Ahora cada clave va por su cuenta: para bajar, una que
# no se puede leer es una que no está (el núcleo la pedirá, que es el lado
# seguro), y se sigue con las demás. Para subirle encima una respuesta negativa,
# no: puede estar y no dejarse leer. Solo un error de S3 en sí (caído, sin red)
# para la bajada.
_NO_ESTA = {"NoSuchKey", "404", "NotFound", "AccessDenied"}

# Forma de un ProvidenciaCSJ.id. El id sale de una búsqueda guardada, y antes de
# convertirlo en una ruta se comprueba que no pueda salirse de la caché.
_RE_ID_CSJ = re.compile(r"[A-Z]{2,3}\d{1,5}-\d{4}")

# El texto de una sentencia de la Constitucional: relatoria/T-406-1992.txt.gz.
# Las notas van en relatoria/notas/, y no son ausencias aunque digan «no anulada».
_RE_TEXTO_RELATORIA = re.compile(r"relatoria/[^/]+\.txt\.gz")
_NOTAS = "relatoria/notas/"

# Una nota de nulidad no caduca en el núcleo (una anulación no se deshace), pero
# el ciclo de vida de cache/ la borra a los 30 días de subirla, y nada la volvía
# a subir: el núcleo la lee sin reescribirla y subir() solo sube lo que cambió.
# Con la relatoría en 403, una vez borrada la SU-163/23 pasaba de «ANULADA» a «no
# se pudo comprobar si la sentencia fue anulada» (reproducido). Al bajar una con
# más de 20 días se le pone mtime = ahora y se sube otra vez: quedan diez días de
# margen para que alguna consulta la lea antes de que caduque en S3.
_RENOVAR_NOTA = 20 * 24 * 3600


def _registrar(datos: dict) -> None:
    print(json.dumps(datos, ensure_ascii=False), flush=True)


def _codigo_error(exc: Exception) -> str | None:
    return ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")


# ------------------------------------------------------- qué dice una entrada
def negativa(rel: str, datos: bytes) -> bool:
    """¿Es la respuesta «no la tengo / no hay nada» de su fuente?

    - El texto de una sentencia (.txt.gz) vacío: la ausencia en la relatoría.
    - Una nota de nulidad con "nota": null: el «no anulada».
    - Una búsqueda de la Corte Suprema con "providencia": null: «no encontrada».
    - Cualquier archivo de 0 bytes.

    Lo que no se puede leer cuenta también como negativo: no es una respuesta
    de la Corte que se pueda repartir, y así nunca pisa en S3 una que sí lo es.
    """
    if not datos:
        return True
    if rel.endswith(".gz"):
        try:
            # Un byte basta para saber si hay texto: sin descomprimir los varios
            # MB de una sentencia de constitucionalidad
            with gzip.GzipFile(fileobj=io.BytesIO(datos)) as f:
                return not f.read(1)
        except (OSError, EOFError, zlib.error):
            return True
    if rel.endswith(".json"):
        try:
            guardado = json.loads(datos)
        except ValueError:
            return True
        if isinstance(guardado, dict):
            for campo in ("nota", "providencia"):
                if campo in guardado:
                    return not guardado[campo]
    return False


def ausencia_relatoria(rel: str, datos: bytes) -> bool:
    """¿Es una ausencia de la relatoría? Esas no se suben ni se bajan."""
    return _RE_TEXTO_RELATORIA.fullmatch(rel) is not None and negativa(rel, datos)


# ------------------------------------------------------------- predicción
def _consultables(citas: list[extraccion.Cita],
                  max_consultadas: int | None = None) -> list[extraccion.Cita]:
    """La primera cita de cada providencia que verificar() le va a preguntar a una fuente.

    Mismas reglas y mismo orden que verificar(): agrupa con _agrupar, decide
    con _necesita_red sobre la primera del grupo (la que consulta) y corta en
    max_consultadas. Una copia aquí podría divergir y dejar sin bajar lo que sí
    se va a leer. Con el tope, lo que pasa de él sale «sin confirmar» sin abrir
    la caché: con 40 providencias se bajaban 80 entradas de S3 y el núcleo leía
    las de 15, y cada bajada inútil salía del plazo de la consulta.
    """
    salida: list[extraccion.Cita] = []
    for grupo in verificacion._agrupar(citas).values():
        if not verificacion._necesita_red(grupo[0], True):
            continue
        if max_consultadas is not None and len(salida) >= max_consultadas:
            break
        salida.append(grupo[0])
    return salida


def _base(consultables: list[extraccion.Cita]) -> list[Path]:
    """Lo que se sabe por la cita sola: texto y nota de la Constitucional, búsqueda de la Suprema."""
    rutas: list[Path] = []
    for c in consultables:
        if c.tribunal == extraccion.CORTE_CONSTITUCIONAL:
            rutas += [cc.ruta_texto(c.tipo, c.numero, c.anio), cc.ruta_nota(c.tipo, c.numero, c.anio)]
        else:
            rutas.append(cs.ruta_busqueda(c.tipo, c.numero, c.anio))
    return list(dict.fromkeys(rutas))


def _textos_csj(consultables: list[extraccion.Cita]) -> list[Path]:
    """El texto de la Corte Suprema va por el id que dio la búsqueda, no por la cita.

    La cita «SP22-2019» se guarda como búsqueda SP22-2019 (forma canónica), pero
    la providencia se registra «SP022-2019» y su texto va con ese nombre. Solo
    se sabe leyendo la búsqueda, que por eso se baja antes.
    """
    rutas: list[Path] = []
    for c in consultables:
        if c.tribunal != extraccion.CORTE_SUPREMA:
            continue
        try:
            guardado = json.loads(cs.ruta_busqueda(c.tipo, c.numero, c.anio).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ident = (guardado.get("providencia") or {}).get("id") if isinstance(guardado, dict) else None
        if isinstance(ident, str) and _RE_ID_CSJ.fullmatch(ident):
            rutas.append(cs.ruta_texto(ident))
    return list(dict.fromkeys(rutas))


def previsibles(citas: list[extraccion.Cita], max_consultadas: int | None = None) -> list[Path]:
    """Las entradas de caché que puede leer o escribir la verificación de estas citas.

    Con max_consultadas, solo las de las providencias que verificar() llegará a
    consultar con ese tope.
    """
    consultables = _consultables(citas, max_consultadas)
    return list(dict.fromkeys(_base(consultables) + _textos_csj(consultables)))


# ------------------------------------------------------------------- foto
def foto(raiz: Path | None = None) -> dict[str, tuple[int, int]]:
    """(mtime en ns, tamaño) de cada archivo de la caché, por ruta relativa."""
    raiz = raiz or red.dir_cache()
    salida: dict[str, tuple[int, int]] = {}
    for ruta in raiz.rglob("*"):
        if ruta.name == _INDICE or ruta.name.endswith(_PARCIAL) or not ruta.is_file():
            continue
        st = ruta.stat()
        salida[ruta.relative_to(raiz).as_posix()] = (st.st_mtime_ns, st.st_size)
    return salida


# ----------------------------------------------------------------- espejo
class Espejo:
    def __init__(self, s3, bucket: str, registrar=_registrar):
        self.s3 = s3
        self.bucket = bucket
        self.registrar = registrar
        self._denegadas = 0         # AccessDenied en la última bajada
        # Lo que la última bajada sabe de S3, para no pisar una positiva con una
        # negativa. _sin_positiva: claves en las que S3 no tenía nada (NoSuchKey)
        # o tenía otra negativa. _bajada_completa: si S3 falló a mitad, de las
        # claves que faltaban no se sabe nada. Sin preparar(), nada: ninguna
        # negativa se sube.
        self._sin_positiva: set[str] = set()
        self._bajada_completa = False

    def _fallo(self, operacion: str, exc: Exception) -> None:
        # Solo el tipo: el mensaje de botocore trae la clave, y la clave dice
        # qué sentencia se consultó (no es el texto, pero tampoco hace falta).
        self.registrar({"evento": "espejo_fallo", "operacion": operacion,
                        "tipo": type(exc).__name__, "codigo": _codigo_error(exc)})

    def preparar(self, citas: list[extraccion.Cita],
                 max_consultadas: int | None = None) -> dict[str, tuple[int, int]]:
        """Baja de S3 lo previsible para estas citas y devuelve la foto para `subir`.

        Con max_consultadas, solo lo de las providencias que verificar() va a
        consultar con ese tope (el mismo que se le pase a verificar()).

        Nunca lanza: si S3 falla, la consulta sigue con lo que haya en disco.
        """
        raiz = red.dir_cache()
        self._denegadas = 0
        self._sin_positiva = set()
        self._bajada_completa = False
        renovar: list[str] = []
        try:
            consultables = _consultables(citas, max_consultadas)
            if self._bajar(raiz, _base(consultables), renovar):
                self._bajada_completa = self._bajar(raiz, _textos_csj(consultables), renovar)
        except Exception as exc:             # una predicción rota tampoco para la consulta
            self._fallo("bajar", exc)
        if self._denegadas:
            # Contar lo que se descarta (ESTADO.md, 1b): si un día la política
            # deniega también lo que sí está, el espejo dejaría de servir y la
            # demo le volvería a pedir todo a la Corte sin que nada fallara a la
            # vista. Así al menos se ve, con un número y sin las claves.
            self.registrar({"evento": "espejo_denegado", "claves": self._denegadas})
        antes = foto(raiz)
        for rel in renovar:
            # Fuera de la foto: para subir() es nueva, y la vuelve a subir
            antes.pop(rel, None)
        return antes

    def _bajar(self, raiz: Path, rutas: list[Path], renovar: list[str]) -> bool:
        """Baja las que falten en disco. False si S3 falló: lo demás también fallaría.

        Una clave que no está o que no se deja leer no es un fallo de S3: se
        sigue con la siguiente.
        """
        for ruta in rutas:
            # Lo que ya está en disco gana: o se bajó en una consulta anterior de
            # este contenedor, o lo escribió el núcleo y se subió. Pedirlo otra vez
            # solo cambiaría algo si el otro contenedor lo hubiera renovado entre
            # tanto, y lo peor de no verlo es una petición más a la Corte.
            if ruta.exists():
                continue
            rel = ruta.relative_to(raiz).as_posix()
            try:
                r = self.s3.get_object(Bucket=self.bucket, Key=PREFIJO + rel)
                datos = r["Body"].read()
                mtime = r["LastModified"].timestamp()
            except Exception as exc:
                codigo = _codigo_error(exc)
                if codigo in _NO_ESTA:
                    if codigo == "AccessDenied":
                        self._denegadas += 1
                    else:
                        self._sin_positiva.add(rel)
                    continue                 # aún no está en el bucket: lo pedirá el núcleo
                self._fallo("bajar", exc)
                return False
            es_negativa = negativa(rel, datos)
            if es_negativa:
                self._sin_positiva.add(rel)
                if ausencia_relatoria(rel, datos):
                    # Una ausencia en S3 solo puede ser de antes de esta regla o
                    # de un error: no se cree. El núcleo volverá a preguntar.
                    continue
            # A un archivo a medias el núcleo lo leería como un gzip roto: se
            # escribe aparte y se renombra, que es atómico.
            ruta.parent.mkdir(parents=True, exist_ok=True)
            parcial = ruta.with_name(ruta.name + _PARCIAL)
            parcial.write_bytes(datos)
            os.replace(parcial, ruta)
            if rel.startswith(_NOTAS) and not es_negativa and time.time() - mtime > _RENOVAR_NOTA:
                os.utime(ruta, None)        # ahora: para el núcleo no cambia nada, no caduca
                renovar.append(rel)
            else:
                os.utime(ruta, (mtime, mtime))
        return True

    def _se_puede_subir(self, rel: str, datos: bytes) -> bool:
        if not negativa(rel, datos):
            return True
        if ausencia_relatoria(rel, datos):
            return False
        # Una negativa solo donde se sabe que S3 no tiene una positiva
        return self._bajada_completa and rel in self._sin_positiva

    def subir(self, antes: dict[str, tuple[int, int]]) -> int:
        """Sube lo nuevo o cambiado desde la foto `antes`. Devuelve cuántos subió.

        Se compara mtime y tamaño, no el contenido: lo que se bajó de S3 y solo
        se leyó conserva el mtime que se le puso, y no se vuelve a subir. Un «no
        anulada» o una búsqueda sin la providencia caducados que el núcleo volvió
        a preguntar sí cambian de mtime, y se suben aunque la respuesta sea la
        misma: así renuevan su plazo. Las ausencias de la relatoría no se suben, y
        ninguna negativa pisa en S3 lo que pueda ser una positiva (ver arriba).
        """
        raiz = red.dir_cache()
        subidos = 0
        try:
            for rel, firma in sorted(foto(raiz).items()):
                if antes.get(rel) == firma:
                    continue
                datos = (raiz / rel).read_bytes()
                if not self._se_puede_subir(rel, datos):
                    continue
                self.s3.put_object(Bucket=self.bucket, Key=PREFIJO + rel, Body=datos)
                subidos += 1
                # Lo que acaba de subir es lo que hay ahora en S3
                if negativa(rel, datos):
                    self._sin_positiva.add(rel)
                else:
                    self._sin_positiva.discard(rel)
        except Exception as exc:
            self._fallo("subir", exc)
        return subidos
