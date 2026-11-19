"""Mini servidor FHIR (R4) con FastAPI y SQLite.

Recursos: Patient, Encounter, Observation y Condition (datos ficticios).
Incluye búsquedas, Patient/{id}/$everything, transacciones (Bundle) y un
endpoint que convierte mensajes HL7 v2 a FHIR (ver converter.py).
"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from pathlib import Path

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from datetime import datetime

from . import dashboard as dash
from . import dicom_service as dcm
from . import seed_demo
from .converter import HL7Error, hl7v2_to_fhir

STATIC_DIR = Path(__file__).parent / "static"

DB_PATH = os.environ.get("FHIR_DB", "fhir.db")
FHIR_JSON = "application/fhir+json"

app = FastAPI(
    title="Mini FHIR Server",
    description="API de interoperabilidad clínica basada en HL7 FHIR R4 (datos ficticios).",
    version="2.0.0",
)

RESOURCE_TYPES = {"Patient", "Encounter", "Observation", "Condition", "ImagingStudy"}
# Orden de creación en transacciones: primero lo que los demás referencian.
CREATE_ORDER = {"Patient": 0, "Encounter": 1, "Observation": 2, "Condition": 2, "ImagingStudy": 2}
# Recursos que apuntan a un paciente mediante "subject".
CLINICAL_TYPES = ("Encounter", "Observation", "Condition", "ImagingStudy")

OBSERVATION_STATUS = {
    "registered", "preliminary", "final", "amended",
    "corrected", "cancelled", "entered-in-error", "unknown",
}
ENCOUNTER_STATUS = {
    "planned", "arrived", "triaged", "in-progress", "onleave",
    "finished", "cancelled", "entered-in-error", "unknown",
}


class FhirError(Exception):
    """Error que se responde como OperationOutcome."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


# ---------- Base de datos ----------
@contextmanager
def get_conn():
    """Abre la base, confirma al salir y revierte todo si hubo un error."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resources (
                id TEXT PRIMARY KEY,
                resource_type TEXT NOT NULL,
                body TEXT NOT NULL,
                last_updated TEXT NOT NULL
            )
            """
        )


init_db()


# ---------- Utilidades ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def operation_outcome(severity: str, code: str, message: str) -> dict:
    """Respuesta de error estándar FHIR."""
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severity, "code": code, "diagnostics": message}],
    }


@app.exception_handler(FhirError)
async def fhir_error_handler(request: Request, exc: FhirError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=operation_outcome("error", exc.code, exc.message),
        media_type=FHIR_JSON,
    )


def reference_id(ref_obj: Any, expected_type: str) -> Optional[str]:
    """De {'reference': 'Patient/123'} devuelve '123'; None si el formato no calza."""
    ref = ref_obj.get("reference", "") if isinstance(ref_obj, dict) else ""
    prefix = f"{expected_type}/"
    if isinstance(ref, str) and ref.startswith(prefix) and len(ref) > len(prefix):
        return ref[len(prefix):]
    return None


def validate(resource_type: str, body: dict[str, Any]) -> Optional[str]:
    """Validación mínima. Devuelve un mensaje de error o None si es válido."""
    if body.get("resourceType") != resource_type:
        return f"resourceType debe ser '{resource_type}'"

    if resource_type == "Patient":
        if not body.get("name"):
            return "Patient requiere al menos un 'name'"
        if body.get("gender") not in (None, "male", "female", "other", "unknown"):
            return "gender debe ser male, female, other o unknown"

    if resource_type in CLINICAL_TYPES:
        if reference_id(body.get("subject"), "Patient") is None:
            return f"{resource_type} requiere subject.reference con formato 'Patient/<id>'"

    if resource_type == "Observation":
        if not body.get("status"):
            return "Observation requiere 'status'"
        if body["status"] not in OBSERVATION_STATUS:
            return "Observation.status no es válido"
        if not body.get("code"):
            return "Observation requiere 'code'"

    if resource_type == "Encounter":
        if not body.get("status"):
            return "Encounter requiere 'status'"
        if body["status"] not in ENCOUNTER_STATUS:
            return "Encounter.status no es válido"
        if not isinstance(body.get("class"), dict) or not body["class"]:
            return "Encounter requiere 'class'"

    if resource_type == "Condition":
        if not body.get("code"):
            return "Condition requiere 'code'"

    if resource_type == "ImagingStudy":
        if not body.get("status"):
            return "ImagingStudy requiere 'status'"

    if resource_type in ("Observation", "Condition", "ImagingStudy") and "encounter" in body:
        if reference_id(body["encounter"], "Encounter") is None:
            return "encounter.reference debe tener formato 'Encounter/<id>'"
    return None


