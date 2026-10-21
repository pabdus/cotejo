variable "region" {
  description = "Región de AWS"
  type        = string
  default     = "us-east-1"
}

variable "perfil" {
  description = "Perfil de AWS CLI con el que se despliega: el de la cuenta PERSONAL, nunca el de un cliente"
  type        = string
  default     = "default"
}

# ---------------------------------------------------------------- límites de la demo
variable "max_consultas_ip" {
  description = "Verificaciones por IP y día (hora de Colombia). Cada una puede costar decenas de peticiones a la Corte"
  type        = number
  default     = 3
}

variable "max_consultas_dia" {
  description = "Verificaciones por día en toda la demo, sumadas todas las IP"
  type        = number
  default     = 100
}

variable "max_caracteres" {
  description = "Longitud máxima del texto pegado"
  type        = number
  default     = 5000
}

variable "plazo_segundos" {
  description = "Plazo de verificar(): pasado este tiempo, las citas que falten salen «sin confirmar» con ese motivo"
  type        = number
  default     = 60

  # Entra en la cuenta del presupuesto (max_peticiones_dia): un plazo negativo la abarataría sin
  # que nada cambie, y con 0 o menos la demo respondería todo «sin confirmar» por tiempo.
  validation {
    condition     = var.plazo_segundos > 0
    error_message = "plazo_segundos tiene que ser positivo."
  }
}

# Los tres topes que siguen salen de la revisión adversarial: con solo la cuota por consultas,
# un texto de 5.000 caracteres con 464 citas no verificables devolvía 165 KB gratis, y uno con
# 446 sentencias inventadas hacía 41 peticiones a la relatoría gastando una sola consulta.
variable "max_citas" {
  description = "Providencias distintas por texto; con más, la demo responde demasiadas_citas sin verificar nada (un escrito real de 5.000 caracteres no llega)"
  type        = number
  default     = 40

  validation {
    condition     = var.max_citas >= 1 && floor(var.max_citas) == var.max_citas
    error_message = "max_citas tiene que ser un entero positivo."
  }
}

variable "max_providencias" {
  description = "Providencias que se consultan a las fuentes por texto (max_consultadas de verificar); las siguientes salen «sin confirmar» con ese motivo"
  type        = number
  default     = 15

  validation {
    condition     = var.max_providencias >= 1 && floor(var.max_providencias) == var.max_providencias
    error_message = "max_providencias tiene que ser un entero positivo."
  }
}

variable "max_peticiones_dia" {
  description = "Peticiones a la relatoría y a la Corte Suprema por día, sumadas todas las consultas; agotadas, limite_global"
  type        = number
  default     = 600

  validation {
    condition     = var.max_peticiones_dia >= 1 && floor(var.max_peticiones_dia) == var.max_peticiones_dia
    error_message = "max_peticiones_dia tiene que ser un entero positivo."
  }

  # La relatoría cortó la conexión a la máquina de Pablo tras unas 800 peticiones en una tarde
  # (ESTADO.md 1c). Desde AWS, un veto así dejaría sin fuente a todos los visitantes, y la cuota
  # por consultas no lo impedía: 100 consultas de 41 peticiones son unas 4.100.
  # Quedar por debajo de 800 no basta (antes la condición era solo esa): el presupuesto se
  # comprueba ANTES de cada consulta (n < tope) y lo gastado se suma al final, así que el día se
  # pasa del tope en lo que hagan las consultas que estaban en vuelo al cruzarlo, una por hueco de
  # concurrencia. A cada una no la acotan las 15 providencias (un código de la Corte Suprema son
  # hasta 4 variantes × 5 páginas) sino el plazo: red.Cliente deja al menos 1 s entre el comienzo
  # de dos intentos al mismo servidor (la pausa del buscador; la de la relatoría es 1,5 s) y no
  # empieza ninguno con menos de 1 s de plazo. Eso da una petición por segundo de plazo y, como
  # la primera petición a un servidor no espera la pausa del otro, una más por cada cambio de
  # Corte, que son como mucho tantos como providencias consultadas: plazo + max_providencias por
  # consulta (la revisión proponía plazo + 10, que no cubre 15 providencias alternando Cortes).
  # De serie: 600 + 1 × (60 + 15) = 675. Con concurrencia 2 serían 750; con 3, ya no cabe.
  validation {
    condition     = var.max_peticiones_dia + var.concurrencia_maxima * (var.plazo_segundos + var.max_providencias) < 800
    error_message = "max_peticiones_dia + concurrencia_maxima × (plazo_segundos + max_providencias) tiene que quedar por debajo de 800, las peticiones de una tarde tras las que la relatoría cortó la conexión (ESTADO.md 1c): el presupuesto se comprueba antes de cada consulta, y cada una de las que estén en vuelo al agotarlo puede pasarse en una petición por segundo de plazo más una por providencia. Con estos valores da ${var.max_peticiones_dia + var.concurrencia_maxima * (var.plazo_segundos + var.max_providencias)}."
  }
}

