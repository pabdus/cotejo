# Estado del proyecto

Documento de traspaso: lo que hace falta saber para retomar el trabajo sin
haber estado en la conversación que lo construyó. El README explica **qué**
hace Cotejo; esto explica **por qué está hecho así** y **qué no se debe
deshacer**.

Última actualización: 21 de octubre de 2026 (demo pública en AWS; ver
`demo/DISEÑO.md` para su contrato y la sección «La demo pública» abajo).

---

## De un vistazo

| | |
|---|---|
| Ubicación | `C:\dev\Cotejo` |
| Entorno | `.venv` propio (ver «Trampas del entorno») |
| Interfaz web | `.venv\Scripts\python servidor.py` → <http://127.0.0.1:8010> |
| Línea de órdenes | `.venv\Scripts\python -m cotejo escrito.docx --html informe.html` |
| Pruebas | `.venv\Scripts\python -m unittest discover -s tests` — 274, todas pasando (6 omitidas: las de red) |
| Pruebas con red | `COTEJO_RED=1` delante — 6 más, contra las fuentes reales |
| Evaluación | `evaluacion/banco.py`, `banco_csj.py` y `escritos_llm.py` |
| Demo pública | `demo/` (Lambda) e `infra/` (Terraform); contrato en `demo/DISEÑO.md` |

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
demo/
  DISEÑO.md                      Contrato de la demo pública, ronda a ronda
  lambda_function.py             Handler de la Function URL: rutas, topes, sonda
  cuota.py                       Límite por IP (HMAC con sal y fecha) y topes diarios
  espejo.py                      Caché de 30 días en S3 alrededor de .cache/
  empaquetar.py                  build/lambda.zip y build/indice.json.gz
  exportar_ejemplos.py           Los 3 ejemplos de la página, con su veredicto comprobado
  local.py                       La misma Lambda en 127.0.0.1:8011, sin AWS
  ejemplos/                      Limpio, sentencias inventadas, pasaje alterado
  pagina/                        La página del portafolio (borrador) y su vista previa
infra/
  *.tf                           Terraform de la demo (estado remoto en S3)
  freno/handler.py               Pone la concurrencia a 0 si se disparan las invocaciones
tests/datos/                     Bytes reales de la relatoría para las pruebas sin red
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

Devuelve una página casi vacía en vez de un 404. Antes eran ~105 palabras
de cabecera; desde que el sitio se rehízo (septiembre de 2026) es la carcasa
vacía de la aplicación, 8.607 bytes y ninguna palabra, para cualquier
dirección desconocida.

Desde octubre de 2026 la carcasa se reconoce **por su firma**
(`cc._es_carcasa`): cero palabras **y** `<app-root>` **y** un `<title>` con
«CORTE CONSTITUCIONAL». No basta con «cero palabras»: un desafío de cortafuegos
hecho solo de `<script>` también las deja, y se tomaba por una ausencia. Un
200 vacío y un 404 tampoco son una ausencia (la relatoría nunca da 404 a lo que
no tiene; un 404 viene de un intermediario). Cualquier respuesta que no sea la
sentencia, la nota de nulidad o la carcasa firmada lanza
`RelatoriaNoDisponible`: «sin confirmar», y no se guarda nada. Si un día el
sitio cambia su carcasa, las inventadas saldrán «sin confirmar» (seguro) y la
sonda de la demo lo dirá.

### 6b. Canario antes de creer una ausencia

Consecuencia de lo anterior: si el sitio cambiara su esquema de direcciones,
TODA consulta devolvería la carcasa, y cada sentencia que aún no esté en el
índice saldría «no existe». Antes de aceptar una ausencia se comprueba que la
T-406/92 se sigue pudiendo leer (`cc.relatoria_fiable`), con un canario de
**menos de 2 minutos** (antes valía una hora: un desafío servido dentro de esa
hora se guardaba como ausencia, y en la demo el espejo lo repartía a todos los
contenedores). Si no, «sin confirmar». Lo mismo con la Corte Suprema y la
SC5686-2018 (`cs.buscador_fiable`, que sigue con una hora).

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

