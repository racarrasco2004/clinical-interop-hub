import os
import shutil
import tempfile

# Base de datos y carpeta de imágenes de PRUEBA, definidas ANTES de importar la app,
# para no tocar fhir.db ni dicom_files/ reales.
_tmp = tempfile.TemporaryDirectory()
os.environ["FHIR_DB"] = os.path.join(_tmp.name, "test_fhir.db")
os.environ["DICOM_DIR"] = os.path.join(_tmp.name, "dicom")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    with main.get_conn() as conn:
        conn.execute("DELETE FROM resources")
    shutil.rmtree(os.environ["DICOM_DIR"], ignore_errors=True)
    yield


@pytest.fixture
def client():
    return TestClient(main.app)
