"""Interfaz común de un proveedor de marketplace.

Cada proveedor (Mercado Libre, Walmart, TikTok…) implementa estos métodos
operando SOBRE UNA CUENTA (dict de storage.get_account). Así el resto de la app
(cola, impresión, reglas) es agnóstico del marketplace.

Fila de pedido normalizada que devuelve `list_ready`:
  { shipment_id, order_id, buyer_name, address, products:[{title,quantity}],
    units, substatus }
El hub añade luego account_id/account_name/provider/pending/multi_unit.
"""
from __future__ import annotations


class ProviderError(Exception):
    """Error de un proveedor (auth o API)."""


class Provider:
    name = "base"
    label = "Proveedor"
    # 'oauth' = flujo con redirect y code (authorize_url/exchange_code).
    # 'client_credentials' = solo Client ID + Secret; conectar = validar con connect().
    auth_mode = "oauth"

    # --- OAuth / conexión ---
    def authorize_url(self, account: dict, state: str) -> str:
        raise NotImplementedError

    def connect(self, account: dict) -> None:
        """Valida credenciales y marca la cuenta conectada (auth_mode client_credentials)."""
        raise NotImplementedError

    def exchange_code(self, account: dict, code: str) -> None:
        """Intercambia el code por tokens y los persiste en la cuenta."""
        raise NotImplementedError

    def refresh(self, account: dict) -> str:
        """Renueva y persiste el token; devuelve un access_token válido."""
        raise NotImplementedError

    def is_connected(self, account: dict) -> bool:
        return bool(account.get("refresh_token"))

    # --- Datos ---
    def list_ready(self, account: dict) -> list[dict]:
        """Pedidos listos para enviar, normalizados (ver arriba)."""
        raise NotImplementedError

    def get_label(self, account: dict, shipment_id: str, fmt: str = "pdf") -> tuple[bytes, str, str]:
        """(contenido, content_type, nombre_archivo)."""
        raise NotImplementedError

    def split(self, account: dict, shipment_id: str, order_id: str,
              quantity: int, reason: str) -> dict:
        raise NotImplementedError
