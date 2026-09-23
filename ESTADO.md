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
| Pruebas | `.venv\Scripts\python -m unittest discover -s tests` — 88, todas pasando |
| Pruebas con red | `COTEJO_RED=1` delante — 6 más, contra las fuentes reales |
| Evaluación | `evaluacion/banco.py`, `banco_csj.py` y `escritos_llm.py` |

Qué es: un verificador de citas jurisprudenciales. Lee un escrito, extrae las
citas y comprueba cada una contra las fuentes oficiales de la Corte
Constitucional y de la Corte Suprema de Colombia. Nació después de que la Corte Suprema sancionara
en febrero de 2026 (AC739-2026) a un abogado por citar diez sentencias
inventadas por IA, estableciendo un «deber positivo de verificación».

---

## Mapa de módulos

```
cotejo/
  extraccion.py                  Qué es una cita y qué no. Comillas anidadas.
  fuentes/red.py                 Cliente HTTP cortés: pausa, reintentos, cortacircuitos
  fuentes/corte_constitucional.py  Índice de datos.gov.co + relatoría + anulaciones
  fuentes/corte_suprema.py       Buscador GraphQL de la Corte Suprema
  comparacion.py                 ¿Dice la sentencia lo que se cita? Palabra a palabra
  verificacion.py                Reglas de decisión y estados
  informe.py                     Terminal, JSON e informe HTML
  web.py + static/index.html     Interfaz local
  documentos.py                  Importar docx, pdf, odt...
evaluacion/
  banco.py                       Constitucional: etiquetas conocidas por construcción
  banco_csj.py                   Corte Suprema: ¿cuántas reales no encuentra el buscador?
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
afectadas quedaron «sin confirmar», ninguna como «no existe». Repetida con la
relatoría accesible, las 58 «no existe» de los escritos del LLM se auditaron
a mano, con controles positivos: todas correctas.

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

Devuelve una página casi vacía en vez de un 404. Se reconoce por longitud
**y** porque no menciona el número de la sentencia. Antes eran ~105 palabras
de cabecera; desde que el sitio se rehízo (septiembre de 2026) es la carcasa
vacía de la aplicación, 8.607 bytes y ninguna palabra, para cualquier
dirección desconocida.

### 6b. Canario antes de creer una ausencia

Consecuencia de lo anterior: si el sitio cambiara su esquema de direcciones,
TODA consulta devolvería la carcasa, y cada sentencia que aún no esté en el
índice saldría «no existe». Antes de aceptar una ausencia se comprueba, como
mucho una vez por hora, que la T-406/92 se sigue pudiendo leer
(`cc.relatoria_fiable`). Si no, «sin confirmar». Lo mismo con la Corte
Suprema y la SC5686-2018 (`cs.buscador_fiable`).

### 7. La ausencia en la relatoría se recuerda solo un día

Una sentencia recién dictada puede tardar en subirse. Guardar su ausencia
para siempre la marcaría como inexistente indefinidamente.

### 8. Las providencias con dos ponentes se comparan por separado

La C-355/06 tiene «Clara Inés Vargas Hernández; Jaime Araujo Rentería».
Juntar los nombres daría por buena una mezcla como «Clara Araujo».

### 9. Lo que no se puede verificar se lista siempre

Consejo de Estado, autos de la Constitucional, radicados, citas por fecha: se
detectan y aparecen como **no verificables**, con el motivo. Un verificador
que calla lo que no mira es peor que ninguno, porque el abogado cree que se
revisó todo.

### 10. En la Corte Suprema, una ausencia es «no encontrada», nunca «no existe»

La Corte Suprema no tiene un índice abierto con el que hacer la doble
comprobación: solo su buscador. Se midió cuánto le falta con citas que
seguro son reales, las que hace la propia Corte en sus providencias
(`evaluacion/banco_csj.py`): de 135 códigos, **6 providencias reales no
estaban** (4,4 %), todas autos o tutelas, y una era errata de la Corte.
Declarar inexistente lo que no aparece acusaría en falso a uno de cada
veinte escritos correctos. De ahí el estado `NO_ENCONTRADA`: cuenta como
problema (pide comprobarla, con un enlace a la búsqueda ya hecha) pero no
afirma nada que no se sepa.

### 11. El buscador de la Corte Suprema busca en el texto, no en el título

Buscar «AP7408-2015» devolvía 0 resultados, y la providencia existe: su
título es «AP7408-2015(44163).doc» pero su texto dice «AP – 7408 - 2015», y
el título no se indexa. Se buscan cuatro formas: compacta y con ceros
(«SP022-2019»), y separadas por espacios. Además, como la búsqueda es de
texto completo, una providencia muy citada queda DETRÁS de todas las que la
citan (AP2118-2020: posición 149 de 155). Los resultados van de lo último
subido a lo primero, y lo citado se subió antes, así que se mira la primera
página y luego las últimas hacia atrás: dos peticiones en vez de dieciséis.

### 12. Sentencias anuladas

La SU-163/23 fue anulada por el Auto 823 de 2024. El índice oficial la sigue
listando; la relatoría sustituye su página por una NOTA DE RELATORÍA y mueve
el texto a `/relatoria/Anuladas/`. Se descubrió porque el banco de pasajes la
descartó por «sin texto» y esta vez se contaban los descartes. Cotejo lee los
primeros 30 KB de la página de cada sentencia verificada (cabecera `Range`;
el servidor responde 206) y recuerda la respuesta 30 días: una sentencia
puede anularse un año después.

### 13. Del texto de un PDF solo se cree lo que coincide

Algunas providencias de la Corte Suprema solo están en PDF, a veces con un OCR
ilegible. Si un pasaje coincide ahí, coincide; si no, puede ser el OCR, y
marcarlo como alterado sería acusar al escrito de algo que no hizo. Esos
pasajes quedan «sin comprobar», dicho así.

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

**Cortesía con los servidores públicos.** La relatoría se consulta cada 1,5 s
como mucho y el buscador de la Corte Suprema cada 1 s; todo se guarda en
`.cache/`. Los bancos de evaluación hacen cientos de peticiones: no lanzar
dos a la vez contra el mismo servidor.

**Las pruebas sin red no pueden tocar la red.** La consulta de anulación se
anula para todo el módulo de pruebas (`setUpModule`); una prueba nueva que
verifique una sentencia existente la haría si no.

---

## La API de la Corte Suprema, para no redescubrirla

`POST https://consultaprovidenciasbk.cortesuprema.gov.co/api`, GraphQL, sin clave.

