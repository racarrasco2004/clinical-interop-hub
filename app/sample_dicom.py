"""Genera imágenes DICOM FICTICIAS para probar el visor (no son de ningún paciente real).

- Una TC de tórax sintética (serie axial de varios cortes).
- Una radiografía de tórax sintética (una imagen).

El paciente coincide con el del mensaje HL7 de samples/hl7 (ficha 12345678, HOSPITAL_DEMO),
así el DICOM se vincula al mismo paciente que creó el conversor.

Uso:  python -m app.sample_dicom            (escribe en samples/dicom/)
"""
import io
import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

UID_ROOT = "1.2.826.0.1.3680043.8.498."
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
CR_STORAGE = "1.2.840.10008.5.1.4.1.1.1"


def _uid(*parts: str) -> str:
    """UID determinista: regenerar los ejemplos produce los mismos identificadores."""
    return generate_uid(prefix=UID_ROOT, entropy_srcs=list(parts))


def _ellipse(yy, xx, cy, cx, ry, rx):
    return ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2 <= 1.0


def ct_slice_hu(size: int, z: float, rng: np.random.Generator) -> np.ndarray:
    """Un corte axial de tórax ficticio en unidades Hounsfield. z va de 0 (arriba) a 1 (abajo)."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = size / 2
    hu = np.full((size, size), -1000.0, dtype=np.float32)  # aire

    body = _ellipse(yy, xx, c, c, size * 0.40, size * 0.47)
    hu[body] = 40.0  # tejido blando
    skin = body & ~_ellipse(yy, xx, c, c, size * 0.385, size * 0.455)
    hu[skin] = 20.0

    lung_h = 0.26 * (1 - 0.7 * abs(z - 0.45))  # los pulmones se achican hacia los extremos
    for side in (-1, 1):
        lung = _ellipse(yy, xx, c - size * 0.02, c + side * size * 0.19, size * lung_h * 1.2, size * 0.15)
        hu[lung] = -820.0
    mediastinum = _ellipse(yy, xx, c + size * 0.02, c, size * 0.13, size * 0.07)
    hu[mediastinum] = 45.0
    heart = _ellipse(yy, xx, c + size * 0.06, c - size * 0.05, size * 0.12 * (1 - z * 0.3), size * 0.11)
    hu[heart] = 55.0

    spine = _ellipse(yy, xx, c + size * 0.30, c, size * 0.06, size * 0.05)
    hu[spine] = 650.0
    canal = _ellipse(yy, xx, c + size * 0.30, c, size * 0.02, size * 0.02)
    hu[canal] = 25.0

    # Costillas: un anillo de hueso en el borde del cuerpo
    ring = _ellipse(yy, xx, c, c, size * 0.37, size * 0.44) & ~_ellipse(yy, xx, c, c, size * 0.345, size * 0.41)
    ribs = ring & (((np.arctan2(yy - c, xx - c) * 9) % (2 * np.pi)) < 2.2)
    hu[ribs] = 500.0

    # Un nódulo esférico en el pulmón derecho, visible solo en algunos cortes
    nodule_z = 0.5
    radius = size * 0.045 * np.sqrt(max(0.0, 1 - ((z - nodule_z) / 0.2) ** 2))
    if radius > 0.5:
        nodule = (yy - (c - size * 0.03)) ** 2 + (xx - (c + size * 0.21)) ** 2 <= radius**2
        hu[nodule] = 30.0

    hu += rng.normal(0, 8, hu.shape).astype(np.float32)  # ruido
    return hu


def _base_dataset(sop_class: str, sop_uid: str, study_uid: str, series_uid: str) -> pydicom.Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = pydicom.uid.PYDICOM_IMPLEMENTATION_UID

    ds = pydicom.Dataset()
    ds.file_meta = meta
    ds.SpecificCharacterSet = "ISO_IR 192"  # UTF-8: permite tildes
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid

    ds.PatientName = "Pérez^Ana^María"
    ds.PatientID = "12345678"
    ds.IssuerOfPatientID = "HOSPITAL_DEMO"
    ds.PatientBirthDate = "19900512"
    ds.PatientSex = "F"
    ds.StudyDate = "20261118"
    ds.StudyTime = "101500"
    ds.AccessionNumber = "ACC0001"
    ds.ReferringPhysicianName = ""
    ds.StudyID = "1"
    ds.Manufacturer = "DEMO (datos ficticios)"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    return ds


def _serialize(ds: pydicom.Dataset) -> bytes:
    buffer = io.BytesIO()
    ds.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


def make_ct_series(slices: int = 16, size: int = 192) -> list[bytes]:
    """TC de tórax ficticia: lista de archivos DICOM (uno por corte)."""
    study_uid = _uid("demo", "study", "ct")
    series_uid = _uid("demo", "series", "ct")
    rng = np.random.default_rng(42)
    files = []
    for i in range(slices):
        z = i / max(1, slices - 1)
        hu = ct_slice_hu(size, z, rng)
        stored = np.clip(np.rint(hu + 1024), 0, 65535).astype("<u2")  # HU = valor - 1024

        ds = _base_dataset(CT_STORAGE, _uid("demo", "ct", str(i)), study_uid, series_uid)
        ds.Modality = "CT"
        ds.StudyDescription = "TC de tórax (ficticia)"
        ds.SeriesDescription = "Axial 5 mm"
        ds.BodyPartExamined = "CHEST"
        ds.SeriesNumber = 2
        ds.InstanceNumber = i + 1
        ds.ImagePositionPatient = [-size, -size, float(i * 5)]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.SliceThickness = 5
        ds.SpacingBetweenSlices = 5
        ds.SliceLocation = float(i * 5)
        ds.PixelSpacing = [2.0, 2.0]
        ds.Rows = ds.Columns = size
        ds.RescaleIntercept = -1024
        ds.RescaleSlope = 1
        ds.RescaleType = "HU"
        ds.WindowCenter = 40
        ds.WindowWidth = 400
        ds.PixelData = stored.tobytes()
        ds["PixelData"].VR = "OW"
        files.append(_serialize(ds))
    return files


def make_chest_xray(size: int = 256) -> bytes:
    """Radiografía de tórax ficticia (una imagen)."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = size / 2
    rng = np.random.default_rng(7)
    img = np.full((size, size), 600.0, dtype=np.float32)  # fondo
    body = _ellipse(yy, xx, c, c, size * 0.48, size * 0.40)
    img[body] = 2200.0
    for side in (-1, 1):
        lung = _ellipse(yy, xx, c - size * 0.03, c + side * size * 0.17, size * 0.30, size * 0.13)
        img[lung] = 1300.0
    img[_ellipse(yy, xx, c + size * 0.05, c, size * 0.45, size * 0.045)] = 2900.0  # columna / mediastino
    img[_ellipse(yy, xx, c + size * 0.08, c - size * 0.06, size * 0.11, size * 0.09)] = 2700.0  # corazón
    for k in range(8):  # costillas
        y = size * (0.22 + k * 0.065)
        for side in (-1, 1):
            rib = _ellipse(yy, xx, y, c + side * size * 0.17, size * 0.012, size * 0.15)
            img[rib & body] += 450.0
    img += rng.normal(0, 25, img.shape).astype(np.float32)

    ds = _base_dataset(CR_STORAGE, _uid("demo", "cr", "0"), _uid("demo", "study", "cr"), _uid("demo", "series", "cr"))
    ds.Modality = "CR"
    ds.StudyDescription = "Radiografía de tórax (ficticia)"
    ds.SeriesDescription = "PA"
    ds.BodyPartExamined = "CHEST"
    ds.StudyTime = "093000"
    ds.AccessionNumber = "ACC0002"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.ImagerPixelSpacing = [1.4, 1.4]
    ds.Rows = ds.Columns = size
    ds.WindowCenter = 1800
    ds.WindowWidth = 2600
    ds.PixelData = np.clip(np.rint(img), 0, 4095).astype("<u2").tobytes()
    ds["PixelData"].VR = "OW"
    return _serialize(ds)


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "samples" / "dicom"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.dcm"):
        old.unlink()
    for i, data in enumerate(make_ct_series(), start=1):
        (out / f"tc_torax_{i:02d}.dcm").write_bytes(data)
    (out / "rx_torax.dcm").write_bytes(make_chest_xray())
    print(f"Listo: {len(list(out.glob('*.dcm')))} archivos DICOM en {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