# ---------------------------------------------------------------- función
variable "concurrencia_maxima" {
  description = "Ejecuciones simultáneas máximas de la Lambda (su concurrencia reservada). Con más de 1, la relatoría recibe ese múltiplo del ritmo de cortesía; 0 la deja frenada"
  type        = number
  default     = 1

  # 1 y no 2 (DISEÑO §2, «Concurrencia reservada 1»): la pausa de cortesía vive en la memoria de
  # cada contenedor, y con dos a la vez la relatoría recibía ~1,4 peticiones/s, en contra de
  # ESTADO («no lanzar dos a la vez contra el mismo servidor»). Un segundo visitante simultáneo
  # recibe 429 de la Function URL, sin gastar consulta, y la página le pide que lo intente en un
  # minuto. 0 es lo que pone el freno, y vale para aplicar un cambio sin reactivar una demo
  # frenada. -1, que para el proveedor es quitar la reserva, queda fuera: la demo podría usar toda
  # la concurrencia sin reservar de la cuenta, y la Function URL atender diez veces esa cifra por
  # segundo. El tope por arriba lo pone la validación de max_peticiones_dia (con lo de serie, 2).
  validation {
    condition     = var.concurrencia_maxima >= 0 && floor(var.concurrencia_maxima) == var.concurrencia_maxima
    error_message = "concurrencia_maxima tiene que ser un entero de 0 en adelante: 0 deja la demo frenada, y -1 quitaría el tope de ejecuciones simultáneas."
  }
}

variable "memoria_mb" {
  description = "Memoria de la Lambda (también fija su CPU: con menos, el arranque en frío con el índice tarda más)"
  type        = number
  default     = 1024
}

variable "timeout_segundos" {
  description = "Tiempo máximo de una invocación"
  type        = number
  default     = 180

  # Antes el plazo solo se miraba entre providencias, y una sola cita de la Corte Suprema (hasta
  # 20 peticiones de menos de 12 s cada una) podía pasar del timeout: la Lambda moría sin informe
  # y las citas pendientes no se listaban. Ahora red.Cliente corta también dentro de cada petición
  # y la Lambda recorta el plazo a lo que le queda menos 25 s (DISEÑO §1b y §2, paso 4), así que
  # un timeout corto ya no rompe el informe, pero le quitaría tiempo a la verificación sin
  # avisar. Los 60 s de margen hacen que el plazo configurado sea el que de verdad se aplica,
  # también con el arranque en frío (el índice se lee de S3 antes de tomar el plazo).
  validation {
    condition     = var.timeout_segundos >= var.plazo_segundos + 60
    error_message = "timeout_segundos tiene que dejar al menos 60 s por encima de plazo_segundos."
  }
}

variable "origenes_cors" {
  description = "Orígenes que pueden llamar a la Function URL desde el navegador: el sitio y el servidor local de la página"
  type        = list(string)
  default     = ["https://pabloduque.dev", "http://localhost:8765"]
}

# ---------------------------------------------------------------- costo y alertas
variable "correo_alertas" {
  description = "Correo que recibe los avisos del presupuesto y la alarma (la suscripción de SNS se confirma con un clic)"
  type        = string
  default     = "pabdus3000@gmail.com"
}

variable "presupuesto_usd" {
  description = "Presupuesto mensual de la demo, en dólares, filtrado por la etiqueta proyecto = cotejo"
  type        = number
  default     = 5
}

variable "alerta_invocaciones_dia" {
  description = "Invocaciones de la función en un día que disparan la alarma (avisa por correo y también frena la demo)"
  type        = number
  default     = 3000
}

variable "alerta_invocaciones_hora" {
  description = "Invocaciones de la función en 60 minutos que disparan el freno automático (concurrencia reservada a 0). La Function URL atiende como mucho 10 × concurrencia_maxima por segundo: con 1, una inundación pasa de 1.000 en menos de dos minutos, y el uso normal (100 verificaciones al día como mucho, el chequeo de salud cada 15 min, algún rechazo por cuota) se queda muy por debajo"
  type        = number
  default     = 1000
}
