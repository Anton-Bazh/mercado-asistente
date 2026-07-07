"""Proveedor TikTok Shop — OAuth con código, pedidos y guías por paquete.

Credenciales: App Key + App Secret de la app en partner.tiktokshop.com.
"Conectar" abre auth.tiktok-shops.com; si el redirect registrado en el Partner
Center no es este equipo, el vendedor copia el `code` de la URL de retorno y
usa «Canjear code» (mismo flujo de respaldo que Mercado Libre).

Todas las llamadas a la API van FIRMADAS: HMAC-SHA256 del path + parámetros
ordenados + body, envuelto en el App Secret (algoritmo oficial, verificado
contra el SDK EcomPHP/tiktokshop-php). La tienda autorizada aporta un
`shop_cipher` (guardado en la columna `site` de la cuenta) que acompaña a los
endpoints de pedidos/fulfillment.

La etiqueta se pide por paquete: shipping_documents → doc_url → PDF.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

import logutil
import storage
from config import (
    TIKTOK_AUTHORIZE_URL, TIKTOK_TOKEN_URL, TIKTOK_REFRESH_URL,
    TIKTOK_API_BASE, TIKTOK_API_VERSION, HTTP_TIMEOUT, TOKEN_REFRESH_MARGIN,
)
from providers.base import Provider, ProviderError

log = logutil.get_logger("tiktok")

# Códigos de la API que indican token inválido/expirado (reintento con refresh).
_TOKEN_ERRORS = {105000, 105001, 105002, 105003, 105004}


class TikTokProvider(Provider):
    name = "tiktok"
    label = "TikTok Shop"

    # --- OAuth ---------------------------------------------------------------
    def authorize_url(self, account: dict, state: str) -> str:
        if not (account.get("app_id") and account.get("client_secret")):
            raise ProviderError("Faltan credenciales (App Key y App Secret del Partner Center).")
        return f"{TIKTOK_AUTHORIZE_URL}?{urlencode({'app_key': account['app_id'], 'state': state})}"

    def exchange_code(self, account: dict, code: str) -> None:
        data = self._auth_request(TIKTOK_TOKEN_URL, {
            "app_key": account["app_id"], "app_secret": account["client_secret"],
            "auth_code": code, "grant_type": "authorized_code",
        })
        self._store(account, data)
        self._fetch_shop(account)

    def refresh(self, account: dict) -> str:
        if not account.get("refresh_token"):
            raise ProviderError("No hay refresh_token; vuelve a conectar la cuenta.")
        data = self._auth_request(TIKTOK_REFRESH_URL, {
            "app_key": account["app_id"], "app_secret": account["client_secret"],
            "refresh_token": account["refresh_token"], "grant_type": "refresh_token",
        })
        self._store(account, data)
        log.info("%s token renovado.", logutil.account_ctx(account))
        return account["access_token"]

    def _auth_request(self, url: str, params: dict) -> dict:
        try:
            resp = httpx.get(url, params=params, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red con TikTok Shop: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError(f"TikTok Shop devolvió {resp.status_code}: {resp.text[:200]}")
        if payload.get("code") != 0 or not payload.get("data"):
            raise ProviderError(f"TikTok Shop: {payload.get('message') or 'error de autorización'}")
        return payload["data"]

    def _store(self, account: dict, data: dict) -> None:
        access = data.get("access_token")
        if not access:
            raise ProviderError("Respuesta de token inválida (sin access_token).")
        exp = int(data.get("access_token_expire_in") or 0)
        if exp < 10_000_000:                      # vino como duración, no epoch
            exp = int(time.time()) + (exp or 7 * 86400)
        storage.update_account_tokens(account["id"], access,
                                      data.get("refresh_token") or account.get("refresh_token"),
                                      exp, nickname=data.get("seller_name"))
        account["access_token"] = access
        if data.get("refresh_token"):
            account["refresh_token"] = data["refresh_token"]
        account["token_expires_at"] = exp
        if data.get("seller_name"):
            account["nickname"] = data["seller_name"]

    def _valid_token(self, account: dict) -> str:
        exp = int(account.get("token_expires_at") or 0)
        if not account.get("access_token") or exp - int(time.time()) <= TOKEN_REFRESH_MARGIN:
            self.refresh(account)
        return account["access_token"]

    def _fetch_shop(self, account: dict) -> None:
        """Tienda autorizada: guarda shop_cipher (site), id y nombre."""
        data = self._call(account, "GET", f"/authorization/{TIKTOK_API_VERSION}/shops",
                          shop=False)
        shops = data.get("shops") or []
        if not shops:
            raise ProviderError("La autorización no incluye ninguna tienda de TikTok Shop.")
        shop = shops[0]
        storage.upsert_account(account["id"], site=shop.get("cipher"),
                               seller_id=str(shop.get("id") or ""),
                               nickname=shop.get("name") or account.get("nickname"))
        account["site"] = shop.get("cipher")
        account["seller_id"] = str(shop.get("id") or "")
        account["nickname"] = shop.get("name") or account.get("nickname")

    # --- HTTP firmado ----------------------------------------------------------
    def _call(self, account: dict, method: str, path: str, *, query: dict | None = None,
              body: dict | None = None, shop: bool = True, _retry: bool = True) -> dict:
        params = {"app_key": account["app_id"], "timestamp": str(int(time.time()))}
        if query:
            params.update({k: str(v) for k, v in query.items()})
        if shop:
            cipher = account.get("site")
            if not cipher:
                self._fetch_shop(account)
                cipher = account.get("site")
            params["shop_cipher"] = cipher
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        params["sign"] = _sign(path, params, body_str if method != "GET" else "",
                               account["client_secret"])
        headers = {"x-tts-access-token": self._valid_token(account),
                   "content-type": "application/json"}
        try:
            resp = httpx.request(method, TIKTOK_API_BASE + path, params=params,
                                 content=body_str or None, headers=headers,
                                 timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError(f"TikTok Shop devolvió {resp.status_code}: {resp.text[:200]}")
        code = payload.get("code")
        if code in _TOKEN_ERRORS and _retry:
            log.debug("%s token inválido (código %s) en %s; renovando y "
                      "reintentando…", logutil.account_ctx(account), code, path)
            self.refresh(account)
            return self._call(account, method, path, query=query, body=body,
                              shop=shop, _retry=False)
        if code != 0:
            log.debug("%s %s %s → código %s: %s", logutil.account_ctx(account),
                      method, path, code, resp.text[:500])
            raise ProviderError(f"TikTok Shop ({code}): {payload.get('message') or resp.text[:150]}")
        return payload.get("data") or {}

    # --- Pedidos -----------------------------------------------------------------
    def list_ready(self, account: dict, limit: int = 50) -> list[dict]:
        data = self._call(account, "POST",
                          f"/order/{TIKTOK_API_VERSION}/orders/search",
                          query={"page_size": min(limit, 100)},
                          body={"order_status": "AWAITING_SHIPMENT"})
        return [_normalize(o) for o in data.get("orders") or []]

    # --- Etiquetas ------------------------------------------------------------
    def get_label(self, account: dict, shipment_id: str, fmt: str = "pdf") -> tuple[bytes, str, str]:
        if fmt == "zpl":
            raise ProviderError("TikTok Shop no entrega etiquetas ZPL; usa PDF.")
        if str(shipment_id).startswith("PO"):
            raise ProviderError("El pedido aún no tiene paquete/guía asignada en TikTok Shop.")
        data = self._call(
            account, "GET",
            f"/fulfillment/{TIKTOK_API_VERSION}/packages/{shipment_id}/shipping_documents",
            query={"document_type": "SHIPPING_LABEL"})
        url = data.get("doc_url")
        if not url:
            raise ProviderError("TikTok Shop no devolvió la guía (doc_url vacío).")
        try:
            resp = httpx.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red al descargar la guía: {exc}") from exc
        if resp.status_code >= 400 or not resp.content:
            raise ProviderError(f"No se pudo descargar la guía ({resp.status_code}).")
        return resp.content, "application/pdf", f"etiqueta_{shipment_id}.pdf"

    def split(self, account: dict, shipment_id: str, order_id: str,
              quantity: int, reason: str) -> dict:
        raise ProviderError("La separación de envíos no está disponible en TikTok Shop.")


# --- helpers de módulo ------------------------------------------------------------
def _sign(path: str, params: dict, body: str, secret: str) -> str:
    """Firma oficial: secret + path + {k}{v} ordenados + body + secret, HMAC-SHA256."""
    keys = sorted(k for k in params if k not in ("sign", "access_token"))
    base = path + "".join(f"{k}{params[k]}" for k in keys) + body
    wrapped = secret + base + secret
    return hmac.new(secret.encode(), wrapped.encode(), hashlib.sha256).hexdigest()


def _normalize(order: dict) -> dict:
    addr = order.get("recipient_address") or {}
    address = addr.get("full_address") or ", ".join(
        d.get("address_name", "") for d in addr.get("district_info") or [] if d.get("address_name"))

    # line_items: una unidad por elemento → agrupar por producto/SKU.
    grouped: dict[tuple, int] = {}
    for it in order.get("line_items") or []:
        key = (it.get("product_name") or "—", it.get("seller_sku") or "")
        grouped[key] = grouped.get(key, 0) + 1
    products = [{"title": t, "sku": s, "quantity": q} for (t, s), q in grouped.items()]
    units = sum(grouped.values())

    packages = order.get("packages") or []
    package_id = str(packages[0].get("id")) if packages and packages[0].get("id") else None
    oid = str(order.get("id") or "")

    payment = order.get("payment") or {}
    created = order.get("create_time")
    if isinstance(created, (int, float)) and created > 0:
        created = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created))

    # district_info viene de mayor a menor (estado → ciudad/municipio → colonia).
    districts = [d.get("address_name") for d in addr.get("district_info") or []
                 if d.get("address_name")]

    return {
        "order_id": oid, "shipment_id": package_id or f"PO{oid}",
        # pack_id: el paquete es el agrupador de TikTok — equivalente más
        # cercano para el registro de etiquetas.
        "pack_id": package_id, "date_created": created,
        "buyer_name": addr.get("name") or "—", "address": address or "—",
        "products": products, "units": units or 1,
        "total_amount": payment.get("total_amount"),
        "currency": payment.get("currency") or "MXN",
        "shipment_status": "ready_to_ship" if package_id else "pending_label",
        "substatus": None if package_id else "sin guía",
        # --- registro de etiquetas (unificación con el Extractor) ---
        "tracking_number": (order.get("tracking_number")
                            or (packages[0].get("tracking_number") if packages else None)),
        "delivery_estimate": None,   # TikTok no expone fecha prometida utilizable
        "receiver": {
            "name": addr.get("name"),
            "zip": addr.get("zipcode") or addr.get("postal_code"),
            "city": districts[1] if len(districts) > 1 else None,
            "state": districts[0] if districts else None,
        },
    }
