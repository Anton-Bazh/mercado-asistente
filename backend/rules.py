"""Motor de reglas de impresión por horario (semanal).

Cada día de la semana se parte en tramos [inicio, fin) con un modo. En cualquier
momento hay exactamente un tramo activo (sin solapes ni ambigüedad nocturna).

Modos:
  - ahorro : imprime solo hojas completas (n-up, ahorra papel).
  - forzar : imprime todo lo pendiente ya, aunque la hoja no se llene (vaciado).
  - pausa  : no imprime; acumula (domingo, corte).

Las reglas se guardan como JSON en storage (una sola clave).
"""
from __future__ import annotations

import json
from datetime import datetime

import storage

MODES = ("ahorro", "forzar", "pausa")
_DAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]  # weekday(): Lun=0…Dom=6


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def default_rules() -> dict:
    """Preset con la operación real del negocio (editable)."""
    laboral = [
        {"start": "00:00", "end": "09:30", "mode": "ahorro", "label": "Madrugada / turno"},
        {"start": "09:30", "end": "09:59", "mode": "forzar", "label": "Vaciado"},
        {"start": "09:59", "end": "10:00", "mode": "pausa", "label": "Corte 10:00"},
        {"start": "10:00", "end": "24:00", "mode": "ahorro", "label": "Supervisado / adelanto"},
    ]
    lunes = [
        {"start": "00:00", "end": "09:59", "mode": "forzar", "label": "Libera acumulado del domingo"},
        {"start": "09:59", "end": "10:00", "mode": "pausa", "label": "Corte 10:00"},
        {"start": "10:00", "end": "24:00", "mode": "ahorro", "label": "Supervisado / adelanto"},
    ]
    return {
        "days": {
            "0": lunes,                                   # Lunes
            "1": laboral, "2": laboral, "3": laboral, "4": laboral,  # Mar–Vie
            "5": [{"start": "00:00", "end": "24:00", "mode": "forzar", "label": "Sábado: adelanto"}],
            "6": [{"start": "00:00", "end": "24:00", "mode": "pausa", "label": "Domingo: acumula"}],
        }
    }


def get_rules() -> dict:
    raw = storage.get_value(storage.AUTO_RULES)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "days" in data:
                return data
        except (ValueError, TypeError):
            pass
    return default_rules()


def validate(rules: dict) -> dict:
    """Valida y normaliza las reglas (lanza ValueError si algo está mal)."""
    if not isinstance(rules, dict) or "days" not in rules:
        raise ValueError("Formato de reglas inválido.")
    out: dict = {"days": {}}
    for d in range(7):
        segs_in = rules["days"].get(str(d), [])
        segs = []
        for s in segs_in:
            try:
                a, b = _hm(s["start"]), _hm(s["end"])
            except (KeyError, ValueError, TypeError):
                raise ValueError(f"Horario inválido en {_DAYS[d]}.")
            if s.get("mode") not in MODES:
                raise ValueError(f"Modo inválido en {_DAYS[d]}.")
            if not (0 <= a < b <= 1440):
                raise ValueError(f"Rango inválido en {_DAYS[d]} ({s['start']}–{s['end']}).")
            segs.append({"start": s["start"], "end": s["end"],
                         "mode": s["mode"], "label": (s.get("label") or "")[:60]})
        segs.sort(key=lambda x: _hm(x["start"]))
        # sin solapes
        prev_end = 0
        for s in segs:
            if _hm(s["start"]) < prev_end:
                raise ValueError(f"Tramos que se solapan en {_DAYS[d]}.")
            prev_end = _hm(s["end"])
        out["days"][str(d)] = segs
    return out


def set_rules(rules: dict) -> dict:
    clean = validate(rules)
    storage.set_value(storage.AUTO_RULES, json.dumps(clean, ensure_ascii=False))
    return clean


def current_mode(dt: datetime | None = None) -> tuple[str, str]:
    """Modo activo ahora → (mode, label). Si no hay tramo, 'pausa'."""
    dt = dt or datetime.now()
    now_min = dt.hour * 60 + dt.minute
    for s in get_rules()["days"].get(str(dt.weekday()), []):
        if _hm(s["start"]) <= now_min < _hm(s["end"]):
            return s["mode"], s.get("label", "")
    return "pausa", "Sin regla (en pausa)"


def preview_week() -> dict:
    """Reglas normalizadas + etiquetas de día para pintar la rejilla."""
    return {"days": get_rules()["days"], "day_names": _DAYS, "modes": list(MODES)}
