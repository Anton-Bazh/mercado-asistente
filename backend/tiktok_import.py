"""Importador del PDF de etiquetas del seller center de TikTok Shop.

TikTok Shop (MX) entrega las guías como un PDF Carta con DOS envíos por hoja:
en cada mitad, la guía de la paquetería ocupa la izquierda y un «Packing List»
con el detalle (Product Name / SKU / Seller SKU / Qty / Order ID) la derecha.
Al recortar la guía para pegarla en el paquete, el detalle se pierde — y el
empaque acababa escribiendo el producto a mano.

Este módulo procesa ese PDF: separa cada mitad, RECORTA la región de la guía
(vectorial, sin rasterizar), lee el packing list por posición de palabras y le
inyecta el talón de control (label_stub) con producto, seller SKU y piezas.
El resultado es un PDF de una guía por página listo para el empaquetado n-up.
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF

import label_stub

# Dimensiones de la guía recortada observadas en los PDFs reales del seller
# center (~11.2 x 14 cm). Se usan también para la vista previa del acomodo.
GUIDE_W_PT = 318.0
GUIDE_H_PT = 396.0

_HALF_H = 396.0  # media hoja Carta


class TikTokImportError(Exception):
    pass


def import_pdf(pdf_bytes: bytes, with_stub: bool = True) -> tuple[bytes, list[dict]]:
    """Devuelve (pdf de guías individuales — una por página —, metadatos).

    metadatos: [{order_id, tracking, products:[{title, sku, quantity}]}] en el
    mismo orden que las páginas del PDF resultante.
    """
    try:
        src = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise TikTokImportError(f"No se pudo abrir el PDF: {exc}") from exc
    try:
        out = fitz.open()
        try:
            meta: list[dict] = []
            for pno in range(src.page_count):
                page = src.load_page(pno)
                words = page.get_text("words")
                for y0 in (0.0, _HALF_H):
                    half = [w for w in words if y0 <= w[1] < y0 + _HALF_H]
                    if len(half) < 10:      # mitad vacía (número impar de guías)
                        continue
                    info = _parse_packing(half)
                    cut_x = _packing_x(half)
                    clip = fitz.Rect(0, y0, cut_x, y0 + _HALF_H)
                    guide = out.new_page(width=clip.width, height=_HALF_H)
                    guide.show_pdf_page(fitz.Rect(0, 0, clip.width, _HALF_H),
                                        src, pno, clip=clip)
                    meta.append(info)
            if not out.page_count:
                raise TikTokImportError(
                    "No se encontraron guías en el PDF (¿es el archivo de etiquetas del seller center?).")
            result = out.tobytes()
        finally:
            out.close()
    finally:
        src.close()

    if with_stub:
        result = stub_per_page(result, meta)
    return result, meta


def stub_per_page(pdf_bytes: bytes, meta: list[dict]) -> bytes:
    """Talón por página con los datos de SU pedido (cada guía es distinta)."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out = fitz.open()
        try:
            for i in range(src.page_count):
                info = meta[i] if i < len(meta) else {}
                one = fitz.open()
                try:
                    one.insert_pdf(src, from_page=i, to_page=i)
                    page_bytes = one.tobytes()
                finally:
                    one.close()
                if info.get("products"):
                    page_bytes = label_stub.add_stub(
                        page_bytes, info["products"],
                        order_ref=str(info.get("order_id") or ""))
                doc = fitz.open(stream=page_bytes, filetype="pdf")
                try:
                    out.insert_pdf(doc)
                finally:
                    doc.close()
            return out.tobytes()
        finally:
            out.close()
    finally:
        src.close()


# --- lectura del packing list --------------------------------------------------
def _packing_x(half_words: list) -> float:
    """x donde inicia el packing list (título «Packing List»); corte de la guía."""
    for w in half_words:
        if w[4] == "Packing":
            return max(60.0, w[0] - 8.0)
    return GUIDE_W_PT   # sin packing list visible: corte por defecto


def _parse_packing(half_words: list) -> dict:
    px = _packing_x(half_words)
    region = sorted((w for w in half_words if w[0] >= px),
                    key=lambda w: (round(w[1]), w[0]))
    text = " ".join(w[4] for w in region)

    order_id = _first(r"Order ID:?\s*(\d+)", text)
    tracking = _first(r"Tracking number:?\s*([A-Z0-9]+)", text.replace(" ", " "))

    # La tabla usa una plantilla fija; los valores NO se alinean bajo los
    # encabezados, así que las columnas se ubican por desplazamiento relativo
    # al inicio de la tabla (x del encabezado «Product Name»), medido en los
    # PDFs reales del seller center:
    #   nombre 0–108 pt · SKU TikTok («Por defecto») 111–150 · Seller SKU
    #   153–243 · cantidad 243+.
    prod_head = next((w for w in region if w[4] == "Product"), None)
    products: list[dict] = []
    if prod_head:
        anchor, y_head = prod_head[0], prod_head[3]
        x_title_max = anchor + 108
        x_seller_min, x_seller_max = anchor + 153, anchor + 243
        y_end = min((w[1] for w in region
                     if w[4] == "Total:" and w[1] > y_head), default=1e9)
        rows = [w for w in region if y_head < w[1] < y_end]
        # Cada producto arranca en la línea donde aparece su cantidad (columna
        # Qty); el nombre puede continuar en las líneas siguientes del bloque.
        qty_marks = sorted((w for w in rows if w[0] >= x_seller_max
                            and w[4].isdigit()), key=lambda w: w[1])
        for i, qw in enumerate(qty_marks):
            y_a = qw[1] - 2
            y_b = qty_marks[i + 1][1] - 2 if i + 1 < len(qty_marks) else y_end
            block = [w for w in rows if y_a <= w[1] < y_b]
            title = " ".join(w[4] for w in sorted(
                (w for w in block if w[0] < x_title_max), key=lambda w: (w[1], w[0])))
            seller_sku = "".join(w[4] for w in sorted(
                (w for w in block if x_seller_min <= w[0] < x_seller_max),
                key=lambda w: (w[1], w[0])))
            if title:
                products.append({"title": title, "sku": seller_sku or "s/n",
                                 "quantity": int(qw[4])})
    return {"order_id": order_id or "", "tracking": tracking or "",
            "products": products}


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None
