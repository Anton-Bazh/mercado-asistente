"""Proveedor Walmart Marketplace México — client_credentials, pedidos y guías.

A diferencia de Mercado Libre no hay redirect OAuth: el vendedor genera un
Client ID / Client Secret en developer.walmart.com y con eso se piden tokens
de 15 minutos (grant client_credentials). "Conectar" = validar credenciales.

La guía la genera Walmart al crear el pedido (Envíos con Walmart): cada
shipment del pedido trae un trackingNumber y la etiqueta se descarga por
tracking — POST /v3/orders/labels?FORMAT=PDF (bulk) o GET /v3/orders/label/
{tracking} (PNG). No hay formato ZPL.
"""
from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

import logutil
import storage
from config import (
    WALMART_TOKEN_URL, WALMART_ORDERS_URL, WALMART_LABELS_URL, WALMART_LABEL_URL,
    WALMART_MARKET, WALMART_SVC_NAME, WALMART_ORDERS_LOOKBACK_DAYS,
    HTTP_TIMEOUT, TOKEN_REFRESH_MARGIN,
)
from providers.base import Provider, ProviderError

log = logutil.get_logger("walmart")

# Marca en refresh_token: la cuenta quedó validada (no existe refresh real,
# cada token se pide de nuevo con las credenciales).
_CONNECTED_MARK = "client_credentials"

# Estados de línea que consideramos "por enviar".
_PENDING_STATUSES = {"Created", "Acknowledged"}


