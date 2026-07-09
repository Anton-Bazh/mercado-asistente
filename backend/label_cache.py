"""Copia local de las etiquetas descargadas — reintento sin depender de ML.

Cuando la impresión falla por causa del SISTEMA (impresora atascada, apagada,
cola detenida…), la etiqueta ya se descargó del marketplace: volver a pedirla
para reintentar es innecesario y, peor, IMPOSIBLE si el paquete ya fue
recolectado (ML responde «status is picked_up» y no la entrega nunca más —
visto el 09-jul-2026 con el envío 47484497514).

Este módulo guarda la última copia descargada de cada etiqueta (tal como la
entregó el proveedor, ya filtrada de hojas de contenido) en data/labels/ y la
sirve como respaldo cuando el marketplace no puede re-entregarla. data/ está
fuera de git y el contenido es el mismo PDF que ya se imprimió u obró en
poder del sistema: no amplía la superficie de datos.
"""
from __future__ import annotations

import re

import logutil
from config import DATA_DIR

log = logutil.get_logger("etiquetas")

CACHE_DIR = DATA_DIR / "labels"


def _path(shipment_id: str | int, fmt: str):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(shipment_id))
    ext = "zpl" if fmt == "zpl" else "pdf"
    return CACHE_DIR / f"{safe}.{ext}"


def save(shipment_id: str | int, content: bytes, fmt: str = "pdf") -> None:
    """Guarda/actualiza la copia local. Nunca interrumpe la impresión si falla."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _path(shipment_id, fmt).write_bytes(content)
    except OSError:
        log.warning("No se pudo guardar la copia local de la etiqueta %s.",
                    shipment_id, exc_info=True)


def load(shipment_id: str | int, fmt: str = "pdf") -> bytes | None:
    """Copia local de la etiqueta, o None si no existe."""
    try:
        p = _path(shipment_id, fmt)
        return p.read_bytes() if p.exists() else None
    except OSError:
        return None
