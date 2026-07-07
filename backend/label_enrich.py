"""Estampado de etiquetas — lógica portada del Extractor de Etiquetas.

Dibuja SOBRE la etiqueta individual que entrega la API (sin crecer el lienzo,
a diferencia del talón de label_stub): folio con el color del día, nombre de
empresa, logo, teléfono de contacto, punto rojo de margen bajo (markup ≤ 5) y
código de lote al pie. Se aplica ANTES del acomodo n-up.

Las posiciones son FRACCIONES del rectángulo de la etiqueta (derivadas de las
proporciones del Extractor sobre el formato de 3 por hoja carta) — ⚠ PENDIENTE
calibrar contra una etiqueta real de la API (H2/H6 de la guía de unificación).

Fuentes: las base del PDF (Helvetica bold) — Poppins no está en el sistema y
las fuentes base solo cubren latin-1 (misma regla que label_stub).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

import logutil

log = logutil.get_logger("estampa")

ASSETS = Path(__file__).parent / "assets" / "logos"

# --- Constantes de negocio (portadas 1:1 del Extractor) ----------------------
# Color del folio según el día (regla de operación del Extractor).
DAY_COLORS = {
    0: "#0000FF",  # Lunes    → Azul
    1: "#000000",  # Martes   → Negro
    2: "#008000",  # Miércoles→ Verde
    3: "#800080",  # Jueves   → Púrpura
    4: "#FF0000",  # Viernes  → Rojo
    5: "#FFA500",  # Sábado   → Naranja
    6: "#0000FF",  # Domingo (no se opera; azul por si acaso)
}

LOGO_FILES = {
    "HOGARDEN": "HOGARDEN.png",
    "INMATMEX": "INMATMEX.png",
    "MTM": "INMATMEX.png",
    "PALO DE ROSA": "PALODEROSA.png",
    "DO MESKA": "DOMESKA.png",
    "TOLEXAL": "TOLEXAL.png",
    "SUPER OFERTAS": "SUPER_OF.png",
    "TAL": "TAL.png",
}

CONTACTS = {
    "INMATMEX": "735 252 7148",
    "MTM": "735 252 7148",
    "DO MESKA": "735 252 7148",
    "TOLEXAL": "735 279 0563",
    "PALO DE ROSA": "735 252 7148",
    "HOGARDEN": "735 252 7148",
    "SUPER OFERTAS": "735 252 7148",
    "TAL": "735 252 7148",
}
DEFAULT_CONTACT = "735 252 7148"

# Umbral de margen bajo (punto rojo + auditoría) — regla del Extractor.
LOW_MARKUP = 5

# --- Zonas del estampado (fracciones del rect de la etiqueta) ----------------
# ⚠ CALIBRAR con etiqueta real de la API antes de la Fase 3 (ver guía §5 F2).
Z_DOT = (0.08, 0.08)          # centro del punto rojo (esquina sup. izquierda)
Z_FOLIO = (0.86, 0.24)        # folio (centrado en x)
Z_LOGO = (0.54, 0.52)         # centro-x del logo · y = base (borde inferior)
Z_CONTACT = (0.54, 0.60)      # "Número de Contacto" (centrado)

# Tamaños en puntos (el Extractor usaba px de canvas a 2×: px/2 = pt).
S_FOLIO = 21.0
S_EMPRESA = {"PALO DE ROSA": 11.5, "SUPER OFERTAS": 9.0}   # especiales
S_EMPRESA_DEF = 14.0
S_CONTACT_LABEL = 7.0
S_CONTACT_NUM = 12.0
S_BATCH = 10.0
R_DOT = 10.0
W_LOGO = {"INMATMEX": 90.0, "MTM": 90.0}                   # ancho especial
W_LOGO_DEF = 60.0


def normalize_company(name: str | None) -> str:
    """Misma normalización que el Extractor: TAL* → TAL, DOMESKA → DO MESKA."""
    key = (name or "").strip().upper()
    if "TAL" in key:
        key = "TAL"
    if key == "DOMESKA":
        key = "DO MESKA"
    return key


def day_color(dt: datetime | None = None) -> str:
    """Color hex del folio según el día de la semana (hoy por defecto)."""
    return DAY_COLORS[(dt or datetime.now()).weekday()]


def enrich(pdf_bytes: bytes, *, folio: int, company: str,
           batch_code: str, markup: float | None = None,
           color: str | None = None) -> bytes:
    """Estampa la etiqueta (todas sus páginas, normalmente una).

    folio: consecutivo de la etiqueta física · company: tienda/empresa (se
    normaliza) · batch_code: código de lote (pie) · markup: si ≤ 5 dibuja el
    punto rojo · color: hex del folio (por defecto, el del día).

    En producción cada envío llega en su propio PDF (1 página = 1 folio); si
    el PDF trae varias páginas (lote de muestra), el folio incrementa por
    página para previsualizar el consecutivo.
    """
    key = normalize_company(company)
    rgb = _hex_rgb(color or day_color())
    contact = CONTACTS.get(key, DEFAULT_CONTACT)
    logo = _logo_pixmap(key)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i, page in enumerate(doc):
            _stamp_page(page, folio=folio + i, company=key, rgb=rgb,
                        contact=contact, logo=logo, batch_code=batch_code,
                        low=(markup is not None and markup <= LOW_MARKUP))
        return doc.tobytes()
    finally:
        doc.close()


def _stamp_page(page: fitz.Page, *, folio: int, company: str,
                rgb: tuple, contact: str, logo, batch_code: str,
                low: bool) -> None:
    r = page.rect
    w, h = r.width, r.height

    # Punto rojo de margen bajo (alerta para empaque).
    if low:
        c = fitz.Point(r.x0 + w * Z_DOT[0], r.y0 + h * Z_DOT[1])
        page.draw_circle(c, R_DOT, color=(1, 1, 1), fill=(1, 0, 0), width=1.5)

    # Folio grande con el color del día + empresa debajo (como el Extractor).
    fx = r.x0 + w * Z_FOLIO[0]
    fy = r.y0 + h * Z_FOLIO[1]
    _center_text(page, str(folio), fx, fy, S_FOLIO, rgb, bold=True)
    size_emp = S_EMPRESA.get(company, S_EMPRESA_DEF)
    _center_text(page, _txt(company), fx, fy + size_emp + 2, size_emp, rgb, bold=True)

    # Logo centrado (base en y) + contacto debajo.
    lx = r.x0 + w * Z_LOGO[0]
    ly = r.y0 + h * Z_LOGO[1]
    if logo is not None:
        lw = W_LOGO.get(company, W_LOGO_DEF)
        lh = lw * logo.height / logo.width
        page.insert_image(fitz.Rect(lx - lw / 2, ly - lh, lx + lw / 2, ly),
                          pixmap=logo, keep_proportion=True)
    cy = r.y0 + h * Z_CONTACT[1]
    _center_text(page, "Numero de Contacto", lx, cy, S_CONTACT_LABEL, (0, 0, 0))
    _center_text(page, contact, lx, cy + S_CONTACT_NUM + 3, S_CONTACT_NUM,
                 (0, 0, 0), bold=True)

    # Código de lote al pie, centrado (identificador del batch en papel).
    _center_text(page, _txt(batch_code), r.x0 + w / 2, r.y1 - 8, S_BATCH,
                 (0, 0, 0), bold=True)


# --- Vista previa / calibración -----------------------------------------------
def sample_preview(company: str = "INMATMEX", markup: float | None = 3.0,
                   folio: int = 101, batch_code: str = "A1B2C",
                   label_w: float | None = None,
                   label_h: float | None = None) -> bytes:
    """Etiqueta de MUESTRA ya estampada (para calibrar sin gastar etiquetas)."""
    import label_layout
    import storage
    from config import DEFAULT_LABEL_W_PT, DEFAULT_LABEL_H_PT
    w = label_w or float(storage.get_value(storage.LABEL_W) or DEFAULT_LABEL_W_PT)
    h = label_h or float(storage.get_value(storage.LABEL_H) or DEFAULT_LABEL_H_PT)
    base = label_layout.sample_labels(1, w, h)
    return enrich(base, folio=folio, company=company,
                  batch_code=batch_code, markup=markup)


# --- helpers -------------------------------------------------------------------
_LOGO_CACHE: dict[str, "fitz.Pixmap | None"] = {}


def _logo_pixmap(company: str):
    """Pixmap del logo de la empresa (cacheado); None si no hay archivo."""
    if company not in _LOGO_CACHE:
        fname = LOGO_FILES.get(company)
        path = ASSETS / fname if fname else None
        try:
            _LOGO_CACHE[company] = fitz.Pixmap(str(path)) if path and path.exists() else None
            if _LOGO_CACHE[company] is None and fname:
                log.warning("Logo de «%s» no encontrado en %s.", company, path)
        except Exception:
            log.warning("No se pudo cargar el logo de «%s».", company, exc_info=True)
            _LOGO_CACHE[company] = None
    return _LOGO_CACHE[company]


def _hex_rgb(hex_color: str) -> tuple:
    s = hex_color.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _center_text(page: fitz.Page, text: str, cx: float, y: float,
                 size: float, color: tuple, bold: bool = False) -> None:
    """Texto centrado en cx, sujetado a los bordes de la página (margen 5pt)."""
    font = "hebo" if bold else "helv"
    tw = fitz.get_text_length(text, fontname=font, fontsize=size)
    x = cx - tw / 2
    x = max(page.rect.x0 + 5, min(x, page.rect.x1 - 5 - tw))
    page.insert_text((x, y), text, fontname=font, fontsize=size, color=color)


def _txt(s: str) -> str:
    """A latin-1 imprimible (las fuentes base del PDF no cubren todo Unicode)."""
    return str(s).encode("latin-1", "replace").decode("latin-1")