class WalmartProvider(Provider):
    name = "walmart"
    label = "Walmart"
    auth_mode = "client_credentials"

    # --- Conexión --------------------------------------------------------------
    def authorize_url(self, account: dict, state: str) -> str:
        raise ProviderError("Walmart no usa autorización con redirect; usa «Conectar».")

    def exchange_code(self, account: dict, code: str) -> None:
        raise ProviderError("Walmart no usa código de autorización; usa «Conectar».")

    def connect(self, account: dict) -> None:
        if not (account.get("app_id") and account.get("client_secret")):
            raise ProviderError("Faltan credenciales (Client ID y Client Secret).")
        self._fetch_token(account)
        storage.upsert_account(account["id"], refresh_token=_CONNECTED_MARK)
        account["refresh_token"] = _CONNECTED_MARK

    def refresh(self, account: dict) -> str:
        return self._fetch_token(account)

    def _fetch_token(self, account: dict) -> str:
        basic = base64.b64encode(
            f"{account['app_id']}:{account['client_secret']}".encode()).decode()
        headers = {
            "Authorization": f"Basic {basic}",
            "WM_MARKET": WALMART_MARKET,
            "WM_QOS.CORRELATION_ID": uuid.uuid4().hex,
            "WM_SVC.NAME": WALMART_SVC_NAME,
            "Accept": "application/json",
        }
        try:
            resp = httpx.post(WALMART_TOKEN_URL, data={"grant_type": "client_credentials"},
                              headers=headers, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red con Walmart: {exc}") from exc
        if resp.status_code != 200:
            log.debug("Token Walmart rechazado (%d): %s", resp.status_code, resp.text[:500])
            raise ProviderError(_describe(resp))
        payload = resp.json()
        access = payload.get("access_token")
        if not access:
            raise ProviderError("Respuesta de token de Walmart inválida (sin access_token).")
        exp = int(time.time()) + int(payload.get("expires_in", 900))
        storage.update_account_tokens(account["id"], access,
                                      account.get("refresh_token"), exp)
        account["access_token"] = access
        account["token_expires_at"] = exp
        log.debug("%s token de 15 min obtenido.", logutil.account_ctx(account))
        return access

    def _valid_token(self, account: dict) -> str:
        exp = int(account.get("token_expires_at") or 0)
        # margen acotado: el token dura 15 min, no aplica el margen de 5 min de ML
        margin = min(TOKEN_REFRESH_MARGIN, 120)
        if not account.get("access_token") or exp - int(time.time()) <= margin:
            self._fetch_token(account)
        return account["access_token"]

    # --- HTTP --------------------------------------------------------------
    def _headers(self, account: dict, accept: str = "application/json") -> dict:
        return {
            "WM_SEC.ACCESS_TOKEN": self._valid_token(account),
            "WM_MARKET": WALMART_MARKET,
            "WM_QOS.CORRELATION_ID": uuid.uuid4().hex,
            "WM_SVC.NAME": WALMART_SVC_NAME,
            "Accept": accept,
        }

    def _request(self, account: dict, method: str, url: str, *,
                 params: dict | None = None, json: dict | None = None,
                 accept: str = "application/json") -> httpx.Response:
        try:
            resp = httpx.request(method, url, params=params, json=json,
                                 headers=self._headers(account, accept),
                                 timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red: {exc}") from exc
        if resp.status_code == 401:
            log.debug("%s 401 en %s; renovando token y reintentando…",
                      logutil.account_ctx(account), url)
            self._fetch_token(account)
            try:
                resp = httpx.request(method, url, params=params, json=json,
                                     headers=self._headers(account, accept),
                                     timeout=HTTP_TIMEOUT)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Error de red: {exc}") from exc
        if resp.status_code >= 400:
            log.debug("%s %s %s → %d: %s", logutil.account_ctx(account),
                      method, url, resp.status_code, resp.text[:500])
            raise ProviderError(f"Walmart respondió {resp.status_code}: {resp.text[:200]}")
        return resp

    # --- Pedidos -------------------------------------------------------------
    def list_ready(self, account: dict, limit: int = 100) -> list[dict]:
        start = (datetime.now(timezone.utc)
                 - timedelta(days=WALMART_ORDERS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        rows: list[dict] = []
        seen: set[str] = set()
        for status in sorted(_PENDING_STATUSES):
            params = {"createdStartDate": start, "statusCodeFilter": status,
                      "limit": str(limit)}
            data = self._request(account, "GET", WALMART_ORDERS_URL, params=params).json()
            for order in _extract_orders(data):
                po = str(order.get("purchaseOrderId") or "")
                if not po or po in seen:
                    continue
                seen.add(po)
                rows.append(_normalize(order))
        return rows

    # --- Etiquetas -----------------------------------------------------------
    def get_label(self, account: dict, shipment_id: str, fmt: str = "pdf") -> tuple[bytes, str, str]:
        if fmt == "zpl":
            raise ProviderError("Walmart no entrega etiquetas ZPL; usa PDF.")
        if str(shipment_id).startswith("PO"):
            raise ProviderError(
                "El pedido aún no tiene guía (trackingNumber) asignada por Walmart.")
        # Preferimos el endpoint bulk con FORMAT=PDF: entrega PDF directo,
        # compatible con el empaquetado n-up y la impresión por CUPS.
        try:
            resp = self._request(account, "POST", WALMART_LABELS_URL,
                                 params={"FORMAT": "PDF"},
                                 json={"trackingNumbers": [str(shipment_id)]},
                                 accept="application/pdf")
            content = resp.content
            if content[:5] == b"%PDF-":
                return content, "application/pdf", f"etiqueta_{shipment_id}.pdf"
        except ProviderError:
            pass
        # Respaldo: etiqueta individual en PNG, convertida a PDF si hay PyMuPDF.
        resp = self._request(account, "GET", f"{WALMART_LABEL_URL}/{shipment_id}",
                             accept="image/png")
        pdf = _png_to_pdf(resp.content)
        if pdf:
            return pdf, "application/pdf", f"etiqueta_{shipment_id}.pdf"
        return resp.content, "image/png", f"etiqueta_{shipment_id}.png"

    def split(self, account: dict, shipment_id: str, order_id: str,
              quantity: int, reason: str) -> dict:
        raise ProviderError("La separación de envíos no está disponible en Walmart.")


# --- helpers de módulo -------------------------------------------------------
def _extract_orders(data: dict) -> list[dict]:
    """Tolera las dos envolturas conocidas de la respuesta de /v3/orders."""
    if not isinstance(data, dict):
        return []
    lst = data.get("list") or {}
    elements = lst.get("elements") or data.get("elements") or {}
    orders = elements.get("order") if isinstance(elements, dict) else None
    if orders is None:
        orders = data.get("order")
    if isinstance(orders, dict):
        orders = [orders]
    return orders or []


def _normalize(order: dict) -> dict:
    shipping = order.get("shippingInfo") or {}
    addr = shipping.get("postalAddress") or {}
    buyer_name = addr.get("name") or "—"
    parts = [addr.get("address1"), addr.get("address2"), addr.get("city"),
             addr.get("state"), addr.get("postalCode")]
    address = ", ".join(p for p in parts if p)

    lines = order.get("orderLines") or []
    if isinstance(lines, dict):   # envoltura {"orderLine":[...]}
        lines = lines.get("orderLine") or []
    products, units, statuses = [], 0, set()
    for ln in lines:
        item = ln.get("item") or {}
        qty = _line_qty(ln)
        products.append({"title": item.get("productName", "—"), "quantity": qty,
                         "sku": item.get("sku") or ""})
        units += qty
        for st in _line_statuses(ln):
            statuses.add(st)

    # La guía viene en shipments[]; sin tracking aún no hay etiqueta que bajar.
    tracking = None
    for sh in order.get("shipments") or []:
        tracking = sh.get("trackingNumber") or tracking
    po = str(order.get("purchaseOrderId") or "")

    total = None
    order_total = order.get("orderTotal") or {}
    if isinstance(order_total, dict):
        total = (order_total.get("totalAmount") or {}).get("amount") \
            if isinstance(order_total.get("totalAmount"), dict) \
            else order_total.get("totalAmount")

    return {
        "order_id": order.get("customerOrderId") or po,
        "po": po,   # purchaseOrderId: es el que viene impreso en la guía FedEx
        "shipment_id": tracking or f"PO{po}",
        # pack_id: Walmart no tiene packs; el PO es el equivalente más cercano
        # (es el número impreso en la guía) — lo usa el registro de etiquetas.
        "pack_id": po or None,
        "date_created": order.get("orderDate"),
        "buyer_name": buyer_name, "address": address or "—",
        "products": products, "units": units or int(order.get("totalQuantity") or 1),
        "total_amount": total, "currency": "MXN",
        "shipment_status": "ready_to_ship" if tracking else "pending_label",
        "substatus": None if tracking else "sin guía",
        # --- registro de etiquetas (unificación con el Extractor) ---
        "tracking_number": tracking,
        "delivery_estimate": _epoch_iso(shipping.get("estimatedDeliveryDate")),
        "receiver": {
            "name": buyer_name,
            "zip": addr.get("postalCode"),
            "city": addr.get("city"),
            "state": addr.get("state"),
        },
    }


def _epoch_iso(value) -> str | None:
    """Walmart manda fechas como epoch en milisegundos; a ISO local."""
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return str(value) if value else None
    if ms <= 0:
        return None
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(ms / 1000.0))


def _line_qty(line: dict) -> int:
    q = line.get("orderLineQuantity") or {}
    try:
        return int(float(q.get("amount", 1)))
    except (TypeError, ValueError):
        return 1


def _line_statuses(line: dict) -> list[str]:
    sts = line.get("orderLineStatus") or line.get("orderLineStatuses") or []
    if isinstance(sts, dict):
        sts = sts.get("orderLineStatus") or []
    return [s.get("status", "") for s in sts if isinstance(s, dict)]


def _png_to_pdf(png_bytes: bytes) -> bytes | None:
    try:
        import fitz
        img = fitz.open(stream=png_bytes, filetype="png")
        try:
            return img.convert_to_pdf()
        finally:
            img.close()
    except Exception:
        return None


def _describe(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, list) and err:
            err = err[0]
        msg = (err or {}).get("description") if isinstance(err, dict) else None
        msg = msg or body.get("error_description") or body.get("message")
    except Exception:
        msg = None
    if not msg:
        msg = resp.text[:200].strip() or "error desconocido"
        if resp.status_code in (400, 401):
            msg += " — revisa el Client ID y Client Secret (mercado MX)."
    return f"Walmart devolvió {resp.status_code}: {msg}"