def stamp_meta(body: dict, version: int) -> dict:
    body["meta"] = {"versionId": str(version), "lastUpdated": now_iso()}
    return body


def resource_exists(conn: sqlite3.Connection, resource_type: str, resource_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM resources WHERE id = ? AND resource_type = ?", (resource_id, resource_type)
    ).fetchone()
    return row is not None


def check_type(resource_type: str) -> None:
    if resource_type not in RESOURCE_TYPES:
        raise FhirError(404, "not-supported", f"Recurso '{resource_type}' no soportado")


def check_references(conn: sqlite3.Connection, resource_type: str, body: dict) -> None:
    """Integridad referencial: el paciente y el encuentro referenciados deben existir."""
    if resource_type == "Patient":
        return
    patient_id = reference_id(body.get("subject"), "Patient")
    if not resource_exists(conn, "Patient", patient_id):
        raise FhirError(422, "not-found", f"El paciente '{patient_id}' no existe")
    encounter_id = reference_id(body.get("encounter"), "Encounter")
    if encounter_id and not resource_exists(conn, "Encounter", encounter_id):
        raise FhirError(422, "not-found", f"El encuentro '{encounter_id}' no existe")


def store_resource(
    conn: sqlite3.Connection, resource_type: str, body: dict, new_id: Optional[str] = None
) -> dict:
    """Valida y guarda un recurso nuevo. Lanza FhirError si algo falla."""
    error = validate(resource_type, body)
    if error:
        raise FhirError(400, "invalid", error)
    check_references(conn, resource_type, body)

    new_id = new_id or str(uuid.uuid4())
    body["id"] = new_id
    stamp_meta(body, 1)
    conn.execute(
        "INSERT INTO resources (id, resource_type, body, last_updated) VALUES (?, ?, ?, ?)",
        (new_id, resource_type, json.dumps(body), body["meta"]["lastUpdated"]),
    )
    return body


