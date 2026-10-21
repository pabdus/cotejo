# Demo pública «Cotejo en vivo»: diseño

Una Lambda con Function URL en la cuenta personal (perfil `default`, us-east-1)
recibe un texto pegado (máximo 5.000 caracteres), lo verifica con Cotejo y
devuelve el informe en JSON. La página que lo muestra vive en
`pabloduque.dev/lab/cotejo.html` y llama a la Function URL con `fetch` (CORS).

Este documento es el contrato entre las piezas: núcleo, Lambda, infraestructura
y página. Si una pieza cambia el contrato, cambia este archivo.

## Principios que no se negocian (de ESTADO.md)

1. «No existe» solo con dos fuentes; un fallo de red nunca lo produce.
   En AWS hay un riesgo nuevo: que un sitio bloquee los rangos de AWS. Un
   bloqueo (403, página de desafío, conexión cortada) tiene que acabar en
   «sin confirmar», y no puede quedar guardado en la caché como si fuera una
   respuesta de la Corte.
2. Cliente HTTP cortés con cortacircuitos (`cotejo/fuentes/red.py`).
3. Lo que no se verifica se lista, con el motivo. Si la demo corta por tiempo,
   las citas que faltan salen «sin confirmar» con ese motivo, nunca se omiten.
4. Privacidad: el texto se procesa en memoria y no se guarda en ningún sitio.
   Los registros llevan conteos y tiempos, nunca texto ni IP. La IP solo se usa
   para el límite diario, como HMAC-SHA256 con sal secreta y con la fecha
   (no se pueden enlazar dos días), y con TTL de 2 días en DynamoDB.

## 1. Cambios en el núcleo (`cotejo/`)

Mínimos, cada uno con prueba en `tests/test_cotejo.py`:

- `cc.ruta_nota(tipo, numero, anio) -> Path`: la ruta que hoy se calcula dentro
  de `nota_de_nulidad`. `nota_de_nulidad` la usa.
- `cc._ruta_texto` se queda; se añade el alias público `cc.ruta_texto`.
- `cc.nota_de_nulidad` solo guarda en caché una respuesta reconocible: si
  encontró la nota de nulidad, o si el texto parece la sentencia
  (`_parece_la_sentencia`, que exige el número y 400 palabras; con 30 KB de
  cabecera basta). Si la página no se reconoce (carcasa vacía en las dos
  direcciones, página de bloqueo), devuelve `None` SIN escribir caché.
  Motivo: en AWS, una página de bloqueo guardada 30 días escondería una
  nulidad durante 30 días.
- `cs.ruta_busqueda(prefijo, numero, anio) -> Path` y `cs.ruta_texto(id) -> Path`
  (id como `ProvidenciaCSJ.id`, p. ej. "SC5686-2018"). `buscar` y `texto` las usan.
- `red.Cliente`:
  - un 403 cuenta como fallo para el cortacircuitos y lanza
    `ServicioNoDisponible` en el acto, sin reintentar (reintentar contra un
    bloqueo es descortés y no sirve). Hoy un 403 lanza `HTTPError` sin contar.
  - contador `peticiones` (int, se incrementa en cada intento de red real) para
    medir cuántas peticiones hace cada consulta a cada fuente.
- `verificacion.verificar(..., hasta: float | None = None)`: plazo en
  `time.monotonic()`. Antes de verificar cada providencia, si el plazo pasó, esa
  y las siguientes salen `SIN_CONFIRMAR` con motivo
  «no se alcanzó a consultar: la verificación llegó a su tiempo máximo». Las
  `NO_VERIFICABLE` se siguen clasificando igual (no consultan nada).

### 1b. Segunda ronda (tras la revisión adversarial)

