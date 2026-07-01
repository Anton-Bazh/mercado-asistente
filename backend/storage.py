"""Almacén local cifrado para credenciales y tokens.

Usa SQLite para persistencia y Fernet (cryptography) para cifrar en reposo los
valores sensibles (Client Secret y tokens OAuth). La clave de cifrado vive en
data/secret.key con permisos 0600 y se genera automáticamente la primera vez.

El esquema es un simple almacén clave-valor; los valores sensibles se marcan
como cifrados al guardarlos.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

from cryptography.fernet import Fernet

from config import DB_PATH, DATA_DIR, KEY_PATH

# Claves de configuración (no sensibles salvo las marcadas)
APP_ID = "app_id"
CLIENT_SECRET = "client_secret"      # sensible (cifrado)
REDIRECT_URI = "redirect_uri"
ACCESS_TOKEN = "access_token"        # sensible (cifrado)
REFRESH_TOKEN = "refresh_token"      # sensible (cifrado)
TOKEN_EXPIRES_AT = "token_expires_at"
SELLER_ID = "seller_id"
SELLER_NICKNAME = "seller_nickname"
PKCE_VERIFIER = "pkce_verifier"      # sensible (cifrado), transitorio durante el login
OAUTH_STATE = "oauth_state"          # transitorio durante el login
DEFAULT_PRINTER = "default_printer"  # predeterminada propia de EtiquetaFlow ('' = ninguna explícita)
LABEL_W = "label_w"                  # tamaño real de la etiqueta (pt), aprendido al descargar una
LABEL_H = "label_h"
# --- Modo automático (impresión por horario en el servidor) ---
AUTO_ENABLED = "auto_enabled"        # '1'/'0'
AUTO_START = "auto_start"            # 'HH:MM' inicio de ventana
AUTO_END = "auto_end"                # 'HH:MM' fin de ventana
AUTO_INTERVAL = "auto_interval_min"  # minutos entre revisiones dentro de la ventana

# Conjunto de claves cuyo valor se cifra en reposo
_ENCRYPTED_KEYS = {CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN, PKCE_VERIFIER}

_lock = threading.Lock()


def _ensure_key() -> bytes:
    """Devuelve la clave Fernet, generándola con permisos 0600 si no existe."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    # Crear con permisos restrictivos desde el inicio
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.chmod(KEY_PATH, 0o600)
    return key