Lo que se aprendió después (octubre de 2026):
- **Páginas de Word con cabecera enorme.** La C-355/06, la SU-214/16 y la
  SU-1073/12 traen más de 30 KB de `<style>` antes del texto: con 30 KB queda
  una palabra. Si la primera ventana se llena sin reconocerse, se pide una
  segunda de 300 KB (basta para las tres, medido), y solo si esa tampoco basta,
  la página entera (la C-355/06 son 3,4 MB). `texto_relatoria`, que ya baja la
  página entera para los pasajes, deja la nota escrita de paso.
- **Nulidad parcial.** La SU-1073/12 perdió dos numerales y su texto sigue en
  su sitio; `/Anuladas/` tiene la carcasa. Se usa el texto original y el
  informe dice «ANULADA PARCIALMENTE».
- **La nulidad se busca en el párrafo de la nota**, en cada «NOTA DE
  RELATORÍA» del principio. Detrás vienen los descriptores, que pueden hablar
  de cualquier nulidad, y la relatoría añade notas de corrección o de
  anonimización años después.
- **Una nota de nulidad guardada no caduca**; el «no anulada» sí, a los 30 días.
- **Si no se pudo mirar, se dice.** Un fallo al consultar la nulidad añade la
  observación «No se pudo comprobar si la sentencia fue anulada» (queda «con
  observaciones»). Antes salía «verificada» en silencio, también la SU-163/23
  con la relatoría dando 403.

### 13. Del texto de un PDF solo se cree lo que coincide

Algunas providencias de la Corte Suprema solo están en PDF, a veces con un OCR
ilegible. Si un pasaje coincide ahí, coincide; si no, puede ser el OCR, y
marcarlo como alterado sería acusar al escrito de algo que no hizo. Esos
pasajes quedan «sin comprobar», dicho así.

### 14. Una sentencia reciente que falta en las dos fuentes es «no encontrada»

El índice de datos.gov.co va con semanas de retraso (en octubre de 2026 llega al
31 de agosto) y la relatoría tarda semanas o meses en publicar algunas C y SU.
Una sentencia real de septiembre puede faltar en las dos, y salía «no existe»:
con el índice recortado al 12-08, la C-273/26 y la SU-261/26, reales, lo daban.
`cc.Indice.quiza_reciente` decide si el número puede ser de una sentencia aún
sin publicar: lo dictado hasta 60 días antes del final del índice está
asentado (si ahí falta, significa algo); por encima, solo es imposible si pasa
de lo que da el ritmo de la Corte con margen (el doble, más 20 números), con
mínimo de una sentencia al día, contando T, C y SU juntas (comparten serie desde
2023) y, para un año cerrado, solo hasta su 31 de diciembre. Simulado con el
índice real recortado cada día de 2024 a 2026: ninguna sentencia real que
faltara en el recorte se quedó sin motivo. Si puede ser reciente, el estado es
`NO_ENCONTRADA` (pide comprobarla, no afirma nada). El precio: hoy, un número
inventado de 2026 entre el 202 y el 534 sale «no encontrada» y no «no existe».
Las inventadas de los escritos del LLM no cambian (0 de 63).

### 15. El cortacircuitos también ve los bloqueos que llegan con 200

Un 403, una redirección y una respuesta de más de 16 MB son fallos del servicio
(sin reintentar). Una página de desafío llega con 200, y `pedir()` ponía los
fallos a 0 en cada una: el cortacircuitos no se abría nunca. Quien lee la
respuesta y no la reconoce llama a `Cliente.respuesta_irreconocible()`, que
corta igual a la tercera seguida. «La relatoría no tiene publicada su página»
no cuenta: no es culpa del servidor.

### 16. Plazo y tope por verificación

`verificar(hasta=..., max_consultadas=...)`. El plazo llega al cliente HTTP
(`Cliente.hasta`): ni una pausa, ni un intento, ni un trozo del cuerpo empiezan
si no caben, y quedarse sin tiempo (`SinTiempo`) no cuenta para el
cortacircuitos. Lo que no se alcanzó a consultar sale «sin confirmar» con el
motivo, nunca se omite (decisión 9). El tope existe porque un texto con cientos
de sentencias inventadas hacía decenas de peticiones a la relatoría con una
sola consulta. Sin plazo ni tope (los bancos, la interfaz local) todo sigue
como antes.

