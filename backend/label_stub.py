"""Talón de control de producto para etiquetas de envío.

Las guías de Mercado Libre traen el detalle del producto impreso; las de
Walmart y TikTok Shop no, y el empaque tenía que escribirlo a mano sobre la
etiqueta o consultar una lista aparte. Este módulo inyecta un talón recortable
en la parte SUPERIOR de cada etiqueta PDF con el producto, el SKU y las
piezas, más una línea de corte punteada con tijeras.

El lienzo crece hacia arriba (alto = talón + etiqueta): la guía original se
coloca íntegra debajo, sin escalar ni desplazar, así el código de barras y
los datos fiscales no se tocan. El empaquetado n-up (label_layout) mide la
primera página del lote, por lo que absorbe el alto extra automáticamente.

Activable por proveedor desde Cuentas/Tiendas; el estado vive en
storage (clave STUB_PROVIDERS, JSON {"walmart": true, "tiktok": true}).
"""
from __future__ import annotations

import json

import fitz  # PyMuPDF

import storage

# Alto del talón (pt). ~2.8 cm: suficiente para 2 productos + línea de corte.
STUB_H_PT = 80.0

# Proveedores que admiten talón y su valor por defecto. Mercado Libre queda
# fuera: su etiqueta ya trae el producto de forma nativa.
_DEFAULTS = {"walmart": True, "tiktok": True}

_MARGIN = 9.0          # margen interior del talón
_INK = (0.13, 0.14, 0.16)
_MUTED = (0.45, 0.47, 0.5)


# --- Configuración -------------------------------------------------------------
# Dos niveles: valor por marketplace (providers) y excepción por tienda
# (accounts, por account_id). Sin excepción, la tienda hereda del marketplace.
def get_config() -> dict:
    """{'providers': {'walmart': bool, 'tiktok': bool}, 'accounts': {id: bool}}"""
    cfg = {"providers": dict(_DEFAULTS), "accounts": {}}
    raw = storage.get_value(storage.STUB_PROVIDERS)
    if raw:
        try:
            saved = json.loads(raw)
            if "providers" not in saved:      # formato viejo: solo providers
                saved = {"providers": saved}
            cfg["providers"].update({k: bool(v) for k, v in
                                     (saved.get("providers") or {}).items()
                                     if k in _DEFAULTS})
            cfg["accounts"] = {str(k): bool(v) for k, v in
                               (saved.get("accounts") or {}).items()}
        except (ValueError, AttributeError):
            pass
    return cfg


def enabled_providers() -> dict:
    return get_config()["providers"]


def set_enabled(provider: str, enabled: bool) -> dict:
    if provider not in _DEFAULTS:
        raise ValueError(f"El talón no aplica al proveedor «{provider}».")
    cfg = get_config()
    cfg["providers"][provider] = bool(enabled)
    storage.set_value(storage.STUB_PROVIDERS, json.dumps(cfg))
    return cfg


def set_account_enabled(account_id: str, enabled: bool | None) -> dict:
    """Excepción por tienda; None la borra (vuelve a heredar del marketplace)."""
    cfg = get_config()
    if enabled is None:
        cfg["accounts"].pop(str(account_id), None)
    else:
        cfg["accounts"][str(account_id)] = bool(enabled)
    storage.set_value(storage.STUB_PROVIDERS, json.dumps(cfg))
    return cfg


def enabled_for(provider: str, account_id: str | None = None) -> bool:
    cfg = get_config()
    if account_id is not None and str(account_id) in cfg["accounts"]:
        # la excepción por tienda solo aplica si el proveedor admite talón
        return provider in _DEFAULTS and cfg["accounts"][str(account_id)]
    return bool(cfg["providers"].get(provider))


# --- Inyección del talón --------------------------------------------------------
def add_stub(pdf_bytes: bytes, products: list[dict],
             order_ref: str = "") -> bytes:
    """Devuelve el PDF con un talón arriba de CADA página de etiqueta.

    products: [{title, sku, quantity}] del pedido (mismo talón en todas las
    páginas del envío). Si no hay productos, devuelve el original sin tocar.
    """
    if not products:
        return pdf_bytes
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if src.page_count == 0:
            return pdf_bytes
        out = fitz.open()
        try:
            for i in range(src.page_count):
                r = src.load_page(i).rect
                page = out.new_page(width=r.width, height=r.height + STUB_H_PT)
                _draw_stub(page, r.width, products, order_ref)
                page.show_pdf_page(
                    fitz.Rect(0, STUB_H_PT, r.width, STUB_H_PT + r.height), src, i)
            return out.tobytes()
        finally:
            out.close()
    finally:
        src.close()