- **Ausencias en la relatoría.** `texto_relatoria` solo acepta una ausencia si
  todas las direcciones devolvieron la carcasa vacía (cero palabras tras
  `_de_html`; desde 1c, con su firma, y desde 1d el 404 ya no vale), **y** el canario se comprobó hace menos de 2 minutos
  (`_CANARIO_VALIDEZ = 120`; antes, 1 hora). Una página con texto que no es la
  sentencia (desafío de un cortafuegos, redirección a otra cosa) lanza
  `RelatoriaNoDisponible`, sin escribir caché. Motivo: con el canario memorizado
  una hora, un desafío servido dentro de esa hora se guardaba como ausencia y el
  espejo lo repartía a todos los contenedores durante 24 h (reproducido).
- **Nulidad que no se pudo mirar.** `nota_de_nulidad` lanza
  `RelatoriaNoDisponible` si no reconoce la respuesta: página con texto que no
  es ni la nota ni la sentencia («puede estar bloqueando esta conexión»), o
  carcasa/404 en todas las direcciones («la relatoría no tiene publicada su
  página»). No escribe caché en ningún caso de esos. Una nota positiva guardada
  no caduca (una anulación no se deshace). `_verificar_cc` convierte cualquier
  fallo de la nulidad en la observación «No se pudo comprobar si la sentencia
  fue anulada: <motivo>.», con lo que el hallazgo queda `CON_OBSERVACIONES`,
  como ya pasa cuando no se obtiene el texto para los pasajes.
- **Plazo dentro de una providencia.** `red.SinTiempo(ServicioNoDisponible)` y
  `Cliente.hasta: float | None` (monotonic). `pedir()` comprueba el plazo antes
  de cada pausa y de cada intento; si la pausa o el intento no caben, lanza
  `SinTiempo` sin contar para el cortacircuitos (no es culpa del servidor). El
  tiempo de espera de cada intento es `min(tiempo_espera, hasta - ahora)`, y si
  se agota por eso también es `SinTiempo`. `Cliente.cortes_por_tiempo` cuenta
  cuántas veces se lanzó. `verificar(hasta=...)` fija `hasta` en `cc.cliente` y
  `cs.cliente` mientras dura (y lo restaura en un `finally`); si durante una
  providencia aumentó `cortes_por_tiempo` de alguno, esa providencia sale
  `SIN_CONFIRMAR` con `MOTIVO_SIN_TIEMPO` y el informe `cortado_por_tiempo`.
- **Tope de providencias.** `verificar(..., max_consultadas: int | None = None)`:
  como mucho esas providencias que necesitan red; las siguientes salen
  `SIN_CONFIRMAR` con `MOTIVO_TOPE` = «no se consultó: esta verificación consulta
  como mucho N providencias por texto» (con N), e `Informe.cortado_por_tope = True`.
  Motivo: una consulta con 446 sentencias inventadas hacía 41 peticiones a la
  relatoría con una sola unidad de cuota.
- **Cuerpo cortado.** `http.client.HTTPException` (IncompleteRead, BadStatusLine)
  se trata como un fallo transitorio: reintento y cortacircuitos.

### 1c. Tercera ronda

- **La carcasa se reconoce por su firma, no por «cero palabras».** Un desafío de
  cortafuegos hecho solo de `<script>` también deja cero palabras tras `_de_html`.
  La carcasa real (la aplicación del sitio, la misma para cualquier dirección
  desconocida: 8.607 bytes) tiene cero palabras **y** `<app-root` **y** un
  `<title>` que contiene «CORTE CONSTITUCIONAL». `cc._es_carcasa(crudo: bytes) -> bool`.
  Una ausencia solo vale con carcasa reconocida (el 404 dejó de valer en 1d) en todas las direcciones y
  canario de menos de 2 minutos; cualquier otra cosa que no sea la sentencia lanza
  `RelatoriaNoDisponible`. Si el sitio cambia su carcasa, las inventadas salen
  «sin confirmar» (seguro) y la sonda lo dice (`relatoria_inexistente` no ok).
- **Páginas de Word con cabecera enorme.** La C-355/06, la SU-214/16 y la
  SU-1073/12 traen más de 30 KB de `<style>` antes de `</head>`: con `Range` de
  30 KB el texto queda en una palabra. `nota_de_nulidad`: si la respuesta con
  `Range` llena la ventana y no se reconoce, pide la página entera y la reconoce
  con las mismas reglas. Además, `texto_relatoria`, que ya baja la página entera,
  escribe `ruta_nota` como subproducto (con la nota o con `null`), para que
  `nota_de_nulidad` la lea de la caché sin una segunda descarga.
