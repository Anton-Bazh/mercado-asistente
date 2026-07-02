"""Lector de packing lists (Excel) del seller center, genérico por columnas.

Walmart entrega junto al PDF de guías un Excel («Pedidos_*.xlsx», hoja
"Detalles Del Pedido") con una fila por línea de pedido: PO, cliente,
producto, SKU, cantidad y (a veces) tracking. TikTok exporta algo análogo.
Este módulo lo convierte en pedidos normalizados para cruzarlos con las guías
importadas y armar el talón de control sin capturar nada a mano.

La detección de columnas es por nombre de encabezado (tolerante a idioma y
variantes) para que el mismo lector sirva a los dos marketplaces.
"""
from __future__ import annotations

import unicodedata
import warnings
from io import BytesIO


class PackingListError(Exception):
    pass


# Detección de columnas: primera coincidencia por subcadena del encabezado
# normalizado (sin acentos, minúsculas). El orden importa (más específico 1º).
_COLUMNS = {
    "po":       ("orden de compra", "purchase order", "po number", "po#"),
    "order_id": ("numero de orden", "order id", "id de orden", "order number",
                 "pedido"),
    "buyer":    ("nombre del cliente", "cliente", "buyer", "recipient",
                 "destinatario"),
    "title":    ("nombre del producto", "producto", "product name",
                 "descripcion", "item name", "articulo"),
    "sku":      ("seller sku", "sku"),
    "quantity": ("cantidad", "qty", "quantity", "piezas"),
    "tracking": ("rastreo", "tracking", "guia"),
}


def parse(data: bytes, filename: str = "") -> list[dict]:
    """Despacha por tipo de archivo: Excel (.xlsx) o Picking List PDF (TikTok)."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:4] == b"PK\x03\x04":
        return parse_xlsx(data)
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        return parse_picking_pdf(data)
    raise PackingListError("Formato no soportado: sube el Excel (.xlsx) o el Picking List (PDF).")


def parse_xlsx(data: bytes) -> list[dict]:
    """Pedidos del Excel: [{po, order_id, buyer, tracking, products:[...]}].

    Agrupa las filas (líneas de pedido) por PO/número de orden, en el orden
    en que aparecen en la hoja.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise PackingListError("Falta la dependencia openpyxl.") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(BytesIO(data), data_only=True)
    except Exception as exc:
        raise PackingListError(f"No se pudo abrir el Excel: {exc}") from exc

    best: tuple[int, list] | None = None
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        cols = _detect_columns(rows[0])
        score = len(cols)
        if "title" in cols and (best is None or score > best[0]):
            best = (score, _extract(rows, cols))
    if best is None:
        raise PackingListError(
            "No se encontró una hoja con columnas de pedido (producto/SKU/cantidad).")
    return best[1]


def _detect_columns(header_row) -> dict[str, int]:
    cols: dict[str, int] = {}
    headers = [_norm(h) for h in header_row]
    for field, needles in _COLUMNS.items():
        for needle in needles:
            idx = next((i for i, h in enumerate(headers)
                        if h and needle in h), None)
            if idx is not None:
                cols[field] = idx
                break
    return cols


def _extract(rows: list, cols: dict[str, int]) -> list[dict]:
    orders: dict[str, dict] = {}
    for row in rows[1:]:
        def val(field):
            i = cols.get(field)
            v = row[i] if i is not None and i < len(row) else None
            return str(v).strip() if v is not None else ""

        title = val("title")
        if not title:
            continue
        key = val("po") or val("order_id") or f"fila{len(orders)}"
        order = orders.setdefault(key, {
            "po": val("po"), "order_id": val("order_id"),
            "buyer": val("buyer"), "tracking": val("tracking"), "products": [],
        })
        try:
            qty = max(1, int(float(val("quantity") or "1")))
        except ValueError:
            qty = 1
        order["products"].append({"title": title, "sku": val("sku"),
                                  "quantity": qty})
        if not order["tracking"] and val("tracking"):
            order["tracking"] = val("tracking")
    return list(orders.values())


