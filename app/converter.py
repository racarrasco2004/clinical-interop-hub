"""Conversor HL7 v2 -> FHIR R4.

Toma un mensaje HL7 v2 (ADT u ORU) y devuelve un Bundle FHIR de tipo
'transaction' con Patient, Encounter, Observation y Condition.

Uso por consola:
    python -m app.converter samples/hl7/oru_r01.hl7
    python -m app.converter samples/hl7/oru_r01.hl7 --post http://127.0.0.1:8000
"""
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from typing import Optional

# Zona horaria que se usa cuando el mensaje HL7 trae hora sin offset.
# FHIR exige offset en los dateTime con hora.
DEFAULT_TZ = "-03:00"

SUPPORTED_MESSAGES = ("ADT", "ORU")


class HL7Error(ValueError):
    """Mensaje HL7 inválido o no soportado."""


# ---------- Tablas de equivalencia ----------
GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown", "A": "other", "N": "unknown"}

ENCOUNTER_CLASS = {
    "I": ("IMP", "inpatient encounter"),
    "O": ("AMB", "ambulatory"),
    "E": ("EMER", "emergency"),
    "P": ("PRENC", "pre-admission"),
}
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
PATIENT_CLASS_V2 = "http://terminology.hl7.org/CodeSystem/v2-0004"

OBX_STATUS = {
    "F": "final",
    "P": "preliminary",
    "C": "corrected",
    "R": "registered",
    "W": "entered-in-error",
    "X": "cancelled",
}

CODE_SYSTEMS = {
    "LN": "http://loinc.org",
    "LOINC": "http://loinc.org",
    "SCT": "http://snomed.info/sct",
    "SNM": "http://snomed.info/sct",
    "I10": "http://hl7.org/fhir/sid/icd-10",
    "ICD10": "http://hl7.org/fhir/sid/icd-10",
    "I9": "http://hl7.org/fhir/sid/icd-9-cm",
    "I9C": "http://hl7.org/fhir/sid/icd-9-cm",
}

INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
INTERPRETATION_CODES = {"N", "H", "L", "HH", "LL", "A", "AA"}


# ---------- Lectura del mensaje ----------
class Segment:
    """Un segmento HL7. get(n) devuelve el campo n (numeración HL7, desde 1)."""

    def __init__(self, fields: list[str]):
        self.fields = fields
        self.name = fields[0]

    def get(self, n: int) -> str:
        return self.fields[n] if n < len(self.fields) else ""


def parse_message(text: str) -> list[Segment]:
    text = text.strip("\x0b\x1c﻿ \r\n\t")  # quita marcos MLLP y espacios
    if not text.startswith("MSH|^~\\&"):
        raise HL7Error("El mensaje debe comenzar con 'MSH|^~\\&'")

    segments = []
    for line in text.replace("\r\n", "\r").replace("\n", "\r").split("\r"):
        line = line.strip()
        if len(line) < 3:
            continue
        fields = line.split("|")
        if fields[0] == "MSH":
            # En MSH el separador de campos es MSH-1: se inserta para que
            # los índices coincidan con la numeración HL7.
            fields = ["MSH", "|"] + fields[1:]
        segments.append(Segment(fields))
    return segments


def unescape(value: str) -> str:
    return (
        value.replace("\\F\\", "|")
        .replace("\\S\\", "^")
        .replace("\\T\\", "&")
        .replace("\\R\\", "~")
        .replace("\\E\\", "\\")
    )


def comp(value: str, n: int) -> str:
    """Componente n (desde 1) de un campo separado por '^'."""
    parts = value.split("^")
    if n > len(parts):
        return ""
    return unescape(parts[n - 1].split("&")[0]).strip()


def reps(value: str) -> list[str]:
    """Repeticiones de un campo (separadas por '~'), sin las vacías."""
    return [r for r in value.split("~") if r]


def first(segments: list[Segment], name: str) -> Optional[Segment]:
    return next((s for s in segments if s.name == name), None)


def hl7_datetime(value: str, tz: Optional[str] = None) -> Optional[str]:
    """Fecha HL7 (AAAAMMDD[HHMM[SS]][+ZZZZ]) -> fecha o dateTime FHIR."""
    m = re.match(r"^(\d{4,14})(?:\.\d+)?([+-]\d{4})?$", value.strip())
    if not m:
        return None
    digits, offset = m.groups()
    if len(digits) not in (4, 6, 8, 10, 12, 14):
        return None
    year = digits[:4]
    if len(digits) == 4:
        return year
    if len(digits) == 6:
        return f"{year}-{digits[4:6]}"
    if len(digits) == 8:
        return f"{year}-{digits[4:6]}-{digits[6:8]}"
    hh = digits[8:10]
    mm = digits[10:12] if len(digits) >= 12 else "00"
    ss = digits[12:14] if len(digits) == 14 else "00"
    zone = f"{offset[:3]}:{offset[3:]}" if offset else (tz or DEFAULT_TZ)
    return f"{year}-{digits[4:6]}-{digits[6:8]}T{hh}:{mm}:{ss}{zone}"


