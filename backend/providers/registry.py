"""Registro de proveedores disponibles."""
from __future__ import annotations

from providers.base import Provider, ProviderError
from providers.mercadolibre import MLProvider
from providers.tiktok import TikTokProvider
from providers.walmart import WalmartProvider

_PROVIDERS: dict[str, Provider] = {
    "ml": MLProvider(),
    "walmart": WalmartProvider(),
    "tiktok": TikTokProvider(),
}

# Proveedores ofrecidos en la interfaz. needs_redirect: el formulario pide
# Redirect URI (TikTok lo registra en el Partner Center, no aquí).
CATALOG = [
    {"id": "ml", "label": "Mercado Libre", "available": True,
     "auth_mode": "oauth", "needs_redirect": True},
    {"id": "walmart", "label": "Walmart", "available": True,
     "auth_mode": "client_credentials", "needs_redirect": False},
    {"id": "tiktok", "label": "TikTok Shop", "available": True,
     "auth_mode": "oauth", "needs_redirect": False},
]


def get_provider(name: str) -> Provider:
    p = _PROVIDERS.get(name)
    if p is None:
        raise ProviderError(f"Proveedor no soportado: {name}")
    return p
