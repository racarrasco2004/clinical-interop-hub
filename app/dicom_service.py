"""DICOM: lectura de archivos, imagen lista para el visor y traducción a FHIR.

- read_dicom / extract_meta: abre el archivo y saca los datos importantes.
- patient_resource / imaging_study_resource: arma los recursos FHIR.
- frame_to_int16: convierte la imagen a números (unidades reales, ej. Hounsfield en TC).
- Los archivos .dcm se guardan en disco (DICOM_DIR); los datos clínicos, en la base FHIR.
"""
import io
import os
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pydicom
from pydicom.multival import MultiValue

from .converter import DEFAULT_TZ, hl7_datetime

DICOM_DIR = Path(os.environ.get("DICOM_DIR", "dicom_files"))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")

DCM_SYSTEM = "http://dicom.nema.org/resources/ontology/DCM"
SOP_CLASS_SYSTEM = "urn:ietf:rfc:3986"
SECONDARY_CAPTURE = "1.2.840.10008.5.1.4.1.1.7"
GENDER = {"M": "male", "F": "female", "O": "other"}

# Tipos de dato (VR) que son binarios: no se muestran en la lista de etiquetas.
BINARY_VR = {"OB", "OW", "OD", "OF", "OL", "OV", "UN"}


class DicomError(ValueError):
    """Archivo DICOM inválido o que no se puede procesar."""


def valid_uid(uid: Any) -> bool:
    return isinstance(uid, str) and len(uid) <= 64 and bool(UID_RE.match(uid))


# ---------- Lectura ----------
def read_dicom(raw: bytes) -> pydicom.Dataset:
    try:
        ds = pydicom.dcmread(io.BytesIO(raw))
    except Exception as error:  # el archivo viene del usuario: cualquier fallo es "no es DICOM"
        raise DicomError(f"El archivo no es un DICOM válido ({type(error).__name__})") from error
    for name in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        if not valid_uid(str(ds.get(name, "") or "")):
            raise DicomError(f"Falta o es inválido el campo {name}")
    return ds


def _text(ds: pydicom.Dataset, name: str) -> str:
    value = ds.get(name)
    return "" if value in (None, "") else str(value).strip()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (list, tuple, MultiValue)):
        value = value[0] if len(value) else None
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(ds: pydicom.Dataset, name: str) -> Optional[int]:
    number = _number(ds.get(name))
    return None if number is None else int(number)


def _pixel_spacing(ds: pydicom.Dataset) -> Optional[list[float]]:
    value = ds.get("PixelSpacing") or ds.get("ImagerPixelSpacing")
    if value is None or len(value) < 2:
        return None
    try:
        return [float(value[0]), float(value[1])]  # [separación entre filas, entre columnas] en mm
    except (TypeError, ValueError):
        return None


def extract_meta(ds: pydicom.Dataset) -> dict:
    patient_id = _text(ds, "PatientID")
    if not patient_id:
        raise DicomError("El DICOM no trae PatientID (0010,0020): no se puede asociar a un paciente")

    name = ds.get("PatientName")
    family = given = ""
    if name:
        family = str(name.family_name or "").strip()
        given = " ".join(x for x in (name.given_name, name.middle_name) if x).strip()

    birth = hl7_datetime(_text(ds, "PatientBirthDate"))
    study_date, study_time = _text(ds, "StudyDate"), _text(ds, "StudyTime")
    offset = _text(ds, "TimezoneOffsetFromUTC")
    started = hl7_datetime(study_date + study_time + (offset if re.match(r"^[+-]\d{4}$", offset) else ""), DEFAULT_TZ)

    return {
        "patient_id": patient_id,
        "issuer": _text(ds, "IssuerOfPatientID"),
        "family": family,
        "given": given,
        "birth_date": birth[:10] if birth else None,
        "gender": GENDER.get(_text(ds, "PatientSex").upper()),
        "study_uid": _text(ds, "StudyInstanceUID"),
        "series_uid": _text(ds, "SeriesInstanceUID"),
        "sop_uid": _text(ds, "SOPInstanceUID"),
        "sop_class": _text(ds, "SOPClassUID") or SECONDARY_CAPTURE,
        "modality": _text(ds, "Modality") or "OT",
        "study_description": _text(ds, "StudyDescription"),
        "series_description": _text(ds, "SeriesDescription"),
        "series_number": _integer(ds, "SeriesNumber"),
        "instance_number": _integer(ds, "InstanceNumber"),
        "body_part": _text(ds, "BodyPartExamined"),
        "started": started,
        "rows": _integer(ds, "Rows"),
        "columns": _integer(ds, "Columns"),
        "frames": int(_number(ds.get("NumberOfFrames")) or 1),
    }


# ---------- DICOM -> FHIR ----------
def modality_coding(code: str) -> dict:
    return {"system": DCM_SYSTEM, "code": code}


def patient_resource(meta: dict) -> dict:
    """Patient FHIR. Si el DICOM trae emisor del ID (ej. HOSPITAL_DEMO) usa el mismo
    'system' que el conversor HL7, así el paciente del HL7 y el de la imagen son el mismo."""
    system = f"urn:hl7v2:authority:{meta['issuer']}" if meta["issuer"] else "urn:dicom:patient-id"
    name: dict = {"family": meta["family"] or "Desconocido"}
    if meta["given"]:
        name["given"] = meta["given"].split()
    patient: dict = {
        "resourceType": "Patient",
        "identifier": [{"system": system, "value": meta["patient_id"]}],
        "name": [name],
    }
    if meta["gender"]:
        patient["gender"] = meta["gender"]
    if meta["birth_date"]:
        patient["birthDate"] = meta["birth_date"]
    return patient


