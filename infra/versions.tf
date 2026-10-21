terraform {
  # use_lockfile (bloqueo del estado con un archivo en el propio S3, sin tabla de DynamoDB)
  # no existe antes de la 1.10.
  required_version = ">= 1.10"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    # Solo para el zip del freno (infra/freno/handler.py): un archivo sin dependencias no
    # justifica pasar por demo/empaquetar.py.
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
  }

  # El estado vive en el bucket de estado del portafolio (versionado y cifrado), en su propia
  # clave: así un apply de Cotejo nunca toca el estado del sitio, y no hay un segundo bucket
  # de estado que mantener. Configuración parcial: bucket, región y perfil van en backend.hcl,
  # que no se versiona (ver backend.hcl.example).
  #   terraform init -backend-config=backend.hcl
  backend "s3" {
    key          = "cotejo/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region  = var.region
  profile = var.perfil

  # La etiqueta proyecto es la que filtra el presupuesto (ver «Costo» en main.tf): un recurso
  # sin ella gastaría fuera de los US$ 5 vigilados.
  default_tags {
    tags = { proyecto = "cotejo", entorno = "demo" }
  }
}
