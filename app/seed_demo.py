"""Generador de datos de demostración para el dashboard (TODO es ficticio).

Crea ~4 meses de actividad de un hospital imaginario: pacientes, atenciones (urgencia,
ambulatorio y hospitalización), diagnósticos CIE-10, signos vitales y estudios de imagen.
Los recursos quedan marcados con la etiqueta meta.tag "urn:hub:origin" = "synthetic",
así se pueden borrar sin tocar los datos reales (HL7 y DICOM).

Uso:  python -m app.seed_demo            (escribe en fhir.db, o en $FHIR_DB)
"""
import json
import math
import random
import sqlite3
import sys
import uuid
from bisect import bisect_left
from datetime import datetime, time, timedelta
from itertools import accumulate
from typing import Optional

from .dashboard import SYNTH_SYSTEM

WARMUP_DAYS = 30  # se simula antes del período visible para que la ocupación sea realista
LOINC = "http://loinc.org"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
DCM = "http://dicom.nema.org/resources/ontology/DCM"

FEMALE = ["María", "Camila", "Valentina", "Sofía", "Catalina", "Fernanda", "Javiera", "Francisca", "Constanza", "Antonia", "Isidora", "Daniela", "Carolina", "Paula", "Macarena"]
MALE = ["José", "Juan", "Luis", "Carlos", "Matías", "Benjamín", "Vicente", "Felipe", "Sebastián", "Diego", "Cristóbal", "Ignacio", "Rodrigo", "Pablo", "Andrés"]
SURNAMES = ["González", "Muñoz", "Rojas", "Díaz", "Pérez", "Soto", "Contreras", "Silva", "Martínez", "Sepúlveda", "Morales", "Rodríguez", "López", "Fuentes", "Hernández", "Torres", "Araya", "Flores", "Espinoza", "Valenzuela"]

# (código CIE-10, descripción, peso)
DX_ER = [("R07.4", "Dolor torácico, no especificado", 14), ("R10.4", "Dolor abdominal, otro y no especificado", 14), ("J06.9", "Infección aguda de las vías respiratorias superiores", 16), ("A09", "Gastroenteritis y colitis de origen infeccioso", 10), ("S06.0", "Conmoción cerebral", 4), ("S52.5", "Fractura del extremo distal del radio", 6), ("N39.0", "Infección de vías urinarias", 8), ("J45.9", "Asma, no especificada", 7), ("I10", "Hipertensión esencial", 6), ("K29.7", "Gastritis, no especificada", 6)]
DX_IMP = [("J18.9", "Neumonía, no especificada", 18), ("I21.9", "Infarto agudo del miocardio", 8), ("I50.9", "Insuficiencia cardíaca", 11), ("N39.0", "Infección de vías urinarias", 10), ("K35.8", "Apendicitis aguda", 9), ("E11.9", "Diabetes mellitus tipo 2", 9), ("S72.0", "Fractura del cuello del fémur", 7), ("J44.1", "EPOC con exacerbación aguda", 9), ("I63.9", "Infarto cerebral", 6), ("K80.2", "Colelitiasis", 7)]
DX_AMB = [("I10", "Hipertensión esencial", 30), ("E11.9", "Diabetes mellitus tipo 2", 24), ("J45.9", "Asma, no especificada", 12), ("M54.5", "Lumbago", 18), ("E78.5", "Hiperlipidemia", 10), ("F41.9", "Trastorno de ansiedad", 6)]

VITALS = {
    "hr": ("8867-4", "Frecuencia cardiaca", "/min"),
    "spo2": ("59408-5", "Saturación de oxígeno", "%"),
    "temp": ("8310-5", "Temperatura corporal", "Cel"),
    "sbp": ("8480-6", "Presión arterial sistólica", "mm[Hg]"),
}
ER_HOUR_W = [1, 1, 1, 1, 1, 1.5, 2, 3, 4, 5, 6, 6, 5, 4.5, 4.5, 4.5, 5, 6, 7, 7, 6, 4, 3, 2]
IMP_HOUR_W = [1, 1, 1, 1, 1, 1, 2, 4, 6, 7, 7, 6, 5, 5, 5, 5, 5, 5, 4, 3, 2, 2, 1, 1]
AMB_HOURS = list(range(8, 18))


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def pick(rng: random.Random, options: list[tuple], idx: int = 2):
    return rng.choices(options, weights=[o[idx] for o in options])[0]