- **Nulidad parcial.** Si la página tiene NOTA DE RELATORÍA pero `/Anuladas/` no
  tiene la sentencia y la página original sí parece la sentencia (SU-1073/12:
  nulidad parcial, el texto sigue en su sitio), se usa el texto original y se
  guarda. Solo se lanza si ninguna de las dos es la sentencia. La observación
  dice «La sentencia fue ANULADA PARCIALMENTE» cuando la nota habla de nulidad
  parcial, y «La sentencia fue ANULADA» en otro caso.
- **El cortacircuitos ve los desafíos con 200.** `Cliente.respuesta_irreconocible()`
  (suma a `raras_seguidas`, que `pedir()` no reinicia; al llegar a
  `fallos_para_cortar` corta como `_fallo`) y `Cliente.respuesta_valida()` (la
  pone a 0). Se llama a la primera cuando la relatoría sirve una página con
  texto o sin firma que no es ni la sentencia, ni la nota, ni la carcasa, y
  cuando el canario falla; a la segunda cuando se reconoce la sentencia, la nota
  o la carcasa con canario vivo. «La relatoría no tiene publicada su página» no
  cuenta: no es culpa del servidor.
- **Claves de la Constitucional normalizadas.** `Cita.clave` usa el número
  normalizado como el índice (`T-25 de 2004` y `T-025 de 2004` → `T-025/2004`),
  igual que ya hace la Corte Suprema. Así no cuentan dos veces para los topes
  ni salen duplicadas en el informe.
- **Bancos de evaluación (1c).** `evaluacion/banco.py` atrapa `RelatoriaNoDisponible`
  en `evaluar_pasajes` y lo cuenta entre los descartes («sin respuesta
  reconocible de la relatoría»); si el cortacircuitos se abre, corta el muestreo
  con un mensaje.

### 1d. Cuarta ronda

- **Sentencias recientes que el índice aún no recoge.** El índice de datos.gov.co
  va con retraso (hoy llega al 31-08-2026) y la relatoría tarda semanas o meses
  en publicar algunas C y SU: una sentencia real de septiembre puede faltar en
  las dos fuentes. Con el índice recortado al 12-08, la C-273/26 y la SU-261/26
  salían «no existe» (reproducido). Regla, en `cc.Indice.quiza_reciente(tipo,
  numero, anio, hoy=None) -> str | None` (devuelve el motivo o None):
  - `fin` = `fecha_mas_reciente`. Si `anio < fin.year - 1` o `anio > hoy.year`: None.
  - `umbral` = mayor número del mismo tipo y año entre las sentencias fechadas
    hasta `fin - 60 días` (0 si no hay). Si `numero <= umbral`: None (ese tramo
    ya está asentado en el índice y la ausencia significa algo).
  - `ritmo` = sentencias de ese tipo y año en el índice / días del año hasta `fin`
    (si no hay, las del año anterior / 365; mínimo 0,1 por día).
  - `dias` = días entre `max(1 de enero de anio, fin - 60 días)` y `hoy`.
  - Si `numero <= umbral + 2 × ritmo × dias + 20`: devuelve el motivo. Si no,
    None: un número imposible para la fecha (la T-981/26 inventada) sigue
    siendo «no existe».
  En `_verificar_cc`, cuando no está en el índice y la relatoría tampoco la tiene,
  si `quiza_reciente` da motivo, el estado es `NO_ENCONTRADA` (pide comprobarla,
  no afirma que no exista) con el motivo: «no figura en el índice oficial, que
  llega hasta el <fin>, ni en la relatoría; puede ser una sentencia reciente que
  aún no se ha publicado. Compruébela antes de radicar».