def to_number(text: str):
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() and "." not in text else number


def codeable(value: str) -> dict:
    """Campo CWE/CE 'codigo^texto^sistema[^alt^alt texto^alt sistema]' -> CodeableConcept."""
    concept: dict = {}
    codings = []
    for start in (1, 4):
        code, text, system = comp(value, start), comp(value, start + 1), comp(value, start + 2)
        if not code:
            continue
        coding = {"code": code}
        if system.upper() in CODE_SYSTEMS:
            coding["system"] = CODE_SYSTEMS[system.upper()]
        if text:
            coding["display"] = text
        codings.append(coding)
    if codings:
        concept["coding"] = codings
    text = comp(value, 2)
    if text:
        concept["text"] = text
    return concept


def make_entry(resource: dict) -> tuple[str, dict]:
    full_url = f"urn:uuid:{uuid.uuid4()}"
    entry = {
        "fullUrl": full_url,
        "resource": resource,
        "request": {"method": "POST", "url": resource["resourceType"]},
    }
    return full_url, entry


# ---------- Segmentos -> recursos ----------
def patient_from_pid(pid: Segment) -> dict:
    patient: dict = {"resourceType": "Patient"}

    identifiers = []
    for rep in reps(pid.get(3)):
        value = comp(rep, 1)
        if not value:
            continue
        ident = {"value": value}
        authority = comp(rep, 4)
        if authority:
            ident["system"] = f"urn:hl7v2:authority:{authority}"
        id_type = comp(rep, 5)
        if id_type:
            ident["type"] = {
                "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": id_type}]
            }
        identifiers.append(ident)
    if identifiers:
        patient["identifier"] = identifiers

    names = []
    for rep in reps(pid.get(5)):
        family = comp(rep, 1)
        given = [g for g in (comp(rep, 2), comp(rep, 3)) if g]
        if family or given:
            name: dict = {}
            if family:
                name["family"] = family
            if given:
                name["given"] = given
            names.append(name)
    if not names:
        raise HL7Error("PID-5 (nombre del paciente) es obligatorio")
    patient["name"] = names

    birth = hl7_datetime(pid.get(7))
    if birth:
        patient["birthDate"] = birth[:10]

    sex = pid.get(8).strip().upper()
    if sex:
        patient["gender"] = GENDER.get(sex, "unknown")

    addresses = []
    for rep in reps(pid.get(11)):
        address: dict = {}
        lines = [x for x in (comp(rep, 1), comp(rep, 2)) if x]
        if lines:
            address["line"] = lines
        for key, n in (("city", 3), ("state", 4), ("postalCode", 5), ("country", 6)):
            if comp(rep, n):
                address[key] = comp(rep, n)
        if address:
            addresses.append(address)
    if addresses:
        patient["address"] = addresses

    telecom = [{"system": "phone", "value": comp(rep, 1)} for rep in reps(pid.get(13)) if comp(rep, 1)]
    if telecom:
        patient["telecom"] = telecom
    return patient


def encounter_from_pv1(pv1: Segment, trigger: str, subject: dict) -> dict:
    cls = pv1.get(2).strip().upper()
    if not cls:
        raise HL7Error("PV1-2 (clase de paciente) es obligatorio")
    if cls in ENCOUNTER_CLASS:
        code, display = ENCOUNTER_CLASS[cls]
        class_coding = {"system": ACT_CODE, "code": code, "display": display}
    else:  # clase local: se conserva el código original de HL7 v2
        class_coding = {"system": PATIENT_CLASS_V2, "code": cls}

    start = hl7_datetime(pv1.get(44))
    end = hl7_datetime(pv1.get(45))
    encounter: dict = {
        "resourceType": "Encounter",
        "status": "finished" if (end or trigger == "A03") else "in-progress",
        "class": class_coding,
        "subject": subject,
    }
    visit = comp(pv1.get(19), 1)
    if visit:
        encounter["identifier"] = [{"value": visit}]
    period = {k: v for k, v in (("start", start), ("end", end)) if v}
    if period:
        encounter["period"] = period
    place = "-".join(x for x in (comp(pv1.get(3), 1), comp(pv1.get(3), 2), comp(pv1.get(3), 3)) if x)
    if place:
        encounter["location"] = [{"location": {"display": place}}]
    return encounter