def replace_references(node: Any, mapping: dict[str, str]) -> None:
    """Cambia las referencias 'urn:uuid:...' por 'Tipo/id' recorriendo el recurso."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "reference" and isinstance(value, str) and value in mapping:
                node[key] = mapping[value]
            else:
                replace_references(value, mapping)
    elif isinstance(node, list):
        for item in node:
            replace_references(item, mapping)


def find_patient_by_identifier(conn: sqlite3.Connection, patient: dict) -> Optional[dict]:
    """Busca un paciente ya guardado con el mismo identificador (system + value)."""
    wanted = {
        (i.get("system"), i.get("value"))
        for i in patient.get("identifier", [])
        if isinstance(i, dict) and i.get("value")
    }
    if not wanted:
        return None
    for row in conn.execute("SELECT body FROM resources WHERE resource_type = 'Patient'"):
        candidate = json.loads(row["body"])
        have = {(i.get("system"), i.get("value")) for i in candidate.get("identifier", [])}
        if wanted & have:
            return candidate
    return None


def process_transaction(conn: sqlite3.Connection, bundle: dict) -> dict:
    """Guarda todos los recursos de un Bundle 'transaction' o ninguno."""
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
        raise FhirError(400, "invalid", "Se esperaba un Bundle de tipo 'transaction'")

    items = []
    reused: list[dict] = []
    mapping: dict[str, str] = {}
    for entry in bundle.get("entry", []):
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            raise FhirError(400, "invalid", "Cada entry necesita un 'resource'")
        resource_type = resource.get("resourceType")
        if resource_type not in RESOURCE_TYPES:
            raise FhirError(400, "not-supported", f"Recurso '{resource_type}' no soportado")
        if resource_type == "Patient":
            existing = find_patient_by_identifier(conn, resource)
            if existing is not None:  # mismo paciente (mismo RUT/ficha): se reutiliza
                if entry.get("fullUrl"):
                    mapping[entry["fullUrl"]] = f"Patient/{existing['id']}"
                reused.append(
                    {"response": {"status": "200 OK", "location": f"Patient/{existing['id']}"}, "resource": existing}
                )
                continue
        new_id = str(uuid.uuid4())
        if entry.get("fullUrl"):
            mapping[entry["fullUrl"]] = f"{resource_type}/{new_id}"
        items.append((resource_type, new_id, resource))

    items.sort(key=lambda item: CREATE_ORDER[item[0]])  # el orden original no importa
    entries = list(reused)
    for resource_type, new_id, resource in items:
        replace_references(resource, mapping)
        stored = store_resource(conn, resource_type, resource, new_id)
        entries.append(
            {"response": {"status": "201 Created", "location": f"{resource_type}/{new_id}"}, "resource": stored}
        )
    return {"resourceType": "Bundle", "type": "transaction-response", "entry": entries}


# ---------- Endpoints ----------
@app.get("/metadata")
def capability_statement() -> dict:
    """CapabilityStatement: describe qué soporta este servidor."""
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": now_iso(),
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "rest": [
            {
                "mode": "server",
                "interaction": [{"code": "transaction"}],
                "resource": [
                    {
                        "type": t,
                        "interaction": [
                            {"code": c} for c in ("read", "create", "update", "delete", "search-type")
                        ],
                    }
                    for t in sorted(RESOURCE_TYPES)
                ],
            }
        ],
    }


@app.post("/convert/hl7v2")
async def convert_hl7v2(request: Request, store: bool = Query(False, description="Guardar los recursos convertidos")):
    """Recibe un mensaje HL7 v2 (texto plano) y devuelve el Bundle FHIR equivalente."""
    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")  # muchos sistemas HL7 antiguos usan Latin-1
    try:
        bundle = hl7v2_to_fhir(text)
    except HL7Error as error:
        raise FhirError(400, "invalid", str(error))

    if not store:
        return JSONResponse(content=bundle, media_type=FHIR_JSON)
    with get_conn() as conn:
        result = process_transaction(conn, bundle)
    return JSONResponse(status_code=201, content=result, media_type=FHIR_JSON)


# ---------- DICOM y visor ----------
def find_study(conn: sqlite3.Connection, study_uid: str) -> Optional[dict]:
    """Busca el ImagingStudy que corresponde a un StudyInstanceUID."""
    wanted = f"urn:oid:{study_uid}"
    rows = conn.execute(
        "SELECT body FROM resources WHERE resource_type = 'ImagingStudy' AND body LIKE ?",
        (f"%{wanted}%",),
    )
    for row in rows:
        study = json.loads(row["body"])
        if any(i.get("value") == wanted for i in study.get("identifier", [])):
            return study
    return None


def register_instance(conn: sqlite3.Connection, meta: dict) -> dict:
    """Vincula una imagen DICOM con su paciente (existente o nuevo) y su ImagingStudy."""
    patient = dcm.patient_resource(meta)
    existing = find_patient_by_identifier(conn, patient)
    patient_id = existing["id"] if existing else store_resource(conn, "Patient", patient)["id"]
    result = {
        "patient": f"Patient/{patient_id}",
        "instance": meta["sop_uid"],
        "newPatient": existing is None,
        "newStudy": False,
        "newInstance": True,
    }

    study = find_study(conn, meta["study_uid"])
    if study is None:
        stored = store_resource(conn, "ImagingStudy", dcm.imaging_study_resource(meta, patient_id))
        result.update(imagingStudy=f"ImagingStudy/{stored['id']}", newStudy=True)
        return result

    result["imagingStudy"] = f"ImagingStudy/{study['id']}"
    if reference_id(study.get("subject"), "Patient") != patient_id:
        raise FhirError(409, "conflict", "El estudio ya existe y pertenece a otro paciente")
    if not dcm.add_instance(study, meta):
        result["newInstance"] = False
        return result
    stamp_meta(study, int(study.get("meta", {}).get("versionId", "1")) + 1)
    conn.execute(
        "UPDATE resources SET body = ?, last_updated = ? WHERE id = ?",
        (json.dumps(study), study["meta"]["lastUpdated"], study["id"]),
    )
    return result


def ingest_dicom(raw: bytes) -> dict:
    """Lee un DICOM, lo vincula en FHIR (Patient + ImagingStudy) y guarda el archivo."""
    meta = dcm.extract_meta(dcm.read_dicom(raw))
    with get_conn() as conn:
        result = register_instance(conn, meta)
        if result["newInstance"]:
            dcm.save_file(meta["sop_uid"], raw)
    return result


def load_instance(sop_uid: str):
    try:
        return dcm.load_file(sop_uid)
    except dcm.DicomError as error:
        raise FhirError(400, "invalid", str(error))
    except FileNotFoundError:
        raise FhirError(404, "not-found", f"No existe la imagen {sop_uid}")
    except Exception as error:  # archivo en disco dañado
        raise FhirError(500, "exception", f"No se pudo leer la imagen: {type(error).__name__}")


def display_name(patient: dict) -> str:
    name = (patient.get("name") or [{}])[0]
    given = " ".join(name.get("given", []))
    return f"{name.get('family', 'Sin nombre')}, {given}".strip(", ")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/viewer")


@app.get("/viewer", include_in_schema=False)
def viewer():
    return FileResponse(STATIC_DIR / "viewer.html", media_type="text/html")


@app.post("/dicom/upload")
async def dicom_upload(request: Request):
    """Recibe un archivo .dcm (cuerpo de la petición), lo guarda y crea Patient + ImagingStudy."""
    raw = await request.body()
    if not raw:
        raise FhirError(400, "invalid", "El cuerpo está vacío: envía el archivo .dcm")
    if len(raw) > dcm.MAX_UPLOAD_BYTES:
        raise FhirError(413, "too-costly", "El archivo supera el máximo de 200 MB")
    try:
        result = ingest_dicom(raw)
    except dcm.DicomError as error:
        raise FhirError(400, "invalid", str(error))
    return JSONResponse(status_code=201 if result["newInstance"] else 200, content=result)


@app.post("/dicom/samples")
def dicom_samples():
    """Carga las imágenes de ejemplo de samples/dicom/ (para probar el visor con un clic)."""
    folder = Path(__file__).resolve().parent.parent / "samples" / "dicom"
    files = sorted(folder.glob("*.dcm"))
    if not files:
        raise FhirError(404, "not-found", "No hay ejemplos. Ejecuta: python -m app.sample_dicom")
    loaded = skipped = 0
    for path in files:
        try:
            result = ingest_dicom(path.read_bytes())
        except dcm.DicomError as error:
            raise FhirError(400, "invalid", f"{path.name}: {error}")
        loaded += result["newInstance"]
        skipped += not result["newInstance"]
    return {"loaded": loaded, "skipped": skipped}


@app.get("/dicom/studies")
def dicom_studies():
    """Estudios con sus series e imágenes, más el paciente: es lo que lee el visor."""
    with get_conn() as conn:
        studies = [
            study
            for study in (
                json.loads(r["body"])
                for r in conn.execute("SELECT body FROM resources WHERE resource_type = 'ImagingStudy' ORDER BY rowid")
            )
            if not dash.is_synthetic(study)  # los estudios de demostración no tienen archivo .dcm
        ]
        patients = {
            p["id"]: p
            for p in (
                json.loads(r["body"])
                for r in conn.execute("SELECT body FROM resources WHERE resource_type = 'Patient'")
            )
        }

    result = []
    for study in studies:
        patient_id = reference_id(study.get("subject"), "Patient")
        patient = patients.get(patient_id, {})
        identifier = (patient.get("identifier") or [{}])[0].get("value", "")
        result.append(
            {
                "id": study["id"],
                "description": study.get("description", ""),
                "started": study.get("started", ""),
                "modalities": [m.get("code") for m in study.get("modality", [])],
                "patient": {"id": patient_id, "name": display_name(patient), "identifier": identifier},
                "series": [
                    {
                        "uid": s["uid"],
                        "number": s.get("number"),
                        "description": s.get("description", ""),
                        "modality": s.get("modality", {}).get("code", ""),
                        "instances": [i["uid"] for i in s.get("instance", [])],
                    }
                    for s in study.get("series", [])
                ],
            }
        )
    return result


@app.get("/dicom/instances/{sop_uid}/frames/{index}")
def dicom_frame(sop_uid: str, index: int):
    """Un corte de la imagen como enteros de 16 bits (int16 little-endian), sin comprimir."""
    ds = load_instance(sop_uid)
    try:
        data, info = dcm.frame_to_int16(ds, index)
    except IndexError:
        raise FhirError(404, "not-found", "Ese corte no existe")
    except dcm.DicomError as error:
        raise FhirError(422, "processing", str(error))

    def num(value):
        return "" if value is None else str(value)

    spacing = info["pixel_spacing"]
    headers = {
        "X-Rows": str(info["rows"]),
        "X-Columns": str(info["columns"]),
        "X-Min": str(info["min"]),
        "X-Max": str(info["max"]),
        "X-Invert": "1" if info["invert"] else "0",
        "X-Pixel-Spacing": f"{spacing[0]},{spacing[1]}" if spacing else "",
        "X-Window-Center": num(info["window_center"]),
        "X-Window-Width": num(info["window_width"]),
        "X-Modality": info["modality"],
        "X-Frames": str(info["frames"]),
        "Cache-Control": "no-cache",
    }
    return Response(content=data.tobytes(), media_type="application/octet-stream", headers=headers)


@app.get("/dicom/instances/{sop_uid}/tags")
def dicom_tags(sop_uid: str):
    """Etiquetas DICOM legibles de una imagen."""
    return dcm.tag_list(load_instance(sop_uid))


# ---------- Dashboard clínico ----------
@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")


@app.get("/dashboard/data")
def dashboard_data(
    days: int = Query(30, ge=7, le=365, description="Largo del período en días"),
    capacity: int = Query(60, ge=1, le=5000, description="Camas totales (para la ocupación)"),
):
    """Indicadores calculados en vivo desde los recursos FHIR guardados."""
    resources: dict[str, list[dict]] = {t: [] for t in RESOURCE_TYPES}
    with get_conn() as conn:
        for row in conn.execute("SELECT resource_type, body FROM resources"):
            resources[row["resource_type"]].append(json.loads(row["body"]))
    return dash.compute(resources, datetime.now().astimezone(), days, capacity)


@app.post("/dashboard/seed")
def dashboard_seed(patients: int = Query(3000, ge=20, le=10000)):
    """Reemplaza los datos de demostración por unos nuevos (no toca los datos reales)."""
    with get_conn() as conn:
        removed = seed_demo.clear(conn)
        created = seed_demo.seed(conn, datetime.now().astimezone(), patients=patients)
    return {"removed": removed, "created": created}


@app.delete("/dashboard/seed")
def dashboard_clear():
    """Borra solo los datos de demostración."""
    with get_conn() as conn:
        removed = seed_demo.clear(conn)
    return {"removed": removed}


@app.post("/")
def transaction(bundle: dict[str, Any]):
    """Transacción: guarda todo el Bundle o nada, resolviendo referencias urn:uuid."""
    with get_conn() as conn:
        result = process_transaction(conn, bundle)
    return JSONResponse(content=result, media_type=FHIR_JSON)


@app.get("/Patient/{patient_id}/$everything")
def patient_everything(patient_id: str):
    """Todo lo registrado de un paciente: su ficha, encuentros, observaciones y diagnósticos."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT body FROM resources WHERE id = ? AND resource_type = 'Patient'", (patient_id,)
        ).fetchone()
        if row is None:
            raise FhirError(404, "not-found", f"Patient/{patient_id} no existe")
        others = conn.execute(
            "SELECT body FROM resources WHERE resource_type != 'Patient' ORDER BY rowid"
        ).fetchall()

    related = [json.loads(r["body"]) for r in others]
    related = [r for r in related if reference_id(r.get("subject"), "Patient") == patient_id]
    related.sort(key=lambda r: CREATE_ORDER[r["resourceType"]])
    resources = [json.loads(row["body"])] + related
    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"fullUrl": f"/{r['resourceType']}/{r['id']}", "resource": r} for r in resources],
    }
    return JSONResponse(content=bundle, media_type=FHIR_JSON)