- **Un 404 de la relatoría no es una ausencia.** La relatoría responde la carcasa,
  nunca 404, a lo que no tiene (comprobado en cinco direcciones). Un 404 viene de
  un intermediario: cuenta como página irreconocible (`RelatoriaNoDisponible`,
  `respuesta_irreconocible()`), sin caché. La única ausencia válida es la carcasa
  firmada en todas las direcciones con canario de menos de 2 minutos.
- **Nulidad solo en el párrafo de la nota.** «nulidad» se busca solo en el
  párrafo que sigue a «NOTA DE RELATORÍA» (hasta la primera línea en blanco; si
  ese párrafo tiene menos de 4 palabras, el siguiente). Una nota de corrección o
  de anonimización seguida de descriptores que dicen «nulidad» no es una anulación.
- **Lectura con plazo y tope de tamaño.** `red.Cliente.pedir` lee el cuerpo por
  trozos de 64 KB y comprueba `hasta` entre trozos (si no cabe: `SinTiempo`), con
  un tope de 16 MB por respuesta (pasarlo cuenta como fallo). En `nota_de_nulidad`,
  antes de la página entera, una segunda ventana con `Range: bytes=0-299999`
  (basta para la C-355/06, la SU-214/16 y la SU-1073/12, medido); la página
  entera solo si esa también se llena sin reconocerse.

Añadidos al implementar y revisar la §1d (todos hacia el lado seguro):
- `quiza_reciente` mira también los números de T, C y SU juntos (desde 2023
  comparten una sola serie) y basta con que una de las dos cuentas dé motivo; el
  ritmo es el mayor entre el del año y el del anterior, con mínimo de 1 al día
  (el de la Corte no ha bajado de 1,21 desde 2015; con 0,1, un índice parado daba
  falsos «no existe»). Para un año cerrado, los días se cuentan hasta su 31 de
  diciembre. Simulado con el índice real recortado cada día de 2024 a 2026: ninguna
  sentencia real que faltara en el recorte se quedó sin motivo. El precio, con el
  índice de hoy: un número inventado de 2026 entre el 202 y el 534 sale «no
  encontrada» en vez de «no existe»; las inventadas de los ejemplos no cambian.
- Un trozo del cuerpo puede pasarse del plazo como mucho un tiempo de espera de
  socket (12 s, dentro de los 25 s de margen). Un `Content-Length` de más de 16 MB
  se rechaza sin leer.
- La nulidad se busca en el párrafo de cada «NOTA DE RELATORÍA» de los primeros
  6.000 caracteres, no solo de la primera.
- Una redirección es un fallo del servicio que cuenta el salto como petición: ninguna
  fuente redirige lo que se le pide. Lo que urllib lee del cuerpo del 3xx antes de
  llegar a `pedir()` queda fuera del plazo, acotado por el tiempo máximo de la Lambda.

## 2. Lambda (`demo/`)

Archivos:

- `demo/lambda_function.py` — `lambda_handler(event, context)`.
- `demo/espejo.py` — caché de 30 días en S3 alrededor de la caché en disco.
- `demo/cuota.py` — límites en DynamoDB.
- `demo/ejemplos/{limpio,inventadas,pasaje_alterado}.txt` y
  `demo/ejemplos/ejemplos.json` (metadatos y veredicto esperado).
- `demo/exportar_ejemplos.py` — verifica los 3 ejemplos con las fuentes reales
  y escribe el JSON que usa la página (ver §4).
- `demo/empaquetar.py` — arma `build/lambda.zip` (rapidfuzz como wheel de
  manylinux para Python 3.12 x86_64, el paquete `cotejo/` sin `web.py` ni
  `static/`, y `demo/*.py`) y `build/indice.json.gz` (el índice, desde la caché
  local si tiene menos de 7 días o descargado).
- `demo/local.py` — servidor local (stdlib `http.server`) en 127.0.0.1:8011 que
  convierte cada petición en un evento de Function URL y llama al handler con
  cuota en memoria y sin S3, con CORS para `http://localhost:8765`. Sirve para
  probar la página sin AWS.

### Variables de entorno

