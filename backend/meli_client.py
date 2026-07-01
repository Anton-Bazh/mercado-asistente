"""Wrapper de la API REST de Mercado Libre.

Obtiene datos del vendedor, lista las ventas en estado 'ready_to_ship' y
descarga las etiquetas de envío (PDF/ZPL). Usa siempre un access_token válido
provisto por auth.get_valid_access_token().
"""
from __future__ import annotations

import httpx

import auth
import storage
from config import (
    API_BASE,
    USERS_ME_URL,
    ORDERS_SEARCH_URL,
    SHIPMENTS_URL,
    LABELS_URL,
    HTTP_TIMEOUT,
)


class MeliError(Exception):
    """Error al consultar la API de Mercado Libre."""


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {auth.get_valid_access_token()}",
        "Accept": "application/json",
    }


def _get(url: str, params: dict | None = None) -> httpx.Response:
    try:
        resp = httpx.get(
            url, params=params, headers=_auth_headers(), timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise MeliError(f"Error de red: {exc}") from exc
    if resp.status_code == 401:
        # Token rechazado: forzar un refresh y reintentar una vez
        auth.refresh_access_token()
        try:
            resp = httpx.get(
                url, params=params, headers=_auth_headers(), timeout=HTTP_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise MeliError(f"Error de red: {exc}") from exc
    if resp.status_code >= 400:
        raise MeliError(f"La API respondió {resp.status_code}: {resp.text[:200]}")
    return resp


# --- Vendedor ----------------------------------------------------------------
def get_seller() -> dict:
    """Devuelve datos del vendedor autenticado y los cachea en el almacén."""
    data = _get(USERS_ME_URL).json()
    seller_id = str(data.get("id", ""))
    nickname = data.get("nickname", "")
    storage.set_value(storage.SELLER_ID, seller_id)
    storage.set_value(storage.SELLER_NICKNAME, nickname)
    return {"id": seller_id, "nickname": nickname}


def _seller_id() -> str:
    sid = storage.get_value(storage.SELLER_ID)
    if not sid:
        sid = get_seller()["id"]
    return sid


# --- Envíos ------------------------------------------------------------------
def get_shipment(shipment_id: str | int) -> dict:
    headers = _auth_headers()
    headers["x-format-new"] = "true"
    try:
        resp = httpx.get(
            f"{SHIPMENTS_URL}/{shipment_id}", headers=headers, timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise MeliError(f"Error de red: {exc}") from exc
    if resp.status_code >= 400:
        raise MeliError(
            f"No se pudo leer el envío {shipment_id}: {resp.status_code}"
        )
    return resp.json()


# --- Órdenes / ventas listas para enviar -------------------------------------
def list_ready_to_ship(limit: int = 50) -> list[dict]:
    """Devuelve las ventas en estado 'ready_to_ship' normalizadas para la tabla.

    Estrategia: se buscan órdenes pagadas recientes, se inspecciona el envío de
    cada una y se conservan solo las que están listas para enviar.
    """
    params = {
        "seller": _seller_id(),
        "order.status": "paid",
        "sort": "date_desc",
        "limit": limit,
    }
    orders = _get(ORDERS_SEARCH_URL, params=params).json().get("results", [])

    rows: list[dict] = []
    seen_shipments: set[str] = set()
    for order in orders:
        shipping = order.get("shipping") or {}
        shipment_id = shipping.get("id")
        if not shipment_id or str(shipment_id) in seen_shipments:
            continue

        try:
            shipment = get_shipment(shipment_id)
        except MeliError:
            continue
        if shipment.get("status") != "ready_to_ship":
            continue
        seen_shipments.add(str(shipment_id))
        rows.append(_normalize_row(order, shipment))

    return rows


def list_ready_with_pending(limit: int = 50) -> tuple[list[dict], set[str]]:
    """Ventas listas para enviar con el flag `pending` calculado.

    `pending` = falta por imprimir: substatus != 'printed' de Mercado Libre y
    sin impresión exitosa reciente en el historial (evita parpadeo). Compartido
    por /api/orders y el modo automático.
    """
    rows = list_ready_to_ship(limit)
    recent = storage.recent_printed_shipment_ids(within_seconds=600)
    try:
        threshold = int(storage.get_value(storage.MULTIUNIT_THRESHOLD) or "1")
    except ValueError:
        threshold = 1
    for r in rows:
        already = (r.get("substatus") == "printed"
                   or str(r.get("shipment_id")) in recent)
        r["pending"] = not already
        # Multi-unidad: supera el umbral → gestión manual (no auto-imprime).
        r["multi_unit"] = int(r.get("units", 1) or 1) > threshold
    return rows, recent


def _normalize_row(order: dict, shipment: dict) -> dict:
    """Aplana los datos de orden + envío a lo que muestra la tabla."""
    buyer = order.get("buyer") or {}
    buyer_name = " ".join(
        p for p in (buyer.get("first_name"), buyer.get("last_name")) if p
    ).strip() or buyer.get("nickname", "—")

    addr = shipment.get("receiver_address") or {}
    city = (addr.get("city") or {})
    state = (addr.get("state") or {})
    address_parts = [
        addr.get("address_line"),
        city.get("name") if isinstance(city, dict) else city,
        state.get("name") if isinstance(state, dict) else state,
        addr.get("zip_code"),
    ]
    address = ", ".join(p for p in address_parts if p)

    items = order.get("order_items") or []
    products = [
        {
            "title": (it.get("item") or {}).get("title", "—"),
            "quantity": it.get("quantity", 1),
        }
        for it in items
    ]
    units = sum(int(p.get("quantity", 1) or 1) for p in products)

    return {
        "order_id": order.get("id"),
        "shipment_id": shipment.get("id"),
        "pack_id": order.get("pack_id"),
        "date_created": order.get("date_created"),
        "buyer_name": buyer_name,
        "address": address or "—",
        "products": products,
        "units": units,
        "total_amount": order.get("total_amount"),
        "currency": order.get("currency_id", ""),
        "shipment_status": shipment.get("status"),
        "substatus": shipment.get("substatus"),
    }


# --- Separación de envíos (split) --------------------------------------------
def split_shipment(shipment_id: str | int, order_id: str | int,
                   quantity: int, reason: str = "DIMENSIONS_EXCEEDED") -> dict:
    """Separa un envío en Mercado Libre (POST /shipments/{id}/split).

    Separa `quantity` unidades de la orden `order_id` a un segundo paquete. ML
    solo permite separar una vez y es irreversible. Devuelve la respuesta JSON.
    """
    body = {
        "reason": reason,
        "packs": [{"orders": [{"id": str(order_id), "quantity": int(quantity)}]}],
    }
    headers = {
        "Authorization": f"Bearer {auth.get_valid_access_token()}",
        "x-format-new": "true",
        "Content-Type": "application/json",
    }
    url = f"{SHIPMENTS_URL}/{shipment_id}/split"
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise MeliError(f"Error de red al separar: {exc}") from exc
    if resp.status_code >= 400:
        raise MeliError(f"No se pudo separar ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        return {"ok": True}


# --- Etiquetas ---------------------------------------------------------------
def get_label(shipment_id: str | int, fmt: str = "pdf") -> tuple[bytes, str, str]:
    """Descarga la etiqueta de un envío.

    fmt: 'pdf' o 'zpl'. Devuelve (contenido, content_type, nombre_archivo).
    """
    response_type = "zpl2" if fmt == "zpl" else "pdf"
    params = {"shipment_ids": str(shipment_id), "response_type": response_type}
    headers = {"Authorization": f"Bearer {auth.get_valid_access_token()}"}
    try:
        resp = httpx.get(
            LABELS_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise MeliError(f"Error de red al pedir la etiqueta: {exc}") from exc
    if resp.status_code >= 400:
        raise MeliError(
            f"No se pudo obtener la etiqueta ({resp.status_code}): {resp.text[:200]}"
        )

    content = resp.content
    if fmt == "zpl":
        return content, "text/plain; charset=utf-8", f"etiqueta_{shipment_id}.zpl"
    _remember_label_size(content)   # aprende el tamaño real para la vista previa
    return content, "application/pdf", f"etiqueta_{shipment_id}.pdf"


def _remember_label_size(pdf_bytes: bytes) -> None:
    """Mide la etiqueta PDF y guarda su tamaño para el acomodo/vista previa."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if doc.page_count:
                r = doc.load_page(0).rect
                storage.set_value(storage.LABEL_W, f"{r.width:.2f}")
                storage.set_value(storage.LABEL_H, f"{r.height:.2f}")
        finally:
            doc.close()
    except Exception:
        pass


def get_labels(shipment_ids: list[str], fmt: str = "pdf") -> tuple[bytes, str, str]:
    """Descarga una etiqueta combinada para varios envíos.

    La API acepta varios shipment_ids separados por coma y devuelve un único
    documento (PDF multipágina o ZPL concatenado).
    """
    response_type = "zpl2" if fmt == "zpl" else "pdf"
    params = {"shipment_ids": ",".join(shipment_ids), "response_type": response_type}
    headers = {"Authorization": f"Bearer {auth.get_valid_access_token()}"}
    try:
        resp = httpx.get(
            LABELS_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise MeliError(f"Error de red al pedir las etiquetas: {exc}") from exc
    if resp.status_code >= 400:
        raise MeliError(
            f"No se pudieron obtener las etiquetas ({resp.status_code}): {resp.text[:200]}"
        )

    content = resp.content
    if fmt == "zpl":
        return content, "text/plain; charset=utf-8", "etiquetas.zpl"
    return content, "application/pdf", "etiquetas.pdf"
