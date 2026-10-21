# Demo pública «Cotejo en vivo» (el contrato está en demo/DISEÑO.md): una Lambda con Function
# URL que verifica las citas de un texto pegado contra la Corte Constitucional y la Corte
# Suprema y devuelve el informe en JSON. Casi $0 cuando nadie la usa.
#
# Desde la raíz de Cotejo, en Git Bash:
#   .venv/Scripts/python demo/empaquetar.py      # arma build/lambda.zip y build/indice.json.gz
#   cd infra
#   cp backend.hcl.example backend.hcl           # solo la primera vez, con el bucket de estado
#   terraform init -backend-config=backend.hcl
#   terraform plan -out=plan.tfplan
#   terraform apply plan.tfplan
#   aws lambda invoke --function-name cotejo-demo --payload '{"accion":"sonda"}' \
#     --cli-binary-format raw-in-base64-out --region us-east-1 --profile default ../build/sonda.json
# Con concurrencia 1, si la sonda da TooManyRequestsException es que alguien está verificando en
# ese momento: repetirla en un minuto.
#
# Tras el primer apply, y antes de anunciar la demo, probar el freno (al final de este archivo) de
# forma controlada: IAM no se comprueba hasta que el permiso se usa, y lo que la página ve con la
# demo frenada (si el 429 de la Function URL trae las cabeceras de CORS; sin ellas, fetch lo da
# como fallo de red) solo se sabe probándolo. Sin alarma de por medio no llega ningún correo.
# Frenar, comprobar y reactivar:
#   aws lambda invoke --function-name cotejo-demo-freno --payload '{}' \
#     --cli-binary-format raw-in-base64-out --region us-east-1 --profile default ../build/freno.json
#   aws lambda get-function-concurrency --function-name cotejo-demo \
#     --region us-east-1 --profile default
#       → "ReservedConcurrentExecutions": 0; si no, el registro de cotejo-demo-freno dice por qué
#   curl -si -H 'Origin: https://pabloduque.dev' "$(terraform output -raw url)"
#       → 429; mirar si trae access-control-allow-origin
#   aws lambda put-function-concurrency --function-name cotejo-demo \
#     --reserved-concurrent-executions 1 --region us-east-1 --profile default
#
# Si init se eterniza descargando el proveedor aws, el del portafolio sirve:
#   terraform init -backend-config=backend.hcl -plugin-dir=../../Portafolio/infra/.terraform/providers
# En PowerShell 5.1 las comillas del payload de la sonda se pierden al pasar a aws.exe: Git Bash.
#
# Después del primer apply: confirmar en el correo la suscripción de SNS (sin ese clic la alarma
# no avisa a nadie) y contar con hasta 24 h hasta que la etiqueta de costos se active.
#
# El índice no se vuelve a subir con un apply normal (ver aws_s3_object.indice). Para pisar el
# de S3 con el local, p. ej. si datos.gov.co bloquea a AWS y la actualización semanal falla:
#   terraform apply -replace=aws_s3_object.indice
#
# Freno automático (al final de este archivo): si la función pasa de 1.000 invocaciones en 60
# minutos, o de 3.000 en un día, cotejo-demo-freno pone su concurrencia reservada a 0 y la
# Function URL responde 429 a todo, también al chequeo de salud del portafolio y a la
# actualización semanal del índice. Llega un correo de la alarma. No se reactiva sola.
# Para reactivarla:
#   1. Mirar qué pasó: el registro de cotejo-demo-freno dice qué alarma y con cuántas
#      invocaciones; el de cotejo-demo, qué rutas (evento rechazado/limitado/verificado...).
#   2. Esperar a que la alarma de hora vuelva sola a OK: lo frenado no cuenta como invocación,
#      así que tarda como mucho una hora. Una alarma solo actúa al PASAR a ALARM: reactivada con
#      ella aún en ALARM, una inundación que siga ya no frena. (La diaria puede seguir en ALARM
#      hasta un día; mientras tanto solo frena la de hora.)
#        aws cloudwatch describe-alarms --alarm-name-prefix cotejo-demo-invocaciones \
#          --query 'MetricAlarms[].[AlarmName,StateValue]' --region us-east-1 --profile default
#   3. Devolverle la concurrencia configurada (concurrencia_maxima, 1 de serie). O con terraform
#      apply (el plan enseña reserved_concurrent_executions: 0 -> 1; ojo, CUALQUIER apply la
#      reactiva: para aplicar otro cambio con la demo aún frenada, -var concurrencia_maxima=0),
#      o a mano:
#        aws lambda put-function-concurrency --function-name cotejo-demo \
#          --reserved-concurrent-executions 1 --region us-east-1 --profile default
#      Nunca delete-function-concurrency: quitaría el tope de 1 y la demo podría usar toda la
#      concurrencia de la cuenta, varias a la vez contra la relatoría.
# Para frenar a mano, invocar el freno con un evento que no es una alarma (también frena):
#   aws lambda invoke --function-name cotejo-demo-freno --payload '{}' \
#     --cli-binary-format raw-in-base64-out --region us-east-1 --profile default ../build/freno.json

