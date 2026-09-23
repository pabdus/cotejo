# Cotejo

**Verifica las citas jurisprudenciales de un escrito antes de radicarlo.**

Lees un escrito —una tutela, una demanda, un recurso— y Cotejo comprueba cada
sentencia de la Corte Constitucional que cita contra las fuentes oficiales:
que existe, que el ponente es el correcto y que lo que se le atribuye entre
comillas lo dice de verdad, palabra por palabra.

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
| **Existencia** | Contra el índice de datos abiertos de la Corte (29.474 providencias, de 1992 a hoy) y, si falta ahí, contra la relatoría. Solo se declara inexistente lo que falta en las dos. |
| **Ponente** | Contra el registrado. Admite que el escrito abrevie el nombre. |
| **Pasajes entre comillas** | Contra el texto completo de la sentencia, palabra por palabra. Tolera tildes, mayúsculas, puntuación, elisiones «(...)» e inserciones «[la Corte]». No tolera que cambie una palabra. |
| **Confusión de año** | Si la T-760 de 2009 no existiera pero sí la de 2008, lo sugiere. |

Lo que todavía **no** puede verificar —Corte Suprema, Consejo de Estado,
autos, radicados— lo detecta y lo lista aparte, con el motivo. El informe
nunca da a entender que se revisó algo que no se revisó.

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
verificar, Cotejo consulta el índice de datos.gov.co y la relatoría de la
Corte, y en esas consultas solo viaja el número de cada sentencia. Para un
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

> **Límite de estas cifras.** Por el fallo de URL de arriba, el banco de
> pasajes descartó en silencio las sentencias SU: los 960 casos son de tutela
> y de constitucionalidad. Hay que repetirlo incluyéndolas.

**Escritos redactados por un LLM** (`evaluacion/escritos_llm.py`) —
**pendiente**. Se pidieron a qwen3.5:9b, en local, 15 fundamentos jurídicos de
tutela con jurisprudencia, ponentes y citas textuales; salieron 68 citas de la
Corte Constitucional. A mitad de la verificación el sitio de la Corte dejó de
responder a esta máquina, y 26 quedaron «sin confirmar» —correctamente: no se
dieron por inexistentes—. Dato preliminar: de las 42 con respuesta
definitiva, **32 no existen**. Es un modelo pequeño; su tasa de invención no
es la de un modelo de frontera, y hay que repetir la medición con la
relatoría accesible y con escritos de ChatGPT, Gemini y Claude.

## Pruebas

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

52 pruebas sin red. Con `COTEJO_RED=1`, tres más contra las fuentes reales.

## Cortesía con la relatoría

Tras unas 800 peticiones en una tarde de evaluación, el sitio de la Corte
dejó de responder a esta máquina, portada incluida. Por eso Cotejo espera
1,5 s entre peticiones (`COTEJO_PAUSA`), reintenta lo transitorio con espera
creciente, y si falla tres veces seguidas deja de insistir durante 15 minutos:
lo que dependa de la relatoría queda «sin confirmar», nunca «no existe». Todo
se guarda en `.cache/`, así que cada sentencia se descarga una sola vez.

## Estructura

```
cotejo/
  extraccion.py                   Qué es una cita y qué no
  fuentes/corte_constitucional.py Índice oficial y relatoría
  comparacion.py                  Pasajes, palabra a palabra
  verificacion.py                 Reglas de decisión
  informe.py                      Terminal, JSON, HTML
  web.py, static/                 Interfaz local
evaluacion/                       Bancos de evaluación
tests/                            Pruebas
```

Las decisiones de diseño, y el fallo del que salió cada una, están en
[ESTADO.md](ESTADO.md).
