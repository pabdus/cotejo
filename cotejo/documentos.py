"""Importacion y exportacion de documentos.

Convierte lo que le eches a texto plano con marcado ligero (Markdown), que es
lo que entiende el resto del programa, y vuelve a exportar a varios formatos.

Formatos de entrada: txt, md, docx, odt, pdf, rtf, html, epub, csv, json.
Formatos de salida:  txt, md, docx, html.

Solo el PDF necesita una dependencia externa (pypdf). El resto se resuelve con
la biblioteca estandar: docx, odt y epub son ficheros ZIP con XML dentro.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from html.parser import HTMLParser

FORMATOS_ENTRADA = {
    ".txt": "Texto plano",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".text": "Texto plano",
    ".docx": "Word",
    ".odt": "OpenDocument",
    ".pdf": "PDF",
    ".rtf": "RTF",
    ".html": "HTML",
    ".htm": "HTML",
    ".epub": "EPUB",
    ".csv": "CSV",
    ".json": "JSON",
}

FORMATOS_SALIDA = {
    "txt": "Texto plano (.txt)",
    "md": "Markdown (.md)",
    "docx": "Word (.docx)",
    "html": "HTML (.html)",
}

LIMITE_BYTES = 25 * 1024 * 1024


class ErrorDocumento(Exception):
    """Fallo controlado al leer o escribir un documento."""


# ==========================================================================
# Utilidades comunes
# ==========================================================================
def _limpiar(texto: str) -> str:
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _decodificar(datos: bytes) -> str:
    """Prueba codificaciones en el orden mas probable en Windows/España."""
    for codec in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return datos.decode(codec)
        except UnicodeDecodeError:
            continue
    return datos.decode("utf-8", errors="replace")


# ==========================================================================
# HTML
# ==========================================================================
class _ExtractorHTML(HTMLParser):
    """Convierte HTML a texto con marcado ligero, saltando script y style."""

    SALTO = {"p", "div", "br", "tr", "section", "article", "blockquote"}
    TITULOS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self.ignorar = 0
        self.titulo_abierto: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "noscript"):
            self.ignorar += 1
        elif tag in self.TITULOS:
            self.partes.append("\n\n" + self.TITULOS[tag] + " ")
            self.titulo_abierto = tag
        elif tag in ("li",):
            self.partes.append("\n- ")
        elif tag in self.SALTO:
            self.partes.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "noscript"):
            self.ignorar = max(0, self.ignorar - 1)
        elif tag == self.titulo_abierto:
            self.partes.append("\n\n")
            self.titulo_abierto = None

    def handle_data(self, data):
        if self.ignorar:
            return
        if data.strip():
            self.partes.append(re.sub(r"\s+", " ", data))

    def texto(self) -> str:
        return _limpiar("".join(self.partes))


def _de_html(datos: bytes) -> str:
    extractor = _ExtractorHTML()
    extractor.feed(_decodificar(datos))
    return extractor.texto()


# ==========================================================================
# DOCX (Word) — ZIP con XML dentro
# ==========================================================================
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _de_docx(datos: bytes) -> str:
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ErrorDocumento(
            "No parece un .docx valido. Si es un .doc antiguo, abrelo en Word "
            "y guardalo como .docx."
        ) from exc

    raiz = ET.fromstring(xml)
    lineas: list[str] = []

    for parrafo in raiz.iter(f"{_NS_W}p"):
        # Nivel de encabezado, si el parrafo usa un estilo Heading/Titulo
        nivel = 0
        estilo = parrafo.find(f"{_NS_W}pPr/{_NS_W}pStyle")
        if estilo is not None:
            val = (estilo.get(f"{_NS_W}val") or "").lower()
            m = re.search(r"(?:heading|titulo|ttulo|encabezado)\s*(\d)", val)
            if m:
                nivel = int(m.group(1))
            elif val in ("title", "titulo"):
                nivel = 1

        # Es un elemento de lista?
        es_lista = parrafo.find(f"{_NS_W}pPr/{_NS_W}numPr") is not None

        trozos: list[str] = []
        for nodo in parrafo.iter():
            etiqueta = nodo.tag
            if etiqueta == f"{_NS_W}t":
                trozos.append(nodo.text or "")
            elif etiqueta in (f"{_NS_W}tab",):
                trozos.append("\t")
            elif etiqueta in (f"{_NS_W}br", f"{_NS_W}cr"):
                trozos.append("\n")

        texto = "".join(trozos).strip()
        if not texto:
            lineas.append("")
            continue
        if nivel:
            lineas.append("#" * min(nivel, 6) + " " + texto)
        elif es_lista:
            lineas.append("- " + texto)
        else:
            lineas.append(texto)

    return _limpiar("\n\n".join(l for l in lineas if l != "") if lineas else "")


# ==========================================================================
# ODT (LibreOffice) — ZIP con content.xml
# ==========================================================================
_NS_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def _de_odt(datos: bytes) -> str:
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            xml = z.read("content.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ErrorDocumento("No parece un .odt valido.") from exc

    raiz = ET.fromstring(xml)
    lineas: list[str] = []

    def texto_de(nodo) -> str:
        return "".join(nodo.itertext()).strip()

    for nodo in raiz.iter():
        if nodo.tag == f"{_NS_TEXT}h":
            nivel = int(nodo.get(f"{_NS_TEXT}outline-level", "1") or 1)
            contenido = texto_de(nodo)
            if contenido:
                lineas.append("#" * min(nivel, 6) + " " + contenido)
        elif nodo.tag == f"{_NS_TEXT}p":
            contenido = texto_de(nodo)
            if contenido:
                lineas.append(contenido)
        elif nodo.tag == f"{_NS_TEXT}list-item":
            contenido = texto_de(nodo)
            if contenido:
                lineas.append("- " + contenido)

    return _limpiar("\n\n".join(lineas))


# ==========================================================================
# EPUB — ZIP de XHTML
# ==========================================================================
def _de_epub(datos: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            nombres = [
                n for n in z.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
                and "nav" not in n.lower() and "toc" not in n.lower()
            ]
            nombres.sort()
            partes = []
            for nombre in nombres[:200]:
                extractor = _ExtractorHTML()
                extractor.feed(_decodificar(z.read(nombre)))
                trozo = extractor.texto()
                if trozo:
                    partes.append(trozo)
    except zipfile.BadZipFile as exc:
        raise ErrorDocumento("No parece un .epub valido.") from exc
    return _limpiar("\n\n".join(partes))


# ==========================================================================
# PDF
# ==========================================================================
def _de_pdf(datos: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ErrorDocumento(
            "Para leer PDF hace falta pypdf. Instalalo con: pip install pypdf"
        ) from exc

    try:
        lector = PdfReader(io.BytesIO(datos))
        if lector.is_encrypted:
            try:
                lector.decrypt("")
            except Exception as exc:
                raise ErrorDocumento("El PDF esta protegido con contrasena.") from exc
        paginas = [(p.extract_text() or "") for p in lector.pages]
    except ErrorDocumento:
        raise
    except Exception as exc:
        raise ErrorDocumento(f"No se pudo leer el PDF: {exc}") from exc

    texto = "\n\n".join(paginas)
    # Los PDF parten las palabras con guion al final de linea
    texto = re.sub(r"(\w)-\n(\w)", r"\1\2", texto)
    # Y cortan las frases a lo ancho de la caja: se recomponen los parrafos
    texto = re.sub(r"(?<![.!?:;\-])\n(?![\n\s•\-*\d])", " ", texto)
    if not texto.strip():
        raise ErrorDocumento(
            "El PDF no contiene texto extraible. Si es un escaneo, "
            "necesitas pasarle un OCR antes."
        )
    return _limpiar(texto)


# ==========================================================================
# RTF
# ==========================================================================
def _de_rtf(datos: bytes) -> str:
    crudo = _decodificar(datos)
    # Grupos que no se imprimen (fuentes, colores, informacion del documento)
    crudo = re.sub(r"\\\*\\[a-z]+[^{}]*", "", crudo)
    for bloque in ("fonttbl", "colortbl", "stylesheet", "info", "pict"):
        crudo = re.sub(r"\{\\" + bloque + r"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "", crudo)
    crudo = re.sub(r"\\par[d]?\b", "\n", crudo)
    crudo = re.sub(r"\\line\b", "\n", crudo)
    crudo = re.sub(r"\\tab\b", "\t", crudo)
    # Caracteres escapados en hexadecimal: \'e9 -> é
    crudo = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", errors="replace"),
        crudo,
    )
    crudo = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), crudo)
    crudo = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", crudo)
    crudo = crudo.replace("{", "").replace("}", "")
    return _limpiar(crudo)


# ==========================================================================
# CSV y JSON
# ==========================================================================
def _de_csv(datos: bytes) -> str:
    texto = _decodificar(datos)
    try:
        dialecto = csv.Sniffer().sniff(texto[:4000], delimiters=",;\t|")
    except csv.Error:
        dialecto = csv.excel
    filas = list(csv.reader(io.StringIO(texto), dialecto))
    return _limpiar("\n\n".join(" ".join(c.strip() for c in fila if c.strip()) for fila in filas))


def _de_json(datos: bytes) -> str:
    try:
        objeto = json.loads(_decodificar(datos))
    except json.JSONDecodeError as exc:
        raise ErrorDocumento(f"JSON mal formado: {exc}") from exc

    trozos: list[str] = []

    def recorrer(nodo):
        if isinstance(nodo, str):
            if len(nodo.strip()) > 1:
                trozos.append(nodo.strip())
        elif isinstance(nodo, dict):
            for valor in nodo.values():
                recorrer(valor)
        elif isinstance(nodo, list):
            for valor in nodo:
                recorrer(valor)

    recorrer(objeto)
    return _limpiar("\n\n".join(trozos))


# ==========================================================================
# Punto de entrada de importacion
# ==========================================================================
_LECTORES = {
    ".docx": _de_docx,
    ".odt": _de_odt,
    ".pdf": _de_pdf,
    ".rtf": _de_rtf,
    ".html": _de_html,
    ".htm": _de_html,
    ".epub": _de_epub,
    ".csv": _de_csv,
    ".json": _de_json,
}


def extension(nombre: str) -> str:
    punto = nombre.rfind(".")
    return nombre[punto:].lower() if punto >= 0 else ""


def importar(nombre: str, datos: bytes) -> dict:
    """Extrae el texto de un documento. Devuelve texto, formato y avisos."""
    if len(datos) > LIMITE_BYTES:
        raise ErrorDocumento(
            f"El fichero pesa {len(datos) / 1024 / 1024:.1f} MB y el limite son "
            f"{LIMITE_BYTES // 1024 // 1024} MB."
        )
    if not datos:
        raise ErrorDocumento("El fichero esta vacio.")

    ext = extension(nombre)
    if ext == ".doc":
        raise ErrorDocumento(
            "El formato .doc antiguo no se admite. Abrelo en Word o LibreOffice "
            "y guardalo como .docx."
        )

    lector = _LECTORES.get(ext)
    if lector is not None:
        texto = lector(datos)
    elif ext in FORMATOS_ENTRADA or ext == "":
        texto = _limpiar(_decodificar(datos))
    else:
        # Sin extension conocida: si parece texto, se acepta igual
        texto = _limpiar(_decodificar(datos))
        if "\x00" in texto[:2000]:
            raise ErrorDocumento(f"Formato no admitido: {ext or 'desconocido'}")

    if not texto.strip():
        raise ErrorDocumento("No se ha podido extraer texto del documento.")

    return {
        "texto": texto,
        "formato": FORMATOS_ENTRADA.get(ext, "Texto"),
        "extension": ext,
        "nombre": nombre,
        "bytes": len(datos),
        "palabras": len(re.findall(r"[^\W\d_]+", texto, re.UNICODE)),
    }


# ==========================================================================
# Exportacion
# ==========================================================================
# Caracteres ilegales en XML 1.0: los controles salvo tabulador, salto de
# linea y retorno de carro. Un PDF o un texto pegado los arrastra sin que se
# vean, y colarlos en el .docx produce un word/document.xml mal formado que
# Word se niega a abrir.
_RE_CONTROL_XML = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f￾￿]"
)


def _escapar_xml(texto: str) -> str:
    texto = _RE_CONTROL_XML.sub("", texto)
    return (
        texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def a_docx(texto: str, titulo: str = "Documento") -> bytes:
    """Genera un .docx valido sin dependencias externas.

    Un .docx es un ZIP con cuatro piezas minimas: los tipos de contenido, la
    relacion raiz, el documento y sus relaciones.
    """
    parrafos_xml: list[str] = []
    for bloque in texto.split("\n\n"):
        bloque = bloque.strip()
        if not bloque:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", bloque)
        if m:
            nivel = len(m.group(1))
            contenido = _escapar_xml(m.group(2))
            parrafos_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading{nivel}"/></w:pPr>'
                f'<w:r><w:t xml:space="preserve">{contenido}</w:t></w:r></w:p>'
            )
            continue
        # Los saltos simples dentro de un bloque se conservan como <w:br/>
        lineas = bloque.split("\n")
        piezas = []
        for i, linea in enumerate(lineas):
            if i:
                piezas.append("<w:br/>")
            piezas.append(
                f'<w:t xml:space="preserve">{_escapar_xml(linea)}</w:t>'
            )
        parrafos_xml.append(f"<w:p><w:r>{''.join(piezas)}</w:r></w:p>")

    documento = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(parrafos_xml)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1417" w:right="1701" w:bottom="1417" w:left="1701"/>'
        "</w:sectPr></w:body></w:document>"
    )

    tipos = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    rels_doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", tipos)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", documento)
        z.writestr("word/_rels/document.xml.rels", rels_doc)
    return buffer.getvalue()


def a_html(texto: str, titulo: str = "Documento") -> bytes:
    cuerpo: list[str] = []
    for bloque in texto.split("\n\n"):
        bloque = bloque.strip()
        if not bloque:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", bloque)
        if m:
            n = len(m.group(1))
            cuerpo.append(f"<h{n}>{html.escape(m.group(2))}</h{n}>")
        elif all(re.match(r"^\s*[-*]\s+", l) for l in bloque.split("\n")):
            items = "".join(
                f"<li>{html.escape(re.sub(r'^\s*[-*]\s+', '', l))}</li>"
                for l in bloque.split("\n")
            )
            cuerpo.append(f"<ul>{items}</ul>")
        else:
            cuerpo.append("<p>" + html.escape(bloque).replace("\n", "<br>") + "</p>")

    pagina = (
        "<!DOCTYPE html>\n<html lang=\"es\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{html.escape(titulo)}</title>\n"
        "<style>body{max-width:42rem;margin:3rem auto;padding:0 1rem;"
        "font:16px/1.7 Georgia,serif;color:#222}h1,h2,h3{line-height:1.3}</style>\n"
        "</head>\n<body>\n" + "\n".join(cuerpo) + "\n</body>\n</html>\n"
    )
    return pagina.encode("utf-8")


def exportar(texto: str, formato: str, titulo: str = "texto-humanizado") -> tuple[bytes, str, str]:
    """Devuelve (contenido, nombre_de_fichero, tipo_mime).

    Se limpian aqui los caracteres de control, en el unico punto por el que
    pasan todos los formatos: no se ven, los arrastran los PDF y el texto
    pegado, y en el .docx generan un XML que Word no puede abrir.
    """
    texto = _RE_CONTROL_XML.sub("", texto)
    formato = (formato or "txt").lower()
    if formato == "docx":
        return (
            a_docx(texto, titulo),
            f"{titulo}.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    if formato == "html":
        return a_html(texto, titulo), f"{titulo}.html", "text/html; charset=utf-8"
    if formato == "md":
        return texto.encode("utf-8"), f"{titulo}.md", "text/markdown; charset=utf-8"
    return texto.encode("utf-8"), f"{titulo}.txt", "text/plain; charset=utf-8"
