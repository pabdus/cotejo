# Cotejo

**Verifica las citas jurisprudenciales de un escrito antes de radicarlo.**

Lees un escrito —una tutela, una demanda, un recurso— y Cotejo comprueba cada
providencia de la Corte Constitucional y de la Corte Suprema de Justicia que
cita contra las fuentes oficiales: que existe, que no fue anulada, que el
ponente es el correcto y que lo que se le atribuye entre comillas lo dice de
verdad, palabra por palabra.

---

## Por qué

En febrero de 2026 la Corte Suprema de Justicia sancionó por primera vez a un
abogado colombiano ([AC739-2026](https://www.infobae.com/colombia/2026/02/16/la-corte-suprema-sanciona-por-primera-vez-a-un-abogado-por-uso-indebido-de-inteligencia-artificial-en-proceso-judicial/))
por fundar un recurso en diez sentencias que no existen, generadas con IA. La
Sala fijó un **deber positivo de verificación**: no basta con no inventar, hay
que comprobar. En junio el [Consejo de Estado](https://www.infobae.com/colombia/2026/06/09/consejo-de-estado-fijo-reglas-para-usar-la-inteligencia-artificial-en-la-justicia-tras-caso-de-abogado-que-habria-inventado-sentencias-con-chatgpt/)
fijó reglas tras otro caso igual. En el mundo hay [más de 1.800 decisiones](https://www.damiencharlotin.com/hallucinations/)
judiciales con citas inventadas por IA.

Las herramientas jurídicas con IA que existen verifican las citas de **sus
propias** respuestas. Cotejo verifica un escrito hecho en cualquier otro
sitio: con ChatGPT, por un asociado, o por la parte contraria.

## Qué comprueba

| | Cómo |
|---|---|
| **Existencia — Corte Constitucional** | Contra el índice de datos abiertos de la Corte (29.474 providencias, de 1992 a hoy) y, si falta ahí, contra la relatoría. Solo se declara inexistente lo que falta en las dos. |
| **Existencia — Corte Suprema** | Contra su buscador de providencias (SC, AC, SL, AL, SP, AP, STC, STL, STP…). Ese buscador no lo tiene todo, así que lo que no aparece se marca **no encontrada**, nunca «no existe» (ver «Lo medido»). |
| **Sentencias anuladas** | La relatoría marca las anuladas con una nota; el índice oficial no. Se avisa. |
| **Ponente** | Contra el registrado. Admite que el escrito abrevie el nombre. |
| **Pasajes entre comillas** | Contra el texto completo de la providencia, palabra por palabra. Tolera tildes, mayúsculas, puntuación, elisiones «(...)» e inserciones «[la Corte]». No tolera que cambie una palabra. |
| **Confusión de año** | Si la T-760 de 2009 no existiera pero sí la de 2008, lo sugiere. |

Lo que todavía **no** puede verificar —Consejo de Estado, autos de la
Constitucional, radicados, citas por fecha («CSJ SC, 12 dic. 2017»)— lo
detecta y lo lista aparte, con el motivo. El informe nunca da a entender que
se revisó algo que no se revisó.

Ejemplo real del informe: un escrito cita la T-406/92 como «El juez, en el
Estado social de derecho **no** es un portador de la visión institucional del
interés general». Cotejo responde:

> El escrito dice «no» donde la sentencia dice «también».

## Cómo se usa

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Interfaz web local, en <http://127.0.0.1:8010>:

```bash
.venv\Scripts\python servidor.py
```

O por línea de órdenes:

```bash
.venv\Scripts\python -m cotejo escrito.docx --html informe.html --json informe.json
```

Lee Word, PDF, ODT, RTF, HTML, Markdown y texto. El código de salida es 1 si
hay algo que requiere atención, para integrarlo en otros flujos.

## Privacidad

El escrito se procesa en el equipo y **no se envía a ningún servidor**. Para
verificar, Cotejo consulta el índice de datos.gov.co, la relatoría de la
Corte Constitucional y el buscador de la Corte Suprema, y en esas consultas
solo viaja el número de cada providencia. Para un
despacho importa: un escrito antes de radicarse es información del cliente.

## Lo medido

Con etiquetas conocidas de antemano (`evaluacion/banco.py`), sobre dos
muestras independientes:

**Existencia** — sentencias reales al azar del índice e inventadas
plausibles, escritas en siete formas distintas («Sentencia T-406 de 1992»,
«T-406/92», «SENTENCIA No. T-406/1992»…), en dos muestras independientes:

| | Reales | Inventadas |
|---|---|---|
| Casos | 500 | 499 |
| Reales dadas por inexistentes | **0** | — |
| Inventadas detectadas | — | **499 (100 %)** |
| Citas que no se llegaron a extraer | 0 | 0 |

La doble comprobación encontró además una sentencia real (T-281/26) que la
relatoría ya tenía y el índice oficial todavía no: por eso existe la segunda
fuente.

> **Matiz.** En esas dos muestras la URL de las sentencias de unificación
> (SU) en la relatoría estaba mal: van sin guion (`SU214-23.htm`). Las SU
> inventadas salieron bien por la vía del índice, pero para ellas la segunda
> comprobación no era válida. Está corregido y tiene prueba.

**Pasajes** — frases reales de sentencias reales, con ocho variantes:

| Variante | Esperado | Semilla 2026 | Semilla 99 |
|---|---|---|---|
| literal, con ruido, con elisión | coincide | 180/180 | 180/180 |
| un «no» insertado | difiere | 60/60 | 60/60 |
| una palabra cambiada | difiere | 60/60 | 60/60 |
| una palabra omitida | difiere | 60/60 | 60/60 |
| frase de otra sentencia | no aparece | 60/60 | 59/60 |
| palabras desordenadas | no aparece | 60/60 | 60/60 |
| **Alteraciones dadas por buenas** | | **0** | **0** |

El único fallo de la segunda muestra es una frase de otra sentencia marcada
como «difiere» en vez de «no aparece» —las sentencias comparten fórmulas casi
idénticas—; sigue marcada como problema.

La primera pasada de la semilla 2026 encontró dos defectos reales, que están
corregidos y tienen prueba: las comillas anidadas y las desparejadas. Por eso
se midió después una muestra nueva: una cifra sobre los mismos datos con los
que se corrigió no demuestra nada.

Por el fallo de URL de arriba, esos 960 casos no incluían sentencias de
unificación. Con la URL corregida se midieron aparte, con semilla nueva:

| Sentencias de unificación (semilla 31) | Esperado | Resultado |
|---|---|---|
| literal, con ruido, con elisión | coincide | 180/180 |
| «no» insertado, palabra cambiada, palabra omitida | difiere | 180/180 |
| frase de otra sentencia | no aparece | 58/60 |
| palabras desordenadas | no aparece | 58/60 |
| **Alteraciones dadas por buenas** | | **0** |

60 SU con texto; se descartaron 4, y se dice cuáles: dos por pasar de 120.000
palabras y dos sin texto. Una de estas, la SU-163/23, **había sido anulada**
en 2024: así se descubrió que el índice oficial sigue listando las
sentencias anuladas, y ahora Cotejo lo avisa. Los cuatro fallos son del
propio banco: dos «frases de otra sentencia» que eran fórmulas de cierre
comunes a todas, y dos frases barajadas que arrastraban una comilla.

**Escritos redactados por un LLM** (`evaluacion/escritos_llm.py`). Se
pidieron a qwen3.5:9b, en local, 15 fundamentos jurídicos de tutela con
jurisprudencia, ponentes y citas textuales. Salieron 68 citas de la Corte
Constitucional:

| | |
|---|---|
| No existen | **58 (85 %)** |
| Existen, con observaciones | 7 (un ponente equivocado; seis con un pasaje que la sentencia no contiene) |
| Verificadas | 3 |
| Pasajes entre comillas que aparecen en la sentencia | **0 de 7** |

Las 58 se auditaron a mano: 35 tienen un número que la Corte no alcanzó ese
año (SU-1385/18, cuando ese año llegó a la 124), y las otras 23 se volvieron a
consultar en la relatoría una por una, con controles positivos. Es un modelo
pequeño; hay que repetirlo con escritos de ChatGPT, Gemini y Claude.

**Corte Suprema** (`evaluacion/banco_csj.py`). La Corte Suprema no tiene un
índice abierto que sirva de segunda fuente, así que antes de dejar que una
ausencia en su buscador signifique algo había que medir cuántas providencias
reales no encuentra. Citas reales con certeza: las que hace la propia Corte
en sus providencias. En dos muestras (semillas 7 y 11), 135 códigos:

| | |
|---|---|
| Encontradas | 128 |
| Reales que el buscador no tiene | **6 (4,4 %)**: STC529-2016, AC3390-2019, STC10418-2020, AC7390-2016, AL481-2022, AC258-2015 |
| Errata de la propia Corte | 1 (AP37858-2016: un radicado escrito como código) |

Todas las que faltan son autos o tutelas. Por eso lo que no aparece ahí se
marca **no encontrada** —hay que comprobarla— y no «no existe»: con uno de
cada veinte escritos correctos acusado en falso, la herramienta no serviría.

La medición destapó además dos defectos del conector, corregidos y con
prueba: el buscador busca en el texto y no en el título, y el AP7408-2015 se
encabeza «AP – 7408 - 2015», que solo encuentra la frase con espacios; y las
providencias muy citadas quedan detrás de las que las citan (el AP2118-2020
salía en la posición 149 de 155), así que se buscan desde el final.

## Pruebas

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

274 pruebas sin red, entre ellas las de la demo pública (`demo/`, `infra/`),
algunas con bytes reales de la relatoría guardados en `tests/datos/`. Con
`COTEJO_RED=1`, seis más contra las fuentes reales.

## Cortesía con los servidores públicos

Tras unas 800 peticiones en una tarde de evaluación, el sitio de la Corte
Constitucional dejó de responder a esta máquina, portada incluida. Por eso
Cotejo espera entre peticiones (1,5 s en la relatoría, `COTEJO_PAUSA`; 1 s en
la Corte Suprema, `COTEJO_PAUSA_CSJ`), reintenta lo transitorio con espera
creciente, y si un servidor falla tres veces seguidas deja de insistirle
durante 15 minutos: lo que dependa de él queda «sin confirmar», nunca «no
existe». Todo se guarda en `.cache/`. Para saber si una sentencia fue anulada
se piden solo los primeros 30 KB de su página.

Y antes de creer una ausencia, Cotejo comprueba que la fuente sigue
respondiendo con algo que seguro existe (la T-406/92, la SC5686-2018): si un
día cambia el formato de un sitio, todo daría «no existe», y eso se detecta.

## Estructura

```
cotejo/
  extraccion.py                   Qué es una cita y qué no
  fuentes/corte_constitucional.py Índice oficial, relatoría y anulaciones
  fuentes/corte_suprema.py        Buscador de providencias de la Corte Suprema
  fuentes/red.py                  Pausas, reintentos y cortacircuitos
  comparacion.py                  Pasajes, palabra a palabra
  verificacion.py                 Reglas de decisión
  informe.py                      Terminal, JSON, HTML
  web.py, static/                 Interfaz local
evaluacion/                       Bancos de evaluación
tests/                            Pruebas
```

Las decisiones de diseño, y el fallo del que salió cada una, están en
[ESTADO.md](ESTADO.md).
