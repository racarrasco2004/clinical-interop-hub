"""Indicadores del dashboard clínico, calculados al vuelo desde los recursos FHIR.

Entrada: los recursos guardados (Patient, Encounter, Observation, Condition, ImagingStudy).
Salida: un diccionario JSON con KPIs, series diarias, histogramas y alertas.

Definiciones (también están en la página del dashboard):
- Atenciones: Encounter con inicio dentro del período, de clase urgencia (EMER),
  ambulatoria (AMB) u hospitalización (IMP).
- Ocupación de camas: pacientes hospitalizados a las 12:00 de cada día / camas totales.
- Estadía: días entre inicio y fin de las hospitalizaciones terminadas en el período.
- Espera en urgencias: minutos entre "arrived" e "in-progress" en Encounter.statusHistory.
- Reingreso a 30 días: altas del período con otra hospitalización del mismo paciente
  dentro de los 30 días siguientes.
"""
import math
from collections import Counter
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, Optional

SYNTH_SYSTEM = "urn:hub:origin"

CLASSES = ("EMER", "AMB", "IMP")
CLASS_LABEL = {"EMER": "Urgencia", "AMB": "Ambulatorio", "IMP": "Hospitalización"}
IGNORED_STATUS = {"cancelled", "entered-in-error"}
CRITICAL = {"HH", "LL", "AA"}
ALERT_DAYS = 7

MODALITY_LABEL = {
    "CR": "Radiografía", "DX": "Radiografía digital", "CT": "TC", "MR": "Resonancia",
    "US": "Ecografía", "MG": "Mamografía", "NM": "Medicina nuclear", "PT": "PET",
}
WAIT_BUCKETS = [("<15", 15), ("15–30", 30), ("30–60", 60), ("60–120", 120), (">120", math.inf)]
STAY_BUCKETS = [("≤1", 1), ("2–3", 3), ("4–7", 7), ("8–14", 14), (">14", math.inf)]
AGE_BANDS = [("0–17", 0, 17), ("18–39", 18, 39), ("40–59", 40, 59), ("60–79", 60, 79), ("80+", 80, 200)]
WEEKDAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


# ---------- Utilidades ----------
def is_synthetic(resource: dict) -> bool:
    """True si el recurso fue creado por el generador de datos de demostración."""
    tags = (resource.get("meta") or {}).get("tag") or []
    return any(isinstance(t, dict) and t.get("system") == SYNTH_SYSTEM for t in tags)


def parse_dt(value: Any, tz: tzinfo) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def ref_id(ref: Any) -> Optional[str]:
    text = ref.get("reference") if isinstance(ref, dict) else None
    return text.split("/", 1)[1] if isinstance(text, str) and "/" in text else None