| Variable | Valor |
|---|---|
| `BUCKET` | bucket de la demo (índice y caché) |
| `TABLA_CUOTA` | tabla DynamoDB, clave `pk` (S), TTL en `expira` |
| `SAL_VISITANTE` | sal secreta (random_id de Terraform) |
| `MAX_CONSULTAS_IP` | `3` por IP y día (hora de Colombia, UTC−5) |
| `MAX_CONSULTAS_DIA` | `100` en total por día |
| `MAX_CARACTERES` | `5000` |
| `PLAZO_SEGUNDOS` | `60` (plazo de `verificar`) |
| `MAX_CITAS` | `40` providencias distintas por texto; más → `demasiadas_citas` |
| `MAX_PROVIDENCIAS` | `15` providencias consultadas por texto (`max_consultadas`) |
| `MAX_PETICIONES_DIA` | `600` peticiones a las fuentes por día, en total |
| `COTEJO_CACHE` | `/tmp/cotejo-cache` |

### Rutas (eventos de Function URL, payload 2.0)

- `GET /` → 200 `{"servicio": "cotejo-demo", "estado": "ok", "providencias": N,
  "indice_hasta": "AAAA-MM-DD", "indice_actualizado": "<ISO>", "max_caracteres": 5000,
  "max_consultas_ip": 3}`. La usa el chequeo de salud del portafolio.
- `POST /verificar` con cuerpo JSON `{"texto": "..."}` →
  - 200 `{"informe": <informe.a_json>, "segundos": float, "restantes": int|null,
    "indice": {"providencias": N, "hasta": "AAAA-MM-DD"},
    "peticiones": {"relatoria": n, "corte_suprema": m}, "cortado_por_tiempo": bool,
    "cortado_por_tope": bool}`.
  - Errores: `{"error": "<código>", "mensaje": "<texto en español>"}`, con
    códigos `mal_formada` (400), `texto_vacio` (422), `texto_largo` (413),
    `demasiadas_citas` (422, más de `MAX_CITAS` providencias distintas),
    `limite_ip` (429, `Retry-After` hasta la medianoche de Colombia),
    `limite_global` (429), `interno` (500).
- Cualquier otra → 404 `{"error": "ruta"}`. OPTIONS lo resuelve la Function URL (CORS).
- Eventos directos (sin `requestContext.http`; una Function URL nunca los produce):
  `{"accion": "actualizar_indice"}` (lo lanza EventBridge cada semana) y
  `{"accion": "sonda"}` (mide si las fuentes responden desde AWS; ver §5).

### Orden dentro de `POST /verificar`

1. Tamaño del cuerpo (> 64 KB → `mal_formada`), base64 si `isBase64Encoded`, JSON.
2. `texto` vacío → `texto_vacio`; `len(texto) > MAX_CARACTERES` → `texto_largo`.
3. Extraer citas. Más de `MAX_CITAS` providencias distintas → `demasiadas_citas`
   (acota también la respuesta de la ruta gratuita: 464 no verificables daban
   165 KB). Si no hay ninguna cita que haya que consultar (todas no
   verificables, o ninguna), se responde el informe SIN consumir cuota.
4. Plazo: `hasta = monotonic() + min(PLAZO, context.get_remaining_time_in_millis()/1000 - 25)`,
   tomado aquí, antes de la cuota y del espejo (los 25 s son para subir el espejo
   y responder).
5. Cuota: primero la de la IP (`ip#<fecha>#<hmac>`, máx. 3), luego la global
   (`dia#<fecha>`, máx. 100), luego el presupuesto de peticiones
   (`pet#<fecha>`, condición `n < MAX_PETICIONES_DIA` con `ADD n :cero`; si no
   cabe, `limite_global`). `UpdateItem` con `ADD n :uno`, condición
   `attribute_not_exists(n) OR n < :max`, `expira` = ahora + 2 días. Si la
   tabla falla (error de DynamoDB que no sea la condición), se responde
   `interno`: sin límite no se sirve.
   - La IP se normaliza antes del HMAC: una IPv6 cuenta por su /64 (una conexión
     doméstica tiene una /64 entera) y una IPv4 mapeada (`::ffff:a.b.c.d`) como
     su IPv4.
   - Caché negativa en la memoria del contenedor: una huella o un día ya
     agotados se rechazan sin escribir en DynamoDB hasta la medianoche.