locals {
  nombre       = "cotejo-demo"
  zip          = "${path.module}/../build/lambda.zip"
  indice       = "${path.module}/../build/indice.json.gz"
  clave_indice = "indice/corte_constitucional_indice.json.gz"
}

data "aws_caller_identity" "yo" {}

# Sal del HMAC con el que se guarda la IP en la tabla de cuota. Es secreta porque las IPv4 son
# pocas: con la sal a la vista bastaría calcular el HMAC de todas para saber qué IP hay detrás
# de cada contador. Vive solo en el estado (cifrado) y en la configuración de la función.
resource "random_id" "sal" {
  byte_length = 32
}

# ---------------------------------------------------------------- bucket: código, índice y caché
resource "aws_s3_bucket" "demo" {
  bucket        = "${local.nombre}-${data.aws_caller_identity.yo.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "demo" {
  bucket                  = aws_s3_bucket.demo.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "demo" {
  bucket = aws_s3_bucket.demo.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "demo" {
  bucket = aws_s3_bucket.demo.id

  # La caché de 30 días del espejo (DISEÑO §2): pasado ese plazo una providencia o un pasaje se
  # vuelven a pedir a la Corte, y así una corrección de la relatoría acaba llegando a la demo.
  # Cada subida reinicia la cuenta, porque S3 la lleva desde la última escritura del objeto.
  rule {
    id     = "cache-30-dias"
    status = "Enabled"
    filter {
      prefix = "cache/"
    }
    expiration {
      days = 30
    }
  }

  # Terraform y boto3 suben por partes los archivos grandes; una subida interrumpida (un apply
  # cortado, una Lambda que llega a su timeout) deja partes invisibles que se cobran sin plazo.
  rule {
    id     = "abortar-subidas-incompletas"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_object" "codigo" {
  bucket      = aws_s3_bucket.demo.id
  key         = "codigo/lambda.zip"
  source      = local.zip
  source_hash = filemd5(local.zip)
}

# Solo la primera subida es de Terraform: después lo reemplaza cada semana la propia Lambda
# (accion actualizar_indice). Sin ignore_changes, el siguiente apply volvería a subir el índice
# local, más viejo, encima del semanal. content_type y metadata van en la lista porque la
# Lambda los puede escribir distintos, y un cambio en cualquiera de los dos hace que el
# proveedor vuelva a subir el archivo entero, no solo el atributo.
resource "aws_s3_object" "indice" {
  bucket       = aws_s3_bucket.demo.id
  key          = local.clave_indice
  source       = local.indice
  source_hash  = filemd5(local.indice)
  content_type = "application/gzip"

  lifecycle {
    ignore_changes = [source, source_hash, etag, content_type, metadata]
  }
}

# ---------------------------------------------------------------- cuota diaria
# Clave pk: «ip#<fecha>#<hmac>» y «dia#<fecha>». El TTL de 2 días borra solos los contadores
# viejos; como la fecha entra en el hash, ni siquiera mientras duran se pueden enlazar dos días
# de la misma IP (DISEÑO, principio 4).
resource "aws_dynamodb_table" "cuota" {
  name         = "${local.nombre}-cuota"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expira"
    enabled        = true
  }
}

# ---------------------------------------------------------------- permisos
resource "aws_cloudwatch_log_group" "registros" {
  name              = "/aws/lambda/${local.nombre}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "asumir" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = local.nombre
  assume_role_policy = data.aws_iam_policy_document.asumir.json
}

data "aws_iam_policy_document" "permisos" {
  statement {
    sid       = "Registros"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.registros.arn}:*"]
  }
  # codigo/ queda fuera: la función no tiene por qué leer ni reescribir su propio paquete.
  statement {
    sid     = "IndiceYCache"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "${aws_s3_bucket.demo.arn}/indice/*",
      "${aws_s3_bucket.demo.arn}/cache/*",
    ]
  }
  # Sin s3:ListBucket, S3 responde AccessDenied (y no NoSuchKey) cuando una entrada de la caché
  # aún no está en el bucket. Limitado a los dos prefijos que la función usa. No está probado
  # contra AWS que la comprobación implícita de un GetObject lleve s3:prefix (si no lo lleva,
  # esta condición no casa y la clave ausente sigue dando AccessDenied); por eso el espejo trata
  # AccessDenied de una clave igual que NoSuchKey, «no está», y sigue con las demás (DISEÑO §2,
  # paso 6). En el peor caso, un permiso roto parecería una caché vacía y la demo le volvería a
  # preguntar a la Corte lo que tenía guardado: más peticiones, ningún resultado que la fuente no
  # haya dado.
  statement {
    sid       = "ListarIndiceYCache"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.demo.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["indice/*", "cache/*"]
    }
  }
  statement {
    sid       = "Cuota"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.cuota.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "permisos"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.permisos.json
}

