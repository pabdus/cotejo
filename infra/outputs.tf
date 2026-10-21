output "url" {
  description = "Function URL pública: va en data-api del <main> de lab/cotejo.html y en el chequeo de salud del portafolio"
  value       = aws_lambda_function_url.demo.function_url
}

output "funcion" {
  description = "Nombre de la función, para invocar la sonda o leer sus registros"
  value       = aws_lambda_function.demo.function_name
}

output "freno" {
  description = "Función que pone la concurrencia de la demo a 0 cuando salta una alarma de invocaciones (sus registros dicen cuál y por qué)"
  value       = aws_lambda_function.freno.function_name
}

output "bucket" {
  description = "Bucket del índice y la caché (para subir el índice a mano con aws s3 cp)"
  value       = aws_s3_bucket.demo.id
}
