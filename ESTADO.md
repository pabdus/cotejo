# Estado del proyecto

Documento de traspaso: lo que hace falta saber para retomar el trabajo sin
haber estado en la conversación que lo construyó. El README explica **qué**
hace Cotejo; esto explica **por qué está hecho así** y **qué no se debe
deshacer**.

Última actualización: 23 de septiembre de 2026.

---

## De un vistazo

| | |
|---|---|
| Ubicación | `C:\dev\Cotejo` |
| Entorno | `.venv` propio (ver «Trampas del entorno») |
| Interfaz web | `.venv\Scripts\python servidor.py` → <http://127.0.0.1:8010> |
| Línea de órdenes | `.venv\Scripts\python -m cotejo escrito.docx --html informe.html` |
| Pruebas | `.venv\Scripts\python -m unittest discover -s tests` — 52, todas pasando |
| Pruebas con red | `COTEJO_RED=1` delante — 3 más, contra las fuentes reales |
| Evaluación | `evaluacion/banco.py` y `evaluacion/escritos_llm.py` |

Qué es: un verificador de citas jurisprudenciales. Lee un escrito, extrae las
citas y comprueba cada una contra las fuentes oficiales de la Corte
Constitucional de Colombia. Nació después de que la Corte Suprema sancionara
en febrero de 2026 (AC739-2026) a un abogado por citar diez sentencias
inventadas por IA, estableciendo un «deber positivo de verificación».

---

## Mapa de módulos

```
cotejo/
  extraccion.py                  Qué es una cita y qué no. Comillas anidadas.
  fuentes/corte_constitucional.py  Índice de datos.gov.co + relatoría
  comparacion.py                 ¿Dice la sentencia lo que se cita? Palabra a palabra
  verificacion.py                Reglas de decisión y estados
  informe.py                     Terminal, JSON e informe HTML
  web.py + static/index.html     Interfaz local
  documentos.py                  Importar docx, pdf, odt...
evaluacion/
  banco.py                       Etiquetas conocidas por construcción
  escritos_llm.py                Escritos redactados por un LLM, para auditar
```

---

## Decisiones que no se deben deshacer

Cada una salió de un fallo real encontrado probando. Sin este contexto
parecen arbitrarias.

### 1. Solo se declara «no existe» con dos fuentes, y un fallo de red nunca lo produce

Un falso «no existe» ante un abogado es lo peor que puede hacer esta
herramienta: le hace dudar de un escrito correcto o, peor, le hace acusar a
la contraparte en falso. Una sentencia solo es inexistente si falta en el
índice oficial **y** la relatoría confirma que no la tiene. Si la relatoría
no responde, el resultado es **sin confirmar**. Hay una prueba para esto
(`test_un_fallo_de_red_nunca_da_no_existe`).

Medido: 0 sentencias reales dadas por inexistentes en 500 casos. Y cuando
la relatoría cortó la conexión en mitad de una evaluación, las 26 citas
afectadas quedaron «sin confirmar», ninguna como «no existe».

### 1b. Las sentencias de unificación van SIN guion en la relatoría

`T-406-92.htm` y `c-214-21.htm`, pero `SU214-23.htm`. Durante toda la primera
evaluación la URL de las SU estuvo mal: 280 consultadas, ninguna con texto.
Nadie lo vio porque la herramienta hacía lo prudente —si no hay texto, no
comprueba el pasaje— y el banco de pasajes descartaba en silencio las
sentencias sin texto. Lo destapó auditar a mano por qué un escrito tenía 0
pasajes comprobados. Lección: **contar lo que se descarta**, no solo lo que
se mide.

### 1c. Cortacircuitos ante la relatoría

Tras ~800 peticiones en una tarde, el sitio de la Corte cortó la conexión a
esta máquina (TLS cerrado al instante, también la portada). Pausa de 1,5 s,
reintentos con espera creciente y, tras tres fallos seguidos, 15 minutos sin
insistir. A un abogado no se le puede dejar sin acceso a la relatoría por
usar la herramienta.

### 2. Los pasajes se comparan palabra a palabra, nunca por parecido de caracteres

La primera versión usaba `rapidfuzz` y daba por buena, con un **96,7 %**, una
cita en la que «también es» se había cambiado por «no es». En derecho eso no
es una errata: es decir lo contrario. Ahora «coincide» exige todas las
palabras, y cualquier diferencia se enseña tal cual.

