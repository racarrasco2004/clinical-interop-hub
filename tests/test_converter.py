from pathlib import Path

import pytest

from app.converter import HL7Error, hl7_datetime, hl7v2_to_fhir

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "hl7"


def by_type(bundle, resource_type):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == resource_type]


def test_adt_a01_creates_patient_encounter_condition():
    bundle = hl7v2_to_fhir((SAMPLES / "adt_a01.hl7").read_text(encoding="utf-8"))
    assert bundle["type"] == "transaction"
    assert bundle["identifier"]["value"] == "MSG0001"

    patient = by_type(bundle, "Patient")[0]
    assert patient["name"][0] == {"family": "Pérez", "given": ["Ana", "María"]}
    assert patient["gender"] == "female"
    assert patient["birthDate"] == "1990-05-12"
    assert patient["identifier"][0]["value"] == "12345678"
    assert patient["address"][0]["city"] == "Santiago"
    assert patient["telecom"][0]["value"] == "+56212345678"

    encounter = by_type(bundle, "Encounter")[0]
    assert encounter["class"]["code"] == "IMP"
    assert encounter["status"] == "in-progress"
    assert encounter["period"]["start"] == "2026-11-18T08:25:00-03:00"
    assert encounter["identifier"][0]["value"] == "V0001"

    condition = by_type(bundle, "Condition")[0]
    assert condition["code"]["coding"][0] == {
        "code": "J18.9",
        "system": "http://hl7.org/fhir/sid/icd-10",
        "display": "Neumonía, no especificada",
    }
    assert condition["recordedDate"] == "2026-11-18"


def test_oru_r01_creates_observations():
    bundle = hl7v2_to_fhir((SAMPLES / "oru_r01.hl7").read_text(encoding="utf-8"))
    observations = by_type(bundle, "Observation")
    assert len(observations) == 3

    hr, temp, text = observations
    assert hr["code"]["coding"][0]["system"] == "http://loinc.org"
    assert hr["valueQuantity"] == {"value": 72, "unit": "/min"}
    assert hr["status"] == "final"
    assert hr["effectiveDateTime"] == "2026-11-18T09:05:00-03:00"
    assert hr["interpretation"][0]["coding"][0]["code"] == "N"
    assert hr["referenceRange"][0]["text"] == "60-100"

    assert temp["valueQuantity"]["value"] == 38.4
    assert temp["interpretation"][0]["coding"][0]["code"] == "H"
    assert temp["effectiveDateTime"] == "2026-11-18T09:00:00-03:00"  # hereda la hora del OBR

    assert text["valueString"] == "Fiebre y tos desde hace 3 dias"


def test_references_point_to_bundle_entries():
    bundle = hl7v2_to_fhir((SAMPLES / "oru_r01.hl7").read_text(encoding="utf-8"))
    urls = {e["fullUrl"] for e in bundle["entry"]}
    for obs in by_type(bundle, "Observation"):
        assert obs["subject"]["reference"] in urls
        assert obs["encounter"]["reference"] in urls


def test_accepts_unix_line_endings_and_mllp_frame():
    msg = "\x0bMSH|^~\\&|A|B|C|D|20261118||ADT^A04|1|P|2.5\nPID|1||1||Rojas^Marta||||F\n\x1c\r"
    bundle = hl7v2_to_fhir(msg)
    assert by_type(bundle, "Patient")[0]["name"][0]["family"] == "Rojas"


def test_escaped_characters_and_unknown_sex():
    msg = "MSH|^~\\&|A|B|C|D|20261118||ADT^A04|1|P|2.5\rPID|1||1||Sá\\T\\nchez^Ana|||Z\r"
    patient = by_type(hl7v2_to_fhir(msg), "Patient")[0]
    assert patient["name"][0]["family"] == "Sá&nchez"
    assert patient["gender"] == "unknown"


def test_hl7_datetime_formats():
    assert hl7_datetime("1990") == "1990"
    assert hl7_datetime("199005") == "1990-05"
    assert hl7_datetime("19900512") == "1990-05-12"
    assert hl7_datetime("202611180830") == "2026-11-18T08:30:00-03:00"
    assert hl7_datetime("20261118083015+0100") == "2026-11-18T08:30:15+01:00"
    assert hl7_datetime("basura") is None
    assert hl7_datetime("") is None


def test_rejects_message_without_msh():
    with pytest.raises(HL7Error):
        hl7v2_to_fhir("PID|1||1||Soto^Luis")


def test_rejects_unsupported_message_type():
    with pytest.raises(HL7Error):
        hl7v2_to_fhir("MSH|^~\\&|A|B|C|D|20261118||ORM^O01|1|P|2.5\rPID|1||1||Soto^Luis")


def test_rejects_message_without_pid():
    with pytest.raises(HL7Error):
        hl7v2_to_fhir("MSH|^~\\&|A|B|C|D|20261118||ADT^A01|1|P|2.5\rEVN|A01")


def test_rejects_patient_without_name():
    with pytest.raises(HL7Error):
        hl7v2_to_fhir("MSH|^~\\&|A|B|C|D|20261118||ADT^A01|1|P|2.5\rPID|1||1")