def observation_from_obx(
    obx: Segment, subject: dict, encounter_ref: Optional[dict], default_effective: Optional[str]
) -> dict:
    code = codeable(obx.get(3))
    if not code:
        raise HL7Error("OBX-3 (identificador de la observación) es obligatorio")

    obs: dict = {
        "resourceType": "Observation",
        "status": OBX_STATUS.get(obx.get(11).strip().upper(), "unknown"),
        "code": code,
        "subject": subject,
    }
    if encounter_ref:
        obs["encounter"] = encounter_ref

    effective = hl7_datetime(obx.get(14)) or default_effective
    if effective:
        obs["effectiveDateTime"] = effective

    value_type = obx.get(2).strip().upper()
    raw = obx.get(5)
    if raw:
        if value_type == "NM" and to_number(raw.strip()) is not None:
            quantity = {"value": to_number(raw.strip())}
            unit = comp(obx.get(6), 1)
            if unit:
                quantity["unit"] = unit
            obs["valueQuantity"] = quantity
        elif value_type in ("CE", "CWE", "CNE"):
            obs["valueCodeableConcept"] = codeable(raw)
        else:
            obs["valueString"] = unescape(raw)

    interpretation = []
    for rep in reps(obx.get(8)):
        flag = comp(rep, 1).upper()
        if flag in INTERPRETATION_CODES:
            interpretation.append({"coding": [{"system": INTERPRETATION_SYSTEM, "code": flag}]})
    if interpretation:
        obs["interpretation"] = interpretation

    ref_range = unescape(obx.get(7)).strip()
    if ref_range:
        obs["referenceRange"] = [{"text": ref_range}]
    return obs


def condition_from_dg1(dg1: Segment, subject: dict, encounter_ref: Optional[dict]) -> dict:
    code = codeable(dg1.get(3))
    if not code:  # versiones antiguas de HL7 usan DG1-4 (descripción)
        description = unescape(dg1.get(4)).strip()
        if not description:
            raise HL7Error("DG1-3 (código del diagnóstico) es obligatorio")
        code = {"text": description}

    condition: dict = {
        "resourceType": "Condition",
        "clinicalStatus": {
            "coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}
            ]
        },
        "code": code,
        "subject": subject,
    }
    if encounter_ref:
        condition["encounter"] = encounter_ref
    recorded = hl7_datetime(dg1.get(5))
    if recorded:
        condition["recordedDate"] = recorded
    return condition


# ---------- Conversión completa ----------
def hl7v2_to_fhir(text: str) -> dict:
    """Convierte un mensaje HL7 v2 (ADT/ORU) en un Bundle FHIR 'transaction'."""
    segments = parse_message(text)
    msh = segments[0]
    message_type = comp(msh.get(9), 1)
    trigger = comp(msh.get(9), 2)
    if message_type not in SUPPORTED_MESSAGES:
        raise HL7Error(f"Tipo de mensaje '{message_type}' no soportado (solo ADT y ORU)")

    pid = first(segments, "PID")
    if pid is None:
        raise HL7Error("El mensaje no tiene segmento PID (paciente)")

    entries = []
    patient_url, entry = make_entry(patient_from_pid(pid))
    entries.append(entry)
    subject = {"reference": patient_url}

    encounter_ref = None
    pv1 = first(segments, "PV1")
    if pv1 is not None:
        encounter_url, entry = make_entry(encounter_from_pv1(pv1, trigger, subject))
        entries.append(entry)
        encounter_ref = {"reference": encounter_url}

    obr = first(segments, "OBR")
    default_effective = hl7_datetime(obr.get(7)) if obr is not None else None

    for seg in segments:
        if seg.name == "OBX":
            resource = observation_from_obx(seg, subject, encounter_ref, default_effective)
        elif seg.name == "DG1":
            resource = condition_from_dg1(seg, subject, encounter_ref)
        else:
            continue
        entries.append(make_entry(resource)[1])

    bundle: dict = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
    control_id = msh.get(10).strip()
    if control_id:
        bundle["identifier"] = {"system": "urn:hl7v2:message-control-id", "value": control_id}
    stamp = hl7_datetime(msh.get(7))
    if stamp and "T" in stamp:
        bundle["timestamp"] = stamp
    return bundle


# ---------- Línea de comandos ----------
def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Uso: python -m app.converter archivo.hl7 [--post http://127.0.0.1:8000]", file=sys.stderr)
        return 2

    with open(argv[1], encoding="utf-8") as f:
        text = f.read()
    try:
        bundle = hl7v2_to_fhir(text)
    except HL7Error as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if "--post" not in argv:
        print(json.dumps(bundle, indent=2, ensure_ascii=False))
        return 0

    pos = argv.index("--post")
    if pos + 1 >= len(argv):
        print("Falta la URL después de --post", file=sys.stderr)
        return 2
    request = urllib.request.Request(
        argv[pos + 1].rstrip("/") + "/",
        data=json.dumps(bundle).encode("utf-8"),
        headers={"Content-Type": "application/fhir+json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            print(f"Servidor respondió {response.status}")
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(f"El servidor rechazó el Bundle ({error.code}): {error.read().decode('utf-8')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"No se pudo conectar con el servidor: {error.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
