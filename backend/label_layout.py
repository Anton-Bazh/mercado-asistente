"""Empaquetado de etiquetas PDF en hojas (n-up) para ahorrar papel.

La API de Mercado Libre devuelve las etiquetas como un PDF con una etiqueta
por página (típicamente formato 10x15 cm). Al imprimirse tal cual, cada
etiqueta ocupa una hoja completa. Este módulo recompone ese PDF en hojas
tamaño Carta acomodando tantas etiquetas como quepan por hoja, manteniendo el
contenido vectorial (el código de barras no se rasteriza).

Se apoya en PyMuPDF (fitz). Solo aplica a PDF: el ZPL es para impresoras
térmicas de rollo, sin concepto de "hoja".
"""
from __future__ import annotations

import fitz  # PyMuPDF

from config import (
    SHEET_WIDTH_PT,
    SHEET_HEIGHT_PT,
    SHEET_MARGIN_PT,
    LABEL_GAP_PT,
)


def _label_rect(page: "fitz.Page") -> "fitz.Rect":
    """Área real de la etiqueta dentro de la página.

    ML a veces entrega la etiqueta 10x15 sobre una página tamaño carta/A4
    (el resto es espacio en blanco). Si se midiera la página, saldría "1 por
    hoja" y el acomodo no ahorraría nada. Se toma el bbox del contenido
    dibujado (+margen); si el contenido llena la página, se usa completa.
    """
    r = page.rect
    try:
        bbox = fitz.Rect()
        for item in page.get_bboxlog():
            bbox |= fitz.Rect(item[1])
        bbox &= r
    except Exception:
        return r
    if bbox.is_empty or bbox.is_infinite:
        return r
    pad = 6.0
    bbox = fitz.Rect(bbox.x0 - pad, bbox.y0 - pad,
                     bbox.x1 + pad, bbox.y1 + pad) & r
    # contenido ≈ página completa: no hay nada que recortar
    if bbox.width * bbox.height >= 0.85 * r.width * r.height:
        return r
    return bbox