@app.post("/{resource_type}", status_code=201)
def create_resource(resource_type: str, body: dict[str, Any]):
    check_type(resource_type)
    with get_conn() as conn:
        stored = store_resource(conn, resource_type, body)
    return JSONResponse(
        status_code=201,
        content=stored,
        media_type=FHIR_JSON,
        headers={"Location": f"/{resource_type}/{stored['id']}"},
    )


@app.get("/{resource_type}/{resource_id}")
def read_resource(resource_type: str, resource_id: str):
    check_type(resource_type)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT body FROM resources WHERE id = ? AND resource_type = ?",
            (resource_id, resource_type),
        ).fetchone()
    if row is None:
        raise FhirError(404, "not-found", f"{resource_type}/{resource_id} no existe")
    return JSONResponse(content=json.loads(row["body"]), media_type=FHIR_JSON)


@app.get("/{resource_type}")
def search_resources(
    resource_type: str,
    name: Optional[str] = Query(None, description="Patient: buscar por apellido o nombre"),
    gender: Optional[str] = Query(None, description="Patient: male, female, other, unknown"),
    patient: Optional[str] = Query(None, description="Encounter, Observation, Condition: id del paciente"),
    code: Optional[str] = Query(None, description="Observation, Condition: código (ej. LOINC 8867-4, CIE-10 J18.9)"),
    status: Optional[str] = Query(None, description="Encounter, Observation: estado (ej. final, finished)"),
):
    check_type(resource_type)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT body FROM resources WHERE resource_type = ? ORDER BY rowid", (resource_type,)
        ).fetchall()

    results = [json.loads(r["body"]) for r in rows]

    if resource_type == "Patient":
        if name:
            q = name.lower()
            results = [
                r
                for r in results
                if any(
                    q in (n.get("family", "") + " " + " ".join(n.get("given", []))).lower()
                    for n in r.get("name", [])
                )
            ]
        if gender:
            results = [r for r in results if r.get("gender") == gender]
    else:
        if patient:
            results = [r for r in results if reference_id(r.get("subject"), "Patient") == patient]
        if code and resource_type in ("Observation", "Condition"):
            results = [
                r
                for r in results
                if any(c.get("code") == code for c in r.get("code", {}).get("coding", []))
            ]
        if status and resource_type in ("Observation", "Encounter", "ImagingStudy"):
            results = [r for r in results if r.get("status") == status]

    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(results),
        "entry": [{"fullUrl": f"/{resource_type}/{r['id']}", "resource": r} for r in results],
    }
    return JSONResponse(content=bundle, media_type=FHIR_JSON)