6. Espejo: traer de S3 las entradas de caché que va a necesitar esta consulta.
   Por clave, `NoSuchKey`, 404 y `AccessDenied` son «no está» y se sigue con
   las demás; otro error se registra y se deja de bajar (S3 caído).
7. `verificar(texto, documento="Texto pegado", indice=..., hasta=hasta,
   max_consultadas=MAX_PROVIDENCIAS)`.
8. Espejo: subir a S3 lo nuevo o cambiado. Presupuesto: `ADD n :peticiones` en
   `pet#<fecha>` (sin condición) con las peticiones medidas.
9. Registro: una línea JSON `{"evento": "verificado", "citas": n, "estados": {...},
   "segundos": s, "peticiones": {...}, "cortado_por_tiempo": b, "cortado_por_tope": b}`.
   Nada más.

### Clientes HTTP en la Lambda

Al arrancar: `cc.cliente.tiempo_espera = cs.cliente.tiempo_espera = 12`,
`reintentos = 2`. Las pausas (1,5 s y 1 s) se quedan como están. El estado del
cortacircuitos vive en la memoria del contenedor.

**Concurrencia reservada 1** (tercera ronda). La pausa de cortesía vive en la
memoria de cada contenedor; con dos a la vez la relatoría recibía el doble de
ritmo (~1,4 peticiones/s), en contra de ESTADO («no lanzar dos a la vez contra el
mismo servidor»). Con 1, un segundo visitante simultáneo recibe 429 de la
Function URL y la página le dice que otra persona está verificando y que lo
intente en un minuto.

### Respuesta de `limite_global`

Lleva `"limite": "consultas"` (tope diario de consultas) o `"limite": "peticiones"`
(presupuesto diario de peticiones a las fuentes), y la página explica cada uno.

### Índice precalculado

En `s3://BUCKET/indice/corte_constitucional_indice.json.gz` (mismo formato que
`.cache/`). En el arranque en frío se lee de S3 y se construye `cc.Indice(filas)`
con `actualizado` = LastModified. Nunca se descarga de datos.gov.co dentro de
una consulta. `actualizar_indice` lo descarga (`cc.Indice._descargar()`), lo
acepta solo si tiene al menos el 95 % de las filas del anterior, y lo sube.
Si datos.gov.co bloquea a AWS, el índice se sube desde la máquina de Pablo con
`demo/empaquetar.py` + `terraform apply` o con `aws s3 cp`.

### Espejo (caché de 30 días en S3)

La caché de Cotejo es de archivos (`COTEJO_CACHE`) y sus caducidades se leen del
mtime. El espejo no cambia esa lógica: antes de la consulta baja de
`s3://BUCKET/cache/<ruta relativa>` las entradas previsibles y les pone como
mtime el LastModified de S3; después sube los archivos nuevos o con mtime
distinto. El bucket expira `cache/` a los 30 días (regla de ciclo de vida).
Así, la misma providencia o el mismo pasaje no se vuelven a pedir a la Corte
en 30 días, y las ausencias se siguen recordando solo un día (lo decide el
módulo con el mtime).

Entradas previsibles por cita:
- Corte Constitucional (sentencia): `cc.ruta_texto(...)`, `cc.ruta_nota(...)`.
- Corte Suprema (código con prefijo en `cs.SALAS`): `cs.ruta_busqueda(...)`; si la
  búsqueda bajada tiene providencia, también `cs.ruta_texto(id)`.

Un fallo de S3 nunca rompe la consulta: se registra y se sigue sin caché.

Tercera ronda:
- **Las ausencias no se suben.** Un `relatoria/*.txt.gz` cuyo contenido
  descomprimido está vacío no se sube a S3: cada contenedor vuelve a preguntar
  sus inventadas (lo acota `MAX_PROVIDENCIAS`) y la ausencia vale un día solo en
  su disco. Así un error de un contenedor no se reparte a los demás. Tampoco se
  sustituye nunca en S3 un objeto no vacío por uno vacío.