def _grid_for(label_w: float, label_h: float,
              sheet_w: float, sheet_h: float) -> tuple[int, int]:
    """Cuántas columnas y filas de una etiqueta caben en la hoja."""
    usable_w = sheet_w - 2 * SHEET_MARGIN_PT
    usable_h = sheet_h - 2 * SHEET_MARGIN_PT
    if label_w <= 0 or label_h <= 0:
        return 1, 1
    # +GAP en el numerador: n celdas necesitan (n-1) separaciones, así que
    # (usable + gap) / (label + gap) da el número entero de celdas que caben.
    cols = int((usable_w + LABEL_GAP_PT) // (label_w + LABEL_GAP_PT))
    rows = int((usable_h + LABEL_GAP_PT) // (label_h + LABEL_GAP_PT))
    return max(1, cols), max(1, rows)


def _best_layout(label_w: float, label_h: float) -> dict:
    """Elige orientación de hoja + rotación de etiqueta que más aprovecha.

    Prueba la hoja en vertical y horizontal, y la etiqueta sin rotar y rotada
    90°. Devuelve la combinación con más etiquetas por hoja.
    """
    candidates = []
    for sheet_w, sheet_h in ((SHEET_WIDTH_PT, SHEET_HEIGHT_PT),
                             (SHEET_HEIGHT_PT, SHEET_WIDTH_PT)):
        for rotate, (lw, lh) in ((0, (label_w, label_h)),
                                 (90, (label_h, label_w))):
            cols, rows = _grid_for(lw, lh, sheet_w, sheet_h)
            candidates.append({
                "per_sheet": cols * rows,
                "cols": cols,
                "rows": rows,
                "rotate": rotate,
                "sheet_w": sheet_w,
                "sheet_h": sheet_h,
                "cell_w": lw,
                "cell_h": lh,
            })
    # Mejor: más etiquetas por hoja; a igualdad, hoja vertical (menos ancho).
    return max(candidates, key=lambda c: (c["per_sheet"], -c["sheet_w"]))


def plan(count: int, label_w: float, label_h: float) -> dict:
    """Cómo quedaría el acomodo de `count` etiquetas de tamaño dado (sin PDF)."""
    lay = _best_layout(label_w, label_h)
    per = lay["per_sheet"]
    sheets = (count + per - 1) // per if count else 0
    return {
        "count": count,
        "labels_per_sheet": per,
        "sheets": sheets,
        "cols": lay["cols"],
        "rows": lay["rows"],
        "rotated": bool(lay["rotate"]),
        "sheet": "Carta",
    }


def sample_labels(count: int, label_w: float, label_h: float,
                  brand: str = "MERCADO ENVIOS") -> bytes:
    """PDF con `count` etiquetas de marcador de posición (una por página)."""
    count = max(1, min(count, 200))
    src = fitz.open()
    try:
        for i in range(count):
            pg = src.new_page(width=label_w, height=label_h)
            r = pg.rect
            pg.draw_rect(r + (2, 2, -2, -2), color=(0.1, 0.1, 0.1), width=1.2)
            # cabecera tipo etiqueta
            pg.draw_rect(fitz.Rect(r.x0 + 2, r.y0 + 2, r.x1 - 2, r.y0 + 22),
                         color=None, fill=(0.11, 0.12, 0.14))
            pg.insert_text((r.x0 + 8, r.y0 + 16), brand,
                           fontsize=8, color=(1, 1, 1))
            pg.insert_text((r.x0 + 10, r.y0 + 44), f"Etiqueta {i + 1}",
                           fontsize=13, color=(0, 0, 0))
            pg.insert_text((r.x0 + 10, r.y0 + 62), "DESTINO (muestra)",
                           fontsize=8, color=(0.4, 0.4, 0.4))
            # pseudo código de barras
            bx, by = r.x0 + 10, r.y1 - 34
            x = bx
            widths = [2, 1, 3, 1, 2, 2, 1, 3, 2, 1, 2, 3, 1, 2]
            for j, w in enumerate(widths * 3):
                if x > r.x1 - 12:
                    break
                if j % 2 == 0:
                    pg.draw_rect(fitz.Rect(x, by, x + w, by + 22), fill=(0, 0, 0), color=None)
                x += w + 1
            pg.insert_text((r.x0 + 10, r.y1 - 6), f"#PREVIEW-{i + 1:03d}",
                           fontsize=8, color=(0.2, 0.2, 0.2))
        return src.tobytes()
    finally:
        src.close()


def build_preview(count: int, label_w: float, label_h: float,
                  brand: str = "MERCADO ENVIOS", pages: bytes | None = None) -> bytes:
    """PDF de muestra: `count` etiquetas de marcador de posición, ya acomodadas.

    Sirve para ver el acomodo real (rejilla, orientación, márgenes, nº de hojas)
    sin descargar nada del marketplace ni gastar papel. `pages` permite pasar
    etiquetas de muestra ya generadas (p. ej. con talón de control inyectado).
    """
    sample = pages if pages is not None else sample_labels(count, label_w, label_h, brand)
    packed, _meta = pack_labels_to_sheets(sample)
    return packed


def per_sheet_for(pdf_bytes: bytes) -> int:
    """Cuántas etiquetas caben por hoja, midiendo la primera página del PDF.

    Mide el área real de la etiqueta (no el papel): una etiqueta 10x15 sobre
    página carta cuenta como 10x15.
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if src.page_count == 0:
            return 1
        r = _label_rect(src.load_page(0))
        return _best_layout(r.width, r.height)["per_sheet"]
    finally:
        src.close()


def label_size(pdf_bytes: bytes) -> tuple[float, float] | None:
    """Tamaño real (w, h) en puntos de la etiqueta de la primera página."""
    try:
        src = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    try:
        if src.page_count == 0:
            return None
        r = _label_rect(src.load_page(0))
        return r.width, r.height
    finally:
        src.close()


def pack_pdf_list(pages: list[bytes]) -> tuple[bytes, dict]:
    """Empaqueta una lista de PDFs de una etiqueta cada uno en hoja(s) n-up.

    Usado por el motor de lote: acumula las etiquetas ya descargadas de una
    hoja y las compone. Fusiona los PDFs en uno multipágina y reutiliza
    pack_labels_to_sheets.
    """
    if not pages:
        return b"", {"labels": 0, "labels_per_sheet": 0, "sheets": 0,
                     "cols": 0, "rows": 0, "packed": False}
    merged = fitz.open()
    try:
        for b in pages:
            doc = fitz.open(stream=b, filetype="pdf")
            try:
                merged.insert_pdf(doc)
            finally:
                doc.close()
        combined = merged.tobytes()
    finally:
        merged.close()
    return pack_labels_to_sheets(combined)


def pack_labels_to_sheets(pdf_bytes: bytes) -> tuple[bytes, dict]:
    """Recompone un PDF de una-etiqueta-por-página en hojas n-up.

    Devuelve (pdf_resultante, metadata). Si hay 0 o 1 páginas, o si solo cabe
    una etiqueta por hoja, devuelve el PDF original sin tocar (metadata refleja
    ese caso).
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        n = src.page_count
        if n == 0:
            return pdf_bytes, {"labels": 0, "labels_per_sheet": 0, "sheets": 0,
                               "cols": 0, "rows": 0, "packed": False}

        # Área real de cada etiqueta (recorta el papel sobrante de ML).
        clips = [_label_rect(src.load_page(i)) for i in range(n)]
        label_w, label_h = clips[0].width, clips[0].height
        layout = _best_layout(label_w, label_h)
        per_sheet = layout["per_sheet"]

        meta = {
            "labels": n,
            "labels_per_sheet": per_sheet,
            "cols": layout["cols"],
            "rows": layout["rows"],
        }

        # Nada que ganar: una sola etiqueta o solo cabe una por hoja.
        if n == 1 or per_sheet <= 1:
            meta["sheets"] = n
            meta["packed"] = False
            return pdf_bytes, meta

        sheet_w, sheet_h = layout["sheet_w"], layout["sheet_h"]
        cols, rows = layout["cols"], layout["rows"]
        cell_w, cell_h = layout["cell_w"], layout["cell_h"]
        rotate = layout["rotate"]

        out = fitz.open()
        try:
            for i in range(n):
                slot = i % per_sheet
                if slot == 0:
                    sheet = out.new_page(width=sheet_w, height=sheet_h)
                col = slot % cols
                row = slot // cols
                x0 = SHEET_MARGIN_PT + col * (cell_w + LABEL_GAP_PT)
                y0 = SHEET_MARGIN_PT + row * (cell_h + LABEL_GAP_PT)
                target = fitz.Rect(x0, y0, x0 + cell_w, y0 + cell_h)
                sheet.show_pdf_page(target, src, i, clip=clips[i], rotate=rotate)

            result = out.tobytes()
        finally:
            out.close()

        meta["sheets"] = (n + per_sheet - 1) // per_sheet
        meta["packed"] = True
        return result, meta
    finally:
        src.close()
