"""Registro de proveedores disponibles."""
from __future__ import annotations

from providers.base import Provider, ProviderError
from providers.mercadolibre import MLProvider

_PROVIDERS: dict[str, Provider] = {
    "ml": MLProvider(),
    # Fase 2: "walmart": WalmartProvider(),
    # Fase 3: "tiktok": TikTokProvider(),
}

# Proveedores ofrecidos en la interfaz (label + si está disponible ya).
CATALOG = [
    {"id": "ml", "label": "Mercado Libre", "available": True},
    {"id": "walmart", "label": "Walmart", "available": False},
    {"id": "tiktok", "label": "TikTok Shop", "available": False},
]


def get_provider(name: str) -> Provider:
    p = _PROVIDERS.get(name)
    if p is None:
        raise ProviderError(f"Proveedor no soportado: {name}")
    return p
