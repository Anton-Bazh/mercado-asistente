"""Sistema de logs de EtiquetaFlow.

Un solo flujo de eventos con dos salidas complementarias:

  * **Consola** — compacta y legible para operar el día a día: hora corta,
    nivel con color (si es una terminal) y mensaje. Solo INFO en adelante.
  * **Archivo** — `data/logs/etiquetaflow.log`, rotativo (5 MB × 7), con
    TODO el detalle: fecha completa, nivel, módulo, hilo, mensaje y traceback
    íntegro. Pensado para analizar errores y warnings a posteriori, incluso
    de días anteriores.

Convenciones:
  * Cada módulo pide su logger con un nombre corto en español:
    `log = logutil.get_logger("lotes")` → servidor, tiendas, lotes, auto,
    cups, almacen, importar, ml, walmart, tiktok…
  * Los eventos de una cuenta/tienda anteponen `ctx(provider, nombre)` para
    poder filtrar por tienda en instalaciones multi-cuenta:
    `log.info("%s cola leída", logutil.ctx("ml", "MiTienda"))`.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time

from config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "etiquetaflow.log"

_configured = False

# Colores ANSI por nivel (solo si la salida es una terminal).
_COLORS = {
    logging.DEBUG: "\033[2m",       # tenue
    logging.INFO: "\033[36m",       # cian
    logging.WARNING: "\033[33m",    # amarillo
    logging.ERROR: "\033[31m",      # rojo
    logging.CRITICAL: "\033[1;41m",  # blanco sobre rojo
}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    """Formato corto para consola; colorea el nivel si hay terminal."""

    def __init__(self, color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)-10s │ %(message)s",
                         datefmt="%H:%M:%S")
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self._color:
            c = _COLORS.get(record.levelno, "")
            if c:
                text = f"{c}{text}{_RESET}"
        return text


def setup() -> None:
    """Configura el logging global (idempotente). Llamar antes de crear la app."""
    global _configured
    if _configured:
        return
    _configured = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Archivo rotativo: máximo detalle (DEBUG+), con hilo y traceback.
    file_h = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(
        "%(asctime)s │ %(levelname)-8s │ %(name)-10s │ %(threadName)-12s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_h)

    # Consola: solo lo relevante (INFO+), compacto y con color.
    con_h = logging.StreamHandler(sys.stderr)
    con_h.setLevel(logging.INFO)
    con_h.setFormatter(_ConsoleFormatter(color=sys.stderr.isatty()))
    root.addHandler(con_h)

    # Warnings de Python (deprecaciones, SSL, etc.) → al mismo flujo.
    logging.captureWarnings(True)

    # httpx/httpcore logean cada request a INFO y python_multipart cada parte
    # de un formulario a DEBUG; bajarlos para no ensuciar.
    for noisy in ("httpx", "httpcore", "multipart", "python_multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # uvicorn ya escribe en consola; añadir SOLO el archivo para no duplicar.
    # Basta con el logger padre: 'uvicorn.error' propaga hasta él. No tocar
    # 'uvicorn.access': darle un handler lo reactivaría aunque el servidor
    # corra con --no-access-log (el middleware ya traza cada request al archivo).
    logging.getLogger("uvicorn").addHandler(file_h)


def get_logger(name: str) -> logging.Logger:
    """Logger de módulo con nombre corto (p. ej. 'lotes', 'auto', 'cups')."""
    return logging.getLogger(name)


def ctx(provider: str | None = None, account: str | None = None) -> str:
    """Etiqueta de contexto tienda/cuenta para anteponer al mensaje.

    ctx('ml', 'MiTienda') → '[ml·MiTienda]'; tolera faltantes: '[ml]', '[MiTienda]'.
    """
    parts = [p for p in (provider, account) if p]
    return f"[{'·'.join(parts)}]" if parts else ""


def account_ctx(acc: dict | None) -> str:
    """ctx() a partir de un dict de cuenta de storage (provider + nombre)."""
    if not acc:
        return ""
    return ctx(acc.get("provider"), acc.get("name") or acc.get("nickname"))


# --- Lectura para la interfaz (pestaña Logs) ----------------------------------
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _parse_ts(text: str) -> int:
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return 0


def read_entries(hours: float = 24, min_level: str = "INFO", logger_name: str = "",
                 query: str = "", limit: int = 300) -> dict:
    """Lee el log (actual + rotados) para la pestaña Logs de la interfaz.

    hours=0 → sin límite de tiempo. Devuelve las entradas MÁS RECIENTES primero:
    {entries, total, loggers} donde `loggers` son los módulos vistos en el rango
    (para poblar el filtro) independientemente del resto de filtros.
    """
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    min_lv = _LEVEL_ORDER.get(min_level.upper(), 20)
    q = query.strip().lower()

    # Del más viejo al más nuevo: etiquetaflow.log.7 … .1 y al final el actual.
    files = sorted(LOG_DIR.glob(LOG_FILE.name + ".*"),
                   key=lambda p: p.name, reverse=True) + [LOG_FILE]
    matched: list[dict] = []
    loggers: set[str] = set()
    for path in files:
        try:
            if not path.exists() or (cutoff and path.stat().st_mtime < cutoff):
                continue   # todo el archivo es anterior al rango
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        last_matched = False   # si la última línea de cabecera pasó los filtros
        for line in text.splitlines():
            parts = line.split(" │ ", 4)
            if len(parts) == 5 and parts[0][:2] == "20":
                last_matched = False
                ts = _parse_ts(parts[0])
                entry = {"ts": ts, "level": parts[1].strip(),
                         "logger": parts[2].strip(), "message": parts[4],
                         "detail": ""}
                if ts < cutoff:
                    continue
                loggers.add(entry["logger"])
                if _LEVEL_ORDER.get(entry["level"], 20) < min_lv:
                    continue
                if logger_name and entry["logger"] != logger_name:
                    continue
                if q and q not in (entry["message"] + " " + entry["logger"]).lower():
                    continue
                matched.append(entry)
                last_matched = True
            elif last_matched and line.strip():
                # continuación (traceback): pertenece a la entrada anterior
                matched[-1]["detail"] += line + "\n"
    return {"entries": list(reversed(matched[-limit:])),
            "total": len(matched), "loggers": sorted(loggers)}
