"""Uso:  python -m cotejo escrito.docx [--html informe.html] [--json salida.json]

Verifica las citas jurisprudenciales de un escrito (docx, pdf, odt, rtf, html,
md, txt) contra las fuentes oficiales y muestra el resultado.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import documentos, informe
from .fuentes import corte_constitucional as cc
from .verificacion import Estado, verificar


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cotejo", description=__doc__.split("\n")[2])
    p.add_argument("fichero", type=Path, nargs="?", help="escrito a verificar")
    p.add_argument("--html", type=Path, help="guarda el informe en HTML")
    p.add_argument("--json", type=Path, help="guarda el resultado en JSON")
    p.add_argument("--sin-red", action="store_true",
                   help="no consulta la relatoría: solo el índice local")
    p.add_argument("--sin-pasajes", action="store_true",
                   help="no comprueba las citas textuales entre comillas")
    p.add_argument("--actualizar-indice", action="store_true",
                   help="descarga de nuevo el índice oficial de la Corte")
    args = p.parse_args(argv)

    if args.actualizar_indice:
        indice = cc.Indice.cargar(forzar_descarga=True)
        print(f"Índice actualizado: {len(indice)} providencias.")
        if not args.fichero:
            return 0
    if not args.fichero:
        p.print_help()
        return 2

    try:
        doc = documentos.importar(args.fichero.name, args.fichero.read_bytes())
    except (OSError, documentos.ErrorDocumento) as exc:
        print(f"No se pudo leer {args.fichero}: {exc}", file=sys.stderr)
        return 2

    inf = verificar(doc["texto"], documento=args.fichero.name,
                    relatoria=not args.sin_red, pasajes=not args.sin_pasajes)
    print(informe.a_texto(inf))
    if args.html:
        args.html.write_text(informe.a_html(inf), encoding="utf-8")
        print(f"Informe HTML: {args.html}")
    if args.json:
        args.json.write_text(informe.a_json_texto(inf), encoding="utf-8")
        print(f"JSON: {args.json}")
    # Código de salida útil para integrarlo: 1 si hay algo que requiere atención
    return 1 if inf.problemas or inf.contar(Estado.SIN_CONFIRMAR) else 0


if __name__ == "__main__":
    sys.exit(main())