class Seeder:
    def __init__(self, now: datetime, patients: int, days: int, seed: int):
        self.now, self.tz = now, now.tzinfo
        self.today = now.date()
        self.days = days
        self.rng = random.Random(seed)
        self.rows: list[tuple] = []
        self.counts = {"Patient": 0, "Encounter": 0, "Observation": 0, "Condition": 0, "ImagingStudy": 0}
        self.pool = [self._patient(i) for i in range(patients)]
        self.ages = [p[1] for p in self.pool]
        self.cum = {
            "EMER": list(accumulate(1 + (a > 70) * 0.8 + (a < 5) * 0.5 for a in self.ages)),
            "AMB": list(accumulate(a + 20 for a in self.ages)),
            "IMP": list(accumulate((a + 10) ** 1.8 for a in self.ages)),
        }

    # ---------- Utilidades de inserción ----------
    def _id(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def _add(self, rtype: str, body: dict) -> str:
        body["id"] = body.get("id") or self._id()
        body["meta"] = {
            "versionId": "1",
            "lastUpdated": self.now.isoformat(timespec="seconds"),
            "tag": [{"system": SYNTH_SYSTEM, "code": "synthetic", "display": "Datos de demostración"}],
        }
        self.rows.append((body["id"], rtype, json.dumps(body, ensure_ascii=False), body["meta"]["lastUpdated"]))
        self.counts[rtype] += 1
        return body["id"]

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    # ---------- Pacientes ----------
    def _patient(self, i: int) -> tuple[str, int]:
        rng = self.rng
        age_lo, age_hi = rng.choices([(0, 14), (15, 29), (30, 44), (45, 64), (65, 79), (80, 95)], weights=[12, 14, 20, 26, 18, 10])[0]
        age = rng.randint(age_lo, age_hi)
        gender = rng.choice(["female", "male"])
        born = self.today - timedelta(days=age * 365 + rng.randint(0, 364))
        pid = self._add(
            "Patient",
            {
                "resourceType": "Patient",
                "identifier": [{"system": "urn:hub:synthetic", "value": f"SYN-{i + 1:05d}"}],
                "name": [{"family": f"{rng.choice(SURNAMES)} {rng.choice(SURNAMES)}", "given": [rng.choice(FEMALE if gender == "female" else MALE)]}],
                "gender": gender,
                "birthDate": born.isoformat(),
            },
        )
        return pid, age

    def _patient_for(self, kind: str) -> tuple[str, int]:
        cum = self.cum[kind]
        i = min(bisect_left(cum, self.rng.random() * cum[-1]), len(cum) - 1)
        return self.pool[i]

    # ---------- Recursos clínicos ----------
    def _timestamp(self, day, hour_weights: Optional[list] = None, hours: Optional[list] = None) -> Optional[datetime]:
        rng = self.rng
        hour = rng.choices(range(24), weights=hour_weights)[0] if hour_weights else rng.choice(hours)
        ts = datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)), tzinfo=self.tz)
        return ts if ts <= self.now else None

    def _encounter(self, cls: str, pid: str, start: datetime, end: Optional[datetime], **extra) -> str:
        body = {
            "resourceType": "Encounter",
            "status": "finished" if end and end <= self.now else "in-progress",
            "class": {"system": ACT_CODE, "code": cls},
            "subject": {"reference": f"Patient/{pid}"},
            "period": {"start": self._iso(start)},
        }
        if end and end <= self.now:
            body["period"]["end"] = self._iso(end)
        body.update(extra)
        return self._add("Encounter", body)

    def _condition(self, eid: str, pid: str, when: datetime, options: list[tuple]) -> None:
        code, text, _ = pick(self.rng, options)
        self._add(
            "Condition",
            {
                "resourceType": "Condition",
                "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
                "code": {"coding": [{"system": ICD10, "code": code, "display": text}]},
                "subject": {"reference": f"Patient/{pid}"},
                "encounter": {"reference": f"Encounter/{eid}"},
                "recordedDate": self._iso(when),
            },
        )

    def _vital(self, kind: str, er: bool) -> tuple[float, str]:
        rng = self.rng
        if kind == "hr":
            value = rng.gauss(90 if er else 80, 14)
            flag = "HH" if value > 130 else "H" if value > 100 else "LL" if value < 40 else "L" if value < 50 else "N"
        elif kind == "spo2":
            value = min(100.0, rng.gauss(88, 4) if rng.random() < 0.06 else rng.gauss(96.5, 2.2))
            flag = "LL" if value < 85 else "L" if value < 92 else "N"
        elif kind == "temp":
            value = rng.gauss(38.6, 0.6) if rng.random() < (0.2 if er else 0.08) else rng.gauss(36.8, 0.4)
            flag = "HH" if value >= 39.5 else "H" if value >= 38 else "L" if value < 35 else "N"
        else:
            value = rng.gauss(125, 18)
            flag = "HH" if value > 180 else "H" if value > 140 else "LL" if value < 70 else "L" if value < 90 else "N"
        return (round(value) if kind != "temp" else round(value, 1)), flag

    def _vitals(self, eid: str, pid: str, when: datetime, kinds: list[str], er: bool) -> None:
        for kind in kinds:
            if when > self.now:
                continue
            value, flag = self._vital(kind, er)
            code, display, unit = VITALS[kind]
            self._add(
                "Observation",
                {
                    "resourceType": "Observation",
                    "status": "final",
                    "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
                    "code": {"coding": [{"system": LOINC, "code": code, "display": display}]},
                    "subject": {"reference": f"Patient/{pid}"},
                    "encounter": {"reference": f"Encounter/{eid}"},
                    "effectiveDateTime": self._iso(when + timedelta(minutes=self.rng.randint(0, 6))),
                    "valueQuantity": {"value": value, "unit": unit},
                    "interpretation": [{"coding": [{"system": INTERPRETATION, "code": flag}]}],
                },
            )

    def _imaging(self, pid: str, when: datetime, weights: dict[str, float]) -> None:
        modality = self.rng.choices(list(weights), weights=list(weights.values()))[0]
        started = when + timedelta(minutes=self.rng.randint(20, 120))
        if started > self.now:
            return
        instances = {"CR": self.rng.randint(1, 2), "US": self.rng.randint(10, 40), "CT": self.rng.randint(80, 300), "MR": self.rng.randint(100, 400)}[modality]
        self._add(
            "ImagingStudy",
            {
                "resourceType": "ImagingStudy",
                "status": "available",
                "subject": {"reference": f"Patient/{pid}"},
                "started": self._iso(started),
                "modality": [{"system": DCM, "code": modality}],
                "numberOfSeries": 1,
                "numberOfInstances": instances,
            },
        )

    # ---------- Simulación por día ----------
    def _admission(self, pid: str, age: int, start: datetime) -> None:
        rng = self.rng
        stay = min(30.0, max(0.5, rng.lognormvariate(math.log(2.8), 0.75) * (1 + age / 300)))
        end = start + timedelta(days=stay)
        eid = self._encounter("IMP", pid, start, end)
        self._condition(eid, pid, start, DX_IMP)
        self._vitals(eid, pid, start, ["hr", "spo2", "temp", "sbp"], er=False)
        self._vitals(eid, pid, start + timedelta(days=1), ["hr", "spo2"], er=False)
        if rng.random() < 0.35:
            self._imaging(pid, start, {"CT": 0.4, "CR": 0.3, "MR": 0.15, "US": 0.15})
        if end <= self.now and rng.random() < 0.06:  # reingreso en menos de 30 días
            again = end + timedelta(days=rng.uniform(2, 25))
            if again <= self.now:
                self._admission_no_readmit(pid, age, again)

    def _admission_no_readmit(self, pid: str, age: int, start: datetime) -> None:
        stay = min(20.0, max(0.5, self.rng.lognormvariate(math.log(3.0), 0.7)))
        eid = self._encounter("IMP", pid, start, start + timedelta(days=stay))
        self._condition(eid, pid, start, DX_IMP)
        self._vitals(eid, pid, start, ["hr", "spo2", "temp", "sbp"], er=False)

    def _er_visit(self, start: datetime, recent: float) -> None:
        rng = self.rng
        pid, age = self._patient_for("EMER")
        triage = start + timedelta(minutes=rng.randint(3, 12))
        evening = 1.4 if 18 <= start.hour <= 21 else 1.0
        wait = min(300.0, max(3.0, rng.lognormvariate(math.log(30), 0.6) * evening * (1 + 0.3 * recent)))
        seen = start + timedelta(minutes=wait)
        end = seen + timedelta(minutes=min(720.0, max(30.0, rng.lognormvariate(math.log(140), 0.5))))
        history = [
            {"status": "arrived", "period": {"start": self._iso(start), "end": self._iso(triage)}},
            {"status": "triaged", "period": {"start": self._iso(triage), "end": self._iso(seen)}},
        ]
        if seen <= self.now:
            history.append({"status": "in-progress", "period": {"start": self._iso(seen)}})
        eid = self._encounter("EMER", pid, start, end, statusHistory=history)
        if seen > self.now:  # todavía esperando atención
            self.rows[-1] = self._patch_status(self.rows[-1], "triaged" if triage <= self.now else "arrived")
        self._condition(eid, pid, seen if seen <= self.now else start, DX_ER)
        self._vitals(eid, pid, triage, rng.sample(["hr", "spo2", "temp", "sbp"], rng.randint(2, 4)), er=True)
        if rng.random() < 0.28:
            self._imaging(pid, seen, {"CR": 0.6, "CT": 0.3, "US": 0.1})
        if end <= self.now and rng.random() < 0.14:  # el paciente queda hospitalizado
            self._admission(pid, age, end + timedelta(minutes=rng.randint(10, 90)))

    @staticmethod
    def _patch_status(row: tuple, status: str) -> tuple:
        body = json.loads(row[2])
        body["status"] = status
        return (row[0], row[1], json.dumps(body, ensure_ascii=False), row[3])

    def _amb_visit(self, start: datetime) -> None:
        rng = self.rng
        pid, _ = self._patient_for("AMB")
        eid = self._encounter("AMB", pid, start, start + timedelta(minutes=rng.randint(20, 45)))
        if rng.random() < 0.35:
            self._condition(eid, pid, start, DX_AMB)
        if rng.random() < 0.6:
            self._vitals(eid, pid, start, ["sbp"], er=False)
        if rng.random() < 0.08:
            self._imaging(pid, start, {"CR": 0.4, "US": 0.3, "MR": 0.3})

    def run(self) -> None:
        rng = self.rng
        total_days = self.days + WARMUP_DAYS
        for back in range(total_days, -1, -1):
            day = self.today - timedelta(days=back)
            weekday = day.weekday()
            recent = max(0.0, 1 - back / 21) if back < 21 else 0.0  # sube la demanda las últimas 3 semanas
            er_rate = 14 * (1.1 if weekday == 0 else 1.05 if weekday >= 5 else 1.0) * (1 + 0.30 * recent)
            amb_rate = 18 * (1.0 if weekday < 5 else 0.35 if weekday == 5 else 0.1)
            imp_rate = 7 * (1.0 if weekday < 5 else 0.8) * (1 + 0.2 * recent)
            for _ in range(poisson(rng, er_rate)):
                ts = self._timestamp(day, hour_weights=ER_HOUR_W)
                if ts:
                    self._er_visit(ts, recent)
            for _ in range(poisson(rng, amb_rate)):
                ts = self._timestamp(day, hours=AMB_HOURS)
                if ts:
                    self._amb_visit(ts)
            for _ in range(poisson(rng, imp_rate)):
                ts = self._timestamp(day, hour_weights=IMP_HOUR_W)
                if ts:
                    pid, age = self._patient_for("IMP")
                    self._admission(pid, age, ts)


def clear(conn: sqlite3.Connection) -> int:
    """Borra solo los recursos sintéticos."""
    cur = conn.execute("DELETE FROM resources WHERE body LIKE ?", (f"%{SYNTH_SYSTEM}%",))
    return cur.rowcount


def seed(conn: sqlite3.Connection, now: datetime, patients: int = 3000, days: int = 90, seed_value: int = 2026) -> dict:
    """Genera los datos y los inserta. Devuelve cuántos recursos creó de cada tipo."""
    seeder = Seeder(now, patients, days, seed_value)
    seeder.run()
    conn.executemany("INSERT INTO resources (id, resource_type, body, last_updated) VALUES (?, ?, ?, ?)", seeder.rows)
    return seeder.counts


def main(argv: list[str]) -> int:
    from . import main as app_main  # evita importar FastAPI salvo que se use la consola

    with app_main.get_conn() as conn:
        removed = clear(conn)
        counts = seed(conn, datetime.now().astimezone())
    print(f"Listo. Se borraron {removed} recursos sintéticos anteriores y se crearon: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
