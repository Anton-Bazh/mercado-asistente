"""Proveedor Mercado Libre — OAuth, pedidos y etiquetas, por cuenta.

Adapta la lógica que antes vivía en auth.py + meli_client.py para operar sobre
una cuenta concreta (tokens propios), permitiendo varias tiendas a la vez.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

import storage
from config import (
    AUTHORIZATION_URL, TOKEN_URL, USERS_ME_URL, ORDERS_SEARCH_URL,
    SHIPMENTS_URL, LABELS_URL, HTTP_TIMEOUT, TOKEN_REFRESH_MARGIN,
)
from providers.base import Provider, ProviderError


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class MLProvider(Provider):
    name = "ml"
    label = "Mercado Libre"

    # --- OAuth ---------------------------------------------------------------
    def authorize_url(self, account: dict, state: str) -> str:
        if not (account.get("app_id") and account.get("client_secret")
                and account.get("redirect_uri")):
            raise ProviderError("Faltan credenciales (App ID, Client Secret y Redirect URI).")
        verifier, challenge = _pkce()
        storage.set_value("pkce_" + account["id"], verifier)
        params = {
            "response_type": "code",
            "client_id": account["app_id"],
            "redirect_uri": account["redirect_uri"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZATION_URL}?{urlencode(params)}"

    def exchange_code(self, account: dict, code: str) -> None:
        verifier = storage.get_value("pkce_" + account["id"])
        data = {
            "grant_type": "authorization_code",
            "client_id": account["app_id"],
            "client_secret": account["client_secret"],
            "code": code,
            "redirect_uri": account["redirect_uri"],
        }
        if verifier:
            data["code_verifier"] = verifier
        self._store(account, self._token_request(data))
        storage.delete_value("pkce_" + account["id"])
        self._fetch_seller(account)

    def refresh(self, account: dict) -> str:
        if not account.get("refresh_token"):
            raise ProviderError("No hay refresh_token; vuelve a conectar la cuenta.")
        data = {
            "grant_type": "refresh_token",
            "client_id": account["app_id"],
            "client_secret": account["client_secret"],
            "refresh_token": account["refresh_token"],
        }
        self._store(account, self._token_request(data))
        return account["access_token"]

    def _token_request(self, data: dict) -> dict:
        try:
            resp = httpx.post(TOKEN_URL, data=data,
                              headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red con Mercado Libre: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(_describe(resp))
        return resp.json()

    def _store(self, account: dict, payload: dict) -> None:
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not access:
            raise ProviderError("Respuesta de token inválida (sin access_token).")
        exp = int(time.time()) + int(payload.get("expires_in", 21600))
        storage.update_account_tokens(account["id"], access,
                                      refresh or account.get("refresh_token"), exp)
        account["access_token"] = access
        if refresh:
            account["refresh_token"] = refresh
        account["token_expires_at"] = exp

    def _valid_token(self, account: dict) -> str:
        exp = int(account.get("token_expires_at") or 0)
        if not account.get("access_token") or exp - int(time.time()) <= TOKEN_REFRESH_MARGIN:
            self.refresh(account)
        return account["access_token"]

    def _fetch_seller(self, account: dict) -> str:
        data = self._get(account, USERS_ME_URL).json()
        sid = str(data.get("id", ""))
        nickname = data.get("nickname", "")
        storage.upsert_account(account["id"], seller_id=sid, nickname=nickname)
        account["seller_id"] = sid
        account["nickname"] = nickname
        return sid

    # --- HTTP ----------------------------------------------------------------
    def _headers(self, account: dict) -> dict:
        return {"Authorization": f"Bearer {self._valid_token(account)}",
                "Accept": "application/json"}

    def _get(self, account: dict, url: str, params: dict | None = None,
             extra_headers: dict | None = None) -> httpx.Response:
        headers = {**self._headers(account), **(extra_headers or {})}
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red: {exc}") from exc
        if resp.status_code == 401:
            self.refresh(account)
            headers = {**self._headers(account), **(extra_headers or {})}
            try:
                resp = httpx.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Error de red: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"La API respondió {resp.status_code}: {resp.text[:200]}")
        return resp

    # --- Pedidos -------------------------------------------------------------
    def list_ready(self, account: dict, limit: int = 50) -> list[dict]:
        seller = account.get("seller_id") or self._fetch_seller(account)
        params = {"seller": seller, "order.status": "paid", "sort": "date_desc", "limit": limit}
        orders = self._get(account, ORDERS_SEARCH_URL, params=params).json().get("results", [])
        rows: list[dict] = []
        seen: set[str] = set()
        for order in orders:
            shipping = order.get("shipping") or {}
            sid = shipping.get("id")
            if not sid or str(sid) in seen:
                continue
            try:
                shipment = self._get_shipment(account, sid)
            except ProviderError:
                continue
            if shipment.get("status") != "ready_to_ship":
                continue
            seen.add(str(sid))
            rows.append(_normalize(order, shipment))
        return rows

    def _get_shipment(self, account: dict, shipment_id) -> dict:
        return self._get(account, f"{SHIPMENTS_URL}/{shipment_id}",
                         extra_headers={"x-format-new": "true"}).json()

    # --- Etiquetas -----------------------------------------------------------
    def get_label(self, account: dict, shipment_id: str, fmt: str = "pdf") -> tuple[bytes, str, str]:
        response_type = "zpl2" if fmt == "zpl" else "pdf"
        params = {"shipment_ids": str(shipment_id), "response_type": response_type}
        headers = {"Authorization": f"Bearer {self._valid_token(account)}"}
        try:
            resp = httpx.get(LABELS_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red al pedir la etiqueta: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"No se pudo obtener la etiqueta ({resp.status_code}): {resp.text[:200]}")
        content = resp.content
        if fmt == "zpl":
            return content, "text/plain; charset=utf-8", f"etiqueta_{shipment_id}.zpl"
        _remember_label_size(content)
        return content, "application/pdf", f"etiqueta_{shipment_id}.pdf"

    # --- Separación ----------------------------------------------------------
    def split(self, account: dict, shipment_id: str, order_id: str,
              quantity: int, reason: str = "DIMENSIONS_EXCEEDED") -> dict:
        body = {"reason": reason,
                "packs": [{"orders": [{"id": str(order_id), "quantity": int(quantity)}]}]}
        headers = {"Authorization": f"Bearer {self._valid_token(account)}",
                   "x-format-new": "true", "Content-Type": "application/json"}
        try:
            resp = httpx.post(f"{SHIPMENTS_URL}/{shipment_id}/split", json=body,
                              headers=headers, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Error de red al separar: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"No se pudo separar ({resp.status_code}): {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return {"ok": True}


# --- helpers de módulo -------------------------------------------------------
def _normalize(order: dict, shipment: dict) -> dict:
    buyer = order.get("buyer") or {}
    buyer_name = " ".join(p for p in (buyer.get("first_name"), buyer.get("last_name")) if p).strip() \
        or buyer.get("nickname", "—")
    addr = shipment.get("receiver_address") or {}
    city = addr.get("city") or {}
    state = addr.get("state") or {}
    parts = [addr.get("address_line"),
             city.get("name") if isinstance(city, dict) else city,
             state.get("name") if isinstance(state, dict) else state,
             addr.get("zip_code")]
    address = ", ".join(p for p in parts if p)
    items = order.get("order_items") or []
    products = [{"title": (it.get("item") or {}).get("title", "—"),
                 "quantity": it.get("quantity", 1)} for it in items]
    units = sum(int(p.get("quantity", 1) or 1) for p in products)
    return {
        "order_id": order.get("id"), "shipment_id": shipment.get("id"),
        "pack_id": order.get("pack_id"), "date_created": order.get("date_created"),
        "buyer_name": buyer_name, "address": address or "—",
        "products": products, "units": units,
        "total_amount": order.get("total_amount"), "currency": order.get("currency_id", ""),
        "shipment_status": shipment.get("status"), "substatus": shipment.get("substatus"),
    }


def _remember_label_size(pdf_bytes: bytes) -> None:
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


def _describe(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error_description") or body.get("error")
    except Exception:
        msg = None
    return f"Mercado Libre devolvió {resp.status_code}: {msg or 'error desconocido'}"
