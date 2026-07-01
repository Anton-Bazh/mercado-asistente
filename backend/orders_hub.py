"""Agregación de pedidos de todas las cuentas/tiendas conectadas.

Recorre las cuentas habilitadas y conectadas, pide sus pedidos listos vía el
proveedor correspondiente, los etiqueta con su tienda y los combina en una sola
cola. Aísla errores: si una cuenta falla, las demás siguen.
"""
from __future__ import annotations

import storage
from providers.registry import get_provider
from providers.base import ProviderError


def connected_accounts() -> list[dict]:
    """Cuentas habilitadas y conectadas (con refresh_token)."""
    return [a for a in storage.list_accounts()
            if a.get("enabled") and a.get("refresh_token")]


def account_public(a: dict) -> dict:
    """Datos de cuenta seguros para la interfaz (sin secretos ni tokens)."""
    import time
    exp = int(a.get("token_expires_at") or 0)
    return {
        "id": a["id"], "provider": a.get("provider"),
        "name": a.get("name") or a.get("nickname") or "Tienda",
        "nickname": a.get("nickname"), "site": a.get("site"),
        "enabled": bool(a.get("enabled")),
        "connected": bool(a.get("refresh_token")),
        "has_secret": bool(a.get("client_secret")),
        "app_id": a.get("app_id") or "", "redirect_uri": a.get("redirect_uri") or "",
        "seller_id": a.get("seller_id"),
        "token_expires_in": max(0, exp - int(time.time())) if a.get("refresh_token") else None,
    }


def list_all_pending() -> dict:
    """Cola combinada: {orders, accounts, errors, printed_today}.

    Cada fila lleva account_id/account_name/provider + pending + multi_unit.
    """
    recent = storage.recent_printed_shipment_ids(within_seconds=600)
    try:
        threshold = int(storage.get_value(storage.MULTIUNIT_THRESHOLD) or "1")
    except ValueError:
        threshold = 1

    orders: list[dict] = []
    errors: list[dict] = []
    accounts_meta: list[dict] = []

    for acc in connected_accounts():
        name = acc.get("name") or acc.get("nickname") or "Tienda"
        accounts_meta.append({"id": acc["id"], "name": name, "provider": acc.get("provider")})
        try:
            rows = get_provider(acc["provider"]).list_ready(acc)
        except ProviderError as exc:
            errors.append({"account_id": acc["id"], "account_name": name, "error": str(exc)})
            continue
        for r in rows:
            r["account_id"] = acc["id"]
            r["account_name"] = name
            r["provider"] = acc.get("provider")
            already = (r.get("substatus") == "printed"
                       or str(r.get("shipment_id")) in recent)
            r["pending"] = not already
            r["multi_unit"] = int(r.get("units", 1) or 1) > threshold
            orders.append(r)

    return {
        "orders": orders,
        "accounts": accounts_meta,
        "errors": errors,
        "printed_today": storage.count_print_history_today(),
    }


def get_label(account_id: str, shipment_id: str, fmt: str = "pdf"):
    acc = storage.get_account(account_id)
    if not acc:
        raise ProviderError("Cuenta no encontrada para el envío.")
    return get_provider(acc["provider"]).get_label(acc, shipment_id, fmt)