### 3. Las comillas se leen con un analizador de anidamiento, no con una expresión regular

Una expresión regular cortaba el pasaje en el primer cierre. Una cita de la
T-847/00 contenía «un “camastro”»; la palabra alterada iba detrás y la
alteración se daba por buena con un 100 %.

Y las comillas desparejadas («““En el caso…”», una comilla de más al copiar)
hacían que el pasaje se saltara **en silencio**: 18 de 480 en el banco
grande. Ahora el pasaje va de la apertura más externa al último cierre, sin
cruzar párrafos.

### 4. Un pasaje solo se atribuye a la cita que lo cierra si están en el mismo párrafo

Un pasaje de la T-760/08 («En la misma providencia la Corte precisó: “…”»)
acababa atribuido a la T-406/92 solo porque esta abría el párrafo siguiente.
La cita que va delante puede estar en el párrafo anterior; la que va detrás
tiene que estar en el mismo.

### 5. El número de expediente no es una sentencia

«Expediente T-7.123.456» tiene la misma letra. Es el falso positivo más
frecuente en un escrito de tutela. Una sentencia nunca pasa de cuatro cifras
ni lleva puntos de millar.

### 6. La relatoría responde 200 también para lo que no existe

Devuelve una página casi vacía (~105 palabras) en vez de un 404. Se reconoce
por longitud **y** porque no menciona el número de la sentencia.

### 7. La ausencia en la relatoría se recuerda solo un día

Una sentencia recién dictada puede tardar en subirse. Guardar su ausencia
para siempre la marcaría como inexistente indefinidamente.

### 8. Las providencias con dos ponentes se comparan por separado

La C-355/06 tiene «Clara Inés Vargas Hernández; Jaime Araujo Rentería».
Juntar los nombres daría por buena una mezcla como «Clara Araujo».

### 9. Lo que no se puede verificar se lista siempre

Corte Suprema, Consejo de Estado, autos, radicados: se detectan y aparecen
como **no verificables**, con el motivo. Un verificador que calla lo que no
mira es peor que ninguno, porque el abogado cree que se revisó todo.

---

## Trampas del entorno

**Cotejo tiene su propio entorno virtual.** El Python global de esta máquina
tiene starlette 1.6 (lo instaló streamlit el 14 de septiembre), que es
incompatible con fastapi 0.115. Con el entorno propio, ningún otro proyecto
le rompe a este las dependencias. Ojo: ese mismo conflicto tiene roto ahora
mismo el servidor de otro proyecto, que usa el Python global.

**El shell corrompe las secuencias de escape** al escribir código por
heredoc: `\b` acaba en 0x08, que compila y deja el regex muerto sin avisar.
Escribe el código con Write/Edit. Hay una prueba que lee cada fuente en
binario (`TestIntegridadDelFuente`).

**La consola de Windows es cp1252.** `PYTHONIOENCODING=utf-8` antes de
imprimir texto con tildes.

**Cortesía con los servidores públicos.** La relatoría se consulta a una
petición por segundo como mucho, y todo se guarda en `.cache/`.

---

## Pendiente, por orden de valor

1. **Corte Suprema de Justicia.** Es la fuente del caso que lo motivó todo
   (AC739-2026) y hoy no se verifica. No publica un índice abierto: habrá que
   consultar su buscador (`cortesuprema.gov.co/corte/index.php/rl_aplicativo/`),
   que es más frágil.
2. **Consejo de Estado.** Mismo problema, buscador propio.
3. **Leyes y decretos**, contra SUIN-Juriscol: número, año, y si el artículo
   citado existe y está vigente.
4. **Afirmaciones sin comillas.** «La Corte sostuvo que X» sin cita literal.
   Necesita un modelo de lenguaje que lea el fragmento real de la sentencia y
   diga si respalda la afirmación. Es la función de más valor que queda, y la
   más difícil de hacer bien sin falsos positivos.
5. **Informe en PDF** firmable, para adjuntar como prueba de verificación.
6. **Otras jurisdicciones:** Argentina, España, México. Cada una necesita su
   propio conector de datos; ese trabajo es el foso del producto.

## Lo que no se puede hacer

- Garantizar que una sentencia que existe se citó **en el sentido correcto**
  cuando no hay pasaje textual. Hoy se comprueba existencia, ponente y
  pasajes literales; el sentido de una paráfrasis, no.
- Verificar jurisprudencia que no está publicada en abierto.
