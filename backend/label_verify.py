"""Cuadre PDF↔API del código físico impreso — mitigación de H6.

La API de ningún marketplace garantiza que el número que expone (tracking,
shipment_id…) sea exactamente el que codifica el código de barras FÍSICO de la
etiqueta: Mercado Libre trae `tracking_number` en el shipment pero no se había
confirmado 1:1 contra la impresión real; Walmart expone su `trackingNumber`
pero la guía es una imagen FedEx rasterizada (sin capa de texto); TikTok usa
`package_id`/tracking del paquete pero el transportista real no siempre lo
refleja igual.

Requisito de Antonio (07-jul-2026): el barcode impreso SE GUARDA SÍ O SÍ, y su
fuente primaria es el PDF (lo físicamente impreso) — la API es respaldo. Este
módulo extrae los dígitos de la capa de texto del PDF (con OCR de respaldo si
la etiqueta viene rasterizada, como Walmart) y arbitra contra el código de la
API:

  - PDF y API coinciden            → "match"    (code = el extraído)
  - PDF trae código, API no aporta  → "match"    (el PDF manda igual)
  - PDF y API difieren              → "mismatch" (el PDF gana; WARNING)
  - PDF ilegible, hay código de API → "ilegible" (respaldo API; sin cuadre)
  - ninguna fuente tiene código     → "revisar"  (code None; nunca se pierde
                                       la fila ni se detiene el lote)

Se aplica en print_jobs._run, tras get_label y antes del estampado
(label_enrich). Alcance de registro en las fases 1-3: solo Mercado Libre; el
módulo queda listo con adaptadores por proveedor para enchufar Walmart/TikTok
cuando se decida (Cambio 3.2 de la guía de unificación).
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF

import logutil

log = logutil.get_logger("cuadre")

# Candidato a "código de barras": secuencia larga de dígitos. 8+ evita
# confundirlo con CPs (5) o folios (1-4 dígitos) que puedan aparecer cerca.
_CODE_RE = re.compile(r"\d{8,}")


def verify(pdf_bytes: bytes, provider: str, api_code: str | None) -> dict:
    """Cuadra el código físico del PDF contra el de la API.

    Devuelve {"code": str|None, "flag": "match"|"mismatch"|"ilegible"|"revisar",
    "extracted": str|None} — `code` es lo que se debe registrar en `etiquetas_i`.
    """
    api_digits = _digits(api_code)
    extracted = _extract_code(pdf_bytes)

    if extracted:
        if api_digits and extracted != api_digits:
            log.warning(
                "%s cuadre MISMATCH: PDF=%s vs API=%s — se registra el del PDF "
                "(lo físicamente impreso).", provider, extracted, api_digits)
            return {"code": extracted, "flag": "mismatch", "extracted": extracted}
        return {"code": extracted, "flag": "match", "extracted": extracted}

    if api_digits:
        log.info("%s etiqueta sin capa de texto legible (ni OCR): respaldo API "
                  "= %s, bandera 'sin cuadre'.", provider, api_digits)
        return {"code": api_digits, "flag": "ilegible", "extracted": None}

    log.warning("%s ninguna fuente tiene código físico (PDF ni API) — se "
                "registra con code NULL y bandera REVISAR.", provider)
    return {"code": None, "flag": "revisar", "extracted": None}


# --- helpers -------------------------------------------------------------------
def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", str(s)) if s else ""


def _extract_code(pdf_bytes: bytes) -> str | None:
    """Dígitos más largos hallados en la 1ª página (texto nativo u OCR)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    try:
        if doc.page_count == 0:
            return None
        page = doc.load_page(0)
        text = page.get_text()
        if len(text.strip()) < 5:      # sin capa de texto → probablemente rasterizada
            text = _ocr(page)
        candidates = _CODE_RE.findall(text)
        return max(candidates, key=len) if candidates else None
    finally:
        doc.close()


def _ocr(page: "fitz.Page") -> str:
    """OCR de respaldo (tesseract) para etiquetas rasterizadas (p. ej. Walmart)."""
    try:
        import pdf_import
    except ImportError:
        return ""
    if not pdf_import.ocr_available():
        return ""
    try:
        return pdf_import._ocr_page(page)
    except Exception:
        return ""