def parse_picking_pdf(data: bytes) -> list[dict]:
    """Picking List de TikTok Shop (PDF A4): tabla No / imagen / Product name /
    SKU / Seller SKU / Qty / Order ID. Cada fila es un PRODUCTO con los pedidos
    que lo llevan → se invierte a pedidos {order_id: products}."""
    import fitz
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PackingListError(f"No se pudo abrir el PDF: {exc}") from exc
    orders: dict[str, dict] = {}
    try:
        for pno in range(doc.page_count):
            words = doc.load_page(pno).get_text("words")
            # anclas de columnas en el renglón de encabezados
            heads = {}
            for w in words:
                if w[4] == "name":
                    heads["name_y"] = w[1]
            if "name_y" not in heads:
                continue
            hy = heads["name_y"]
            row_hdr = {w[4]: w[0] for w in words if abs(w[1] - hy) < 2}
            x_name = row_hdr.get("Product", 130)
            x_seller = row_hdr.get("Seller", 341)
            x_qty = row_hdr.get("Qty", 408)
            x_order = row_hdr.get("Order", 455)
            skus = sorted(w[0] for w in words
                          if w[4] == "SKU" and abs(w[1] - hy) < 2)
            x_sku = next((x for x in skus if x_name < x < x_seller), 270)

            body = [w for w in words if w[1] > hy + 6]
            # separadores de fila: el número de la columna "No"
            marks = sorted((w for w in body if w[0] < x_name - 75
                            and w[4].isdigit()), key=lambda w: w[1])
            for i, mk in enumerate(marks):
                y_a = mk[1] - 3
                y_b = marks[i + 1][1] - 3 if i + 1 < len(marks) else 1e9
                block = [w for w in body if y_a <= w[1] < y_b]
                title = " ".join(w[4] for w in sorted(
                    (w for w in block if x_name - 6 <= w[0] < x_sku - 6),
                    key=lambda w: (w[1], w[0])))
                seller_sku = "".join(w[4] for w in sorted(
                    (w for w in block if x_seller - 6 <= w[0] < x_qty - 6),
                    key=lambda w: (w[1], w[0])))
                qty_w = [w[4] for w in block
                         if x_qty - 6 <= w[0] < x_order - 6 and w[4].isdigit()]
                ids = [w[4] for w in block
                       if w[0] >= x_order - 6 and w[4].isdigit() and len(w[4]) >= 15]
                if not title or not ids:
                    continue
                total = int(qty_w[0]) if qty_w else len(ids)
                # reparto de piezas: exacto si divide parejo; si no, 1 c/u
                each = total // len(ids) if total % len(ids) == 0 else 1
                for oid in ids:
                    order = orders.setdefault(oid, {
                        "po": "", "order_id": oid, "buyer": "",
                        "tracking": "", "products": []})
                    order["products"].append({"title": title,
                                              "sku": seller_sku or "s/n",
                                              "quantity": max(1, each)})
    finally:
        doc.close()
    if not orders:
        raise PackingListError("No se encontraron pedidos en el Picking List.")
    return list(orders.values())


def match(meta: list[dict], orders: list[dict]) -> dict:
    """Cruza guías importadas con pedidos del Excel. Modifica meta en sitio.

    1º por identificadores (PO / nº de orden / tracking / cliente); 2º, si
    NINGUNA guía trae identificación (PDF rasterizado sin OCR) y hay el mismo
    número de guías que de pedidos, por POSICIÓN (marcado para verificar).
    Devuelve {matched, positional}.
    """
    by_key: dict[str, dict] = {}
    for o in orders:
        for k in (o.get("po"), o.get("order_id"), o.get("tracking")):
            if k:
                by_key[str(k)] = o
        if o.get("buyer"):
            by_key.setdefault("b:" + _norm(o["buyer"]), o)

    matched = 0
    identified = False
    for m in meta:
        keys = [m.get("order_id"), m.get("order_alt"), m.get("tracking")]
        if m.get("buyer"):
            keys.append("b:" + _norm(m["buyer"]))
        if any(k for k in (m.get("order_id"), m.get("order_alt"),
                           m.get("tracking"), m.get("buyer"))):
            identified = True
        if m["products"]:
            continue
        for k in keys:
            o = by_key.get(str(k)) if k else None
            if o and o["products"]:
                m["products"] = o["products"]
                m["matched"] = "excel"
                # el packing list es la fuente autoritativa de identidad
                # (el buyer del OCR puede traer ruido del remitente)
                m["buyer"] = o.get("buyer") or m.get("buyer", "")
                matched += 1
                break

    positional = 0
    remaining = [m for m in meta if not m["products"]]
    if not identified and remaining and len(meta) == len(orders):
        for m, o in zip(meta, orders):
            if m["products"]:
                continue
            m["products"] = o["products"]
            m["matched"] = "posicion"
            m["buyer"] = o.get("buyer", "")
            if not m.get("order_id"):
                m["order_id"] = o.get("po") or o.get("order_id") or ""
            positional += 1
    return {"matched": matched, "positional": positional}


def _norm(s) -> str:
    s = str(s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")