@app.put("/{resource_type}/{resource_id}")
def update_resource(resource_type: str, resource_id: str, body: dict[str, Any]):
    check_type(resource_type)
    error = validate(resource_type, body)
    if error:
        raise FhirError(400, "invalid", error)
    if body.get("id") not in (None, resource_id):
        raise FhirError(400, "invalid", "El id del cuerpo no coincide con la URL")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT body FROM resources WHERE id = ? AND resource_type = ?",
            (resource_id, resource_type),
        ).fetchone()
        if row is None:
            raise FhirError(404, "not-found", f"{resource_type}/{resource_id} no existe")
        check_references(conn, resource_type, body)

        old_version = int(json.loads(row["body"]).get("meta", {}).get("versionId", "1"))
        body["id"] = resource_id
        stamp_meta(body, old_version + 1)
        conn.execute(
            "UPDATE resources SET body = ?, last_updated = ? WHERE id = ?",
            (json.dumps(body), body["meta"]["lastUpdated"], resource_id),
        )
    return JSONResponse(content=body, media_type=FHIR_JSON)


@app.delete("/{resource_type}/{resource_id}", status_code=204)
def delete_resource(resource_type: str, resource_id: str):
    check_type(resource_type)
    with get_conn() as conn:
        if not resource_exists(conn, resource_type, resource_id):
            raise FhirError(404, "not-found", f"{resource_type}/{resource_id} no existe")
        in_use = conn.execute(
            "SELECT 1 FROM resources WHERE id != ? AND body LIKE ?",
            (resource_id, f'%"reference": "{resource_type}/{resource_id}"%'),
        ).fetchone()
        if in_use:
            raise FhirError(
                409, "conflict", f"{resource_type}/{resource_id} está referenciado por otros recursos"
            )
        study_files: list[str] = []
        if resource_type == "ImagingStudy":
            row = conn.execute("SELECT body FROM resources WHERE id = ?", (resource_id,)).fetchone()
            study_files = dcm.study_instance_uids(json.loads(row["body"]))
        conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))
    dcm.delete_files(study_files)  # al borrar un estudio también se borran sus archivos .dcm
    return Response(status_code=204)