### 17. Las claves de la Constitucional van normalizadas

«T-25 de 2004» y «T-025 de 2004» son la misma providencia (`T-025/2004`): antes
salían dos hallazgos y contaban dos veces para los topes. La Corte Suprema ya
se normalizaba.

---

## La demo pública

`demo/DISEÑO.md` es el contrato completo, con el porqué de cada decisión y lo
que se encontró en cada una de las cuatro rondas de revisión adversarial. Lo
esencial:

- Lambda con Function URL en la cuenta personal (perfil `default`, us-east-1).
  Recibe un texto pegado de hasta 5.000 caracteres y devuelve el informe en JSON.
  La página está en `pabloduque.dev/lab/cotejo.html` (borrador en `demo/pagina/`).
- Antes de construir se midió desde Lambdas de prueba en us-east-1 y sa-east-1
  (20-10-2026): la relatoría, datos.gov.co y el buscador de la Corte Suprema
  responden a las IP de AWS. Sin bloqueos.
- Límites: 3 consultas por IP al día (HMAC-SHA256 de la IP con sal secreta y la
  fecha, IPv6 por su /64, TTL de 2 días; nunca la IP en claro), 100 consultas y
  600 peticiones a las fuentes al día en total, 40 providencias por texto y 15
  consultadas, 60 s de plazo. Concurrencia reservada 1: la pausa de cortesía
  vive en la memoria de cada contenedor, y con dos a la vez la relatoría
  recibía el doble de ritmo.
- Caché de 30 días en S3 (`espejo.py`) alrededor de `.cache/`, sin tocar su
  lógica de caducidades. Las ausencias no se suben: así el error de un
  contenedor no se reparte a los demás.
- Costo: presupuesto de US$ 5/mes por la etiqueta `proyecto`, alarma por
  invocaciones y un freno automático que pone la concurrencia a 0.
- `demo/local.py` levanta la misma Lambda en local, sin AWS, para probar la página.

---

## Trampas del entorno

**Cotejo tiene su propio entorno virtual.** El Python global de esta máquina
tiene starlette 1.6 (lo instaló streamlit el 14 de septiembre), que es
incompatible con fastapi 0.115. Con el entorno propio, ningún otro proyecto
le rompe a este las dependencias. El paquete de la Lambda no lleva fastapi:
solo rapidfuzz (`demo/empaquetar.py`).

**Las pruebas siempre con límite de tiempo.** Una lectura por trozos contra un
doble de prueba que nunca devuelve `b""` dejó la batería colgada; con
`timeout 240` delante se ve enseguida.

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

### Pendiente de la demo

- **Antes de publicar el repo:** el historial de git nombra en 8 sitios el
  proyecto del que viene `documentos.py` (ya no está en el árbol actual).
  Publicarlo exige reescribir el historial o un repo nuevo con un solo commit.
  Decisión de Pablo.
- Tras el primer `apply`: lanzar la sonda, probar el freno de forma controlada,
  confirmar la suscripción de SNS por correo, y comprobar si el 429 de la
  Function URL (concurrencia 1 o freno) lleva cabeceras CORS. Si no las lleva,
  la página dirá «sin conexión» donde debería decir «ocupado».
- Huecos del tramo asentado del índice (en 2026, los números 45, 85, 129, 130 y
  139) siguen dando «no existe» si la relatoría tampoco los tiene. No se ha
  comprobado si alguno es una sentencia real que falta en el índice.
- La C-355/06 en la sonda vigila la segunda ventana de la nulidad; si un día
  deja de bastar con 300 KB, cada nulidad de una página así baja la entera.

## Lo que no se puede hacer

- Garantizar que una sentencia que existe se citó **en el sentido correcto**
  cuando no hay pasaje textual. Hoy se comprueba existencia, ponente y
  pasajes literales; el sentido de una paráfrasis, no.
- Verificar jurisprudencia que no está publicada en abierto.