# ---------------------------------------------------------------- función y URL pública
resource "aws_lambda_function" "demo" {
  function_name    = local.nombre
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  architectures    = ["x86_64"] # la rueda de rapidfuzz del paquete es manylinux x86_64
  s3_bucket        = aws_s3_bucket.demo.id
  s3_key           = aws_s3_object.codigo.key
  source_code_hash = filebase64sha256(local.zip)
  memory_size      = var.memoria_mb
  timeout          = var.timeout_segundos

  # Una a la vez: la pausa de cortesía (1,5 s la relatoría, 1 s la Corte Suprema) vive en la
  # memoria de cada contenedor, y con dos la relatoría recibía el doble de ritmo, ~1,4
  # peticiones/s (DISEÑO §2, «Concurrencia reservada 1»). Lo que llega mientras tanto recibe un
  # 429 de la Function URL sin ejecutarse ni cobrarse: un segundo visitante, y también el chequeo
  # de salud del portafolio si coincide con una verificación (hasta un minuto y medio).
  reserved_concurrent_executions = var.concurrencia_maxima

  environment {
    variables = {
      BUCKET             = aws_s3_bucket.demo.id
      TABLA_CUOTA        = aws_dynamodb_table.cuota.name
      SAL_VISITANTE      = random_id.sal.hex
      MAX_CONSULTAS_IP   = tostring(var.max_consultas_ip)
      MAX_CONSULTAS_DIA  = tostring(var.max_consultas_dia)
      MAX_CARACTERES     = tostring(var.max_caracteres)
      PLAZO_SEGUNDOS     = tostring(var.plazo_segundos)
      MAX_CITAS          = tostring(var.max_citas)
      MAX_PROVIDENCIAS   = tostring(var.max_providencias)
      MAX_PETICIONES_DIA = tostring(var.max_peticiones_dia)
      COTEJO_CACHE       = "/tmp/cotejo-cache"
    }
  }

  depends_on = [aws_cloudwatch_log_group.registros, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function_url" "demo" {
  function_name      = aws_lambda_function.demo.function_name
  authorization_type = "NONE"

  # El preflight (OPTIONS) lo contesta la Function URL sin invocar la función. Retry-After se
  # expone porque no es una cabecera simple de CORS: sin esto, fetch no la deja leer y la página
  # no podría decir cuándo vuelve a haber consultas tras un limite_ip.
  cors {
    allow_origins  = var.origenes_cors
    allow_methods  = ["GET", "POST"]
    allow_headers  = ["content-type"]
    expose_headers = ["retry-after"]
    max_age        = 7200 # Chrome no guarda un preflight más de 2 h
  }
}

resource "aws_lambda_permission" "url_publica" {
  statement_id           = "UrlPublica"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.demo.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Desde octubre de 2025, AWS exige además lambda:InvokeFunction para que una URL nueva sin
# autenticación responda (sin este permiso, todo devuelve 403). La condición de
# invoked_via_function_url es lo importante: sin ella, cualquiera con credenciales de AWS podría
# invocar la función directamente con {"accion": "actualizar_indice"} o {"accion": "sonda"},
# saltándose la cuota y poniéndola a pedir a la Corte y a datos.gov.co a su antojo.
resource "aws_lambda_permission" "url_publica_invocar" {
  statement_id             = "UrlPublicaInvocar"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.demo.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------------------------------------------------------- actualización semanal del índice
# Lunes a las 08:00 UTC (3 de la mañana en Colombia): el contenedor que baja el índice ocupa el
# único hueco de concurrencia, y mientras tanto un visitante recibiría 429; mejor cuando nadie
# está verificando. Si coincide con una verificación, la invocación asíncrona sale frenada y
# Lambda la reintenta desde su cola durante hasta 6 h: maximum_retry_attempts (más abajo) solo
# cuenta los errores de la función, no la falta de hueco.
resource "aws_cloudwatch_event_rule" "indice" {
  name                = "${local.nombre}-indice-semanal"
  schedule_expression = "cron(0 8 ? * MON *)"
}

resource "aws_cloudwatch_event_target" "indice" {
  rule  = aws_cloudwatch_event_rule.indice.name
  arn   = aws_lambda_function.demo.arn
  input = jsonencode({ accion = "actualizar_indice" })
}

resource "aws_lambda_permission" "indice" {
  statement_id  = "EventBridgeIndice"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.demo.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.indice.arn
}

# La invocación de EventBridge es asíncrona y Lambda la repite dos veces si falla: dos descargas
# completas más a datos.gov.co en pocos minutos, justo cuando puede estar bloqueando a AWS. Si
# falla, el índice anterior sigue sirviendo y el lunes siguiente se vuelve a intentar.
resource "aws_lambda_function_event_invoke_config" "demo" {
  function_name          = aws_lambda_function.demo.function_name
  maximum_retry_attempts = 0
}

# ---------------------------------------------------------------- costo
# El presupuesto filtra por la etiqueta proyecto = cotejo, que el proveedor pone en todo
# (default_tags en versions.tf). Una etiqueta solo cuenta para costos cuando está activada en
# facturación, y la activación tarda hasta 24 h: hasta entonces el presupuesto marca $0 aunque
# haya gasto. Si el apply falla aquí porque facturación no conoce la clave (pasa en una cuenta
# que nunca la usó en un recurso), repetirlo al día siguiente. La clave proyecto la comparten
# otras demos de la cuenta y un destroy la desactiva para todas; si alguna la necesita, antes:
#   terraform state rm aws_ce_cost_allocation_tag.proyecto
resource "aws_ce_cost_allocation_tag" "proyecto" {
  tag_key = "proyecto"
  status  = "Active"
}

resource "aws_budgets_budget" "demo" {
  name         = local.nombre
  budget_type  = "COST"
  limit_amount = tostring(var.presupuesto_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:proyecto$cotejo"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.correo_alertas]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.correo_alertas]
  }

  depends_on = [aws_ce_cost_allocation_tag.proyecto]
}

# El presupuesto llega con uno o dos días de retraso; la alarma avisa el mismo día. Con 100
# verificaciones diarias como máximo, el chequeo de salud del portafolio (cada 15 min) y los
# rechazos por cuota, el uso normal se queda en unos cientos de invocaciones: 3.000 es alguien
# golpeando la URL pública, que se cobra aunque la función solo ejecute para rechazarlo.
# Todo lo que se publica en este tema llega también al freno (al final del archivo), así que la
# alarma diaria, además de avisar, frena: 3.000 en un día también es abuso, aunque vaya a menos
# de 1.000 por hora.
resource "aws_sns_topic" "alertas" {
  name = "${local.nombre}-alertas"
}

# AWS manda un correo de confirmación: hasta que se pulsa el enlace, la suscripción queda
# «pendiente» y la alarma no le llega a nadie. Terraform no puede confirmarla.
resource "aws_sns_topic_subscription" "correo" {
  topic_arn = aws_sns_topic.alertas.arn
  protocol  = "email"
  endpoint  = var.correo_alertas
}

# Un solo periodo de 86.400 s: la suma del día entero, no un pico por hora. Periodo por número
# de evaluaciones se queda en un día, dentro de lo que CloudWatch acepta.
resource "aws_cloudwatch_metric_alarm" "invocaciones" {
  alarm_name          = "${local.nombre}-invocaciones-dia"
  alarm_description   = "La demo de Cotejo pasó de ${var.alerta_invocaciones_dia} invocaciones en un día"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.demo.function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alerta_invocaciones_dia
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alertas.arn]
}

# ---------------------------------------------------------------- freno automático
# El tope de una Function URL es 10 × la concurrencia reservada: con 1, 10 peticiones/s, unas
# 864.000 invocaciones al día. Las verificaciones no llegan a ese ritmo (cada una ocupa el único
# hueco decenas de segundos), pero los rechazos baratos sí (limite_ip, rutas que no existen), y
# cada uno se cobra aunque la función solo ejecute para rechazarlo: la petición, unos
# milisegundos de cómputo y, sobre todo, tres o cuatro líneas de registro. Sostenido, unos
# US$ 0,4 al día sin capa gratuita: los US$ 5 en unas dos semanas, y el presupuesto avisa con
# horas de retraso y no corta nada. Lo que la URL rechaza con 429 por falta de hueco no es una
# invocación: ni se cobra ni cuenta en la métrica. La alarma de hora salta en minutos: CloudWatch
# la evalúa cada minuto sobre los últimos 60 (ventana deslizante, no horas en punto), y a 10/s se
# pasa de 1.000 en menos de dos minutos, mientras que el uso normal se queda muy por debajo
# (variables.tf). Si se sube concurrencia_maxima, el tope de la URL y el ritmo de la inundación
# suben con ella y el umbral sigue sirviendo; el freno no depende del valor, siempre pone 0.
# Publica en el mismo tema que la diaria; el freno, suscrito a él, pone la concurrencia de
# cotejo-demo a 0. Cómo se reactiva: cabecera de este archivo.
resource "aws_cloudwatch_metric_alarm" "invocaciones_hora" {
  alarm_name          = "${local.nombre}-invocaciones-hora"
  alarm_description   = "La demo de Cotejo pasó de ${var.alerta_invocaciones_hora} invocaciones en una hora: cotejo-demo-freno pone su concurrencia a 0 (reactivar a mano, ver infra/main.tf)"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.demo.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alerta_invocaciones_hora
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alertas.arn]
}

# El zip se arma en cada plan (un data source se lee siempre) y va a build/, que no se versiona.
# output_file_mode fija los permisos dentro del zip: sin él dependen del sistema que corre
# terraform, y el hash, y con él la función, cambiarían al hacer apply desde otra máquina.
data "archive_file" "freno" {
  type             = "zip"
  source_file      = "${path.module}/freno/handler.py"
  output_path      = "${path.module}/../build/freno.zip"
  output_file_mode = "0644"
}

# Creado antes que la función: si no, Lambda lo crea solo, sin caducidad.
resource "aws_cloudwatch_log_group" "freno" {
  name              = "/aws/lambda/${local.nombre}-freno"
  retention_in_days = 14
}

resource "aws_iam_role" "freno" {
  name               = "${local.nombre}-freno"
  assume_role_policy = data.aws_iam_policy_document.asumir.json
}

# Lo único que el freno puede hacer. Ni lambda:GetFunction*, ni UpdateFunction*, ni
# DeleteFunctionConcurrency (que quitaría el tope de 1 en vez de bajarlo), y nada sobre otra
# función. IAM no tiene clave de condición para el NÚMERO que se pone con PutFunctionConcurrency:
# que sea 0 lo garantiza el código, que no lo lee del mensaje (infra/freno/handler.py).
data "aws_iam_policy_document" "freno" {
  statement {
    sid       = "Registros"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.freno.arn}:*"]
  }
  # El ARN sin calificador: la concurrencia reservada es de la función, no de una versión o alias.
  statement {
    sid       = "Frenar"
    actions   = ["lambda:PutFunctionConcurrency"]
    resources = [aws_lambda_function.demo.arn]
  }
}