def percentile(values: list[float], q: float) -> Optional[float]:
    """Percentil por rango más cercano."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def median(values: list[float]) -> Optional[float]:
    return percentile(values, 0.5)


def mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def delta_pct(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def delta_pp(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    return None if cur is None or prev is None else round(cur - prev, 1)


def rnd(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


def bucketize(values: list[float], buckets: list, inclusive: bool) -> list[dict]:
    counts = [0] * len(buckets)
    for v in values:
        for i, (_, edge) in enumerate(buckets):
            if v <= edge if inclusive else v < edge:
                counts[i] += 1
                break
    return [{"label": label, "count": counts[i]} for i, (label, _) in enumerate(buckets)]


def display_name(patient: Optional[dict]) -> str:
    name = ((patient or {}).get("name") or [{}])[0]
    given = " ".join(name.get("given", []))
    return f"{name.get('family', 'Sin nombre')}, {given}".strip(", ")


def code_label(code: dict) -> tuple[str, str]:
    coding = next((c for c in (code or {}).get("coding", []) if c.get("code")), {})
    key = coding.get("code") or (code or {}).get("text") or "?"
    label = coding.get("display") or (code or {}).get("text") or key
    return key, label


# ---------- Normalización ----------
def _encounters(items: list[dict], tz: tzinfo) -> list[dict]:
    out = []
    for e in items:
        cls = (e.get("class") or {}).get("code")
        if cls not in CLASSES or e.get("status") in IGNORED_STATUS:
            continue
        period = e.get("period") or {}
        start = parse_dt(period.get("start"), tz)
        if start is None:
            continue
        history = {}
        for h in e.get("statusHistory", []):
            if isinstance(h, dict):
                history[h.get("status")] = parse_dt((h.get("period") or {}).get("start"), tz)
        wait = None
        if history.get("arrived") and history.get("in-progress"):
            wait = (history["in-progress"] - history["arrived"]).total_seconds() / 60
        out.append(
            {
                "id": e.get("id"), "patient": ref_id(e.get("subject")), "cls": cls,
                "start": start, "end": parse_dt(period.get("end"), tz), "wait": wait,
            }
        )
    return out


# ---------- Cálculo principal ----------
def compute(res: dict[str, list[dict]], now: datetime, days: int, capacity: int) -> dict:
    tz = now.tzinfo
    today = now.date()
    first_day = today - timedelta(days=days - 1)
    w0 = datetime.combine(first_day, time.min, tzinfo=tz)
    p0 = w0 - timedelta(days=days)  # período anterior, de igual largo

    patients = {p["id"]: p for p in res.get("Patient", []) if p.get("id")}
    encs = _encounters(res.get("Encounter", []), tz)
    enc_by_id = {e["id"]: e for e in encs}
    imp = [e for e in encs if e["cls"] == "IMP"]
    imp_by_patient: dict[Optional[str], list[dict]] = {}
    for e in imp:
        imp_by_patient.setdefault(e["patient"], []).append(e)

    def in_range(dt: Optional[datetime], lo: datetime, hi: datetime) -> bool:
        return dt is not None and lo <= dt < hi

    def census(t: datetime) -> int:
        return sum(1 for e in imp if e["start"] <= t and (e["end"] is None or e["end"] > t))

    def occupancy_series(start_day, n_days: int) -> list[float]:
        series = []
        for i in range(n_days):
            day = start_day + timedelta(days=i)
            t = now if day == today else datetime.combine(day, time(12, 0), tzinfo=tz)
            series.append(round(census(t) / capacity * 100, 1) if t <= now else 0.0)
        return series

    def metrics(lo: datetime, hi: datetime, start_day) -> dict:
        window = [e for e in encs if in_range(e["start"], lo, hi)]
        stays = [(e["end"] - e["start"]).total_seconds() / 86400 for e in imp if e["end"] and in_range(e["end"], lo, hi)]
        waits = [e["wait"] for e in window if e["cls"] == "EMER" and e["wait"] is not None]
        discharged = [e for e in imp if e["end"] and in_range(e["end"], lo, hi)]
        readmitted = sum(
            1
            for e in discharged
            if any(o is not e and e["end"] < o["start"] <= e["end"] + timedelta(days=30) for o in imp_by_patient.get(e["patient"], []))
        )
        occ = occupancy_series(start_day, days)
        return {
            "count": len(window),
            "stay": mean(stays),
            "wait_median": median(waits),
            "wait_p90": percentile(waits, 0.9),
            "wait_within60": (sum(1 for w in waits if w <= 60) / len(waits) * 100) if waits else None,
            "readmission": (readmitted / len(discharged) * 100) if discharged else None,
            "occupancy_avg": mean(occ),
            "occupancy": occ,
        }

    cur = metrics(w0, now + timedelta(seconds=1), first_day)
    # Solo se compara con el período anterior si los datos llegan hasta ahí (si no, saldría un % engañoso).
    earliest = min((e["start"] for e in encs), default=None)
    prev_ok = earliest is not None and earliest <= p0 + timedelta(days=1)
    prev = metrics(p0, w0, first_day - timedelta(days=days)) if prev_ok else {}

    def pv(key: str) -> Optional[float]:
        return prev.get(key)

    window = [e for e in encs if in_range(e["start"], w0, now + timedelta(seconds=1))]
    dates = [(first_day + timedelta(days=i)) for i in range(days)]
    per_day = {c: [0] * days for c in CLASSES}
    for e in window:
        per_day[e["cls"]][(e["start"].date() - first_day).days] += 1

    waits = [e["wait"] for e in window if e["cls"] == "EMER" and e["wait"] is not None]
    stays = [(e["end"] - e["start"]).total_seconds() / 86400 for e in imp if e["end"] and in_range(e["end"], w0, now + timedelta(seconds=1))]

    heat = [[0] * 24 for _ in range(7)]
    for e in window:
        if e["cls"] == "EMER":
            heat[e["start"].weekday()][e["start"].hour] += 1

    # Diagnósticos del período
    dx = Counter()
    dx_label: dict[str, str] = {}
    for c in res.get("Condition", []):
        enc = enc_by_id.get(ref_id(c.get("encounter")))
        when = enc["start"] if enc else parse_dt(c.get("recordedDate"), tz)
        if when is None or not (w0 <= when <= now):
            continue
        key, label = code_label(c.get("code"))
        dx[key] += 1
        dx_label[key] = label
    total_dx = sum(dx.values())
    top_dx = [
        {"code": k, "label": dx_label[k], "count": n, "pct": round(n / total_dx * 100, 1)}
        for k, n in dx.most_common(8)
    ]

    # Edad de los pacientes atendidos
    ages = [0] * len(AGE_BANDS)
    seen = {e["patient"] for e in window if e["patient"] in patients}
    for pid in seen:
        born = parse_dt(patients[pid].get("birthDate"), tz)
        if born is None:
            continue
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        for i, (_, lo_age, hi_age) in enumerate(AGE_BANDS):
            if lo_age <= age <= hi_age:
                ages[i] += 1
                break

    # Estudios de imagen
    modalities = Counter()
    for s in res.get("ImagingStudy", []):
        if in_range(parse_dt(s.get("started"), tz), w0, now + timedelta(seconds=1)):
            code = next((m.get("code") for m in s.get("modality", []) if m.get("code")), "OT")
            modalities[code] += 1
    imaging = [{"modality": m, "label": MODALITY_LABEL.get(m, m), "count": n} for m, n in modalities.most_common()]

    # Observaciones críticas
    alerts, critical_in_window = [], 0
    alert_from = now - timedelta(days=ALERT_DAYS)
    for o in res.get("Observation", []):
        codes = {c.get("code") for i in o.get("interpretation", []) for c in i.get("coding", [])}
        level = next((c for c in ("HH", "LL", "AA") if c in codes), None)
        when = parse_dt(o.get("effectiveDateTime"), tz)
        if level is None or when is None or when > now:
            continue
        if w0 <= when:
            critical_in_window += 1
        if when >= alert_from:
            value = o.get("valueQuantity") or {}
            value_text = (f"{value['value']:g} {value.get('unit', '')}".strip() if "value" in value else str(o.get("valueString", "")))
            pid = ref_id(o.get("subject"))
            alerts.append(
                {
                    "when": when.isoformat(timespec="minutes"), "patient_id": pid,
                    "patient": display_name(patients.get(pid)), "test": code_label(o.get("code"))[1],
                    "value": value_text, "level": level,
                }
            )
    alerts.sort(key=lambda a: a["when"], reverse=True)

    census_now = census(now)
    synthetic = sum(1 for items in res.values() for r in items if is_synthetic(r))

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "days": days,
        "capacity": capacity,
        "range": {"from": first_day.isoformat(), "to": today.isoformat()},
        "synthetic_resources": synthetic,
        "prev_available": prev_ok,
        "totals": {
            "patients": len(patients),
            "encounters": len(res.get("Encounter", [])),
            "observations": len(res.get("Observation", [])),
            "conditions": len(res.get("Condition", [])),
            "imaging_studies": len(res.get("ImagingStudy", [])),
        },
        "kpis": {
            "encounters": {"value": cur["count"], "prev": pv("count"), "delta": delta_pct(cur["count"], pv("count"))},
            "occupancy_now": {"value": round(census_now / capacity * 100, 1), "census": census_now, "capacity": capacity},
            "occupancy_avg": {
                "value": rnd(cur["occupancy_avg"]), "prev": rnd(pv("occupancy_avg")),
                "delta_pp": delta_pp(cur["occupancy_avg"], pv("occupancy_avg")),
            },
            "avg_stay": {"value": rnd(cur["stay"]), "prev": rnd(pv("stay")), "delta": delta_pct(cur["stay"], pv("stay"))},
            "er_wait": {
                "median": rnd(cur["wait_median"], 0), "prev": rnd(pv("wait_median"), 0),
                "delta": delta_pct(cur["wait_median"], pv("wait_median")),
                "p90": rnd(cur["wait_p90"], 0), "within60": rnd(cur["wait_within60"]),
            },
            "readmission": {
                "value": rnd(cur["readmission"]), "prev": rnd(pv("readmission")),
                "delta_pp": delta_pp(cur["readmission"], pv("readmission")),
            },
            "critical": {"value": critical_in_window},
        },
        "daily": {
            "dates": [d.isoformat() for d in dates],
            "EMER": per_day["EMER"], "AMB": per_day["AMB"], "IMP": per_day["IMP"],
            "occupancy": cur["occupancy"],
        },
        "er_wait_hist": bucketize(waits, WAIT_BUCKETS, inclusive=False),
        "stay_hist": bucketize(stays, STAY_BUCKETS, inclusive=True),
        "top_diagnoses": top_dx,
        "age_bands": [{"label": label, "count": ages[i]} for i, (label, _, _) in enumerate(AGE_BANDS)],
        "imaging": imaging,
        "heatmap": {"weekdays": WEEKDAYS, "matrix": heat, "max": max(max(row) for row in heat)},
        "alerts": alerts[:8],
    }