_fernet = Fernet(_ensure_key())


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings ("
        " key TEXT PRIMARY KEY,"
        " value TEXT NOT NULL,"
        " encrypted INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS print_history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts INTEGER NOT NULL,"
        " batch_id TEXT NOT NULL,"
        " shipment_id TEXT NOT NULL,"
        " order_id TEXT,"
        " buyer_name TEXT,"
        " product_summary TEXT,"
        " format TEXT NOT NULL,"
        " printer TEXT,"
        " sheets INTEGER,"
        " ok INTEGER NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'ok',"
        " error TEXT"
        ")"
    )
    # Migración defensiva: añadir 'status' si la tabla es de una versión previa.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(print_history)")}
    if "status" not in cols:
        conn.execute("ALTER TABLE print_history ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
        # Deriva el estado de las filas antiguas: ok=1→'ok', ok=0→'error'.
        conn.execute("UPDATE print_history SET status = CASE WHEN ok = 1 THEN 'ok' ELSE 'error' END")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_ts ON print_history (ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_shipment ON print_history (shipment_id)"
    )
    return conn


def init() -> None:
    """Inicializa la base de datos (idempotente)."""
    with _lock:
        conn = _connect()
        conn.commit()
        conn.close()


def set_value(key: str, value: Optional[str]) -> None:
    """Guarda un valor. Si value es None, elimina la clave."""
    with _lock:
        conn = _connect()
        try:
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                encrypted = 1 if key in _ENCRYPTED_KEYS else 0
                stored = (
                    _fernet.encrypt(value.encode()).decode() if encrypted else value
                )
                conn.execute(
                    "INSERT INTO settings (key, value, encrypted) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " encrypted=excluded.encrypted",
                    (key, stored, encrypted),
                )
            conn.commit()
        finally:
            conn.close()


def get_value(key: str) -> Optional[str]:
    """Recupera un valor, descifrándolo si corresponde. None si no existe."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value, encrypted FROM settings WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    value, encrypted = row
    if encrypted:
        return _fernet.decrypt(value.encode()).decode()
    return value


def delete_value(key: str) -> None:
    set_value(key, None)


def set_many(items: dict[str, Optional[str]]) -> None:
    for k, v in items.items():
        set_value(k, v)


# --- Historial de impresión --------------------------------------------------
# status: 'ok' (impreso confirmado) · 'blocked' (no se pidió a ML; a salvo)
#         · 'risk' (pedido a ML pero sin confirmar → revisar) · 'error' (otro fallo)
_HISTORY_COLS = (
    "id", "ts", "batch_id", "shipment_id", "order_id", "buyer_name",
    "product_summary", "format", "printer", "sheets", "ok", "status", "error",
)


def add_print_history(
    batch_id: str,
    shipment_id: str,
    fmt: str,
    status: str = "ok",
    order_id: Optional[str] = None,
    buyer_name: Optional[str] = None,
    product_summary: Optional[str] = None,
    printer: Optional[str] = None,
    sheets: Optional[int] = None,
    error: Optional[str] = None,
    ts: Optional[int] = None,
) -> None:
    """Registra un intento de impresión de un envío con su estado."""
    ok = 1 if status == "ok" else 0
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO print_history"
                " (ts, batch_id, shipment_id, order_id, buyer_name,"
                "  product_summary, format, printer, sheets, ok, status, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts if ts is not None else int(time.time()),
                    batch_id, str(shipment_id),
                    str(order_id) if order_id is not None else None,
                    buyer_name, product_summary, fmt, printer, sheets,
                    ok, status, error,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def list_print_history(
    limit: int = 50,
    offset: int = 0,
    date_from: Optional[int] = None,
    date_to: Optional[int] = None,
    fmt: Optional[str] = None,
    result: Optional[str] = None,  # 'ok'|'error'|'risk'|'blocked'|None
) -> tuple[list[dict], int]:
    """Lista paginada del historial con filtros. Devuelve (items, total)."""
    where: list[str] = []
    params: list = []
    if date_from is not None:
        where.append("ts >= ?"); params.append(int(date_from))
    if date_to is not None:
        where.append("ts <= ?"); params.append(int(date_to))
    if fmt in ("pdf", "zpl"):
        where.append("format = ?"); params.append(fmt)
    if result == "ok":
        where.append("status = 'ok'")
    elif result == "risk":
        where.append("status = 'risk'")
    elif result == "blocked":
        where.append("status = 'blocked'")
    elif result == "error":
        where.append("status NOT IN ('ok','risk','blocked')")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM print_history" + clause, params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT " + ", ".join(_HISTORY_COLS) + " FROM print_history"
                + clause + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)],
            ).fetchall()
        finally:
            conn.close()

    items = []
    for r in rows:
        d = dict(zip(_HISTORY_COLS, r))
        d["ok"] = bool(d["ok"])
        items.append(d)
    return items, total


def count_print_history_today() -> int:
    """Cuenta impresiones exitosas del día local en curso."""
    now = time.localtime()
    start = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                             0, 0, 0, 0, 0, -1)))
    with _lock:
        conn = _connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM print_history WHERE status = 'ok' AND ts >= ?",
                (start,),
            ).fetchone()[0]
        finally:
            conn.close()
    return int(n)


def count_risk() -> int:
    """Envíos que quedaron 'en riesgo' (pedidos a ML sin confirmar impresión)."""
    with _lock:
        conn = _connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM print_history WHERE status = 'risk'"
            ).fetchone()[0]
        finally:
            conn.close()
    return int(n)


def recent_printed_shipment_ids(within_seconds: int = 600) -> set[str]:
    """shipment_ids con una impresión exitosa reciente (para marcar 'pending')."""
    cutoff = int(time.time()) - within_seconds
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT shipment_id FROM print_history"
                " WHERE status = 'ok' AND ts >= ?",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    return {str(r[0]) for r in rows}
