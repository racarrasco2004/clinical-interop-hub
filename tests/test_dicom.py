import io
import os
from pathlib import Path

import numpy as np
import pydicom
import pytest

from app import dicom_service as dcm
from app.sample_dicom import make_chest_xray, make_ct_series

HL7_ORU = (Path(__file__).resolve().parent.parent / "samples" / "hl7" / "oru_r01.hl7").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ct():
    return make_ct_series(slices=4, size=64)


def upload(client, raw):
    return client.post("/dicom/upload", content=raw, headers={"Content-Type": "application/dicom"})


def sop_of(raw):
    return pydicom.dcmread(io.BytesIO(raw)).SOPInstanceUID


# ---------- Subida y vínculo FHIR ----------
def test_upload_creates_patient_and_imaging_study(client, ct):
    r = upload(client, ct[0])
    assert r.status_code == 201
    body = r.json()
    assert body["newPatient"] and body["newStudy"] and body["newInstance"]

    patient = client.get("/" + body["patient"]).json()
    assert patient["name"][0] == {"family": "Pérez", "given": ["Ana", "María"]}
    assert patient["birthDate"] == "1990-05-12"

    study = client.get("/" + body["imagingStudy"]).json()
    assert study["resourceType"] == "ImagingStudy"
    assert study["status"] == "available"
    assert study["subject"]["reference"] == body["patient"]
    assert study["numberOfSeries"] == 1 and study["numberOfInstances"] == 1
    assert study["modality"][0]["code"] == "CT"
    assert study["started"] == "2026-11-18T10:15:00-03:00"


def test_series_grows_and_instances_are_sorted(client, ct):
    for raw in reversed(ct):  # se suben en desorden a propósito
        assert upload(client, raw).status_code == 201
    assert client.get("/Patient").json()["total"] == 1
    studies = client.get("/ImagingStudy").json()
    assert studies["total"] == 1
    study = studies["entry"][0]["resource"]
    assert study["numberOfInstances"] == 4
    numbers = [i["number"] for i in study["series"][0]["instance"]]
    assert numbers == [1, 2, 3, 4]
    assert study["meta"]["versionId"] == "4"


def test_same_image_twice_is_idempotent(client, ct):
    assert upload(client, ct[0]).status_code == 201
    r = upload(client, ct[0])
    assert r.status_code == 200 and r.json()["newInstance"] is False
    study = client.get("/ImagingStudy").json()["entry"][0]["resource"]
    assert study["numberOfInstances"] == 1


def test_two_studies_for_same_patient(client, ct):
    upload(client, ct[0])
    upload(client, make_chest_xray(64))
    assert client.get("/Patient").json()["total"] == 1
    assert client.get("/ImagingStudy").json()["total"] == 2


def test_dicom_links_to_patient_created_by_hl7(client, ct):
    """El corazón del megaproyecto: HL7 -> FHIR crea al paciente y el DICOM se asocia al mismo."""
    r = client.post("/convert/hl7v2", params={"store": "true"}, content=HL7_ORU)
    assert r.status_code == 201
    r = upload(client, ct[0])
    assert r.json()["newPatient"] is False
    assert client.get("/Patient").json()["total"] == 1

    pid = r.json()["patient"].split("/")[1]
    types = sorted(e["resource"]["resourceType"] for e in client.get(f"/Patient/{pid}/$everything").json()["entry"])
    assert types.count("ImagingStudy") == 1
    assert "Observation" in types and "Encounter" in types


def test_invalid_uploads(client):
    assert upload(client, b"").status_code == 400
    r = upload(client, b"esto no es un DICOM")
    assert r.status_code == 400
    assert r.json()["resourceType"] == "OperationOutcome"


def test_dicom_without_patient_id_is_rejected(client, ct):
    ds = pydicom.dcmread(io.BytesIO(ct[0]))
    ds.PatientID = ""
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    r = upload(client, buffer.getvalue())
    assert r.status_code == 400 and "PatientID" in r.json()["issue"][0]["diagnostics"]


def test_study_of_another_patient_is_rejected(client, ct):
    upload(client, ct[0])
    ds = pydicom.dcmread(io.BytesIO(ct[1]))
    ds.PatientID = "99999999"
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    assert upload(client, buffer.getvalue()).status_code == 409
    assert client.get("/Patient").json()["total"] == 1  # el paciente nuevo se revirtió


# ---------- Imagen ----------
def test_frame_endpoint_returns_hounsfield_values(client, ct):
    upload(client, ct[1])
    r = client.get(f"/dicom/instances/{sop_of(ct[1])}/frames/0")
    assert r.status_code == 200
    assert r.headers["X-Rows"] == "64" and r.headers["X-Columns"] == "64"
    assert r.headers["X-Pixel-Spacing"] == "2.0,2.0"
    assert r.headers["X-Modality"] == "CT"
    assert r.headers["X-Invert"] == "0"
    values = np.frombuffer(r.content, dtype="<i2")
    assert values.size == 64 * 64
    assert values.min() < -900  # aire fuera del cuerpo
    assert values.max() > 300   # hueso
    assert values.min() == int(r.headers["X-Min"]) and values.max() == int(r.headers["X-Max"])