def _draw_stub(page: fitz.Page, width: float, products: list[dict],
               order_ref: str) -> None:
    x0, x1 = _MARGIN, width - _MARGIN
    max_w = x1 - x0

    # Encabezado: qué es y qué hacer con él; referencia del pedido a la derecha.
    page.insert_text((x0, 11), _fit("TALÓN DE CONTROL · retirar antes de entregar a paquetería",
                                    6.3, max_w), fontsize=6.3, color=_MUTED)
    if order_ref:
        ref = _fit(f"#{order_ref}", 6.3, max_w * 0.4)
        w = fitz.get_text_length(ref, fontname="helv", fontsize=6.3)
        page.insert_text((x1 - w, 11), ref, fontsize=6.3, color=_MUTED)

    total = sum(int(p.get("quantity", 1) or 1) for p in products)
    if len(products) == 1:
        p = products[0]
        page.insert_text((x0, 30), _fit(_txt(p.get("title") or "—"), 11, max_w, "hebo"),
                         fontname="hebo", fontsize=11, color=_INK)
        qty = int(p.get("quantity", 1) or 1)
        detail = f"SKU {_txt(p.get('sku') or 's/n')}  ·  {qty} {'pieza' if qty == 1 else 'piezas'}"
        page.insert_text((x0, 47), _fit(detail, 9, max_w, "cour"),
                         fontname="cour", fontsize=9, color=_INK)
    else:
        y = 26.0
        for p in products[:2]:
            qty = int(p.get("quantity", 1) or 1)
            line = f"{qty}× {_txt(p.get('title') or '-')} · SKU {_txt(p.get('sku') or 's/n')}"
            page.insert_text((x0, y), _fit(line, 8.6, max_w, "hebo"),
                             fontname="hebo", fontsize=8.6, color=_INK)
            y += 13
        extra = len(products) - 2
        tail = f"{total} piezas en total" + (f"  ·  +{extra} producto(s) más" if extra > 0 else "")
        page.insert_text((x0, y + 1), _fit(tail, 8, max_w, "cour"),
                         fontname="cour", fontsize=8, color=_INK)

    # Línea de corte punteada con tijeras.
    cut_y = STUB_H_PT - 12
    _draw_scissors(page, x0, cut_y)
    page.draw_line(fitz.Point(x0 + 16, cut_y), fitz.Point(x1, cut_y),
                   color=_MUTED, width=0.9, dashes="[3 3] 0")
    page.insert_text((x1 - fitz.get_text_length("corte", fontname="helv", fontsize=5.6) ,
                      cut_y - 3), "corte", fontsize=5.6, color=_MUTED)


def _draw_scissors(page: fitz.Page, x: float, y: float) -> None:
    """Tijeras vectoriales (✂ no existe en las fuentes base del PDF)."""
    # dos hojas cruzadas apuntando a la derecha + dos argollas atrás
    page.draw_line(fitz.Point(x + 3, y - 4), fitz.Point(x + 12, y + 3),
                   color=_MUTED, width=1.1)
    page.draw_line(fitz.Point(x + 3, y + 4), fitz.Point(x + 12, y - 3),
                   color=_MUTED, width=1.1)
    page.draw_circle(fitz.Point(x + 2, y - 5), 2.0, color=_MUTED, width=0.9)
    page.draw_circle(fitz.Point(x + 2, y + 5), 2.0, color=_MUTED, width=0.9)


# --- Muestra para las vistas previas de la interfaz ------------------------------
SAMPLE_PRODUCTS = [{"title": "WPC Gris 1M", "sku": "WPC-GRIS-1M", "quantity": 2}]


# --- helpers -------------------------------------------------------------------
def _txt(s: str) -> str:
    """A latin-1 imprimible (las fuentes base del PDF no cubren todo Unicode)."""
    return str(s).encode("latin-1", "replace").decode("latin-1")


def _fit(text: str, size: float, max_w: float, fontname: str = "helv") -> str:
    text = _txt(text)
    if fitz.get_text_length(text, fontname=fontname, fontsize=size) <= max_w:
        return text
    while text and fitz.get_text_length(text + "...", fontname=fontname,
                                        fontsize=size) > max_w:
        text = text[:-1]
    return text + "..."
