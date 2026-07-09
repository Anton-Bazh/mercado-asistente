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

import logutil
from config import DB_PATH, DATA_DIR, KEY_PATH

log = logutil.get_logger("almacen")

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
# --- Modo automático (motor de reglas por horario en el servidor) ---
AUTO_ENABLED = "auto_enabled"        # '1'/'0'
AUTO_INTERVAL = "auto_interval_min"  # minutos entre revisiones
AUTO_RULES = "auto_rules"            # JSON: reglas semanales por día/tramo/modo
MULTIUNIT_THRESHOLD = "multiunit_threshold"  # >N unidades → lista de separación (def. 1)
STUB_PROVIDERS = "stub_providers"    # JSON {'walmart': bool, 'tiktok': bool} — talón de control

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
    log.info("Clave de cifrado nueva generada en %s (0600).", KEY_PATH)
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
        log.info("Migración: columna 'status' añadida a print_history.")
    if "account" not in cols:
        try:
            conn.execute("ALTER TABLE print_history ADD COLUMN account TEXT")
            log.info("Migración: columna 'account' añadida a print_history.")
        except sqlite3.OperationalError:
            pass
    if "origin" not in cols:
        # 'manual' | 'auto' — quién arrancó la impresión (persona o scheduler)
        try:
            conn.execute("ALTER TABLE print_history ADD COLUMN origin TEXT")
            log.info("Migración: columna 'origin' añadida a print_history.")
        except sqlite3.OperationalError:
            pass
    if "account_id" not in cols:
        # id de la tienda: sin él, la REIMPRESIÓN desde Historial no sabe a
        # qué cuenta pedir la etiqueta (bug de la prueba real del 09-jul;
        # solo se guardaba el nombre). Filas viejas quedan NULL y la
        # impresión las resuelve por nombre o por cuenta única.
        try:
            conn.execute("ALTER TABLE print_history ADD COLUMN account_id TEXT")
            log.info("Migración: columna 'account_id' añadida a print_history.")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_ts ON print_history (ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_shipment ON print_history (shipment_id)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS accounts ("
        " id TEXT PRIMARY KEY,"
        " provider TEXT NOT NULL,"
        " name TEXT,"
        " site TEXT,"
        " app_id TEXT,"
        " client_secret TEXT,"        # cifrado
        " redirect_uri TEXT,"
        " access_token TEXT,"         # cifrado
        " refresh_token TEXT,"        # cifrado
        " token_expires_at INTEGER,"
        " seller_id TEXT,"
        " nickname TEXT,"
        " enabled INTEGER NOT NULL DEFAULT 1,"
        " created INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    return conn


def init() -> None:
    """Inicializa la base de datos (idempotente)."""
    with _lock:
        conn = _connect()
        conn.commit()
        conn.close()
    migrate_single_account()


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
    "account", "origin", "account_id",
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
    account: Optional[str] = None,
    origin: str = "manual",
    ts: Optional[int] = None,
    account_id: Optional[str] = None,
) -> None:
    """Registra un intento de impresión de un envío con su estado."""
    ok = 1 if status == "ok" else 0
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO print_history"
                " (ts, batch_id, shipment_id, order_id, buyer_name,"
                "  product_summary, format, printer, sheets, ok, status, error,"
                "  account, origin, account_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts if ts is not None else int(time.time()),
                    batch_id, str(shipment_id),
                    str(order_id) if order_id is not None else None,
                    buyer_name, product_summary, fmt, printer, sheets,
                    ok, status, error, account, origin, account_id,
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
    return count_print_history_today_split()["total"]


def count_print_history_today_split() -> dict:
    """Impresiones exitosas de hoy, desglosadas por origen (auto/manual)."""
    now = time.localtime()
    start = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                             0, 0, 0, 0, 0, -1)))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT COALESCE(origin, 'manual'), COUNT(*) FROM print_history"
                " WHERE status = 'ok' AND ts >= ? GROUP BY 1",
                (start,),
            ).fetchall()
        finally:
            conn.close()
    split = {"auto": 0, "manual": 0}
    for origin, n in rows:
        split["auto" if origin == "auto" else "manual"] += int(n)
    split["total"] = split["auto"] + split["manual"]
    return split


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


