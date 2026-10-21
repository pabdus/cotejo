"""Freno automático de la demo «Cotejo en vivo» (DISEÑO.md §3).

Lo invoca SNS cuando una alarma de invocaciones de cotejo-demo pasa a ALARM (la
de hora, más de 1.000 en 60 minutos, y también la diaria, que publica en el
mismo tema). Pone la concurrencia reservada de cotejo-demo a 0: desde ese
momento la Function URL responde 429 sin ejecutar la función, y una inundación
deja de costar. El presupuesto avisaría con horas de retraso y no corta nada;
a 10 peticiones por segundo, el tope de una Function URL con concurrencia 1,
los US$ 5 del mes se van en unas dos semanas sin que nadie mire el correo.

No reactiva nunca: eso se hace a mano, después de mirar qué pasó (cabecera de
infra/main.tf). Solo biblioteca estándar y boto3, que viene con el runtime.

El rol solo puede lambda:PutFunctionConcurrency sobre cotejo-demo y escribir en
su propio grupo de registros. IAM no puede limitar el NÚMERO que se pone con ese
permiso, así que el número es una constante del código y nunca sale del mensaje:
en el tema puede publicar cualquiera con sns:Publish en la cuenta.
"""
from __future__ import annotations

import json
import os

CONCURRENCIA_FRENADA = 0

# Estados de una alarma de CloudWatch que no son una alarma. Hoy las alarmas no
# tienen ok_actions y no llegan; si algún día se añaden, la vuelta a OK no puede
# frenar la demo que se acaba de reactivar.
_NO_FRENAN = {"OK", "INSUFFICIENT_DATA"}

_lambda = None


def _cliente():
    # Perezoso, como en demo/lambda_function.py: boto3 viene con el runtime de
    # Lambda pero no con el .venv de Cotejo, y las pruebas inyectan uno falso.
    global _lambda
    if _lambda is None:
        import boto3
        _lambda = boto3.client("lambda")
    return _lambda


def _registrar(datos: dict) -> None:
    """Una línea JSON en CloudWatch, como la función que frena."""
    print(json.dumps(datos, ensure_ascii=False), flush=True)


def _avisos(event) -> list[dict]:
    """Nombre de la alarma, estado nuevo y motivo de cada mensaje de SNS.

    Un mensaje que no se entiende queda con estado None, y eso frena: ante la
    duda, una demo parada cuesta 0 y una inundación no. Lo mismo una invocación
    directa sin Records, que es la forma de frenar a mano.
    """
    registros = event.get("Records") if isinstance(event, dict) else None
    avisos = []
    for registro in registros if isinstance(registros, list) else []:
        sns = registro.get("Sns") if isinstance(registro, dict) else None
        mensaje = sns.get("Message") if isinstance(sns, dict) else None
        try:
            datos = json.loads(mensaje) if isinstance(mensaje, str) else None
        except ValueError:
            datos = None
        if not isinstance(datos, dict):
            datos = {}
        estado = datos.get("NewStateValue")
        avisos.append({
            "alarma": str(datos.get("AlarmName"))[:256] if datos.get("AlarmName") else None,
            "estado": estado if isinstance(estado, str) else None,
            # «Threshold Crossed: 1 datapoint [1234.0 (...)] was greater than...»:
            # cuántas invocaciones hubo, que es lo que hay que mirar antes de reactivar
            "motivo": str(datos.get("NewStateReason"))[:500] if datos.get("NewStateReason") else None,
        })
    return avisos


def handler(event, context):
    # Sin FUNCION, KeyError: un error a la vista (y los dos reintentos de una
    # invocación asíncrona) antes que un freno que no frena y no lo dice.
    funcion = os.environ["FUNCION"]
    avisos = _avisos(event)
    if avisos and all(a["estado"] in _NO_FRENAN for a in avisos):
        _registrar({"evento": "freno_ignorado", "funcion": funcion, "avisos": avisos})
        return {"frenada": False, "funcion": funcion}

    # Si falla (permiso, límite de la API), la excepción sube a propósito: Lambda
    # reintenta la invocación asíncrona dos veces, y poner 0 otra vez no hace daño.
    r = _cliente().put_function_concurrency(
        FunctionName=funcion, ReservedConcurrentExecutions=CONCURRENCIA_FRENADA)
    concurrencia = r.get("ReservedConcurrentExecutions") if isinstance(r, dict) else None
    _registrar({"evento": "frenada", "funcion": funcion, "concurrencia": concurrencia,
                "avisos": avisos})
    return {"frenada": True, "funcion": funcion, "concurrencia": concurrencia}