def add_instance(study: dict, meta: dict) -> bool:
    """Agrega la imagen al ImagingStudy (creando la serie si hace falta).
    Devuelve False si esa imagen ya estaba registrada."""
    series_list = study.setdefault("series", [])
    series = next((s for s in series_list if s["uid"] == meta["series_uid"]), None)
    if series is None:
        series = {"uid": meta["series_uid"], "modality": modality_coding(meta["modality"]), "instance": []}
        if meta["series_number"] is not None:
            series["number"] = meta["series_number"]
        if meta["series_description"]:
            series["description"] = meta["series_description"]
        if meta["body_part"]:
            series["bodySite"] = {"display": meta["body_part"]}
        series_list.append(series)

    if any(i["uid"] == meta["sop_uid"] for i in series["instance"]):
        return False

    instance = {
        "uid": meta["sop_uid"],
        "sopClass": {"system": SOP_CLASS_SYSTEM, "code": f"urn:oid:{meta['sop_class']}"},
    }
    if meta["instance_number"] is not None:
        instance["number"] = meta["instance_number"]
    series["instance"].append(instance)
    series["instance"].sort(key=lambda i: (i.get("number") is None, i.get("number") or 0, i["uid"]))
    series["numberOfInstances"] = len(series["instance"])

    series_list.sort(key=lambda s: (s.get("number") is None, s.get("number") or 0, s["uid"]))
    study["numberOfSeries"] = len(series_list)
    study["numberOfInstances"] = sum(s["numberOfInstances"] for s in series_list)
    modalities = sorted({s["modality"]["code"] for s in series_list})
    study["modality"] = [modality_coding(m) for m in modalities]
    return True


def imaging_study_resource(meta: dict, patient_id: str) -> dict:
    study: dict = {
        "resourceType": "ImagingStudy",
        "status": "available",
        "subject": {"reference": f"Patient/{patient_id}"},
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{meta['study_uid']}"}],
    }
    if meta["started"]:
        study["started"] = meta["started"]
    if meta["study_description"]:
        study["description"] = meta["study_description"]
    add_instance(study, meta)
    return study


def study_instance_uids(study: dict) -> list[str]:
    return [i["uid"] for s in study.get("series", []) for i in s.get("instance", [])]


# ---------- Archivos en disco ----------
def _path(sop_uid: str) -> Path:
    if not valid_uid(sop_uid):
        raise DicomError("UID inválido")
    return DICOM_DIR / f"{sop_uid}.dcm"


def save_file(sop_uid: str, raw: bytes) -> None:
    path = _path(sop_uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(raw)
    os.replace(temp, path)


def load_file(sop_uid: str) -> pydicom.Dataset:
    path = _path(sop_uid)
    if not path.is_file():
        raise FileNotFoundError(sop_uid)
    return pydicom.dcmread(path)


def delete_files(sop_uids: list[str]) -> None:
    for uid in sop_uids:
        try:
            _path(uid).unlink(missing_ok=True)
        except (DicomError, OSError):
            pass


# ---------- Imagen para el visor ----------
def frame_to_int16(ds: pydicom.Dataset, index: int = 0) -> tuple[np.ndarray, dict]:
    """Devuelve un corte como matriz int16 en unidades reales (ej. Hounsfield) y datos de apoyo.

    Escala de grises. Si la imagen es a color, se convierte a gris."""
    if "PixelData" not in ds:
        raise DicomError("Este DICOM no contiene imagen")
    frames = int(_number(ds.get("NumberOfFrames")) or 1)
    if not 0 <= index < frames:
        raise IndexError(index)

    try:
        pixels = ds.pixel_array
    except Exception as error:
        raise DicomError(
            f"No se pudo decodificar la imagen ({type(error).__name__}: {str(error)[:150]}). "
            "Si está comprimida, instala 'pylibjpeg' y 'pylibjpeg-libjpeg'."
        ) from error

    if frames > 1:
        pixels = pixels[index]

    if int(ds.get("SamplesPerPixel", 1)) >= 3:
        pixels = pixels[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        values = pixels
    else:
        try:
            values = pydicom.pixels.apply_modality_lut(pixels, ds)
        except Exception:
            values = pixels.astype(np.float32)

    data = np.clip(np.rint(values), -32768, 32767).astype("<i2")
    info = {
        "rows": int(data.shape[0]),
        "columns": int(data.shape[1]),
        "min": int(data.min()),
        "max": int(data.max()),
        "invert": _text(ds, "PhotometricInterpretation") == "MONOCHROME1",
        "pixel_spacing": _pixel_spacing(ds),
        "window_center": _number(ds.get("WindowCenter")),
        "window_width": _number(ds.get("WindowWidth")),
        "modality": _text(ds, "Modality"),
        "frames": frames,
    }
    return data, info


def tag_list(ds: pydicom.Dataset) -> list[dict]:
    """Lista de etiquetas DICOM legibles (sin la imagen ni datos binarios)."""
    rows = []
    for element in ds:
        if element.tag == (0x7FE0, 0x0010) or element.VR in BINARY_VR:
            continue
        if element.VR == "SQ":
            value = f"[secuencia de {len(element.value)} ítem(s)]"
        else:
            value = str(element.value)
            if len(value) > 120:
                value = value[:117] + "..."
        rows.append(
            {
                "tag": f"({element.tag.group:04X},{element.tag.element:04X})",
                "name": element.name,
                "vr": element.VR,
                "value": value,
            }
        )
    return rows