resource "aws_iam_role_policy" "freno" {
  name   = "permisos"
  role   = aws_iam_role.freno.id
  policy = data.aws_iam_policy_document.freno.json
}

# Sin concurrencia reservada propia: le restaría una a la cuenta para algo que corre una vez en
# meses, y una invocación asíncrona que no encuentra hueco espera en la cola de Lambda (hasta 6 h)
# en vez de perderse. Si la llamada a AWS falla, Lambda la repite dos veces (lo de serie), y
# poner 0 otra vez no hace daño.
resource "aws_lambda_function" "freno" {
  function_name    = "${local.nombre}-freno"
  role             = aws_iam_role.freno.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.freno.output_path
  source_code_hash = data.archive_file.freno.output_base64sha256
  memory_size      = 128
  timeout          = 30

  environment {
    variables = {
      FUNCION = aws_lambda_function.demo.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.freno, aws_iam_role_policy.freno]
}

# Solo SNS, y solo desde este tema, puede invocar el freno (además de quien tenga
# lambda:InvokeFunction en la cuenta, que es como se frena a mano).
resource "aws_lambda_permission" "freno_sns" {
  statement_id  = "AlarmasSns"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.freno.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alertas.arn
}

# A diferencia del correo, una suscripción de Lambda no se confirma: queda activa con el apply.
resource "aws_sns_topic_subscription" "freno" {
  topic_arn  = aws_sns_topic.alertas.arn
  protocol   = "lambda"
  endpoint   = aws_lambda_function.freno.arn
  depends_on = [aws_lambda_permission.freno_sns]
}
