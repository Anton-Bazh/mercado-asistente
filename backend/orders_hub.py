"""Agregación de pedidos de todas las cuentas/tiendas conectadas.

Recorre las cuentas habilitadas y conectadas, pide sus pedidos listos vía el
proveedor correspondiente, los etiqueta con su tienda y los combina en una sola
cola. Aísla errores: si una cuenta falla, las demás siguen.
"""
from __future__ import annotations

from datetime import datetime

import label_stub
import logutil
import storage
from providers.registry import get_provider
from providers.base import ProviderError

log = logutil.get_logger("tiendas")

# Último error visto por cuenta: el frontend sondea la cola cada pocos
# segundos, así que solo se loguea a WARNING cuando el error cambia (o cuando
# la cuenta se recupera); las repeticiones van a DEBUG (quedan en el archivo).
_LAST_ERR: dict[str, str] = {}

# Datos del pedido por envío, para armar el talón de control al momento de
# pedir la etiqueta (get_label solo recibe el shipment_id). Se alimenta en
# cada refresco de la cola; acotado para no crecer sin límite.
_ORDER_INFO: dict[str, dict] = {}
_ORDER_INFO_MAX = 800


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


def due_bucket(iso: str | None) -> str:
    """Clasifica la fecha límite de despacho del marketplace.

    'today' = debe imprimirse hoy · 'upcoming' = para los siguientes días ·
    'overdue' = la fecha límite ya pasó. Sin fecha (marketplaces que no la
    reportan) se asume 'today': el pedido ya está listo para enviar.
    """
    if not iso:
        return "today"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "today"
    limit_day = dt.astimezone().date()
    today = datetime.now().astimezone().date()
    if limit_day > today:
        return "upcoming"
    if limit_day < today:
        return "overdue"
    return "today"


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
        lctx = logutil.account_ctx(acc)
        try:
            rows = get_provider(acc["provider"]).list_ready(acc)
        except ProviderError as exc:
            errors.append({"account_id": acc["id"], "account_name": name, "error": str(exc)})
            if _LAST_ERR.get(acc["id"]) != str(exc):
                _LAST_ERR[acc["id"]] = str(exc)
                log.warning("%s no se pudieron leer los pedidos: %s", lctx, exc)
            else:
                log.debug("%s sigue fallando la lectura de pedidos: %s", lctx, exc)
            continue
        if acc["id"] in _LAST_ERR:
            del _LAST_ERR[acc["id"]]
            log.info("%s se recuperó: pedidos leídos de nuevo con éxito.", lctx)
        log.debug("%s cola leída: %d pedido(s) listos para enviar.", lctx, len(rows))
        for r in rows:
            r["account_id"] = acc["id"]
            r["account_name"] = name
            r["provider"] = acc.get("provider")
            _remember_order(r)
            already = (r.get("substatus") == "printed"
                       or str(r.get("shipment_id")) in recent)
            r["pending"] = not already
            r["multi_unit"] = int(r.get("units", 1) or 1) > threshold
            r["due"] = due_bucket(r.get("handling_limit"))
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
        log.error("Se pidió la etiqueta del envío %s de una cuenta inexistente (%s).",
                  shipment_id, account_id)
        raise ProviderError("Cuenta no encontrada para el envío.")
    log.debug("%s pidiendo etiqueta del envío %s (%s)…",
              logutil.account_ctx(acc), shipment_id, fmt)
    content, ctype, fname = get_provider(acc["provider"]).get_label(acc, shipment_id, fmt)
    # Talón de control de producto (Walmart/TikTok): solo PDF, y nunca debe
    # bloquear la impresión si algo falla al dibujarlo.
    if fmt == "pdf" and ctype.startswith("application/pdf") \
            and label_stub.enabled_for(acc.get("provider", ""), acc.get("id")):
        info = _ORDER_INFO.get(str(shipment_id))
        if not info:
            # busca → planea → modifica: si el envío no está en caché (reinicio,
            # reimpresión), se consulta el pedido al marketplace antes de talonar.
            try:
                for r in get_provider(acc["provider"]).list_ready(acc):
                    _remember_order(r)
            except ProviderError as exc:
                log.debug("%s no se pudo reconsultar el pedido del envío %s "
                          "para el talón: %s", logutil.account_ctx(acc),
                          shipment_id, exc)
            info = _ORDER_INFO.get(str(shipment_id))
        if info and info.get("products"):
            try:
                content = label_stub.add_stub(content, info["products"],
                                              order_ref=str(info.get("order_id") or ""))
            except Exception:
                log.warning("%s no se pudo dibujar el talón de control del envío "
                            "%s (la etiqueta sale sin talón).",
                            logutil.account_ctx(acc), shipment_id, exc_info=True)
    return content, ctype, fname


def _remember_order(row: dict) -> None:
    sid = str(row.get("shipment_id") or "")
    if not sid:
        return
    _ORDER_INFO[sid] = {"products": row.get("products") or [],
                        "order_id": row.get("order_id")}
    while len(_ORDER_INFO) > _ORDER_INFO_MAX:
        _ORDER_INFO.pop(next(iter(_ORDER_INFO)))