def test_frame_out_of_range_and_unknown_instance(client, ct):
    upload(client, ct[0])
    assert client.get(f"/dicom/instances/{sop_of(ct[0])}/frames/5").status_code == 404
    assert client.get("/dicom/instances/1.2.3.4/frames/0").status_code == 404


def test_uid_path_traversal_is_blocked(client):
    r = client.get("/dicom/instances/..%2F..%2Fetc%2Fpasswd/frames/0")
    assert r.status_code in (400, 404)
    r = client.get("/dicom/instances/abc/tags")
    assert r.status_code == 400


def test_monochrome1_sets_invert_flag(client, ct):
    ds = pydicom.dcmread(io.BytesIO(ct[0]))
    ds.PhotometricInterpretation = "MONOCHROME1"
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    upload(client, buffer.getvalue())
    r = client.get(f"/dicom/instances/{ds.SOPInstanceUID}/frames/0")
    assert r.headers["X-Invert"] == "1"


def test_multiframe_image(client, ct):
    ds = pydicom.dcmread(io.BytesIO(ct[0]))
    frames = np.stack([ds.pixel_array, ds.pixel_array // 2])
    ds.NumberOfFrames = 2
    ds.PixelData = frames.astype("<u2").tobytes()
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    upload(client, buffer.getvalue())
    first = client.get(f"/dicom/instances/{ds.SOPInstanceUID}/frames/0")
    second = client.get(f"/dicom/instances/{ds.SOPInstanceUID}/frames/1")
    assert first.headers["X-Frames"] == "2"
    assert first.content != second.content


def test_tags_endpoint_is_readable_and_has_no_pixels(client, ct):
    upload(client, ct[0])
    tags = client.get(f"/dicom/instances/{sop_of(ct[0])}/tags").json()
    by_name = {t["name"]: t["value"] for t in tags}
    assert by_name["Patient ID"] == "12345678"
    assert by_name["Modality"] == "CT"
    assert all(t["tag"] != "(7FE0,0010)" for t in tags)


def test_rgb_image_is_converted_to_gray():
    ds = pydicom.dcmread(io.BytesIO(make_chest_xray(16)))
    rgb = np.zeros((16, 16, 3), dtype=np.uint8)
    rgb[..., 0] = 200
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.PlanarConfiguration = 0
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelData = rgb.tobytes()
    ds["PixelData"].VR = "OB"
    for tag in ("WindowCenter", "WindowWidth"):
        delattr(ds, tag)
    data, info = dcm.frame_to_int16(ds, 0)
    assert data.shape == (16, 16) and int(data[0, 0]) == round(200 * 0.299)


# ---------- Listado, visor y borrado ----------
def test_studies_listing_for_viewer(client, ct):
    for raw in ct:
        upload(client, raw)
    studies = client.get("/dicom/studies").json()
    assert len(studies) == 1
    study = studies[0]
    assert study["patient"]["name"] == "Pérez, Ana María"
    assert study["patient"]["identifier"] == "12345678"
    assert study["modalities"] == ["CT"]
    assert len(study["series"][0]["instances"]) == 4


def test_viewer_page_is_served(client):
    r = client.get("/viewer")
    assert r.status_code == 200 and "Visor DICOM" in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/viewer"


def test_load_samples_endpoint(client):
    r = client.post("/dicom/samples")
    assert r.status_code == 200
    first = r.json()
    assert first["loaded"] == 17 and first["skipped"] == 0
    again = client.post("/dicom/samples").json()
    assert again == {"loaded": 0, "skipped": 17}
    assert client.get("/ImagingStudy").json()["total"] == 2


def test_deleting_study_removes_files_and_patient_is_protected(client, ct):
    body = upload(client, ct[0]).json()
    path = dcm.DICOM_DIR / f"{body['instance']}.dcm"
    assert path.is_file()
    assert client.delete("/" + body["patient"]).status_code == 409  # tiene un estudio
    assert client.delete("/" + body["imagingStudy"]).status_code == 204
    assert not path.exists()
    assert client.delete("/" + body["patient"]).status_code == 204


def test_imaging_study_validation_through_generic_api(client):
    r = client.post("/ImagingStudy", json={"resourceType": "ImagingStudy", "status": "available"})
    assert r.status_code == 400
    r = client.post(
        "/ImagingStudy",
        json={"resourceType": "ImagingStudy", "status": "available", "subject": {"reference": "Patient/no-existe"}},
    )
    assert r.status_code == 422