# --- Cuentas / tiendas (multi-proveedor) -------------------------------------
_ACCOUNT_COLS = (
    "id", "provider", "name", "site", "app_id", "client_secret", "redirect_uri",
    "access_token", "refresh_token", "token_expires_at", "seller_id",
    "nickname", "enabled", "created",
)
_ACCOUNT_ENC = {"client_secret", "access_token", "refresh_token"}


def _row_to_account(row) -> dict:
    d = dict(zip(_ACCOUNT_COLS, row))
    for k in _ACCOUNT_ENC:
        if d.get(k):
            try:
                d[k] = _fernet.decrypt(d[k].encode()).decode()
            except Exception:
                d[k] = None
    d["enabled"] = bool(d["enabled"])
    return d


def list_accounts() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT " + ", ".join(_ACCOUNT_COLS) + " FROM accounts ORDER BY created"
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_account(r) for r in rows]


def get_account(account_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT " + ", ".join(_ACCOUNT_COLS) + " FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_account(row) if row else None


def upsert_account(account_id: str, **fields) -> None:
    """Crea o actualiza una cuenta. Cifra secret/tokens. Solo toca los campos dados."""
    data = {}
    for k, v in fields.items():
        if k not in _ACCOUNT_COLS:
            continue
        if k in _ACCOUNT_ENC and v is not None:
            v = _fernet.encrypt(v.encode()).decode()
        data[k] = v
    with _lock:
        conn = _connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if exists:
                if data:
                    sets = ", ".join(f"{k} = ?" for k in data)
                    conn.execute(f"UPDATE accounts SET {sets} WHERE id = ?",
                                 (*data.values(), account_id))
            else:
                data.setdefault("provider", "ml")
                data.setdefault("enabled", 1)
                data.setdefault("created", int(time.time()))
                data["id"] = account_id
                cols = ", ".join(data)
                ph = ", ".join("?" for _ in data)
                conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({ph})",
                             tuple(data.values()))
            conn.commit()
        finally:
            conn.close()


def update_account_tokens(account_id: str, access_token: str, refresh_token: Optional[str],
                          token_expires_at: int, seller_id: Optional[str] = None,
                          nickname: Optional[str] = None) -> None:
    fields = {"access_token": access_token, "token_expires_at": token_expires_at}
    if refresh_token:
        fields["refresh_token"] = refresh_token
    if seller_id is not None:
        fields["seller_id"] = seller_id
    if nickname is not None:
        fields["nickname"] = nickname
    upsert_account(account_id, **fields)


def clear_account_tokens(account_id: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE accounts SET access_token=NULL, refresh_token=NULL,"
                " token_expires_at=NULL, seller_id=NULL, nickname=NULL WHERE id=?",
                (account_id,))
            conn.commit()
        finally:
            conn.close()


def set_account_enabled(account_id: str, enabled: bool) -> None:
    upsert_account(account_id, enabled=1 if enabled else 0)


def delete_account(account_id: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
        finally:
            conn.close()


def migrate_single_account() -> None:
    """Si hay tokens de la versión de una sola cuenta, crea la cuenta ML."""
    import uuid
    if list_accounts():
        return
    app_id = get_value(APP_ID)
    refresh = get_value(REFRESH_TOKEN)
    if not (app_id or refresh):
        return
    upsert_account(
        uuid.uuid4().hex[:12], provider="ml",
        name=get_value(SELLER_NICKNAME) or "Mercado Libre",
        site="MLM", app_id=app_id, client_secret=get_value(CLIENT_SECRET),
        redirect_uri=get_value(REDIRECT_URI),
        access_token=get_value(ACCESS_TOKEN), refresh_token=refresh,
        token_expires_at=int(get_value(TOKEN_EXPIRES_AT) or 0),
        seller_id=get_value(SELLER_ID), nickname=get_value(SELLER_NICKNAME),
        enabled=1,
    )


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