- `getSearchResult(searchQuery: {query, typeOfQuery, start, isExact, magistrate,
  year, autoSentencia, order, roomTutelas, addedQueries})`. `typeOfQuery` es la
  sala: CIVIL, LABORAL, PENAL, TUTELAS. `start` es desplazamiento, de 10 en 10.
  `order` se ignora. `addedQueries: [{text, isExact}]` añade condiciones.
- `getContentSearch(previewDocument: {id: <onlinePath>, room, text})` → el texto
  en HTML.
- `/filters` → salas, años y magistrados.
- Visor para el abogado: `https://consultaprovidencias.cortesuprema.gov.co/#/visualizador/<base64 latin-1 de onlinePath, sin escapar>/<SALA>/<código>`.
  Búsqueda: `#/resultados-busqueda/%22<código>%22/Civil`.
- Retraso de publicación: en septiembre de 2026, lo último de la Sala Civil
  era de finales de abril. Un año se da por completo el 1 de julio siguiente.

## Pendiente, por orden de valor

1. **Consejo de Estado.** Mismo problema que la Corte Suprema: buscador propio.
2. **Citas de la Corte Suprema por fecha y radicado** («CSJ SC, 12 dic. 2017,
   rad. 2008-00123»), que es como se citaba antes de los códigos. El buscador
   encuentra por radicado (el título de Penal lo lleva: «AP7408-2015(44163)»).
3. **Ponente de la Corte Suprema desde el texto.** El buscador no da ponente
   para algunas (AP2118-2020) y da dos para otras (STP11082-2021, donde el
   texto dice que fue Eugenio Fernández Carlier). La cabecera del documento lo
   dice: «EUGENIO FERNÁNDEZ CARLIER / Magistrado Ponente».
4. **Leyes y decretos**, contra SUIN-Juriscol: número, año, y si el artículo
   citado existe y está vigente.
5. **Afirmaciones sin comillas.** «La Corte sostuvo que X» sin cita literal.
   Necesita un modelo de lenguaje que lea el fragmento real de la sentencia y
   diga si respalda la afirmación. Es la función de más valor que queda, y la
   más difícil de hacer bien sin falsos positivos.
6. **Informe en PDF** firmable, para adjuntar como prueba de verificación.
7. **Escritos de modelos de frontera** (ChatGPT, Gemini, Claude) para la
   evaluación: la de qwen3.5:9b dice poco de lo que inventa un modelo grande.
8. **Otras jurisdicciones:** Argentina, España, México. Cada una necesita su
   propio conector de datos; ese trabajo es el foso del producto.

## Lo que no se puede hacer

- Garantizar que una sentencia que existe se citó **en el sentido correcto**
  cuando no hay pasaje textual. Hoy se comprueba existencia, ponente y
  pasajes literales; el sentido de una paráfrasis, no.
- Verificar jurisprudencia que no está publicada en abierto.
