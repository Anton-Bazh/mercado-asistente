"""Importador de PDFs de etiquetas del seller center, por marketplace.

Punto de entrada único para procesar PDFs descargados a mano:
  - TikTok Shop: hoja Carta con 2 envíos (guía + packing list) → tiktok_import
    (recorte calibrado con PDFs reales + lectura del packing list).
  - Walmart: una guía por página; se conserva la página íntegra y se intenta
    leer producto/SKU/cantidad del texto. Cuando Antonio aporte un PDF real
    del seller center, el recorte y la lectura se calibran como en TikTok.

Si el talón de control está activo para el marketplace (label_stub), cada guía
sale con su talón; las guías sin producto detectado pasan intactas.
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF

import label_stub
import tiktok_import
from tiktok_import import TikTokImportError as PdfImportError  # error común

PROVIDERS = {"tiktok": "TikTok Shop", "walmart": "Walmart"}


def import_pdf(provider: str, pdf_bytes: bytes,
               with_stub: bool = True) -> tuple[bytes, list[dict]]:
    """Devuelve (pdf de guías — una por página —, metadatos por guía)."""
    if provider == "tiktok":
        return tiktok_import.import_pdf(pdf_bytes, with_stub)
    if provider == "walmart":
        return _import_walmart(pdf_bytes, with_stub)
    raise PdfImportError(f"Importación de PDF no soportada para «{provider}».")


# --- Walmart -------------------------------------------------------------------
# Palabras que suelen anteceder al dato en las guías/etiquetas de Walmart MX.
_SKU_RE = re.compile(r"\bSKU:?\s*([A-Za-z0-9_\-\.]{3,40})")
_QTY_RE = re.compile(r"\b(?:Cantidad|Qty|Piezas|Pzas?)\.?:?\s*(\d{1,3})\b", re.I)
_PRODUCT_RE = re.compile(r"\b(?:Producto|Descripci[oó]n|Art[ií]culo|Item)\.?:?\s*(.{4,80})", re.I)
_ORDER_RE = re.compile(r"\b(?:Pedido|Orden|Order|PO)\W{0,3}#?\s*([0-9\-]{6,20})", re.I)


def _import_walmart(pdf_bytes: bytes, with_stub: bool) -> tuple[bytes, list[dict]]:
    try:
        src = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfImportError(f"No se pudo abrir el PDF: {exc}") from exc
    try:
        if src.page_count == 0:
            raise PdfImportError("El PDF no tiene páginas.")
        meta = [_parse_walmart_page(src.load_page(i).get_text())
                for i in range(src.page_count)]
        result = src.tobytes()
    finally:
        src.close()
    if with_stub:
        result = tiktok_import.stub_per_page(result, meta)
    return result, meta


def _parse_walmart_page(text: str) -> dict:
    flat = " ".join(text.split())
    sku = _SKU_RE.search(flat)
    qty = _QTY_RE.search(flat)
    title = _PRODUCT_RE.search(flat)
    order = _ORDER_RE.search(flat)
    products = []
    if sku or title:
        name = (title.group(1).strip() if title else "").rstrip(".,;")
        # el texto viene aplanado: cortar el nombre donde empieza otro campo
        name = re.split(r"\b(?:SKU|Cantidad|Qty|Piezas|Pzas?|Pedido|Orden|Order)\b",
                        name, 1)[0].strip().rstrip(".,;:")
        products.append({
            "title": name or (f"SKU {sku.group(1)}" if sku else "—"),
            "sku": sku.group(1) if sku else "",
            "quantity": int(qty.group(1)) if qty else 1,
        })
    return {"order_id": order.group(1) if order else "",
            "tracking": "", "products": products}