- **Notas positivas.** Al bajar una nota con nulidad cuyo LastModified tiene más
  de 20 días, se le pone mtime = ahora para que `subir` la vuelva a subir y el
  ciclo de vida de `cache/` (30 días) no la borre: una anulación no se deshace.
- **Qué es «negativo».** No solo el `.txt.gz` vacío: también `{"nota": null}`, una
  búsqueda de la Corte Suprema con `providencia: null`, 0 bytes y cualquier cosa
  ilegible. Un negativo nunca se sube encima de un positivo; si S3 respondió
  `AccessDenied` o la bajada falló, se trata como «desconocido» y el negativo no
  se sube. Las ausencias que ya estuvieran en S3 tampoco se bajan.
- **Solo lo que se va a consultar.** `preparar(citas, max_consultadas=N)` baja
  solo las entradas de las primeras N providencias que necesitan red, en el
  orden de `verificacion._agrupar` (el mismo en que `verificar` aplica el tope).

## 3. Infraestructura (`infra/`, Terraform)

Estado remoto en el bucket del portafolio, clave `cotejo/terraform.tfstate`
(`backend.hcl` no versionado; `backend.hcl.example` sí). Proveedor aws ~> 6.0,
perfil `default`, región us-east-1, etiquetas `proyecto = "cotejo"`, `entorno = "demo"`.

- S3 `cotejo-demo-<cuenta>`: acceso público bloqueado, cifrado por defecto,
  ciclo de vida: `cache/` expira a 30 días; objetos `codigo/lambda.zip` e
  `indice/corte_constitucional_indice.json.gz` (este con `ignore_changes` en el
  contenido para no pisar la actualización semanal).
- DynamoDB `cotejo-demo-cuota`, PAY_PER_REQUEST, `pk` (S), TTL `expira`.
- Lambda `cotejo-demo`, python3.12, x86_64, 1024 MB, timeout 180 s,
  `reserved_concurrent_executions = 1` (ver «Clientes HTTP»), registros 14 días.
  Validación: `max_peticiones_dia + concurrencia * (plazo_segundos + max_providencias) < 800`
  (las consultas en vuelo pueden pasarse del presupuesto como mucho en una
  petición por segundo de plazo más una por providencia, al alternar entre las
  dos Cortes; de serie, 600 + 1 × (60 + 15) = 675).
- Function URL sin autenticación, CORS: orígenes `https://pabloduque.dev` y
  `http://localhost:8765`, métodos GET y POST, cabecera `content-type`.
- IAM mínimo: logs del grupo propio; `s3:GetObject`/`PutObject` en `indice/*` y
  `cache/*`; `s3:ListBucket` limitado a esos prefijos; `dynamodb:UpdateItem` en la tabla.
- EventBridge semanal → `{"accion": "actualizar_indice"}`.
- Costo: presupuesto `cotejo-demo` de US$ 5/mes filtrado por la etiqueta
  `proyecto$cotejo` (activa la etiqueta con `aws_ce_cost_allocation_tag`), avisos
  al 80 % real y al 100 % previsto; y una alarma de CloudWatch si las invocaciones
  pasan de 3.000 en un día (SNS por correo, hay que confirmar la suscripción;
  esa alarma también acciona el freno, porque publica en el mismo tema).
  Correo: variable `correo_alertas`.
- **Freno automático.** Alarma por hora (más de 1.000 invocaciones en 3.600 s) →
  SNS → Lambda `cotejo-demo-freno` (código en `infra/freno/handler.py`, solo
  biblioteca estándar y boto3) que pone la concurrencia reservada de
  `cotejo-demo` a 0: la Function URL responde 429 hasta que se reactive
  (`terraform apply` la devuelve a `concurrencia_maxima`, 1, o
  `aws lambda put-function-concurrency`).
  IAM del freno: `lambda:PutFunctionConcurrency` solo sobre `cotejo-demo` y sus
  registros. Motivo: el tope de una Function URL es 10 × la concurrencia
  reservada (10 peticiones/s con 1), y una inundación sostenida pasa de US$ 5 en
  días; el presupuesto avisa con horas de retraso.
