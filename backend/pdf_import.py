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
# Las guías reales de Walmart MX son etiquetas FedEx RASTERIZADAS (sin capa de
# texto) que NO traen el producto: solo destinatario, PO y tracking. Por eso:
#   1. Si la página no tiene texto, se lee por OCR (tesseract, si está
#      instalado) para identificar la guía (PO / TRK# / destinatario).
#   2. El producto llega después: cruce con la API de Walmart (por PO) o
#      captura manual en la interfaz → regenerar talones.
_SKU_RE = re.compile(r"\bSKU:?\s*([A-Za-z0-9_\-\.]{3,40})")
_QTY_RE = re.compile(r"\b(?:Cantidad|Qty|Piezas|Pzas?)\.?:?\s*(\d{1,3})\b", re.I)
_PRODUCT_RE = re.compile(r"\b(?:Producto|Descripci[oó]n|Art[ií]culo|Item)\.?:?\s*(.{4,80})", re.I)
# El bloque inferior impreso a máquina ("shipViaPo/shipViaOrder") es lo que el
# OCR lee más fiable en todos los formatos de guía (FedEx, MXFDD…); el "PO:"
# grande de arriba suele salir corrupto (espacios/dígitos fantasma).
_SHIPVIA_PO_RE = re.compile(r"shipViaPo\s*:?\s*([0-9]{10,18})", re.I)
_SHIPVIA_ORDER_RE = re.compile(r"shipViaOrder\s*:?\s*([0-9]{10,18})", re.I)
_ORDER_RE = re.compile(r"\bPO:?\W{0,3}([0-9][0-9 ]{8,20}[0-9])\b|\b(?:Pedido|Orden|Order)\W{0,3}(?:No\W{0,3})?#?\s*([0-9\-]{6,20})", re.I)
_TRK_RE = re.compile(r"\bTRK\W{0,3}#?\s*([0-9][0-9 ]{8,22}[0-9])|\bTracking\W{0,3}#?\s*([A-Z0-9]{10,24})", re.I)
_TO_RE = re.compile(r"\b(?:TO|Para)[:\s]+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\- ]{4,45})", re.I)


def _import_walmart(pdf_bytes: bytes, with_stub: bool) -> tuple[bytes, list[dict]]:
    try:
        src = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfImportError(f"No se pudo abrir el PDF: {exc}") from exc
    try:
        if src.page_count == 0:
            raise PdfImportError("El PDF no tiene páginas.")
        meta = []
        for i in range(src.page_count):
            page = src.load_page(i)
            text = page.get_text()
            if len(text.strip()) < 20:          # etiqueta rasterizada → OCR
                text = _ocr_page(page)
            meta.append(_parse_walmart_page(text))
        result = src.tobytes()
    finally:
        src.close()
    if with_stub:
        result = tiktok_import.stub_per_page(result, meta)
    return result, meta


def ocr_available() -> bool:
    import shutil
    return shutil.which("tesseract") is not None


def _ocr_page(page: fitz.Page) -> str:
    """OCR de una página rasterizada vía tesseract (si está instalado)."""
    if not ocr_available():
        return ""
    import subprocess
    import tempfile
    pix = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as f:
        pix.save(f.name)
        try:
            out = subprocess.run(
                ["tesseract", f.name, "stdout", "-l", "spa+eng", "--psm", "4"],
                capture_output=True, text=True, timeout=60)
            return out.stdout or ""
        except Exception:
            return ""


def _parse_walmart_page(text: str) -> dict:
    flat = " ".join(text.split())
    sku = _SKU_RE.search(flat)
    qty = _QTY_RE.search(flat)
    title = _PRODUCT_RE.search(flat)
    ship_po = _SHIPVIA_PO_RE.search(flat)
    ship_order = _SHIPVIA_ORDER_RE.search(flat)
    order = _ORDER_RE.search(flat)
    trk = _TRK_RE.search(flat)
    to = _TO_RE.search(flat)
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
    buyer = ""
    if to:
        # el OCR aplana columnas: cortar el nombre donde empieza otro campo del label
        buyer = re.split(r"\b(?:SHIP|CAD|ACTWGT|DIMMED|BILL|REF|INV|PO|RMA|DEPT|TEL)\b",
                         to.group(1), 1)[0].strip()
    order_id = ""
    if ship_po:
        order_id = ship_po.group(1)
    elif order:
        order_id = (order.group(1) or order.group(2) or "").replace(" ", "")
    return {"order_id": order_id,
            "order_alt": ship_order.group(1) if ship_order else "",
            "tracking": (trk.group(1) or trk.group(2) or "").replace(" ", "") if trk else "",
            "buyer": buyer, "products": products}
