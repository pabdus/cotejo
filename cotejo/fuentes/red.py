"""Acceso a servidores públicos con cortesía y sin falsos «no existe».

Cada fuente oficial tiene su propio `Cliente`, con su pausa y su
cortacircuitos, porque que un servidor nos corte no dice nada de los otros.

Por qué existe este módulo: tras unas 800 peticiones en una tarde de
evaluación, el sitio de la Corte Constitucional dejó de responder a esta
máquina —ni la portada—, cortando la conexión TLS al instante. Si eso le pasa
a un abogado, se queda sin acceso a la relatoría por culpa de la herramienta.
De ahí las tres reglas:

1. Pausa entre peticiones al mismo servidor.
2. Reintentos con espera creciente para lo transitorio: cortes de conexión,
   tiempos de espera, 429 y 5xx.
3. Cortacircuitos: tras varios fallos seguidos, dejar de insistir durante un
   rato. Quien llama recibe `ServicioNoDisponible` y tiene que tratarlo como
   «sin confirmar», nunca como «no existe».
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CABECERA = {"User-Agent": "Cotejo/0.1 (verificacion de citas juridicas)"}


def dir_cache() -> Path:
    base = os.environ.get("COTEJO_CACHE")
    ruta = Path(base) if base else Path(__file__).resolve().parents[2] / ".cache"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


class ServicioNoDisponible(Exception):
    """El servidor no responde. NO quiere decir que lo buscado no exista."""


class Cliente:
    def __init__(self, nombre: str, pausa: float, reintentos: int = 3,
                 fallos_para_cortar: int = 3, corte_segundos: float = 15 * 60,
                 tiempo_espera: float = 60):
        self.nombre = nombre
        self.pausa = pausa
        self.reintentos = reintentos
        self.fallos_para_cortar = fallos_para_cortar
        self.corte_segundos = corte_segundos
        self.tiempo_espera = tiempo_espera
        self.ultima = 0.0
        self.fallos_seguidos = 0
        self.cortado_hasta = 0.0

    def disponible(self) -> bool:
        return time.time() >= self.cortado_hasta

    def reiniciar(self) -> None:
        self.fallos_seguidos, self.cortado_hasta = 0, 0.0

    def pedir(self, url: str, datos: bytes | None = None,
              cabeceras: dict | None = None) -> bytes:
        """GET, o POST si hay `datos`. Un 404 devuelve b"": es una respuesta."""
        if not self.disponible():
            raise ServicioNoDisponible(
                f"{self.nombre} dejó de responder hace poco; se espera antes de insistir")

        ultimo: Exception | None = None
        for intento in range(self.reintentos):
            espera = self.pausa * (2 ** intento) - (time.time() - self.ultima)
            if espera > 0:
                time.sleep(espera)
            self.ultima = time.time()
            try:
                req = urllib.request.Request(
                    url, data=datos, headers={**CABECERA, **(cabeceras or {})},
                    method="POST" if datos is not None else "GET")
                with urllib.request.urlopen(req, timeout=self.tiempo_espera) as r:
                    cuerpo = r.read()
                self.fallos_seguidos = 0
                return cuerpo
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self.fallos_seguidos = 0
                    return b""
                if exc.code < 500 and exc.code != 429:
                    raise
                ultimo = exc
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                ultimo = exc

        self.fallos_seguidos += 1
        if self.fallos_seguidos >= self.fallos_para_cortar:
            self.cortado_hasta = time.time() + self.corte_segundos
        raise ServicioNoDisponible(f"{self.nombre}: {type(ultimo).__name__}: {ultimo}")