- Salidas: `url`, `funcion`, `bucket`, `freno`.
- Chequeo de salud del portafolio: con concurrencia 1, un `GET /` que coincide con
  una consulta recibe 429. La página de estado solo da una demo por caída tras dos
  chequeos fallidos seguidos (15 min entre uno y otro), y una consulta dura como
  mucho unos 85 s, así que no da falsos «caída».

## 4. Página (`pabloduque.dev/lab/cotejo.html`)

En el estilo de `/lab` (lab.css, lab.js, theme.js; ES|EN; claro y oscuro).

- Tres ejemplos precargados, servidos como estáticos desde
  `/data/cotejo/ejemplos.json` (lo genera `demo/exportar_ejemplos.py` con el
  commit y la fecha): al elegir uno se ven su texto y su informe al instante,
  marcado «verificado el <fecha>». «Verificar en vivo» lo repite ahora y gasta
  una consulta.
- Texto propio: área de texto con contador (máx. 5.000), botón, consultas que
  quedan hoy, y el informe dibujado desde el JSON (estado, providencia con
  enlace a la fuente, ponente, motivo, observaciones, pasajes con las
  diferencias palabra a palabra, «¿quizá quería citar…?»).
- Los motivos y observaciones de Cotejo están en español; en EN la interfaz se
  traduce y una nota lo dice.
- La URL de la API va en `data-api` del `<main>`; en localhost se puede cambiar
  con `?api=`.
- Aviso de privacidad propio de la demo: el texto viaja a un servidor en AWS, se
  procesa en memoria y no se guarda.

Errores y cortes que la página explica: `demasiadas_citas`, y `cortado_por_tope`
(«esta demo consulta como mucho 15 providencias por texto; las demás salen sin
confirmar»).

`demo/exportar_ejemplos.py` se niega a exportar si `git status --porcelain` muestra
cambios en `cotejo/` o en `demo/` (salvo `demo/pagina/`): el commit que se publica
tiene que ser el que produjo el informe.

`ejemplos.json`:

```json
{"generado": "AAAA-MM-DD", "commit": "<hash corto de Cotejo>",
 "ejemplos": [{"id": "limpio", "titulo": {"es": "...", "en": "..."},
               "descripcion": {"es": "...", "en": "..."}, "texto": "...",
               "respuesta": <misma forma que el 200 de POST /verificar>}]}
```

## 5. Sonda

`{"accion": "sonda"}` pide, con el cliente cortés, la portada de la
Constitucional, la T-406/92, una dirección inexistente (tiene que ser la carcasa
firmada; un 404 no vale), la T-760/08 con `Range` de 30 KB, la C-355/06 con
`Range` de 300 KB (la segunda ventana de la nulidad), dos filas de datos.gov.co
y la SC5686-2018 en el buscador de la Corte Suprema, y
devuelve código, bytes, segundos y si el contenido es el esperado. Se lanza a
mano tras el primer `apply`:

    aws lambda invoke --function-name cotejo-demo --payload '{"accion":"sonda"}' \
      --cli-binary-format raw-in-base64-out --region us-east-1 --profile default ../build/sonda.json

Medido antes de construir, con una Lambda de prueba aparte (20-10-2026): desde
us-east-1 y sa-east-1 responden la relatoría (T-406/92 con 160.144 bytes; lo
inexistente, la carcasa de 8.607 bytes; `Range` con 206), datos.gov.co y el
buscador de la Corte Suprema (encuentra la SC5686-2018). Ningún bloqueo.

Notas de la infraestructura: el índice lleva `ignore_changes`, así que para
volver a subirlo desde la máquina de Pablo hace falta
`terraform apply -replace=aws_s3_object.indice` o `aws s3 cp`; la Function URL
expone `retry-after` en CORS.
