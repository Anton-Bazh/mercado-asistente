"""Registro de etiquetas — unificación con el Extractor de Etiquetas.

EtiquetaFlow NO está conectado a la BD corporativa (Supabase) y no se conectará
hasta que el sistema esté listo (decisión de Antonio, 07-jul-2026). Mientras
tanto, este módulo registra TODO en el SQLite propio (data/meli.db) en tablas
espejo con las MISMAS columnas que las de Supabase:

  dev_etiquetas_i · dev_v_code · dev_auditoria

y las lecturas del negocio (markup/tienda por venta, folio máximo) salen del
fixture `dev_ml_sales`, que se puebla a mano con `seed_ml_sales()`.

Cuando llegue el momento de conectar, se sustituye el interior de estas
funciones por llamadas a la BD corporativa SIN tocar al resto del código:
la interfaz (nombres, parámetros y retornos) ya es la definitiva.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import logutil
from config import DB_PATH, DATA_DIR

log = logutil.get_logger("bd")


class SupaError(Exception):
    """Fallo del registro de etiquetas."""


def status() -> dict:
    """Estado para la interfaz (chip «modo desarrollo (BD local)»)."""
    with _lock:
        conn = _connect()
        try:
            rows = {
                t: conn.execute(f"SELECT COUNT(*) FROM dev_{t}").fetchone()[0]
                for t in ("etiquetas_i", "v_code", "auditoria", "ml_sales")
            }
        finally:
            conn.close()
    return {"mode": "local", "dev_rows": rows}


# === Lecturas =================================================================
def get_markups(pack_ids: list[str]) -> dict[str, dict]:
    """{num_venta: {markup, tienda}} desde ml_sales para los pack_ids dados."""
    ids = [str(p) for p in pack_ids if p]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT num_venta, markup, tienda FROM dev_ml_sales"
                f" WHERE num_venta IN ({marks})", ids).fetchall()
        finally:
            conn.close()
    return {str(r["num_venta"]): {"markup": r["markup"], "tienda": r["tienda"]}
            for r in rows}


def max_folio() -> int:
    """Folio más alto registrado (0 si no hay). Para autosugerir el inicial."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT MAX(folio) FROM dev_etiquetas_i").fetchone()
        finally:
            conn.close()
    return int(row[0] or 0)


def batch_code_exists(code_i: str) -> bool:
    """True si el código de lote ya fue registrado (check de duplicidad)."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT 1 FROM dev_v_code WHERE code_i = ? LIMIT 1",
                               (code_i,)).fetchone()
        finally:
            conn.close()
    return row is not None


# === Escrituras ===============================================================
# Columnas que acepta el INSERT (las mismas que usa hoy el Extractor;
# id/created_at los pone la BD; deli_hour existe pero no se inserta — igual hoy).
_ETIQUETAS_COLS = (
    "code", "sales_num", "product", "sku", "quantity", "deli_date",
    "organization", "sou_file", "personal_inc", "hour", "imp_date",
    "client", "cp", "state", "city", "folio", "client_name", "code_i", "pack_id",
)
_VCODE_COLS = ("code_i", "corte_etiquetas", "personal_inc", "first_code", "personal_bar")
_AUDITORIA_COLS = ("venta_id", "folio_venta", "auditor_uid",
                   "auditor_nombre_completo", "auditor_email",
                   "fecha_revision", "code_i")


def insert_etiquetas(rows: list[dict]) -> int:
    """Inserta las filas de etiquetas (una por SKU). Devuelve cuántas."""
    clean = [{c: r.get(c) for c in _ETIQUETAS_COLS} for r in rows]
    if not clean:
        return 0
    _insert("dev_etiquetas_i", _ETIQUETAS_COLS, clean)
    log.info("%d etiqueta(s) registradas en BD local dev (lote %s).",
             len(clean), clean[0].get("code_i"))
    return len(clean)


def insert_v_code(row: dict) -> None:
    _insert("dev_v_code", _VCODE_COLS, [{c: row.get(c) for c in _VCODE_COLS}])


def insert_auditoria(row: dict) -> None:
    _insert("dev_auditoria", _AUDITORIA_COLS,
            [{c: row.get(c) for c in _AUDITORIA_COLS}])


def seed_ml_sales(rows: list[dict]) -> int:
    """Carga/actualiza el fixture dev_ml_sales para pruebas de desarrollo."""
    with _lock:
        conn = _connect()
        try:
            conn.executemany(
                "INSERT INTO dev_ml_sales (num_venta, markup, tienda) VALUES (?, ?, ?)"
                " ON CONFLICT(num_venta) DO UPDATE SET markup=excluded.markup,"
                " tienda=excluded.tienda",
                [(str(r["num_venta"]), r.get("markup"), r.get("tienda")) for r in rows])
            conn.commit()
        finally:
            conn.close()
    log.info("Fixture dev_ml_sales: %d venta(s) cargadas.", len(rows))
    return len(rows)


# === SQLite ===================================================================
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dev_etiquetas_i ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL,"
        " code TEXT, sales_num NUMERIC, product TEXT, sku TEXT,"
        " quantity INTEGER, deli_date TEXT, organization TEXT, sou_file TEXT,"
        " personal_inc TEXT, hour TEXT, imp_date TEXT, client TEXT,"
        " cp INTEGER, state TEXT, city TEXT, folio INTEGER, client_name TEXT,"
        " deli_hour TEXT, code_i TEXT, pack_id NUMERIC"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dev_v_code ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL,"
        " code_i TEXT, corte_etiquetas TEXT, personal_inc TEXT,"
        " first_code NUMERIC, personal_bar TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dev_auditoria ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL,"
        " venta_id TEXT, folio_venta TEXT, auditor_uid TEXT,"
        " auditor_nombre_completo TEXT, auditor_email TEXT,"
        " fecha_revision TEXT, code_i TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dev_ml_sales ("
        " num_venta TEXT PRIMARY KEY,"
        " markup NUMERIC,"
        " tienda TEXT"
        ")"
    )
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert(table: str, cols: tuple[str, ...], rows: list[dict]) -> None:
    sql = (f"INSERT INTO {table} (created_at, {', '.join(cols)}) "
           f"VALUES (?, {', '.join('?' for _ in cols)})")
    with _lock:
        conn = _connect()
        try:
            conn.executemany(sql, [(_now(), *[r.get(c) for c in cols]) for r in rows])
            conn.commit()
        except sqlite3.Error as exc:
            raise SupaError(f"BD local dev: {exc}") from exc
        finally:
            conn.close()
