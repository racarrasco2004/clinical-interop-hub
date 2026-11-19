def new_patient(client, family="Pérez", given="Ana", gender="female"):
    return client.post(
        "/Patient",
        json={
            "resourceType": "Patient",
            "name": [{"family": family, "given": [given]}],
            "gender": gender,
            "birthDate": "1990-05-12",
        },
    )


def observation(pid, encounter_id=None):
    body = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
        "subject": {"reference": f"Patient/{pid}"},
        "valueQuantity": {"value": 72, "unit": "beats/minute"},
    }
    if encounter_id:
        body["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    return body


def encounter(pid, status="in-progress"):
    return {
        "resourceType": "Encounter",
        "status": status,
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": f"Patient/{pid}"},
    }


def condition(pid):
    return {
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "J18.9"}]},
        "subject": {"reference": f"Patient/{pid}"},
    }


# ---------- Patient ----------
def test_metadata(client):
    r = client.get("/metadata")
    assert r.status_code == 200
    body = r.json()
    assert body["resourceType"] == "CapabilityStatement"
    types = {res["type"] for res in body["rest"][0]["resource"]}
    assert types == {"Patient", "Encounter", "Observation", "Condition", "ImagingStudy"}


def test_create_and_read_patient(client):
    r = new_patient(client)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.headers["location"] == f"/Patient/{pid}"

    got = client.get(f"/Patient/{pid}")
    assert got.status_code == 200
    assert got.json()["name"][0]["family"] == "Pérez"
    assert got.json()["meta"]["versionId"] == "1"


def test_invalid_patient_returns_operation_outcome(client):
    r = client.post("/Patient", json={"resourceType": "Patient"})
    assert r.status_code == 400
    assert r.json()["resourceType"] == "OperationOutcome"


def test_wrong_resource_type_in_body(client):
    r = client.post("/Patient", json={"resourceType": "Observation"})
    assert r.status_code == 400


def test_search_patient_by_name_and_gender(client):
    new_patient(client, "Soto", "Luis", "male")
    new_patient(client, "Rojas", "Marta", "female")

    assert client.get("/Patient", params={"name": "soto"}).json()["total"] == 1
    assert client.get("/Patient", params={"gender": "female"}).json()["total"] == 1


def test_update_increments_version(client):
    pid = new_patient(client).json()["id"]
    body = {
        "resourceType": "Patient",
        "name": [{"family": "Pérez", "given": ["Ana María"]}],
        "gender": "female",
    }
    r = client.put(f"/Patient/{pid}", json=body)
    assert r.status_code == 200
    assert r.json()["meta"]["versionId"] == "2"


def test_update_missing_resource_is_404(client):
    body = {"resourceType": "Patient", "name": [{"family": "X"}]}
    assert client.put("/Patient/no-existe", json=body).status_code == 404


def test_delete_patient(client):
    pid = new_patient(client).json()["id"]
    assert client.delete(f"/Patient/{pid}").status_code == 204
    assert client.get(f"/Patient/{pid}").status_code == 404


def test_unsupported_resource_returns_operation_outcome(client):
    r = client.get("/Medication")
    assert r.status_code == 404
    assert r.json()["resourceType"] == "OperationOutcome"


# ---------- Observation ----------
def test_observation_requires_existing_patient(client):
    r = client.post("/Observation", json=observation("no-existe"))
    assert r.status_code == 422


def test_observation_invalid_status(client):
    pid = new_patient(client).json()["id"]
    body = observation(pid)
    body["status"] = "inventado"
    assert client.post("/Observation", json=body).status_code == 400


def test_create_and_search_observation_by_patient_and_code(client):
    pid = new_patient(client).json()["id"]
    assert client.post("/Observation", json=observation(pid)).status_code == 201

    r = client.get("/Observation", params={"patient": pid, "code": "8867-4"})
    assert r.json()["total"] == 1
    r = client.get("/Observation", params={"patient": pid, "code": "0000-0"})
    assert r.json()["total"] == 0


# ---------- Encounter y Condition ----------
def test_encounter_crud_and_search(client):
    pid = new_patient(client).json()["id"]
    r = client.post("/Encounter", json=encounter(pid))
    assert r.status_code == 201
    eid = r.json()["id"]

    assert client.get("/Encounter", params={"patient": pid}).json()["total"] == 1
    assert client.get("/Encounter", params={"status": "finished"}).json()["total"] == 0

    updated = encounter(pid, status="finished")
    r = client.put(f"/Encounter/{eid}", json=updated)
    assert r.status_code == 200
    assert client.get("/Encounter", params={"status": "finished"}).json()["total"] == 1


def test_encounter_requires_class(client):
    pid = new_patient(client).json()["id"]
    body = encounter(pid)
    del body["class"]
    assert client.post("/Encounter", json=body).status_code == 400


def test_condition_create_and_search(client):
    pid = new_patient(client).json()["id"]
    assert client.post("/Condition", json=condition(pid)).status_code == 201
    r = client.get("/Condition", params={"patient": pid, "code": "J18.9"})
    assert r.json()["total"] == 1


def test_condition_requires_patient(client):
    assert client.post("/Condition", json=condition("no-existe")).status_code == 422


def test_observation_with_encounter_checks_it_exists(client):
    pid = new_patient(client).json()["id"]
    assert client.post("/Observation", json=observation(pid, "no-existe")).status_code == 422

    eid = client.post("/Encounter", json=encounter(pid)).json()["id"]
    assert client.post("/Observation", json=observation(pid, eid)).status_code == 201


# ---------- Integridad al borrar ----------
def test_cannot_delete_patient_with_records(client):
    pid = new_patient(client).json()["id"]
    client.post("/Condition", json=condition(pid))
    r = client.delete(f"/Patient/{pid}")
    assert r.status_code == 409
    assert client.get(f"/Patient/{pid}").status_code == 200


# ---------- $everything ----------
def test_patient_everything(client):
    pid = new_patient(client).json()["id"]
    other = new_patient(client, "Soto", "Luis", "male").json()["id"]
    eid = client.post("/Encounter", json=encounter(pid)).json()["id"]
    client.post("/Observation", json=observation(pid, eid))
    client.post("/Condition", json=condition(pid))
    client.post("/Condition", json=condition(other))

    r = client.get(f"/Patient/{pid}/$everything")
    assert r.status_code == 200
    types = [e["resource"]["resourceType"] for e in r.json()["entry"]]
    assert types[0] == "Patient"
    assert sorted(types) == ["Condition", "Encounter", "Observation", "Patient"]


def test_everything_unknown_patient(client):
    assert client.get("/Patient/no-existe/$everything").status_code == 404


# ---------- Transacciones ----------
def transaction_bundle():
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": "urn:uuid:obs-1",
                "resource": {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"coding": [{"code": "8867-4"}]},
                    "subject": {"reference": "urn:uuid:pat-1"},
                    "encounter": {"reference": "urn:uuid:enc-1"},
                },
            },
            {
                "fullUrl": "urn:uuid:enc-1",
                "resource": {
                    "resourceType": "Encounter",
                    "status": "finished",
                    "class": {"code": "AMB"},
                    "subject": {"reference": "urn:uuid:pat-1"},
                },
            },
            {
                "fullUrl": "urn:uuid:pat-1",
                "resource": {"resourceType": "Patient", "name": [{"family": "Díaz"}]},
            },
        ],
    }


def test_transaction_resolves_references_even_if_unordered(client):
    r = client.post("/", json=transaction_bundle())
    assert r.status_code == 200
    assert r.json()["type"] == "transaction-response"

    obs = client.get("/Observation").json()["entry"][0]["resource"]
    assert obs["subject"]["reference"].startswith("Patient/")
    assert obs["encounter"]["reference"].startswith("Encounter/")


def test_transaction_is_atomic(client):
    bundle = transaction_bundle()
    bundle["entry"].append(
        {"fullUrl": "urn:uuid:bad", "resource": {"resourceType": "Condition", "subject": {"reference": "urn:uuid:pat-1"}}}
    )  # Condition sin 'code' -> inválida
    r = client.post("/", json=bundle)
    assert r.status_code == 400
    for t in ("Patient", "Encounter", "Observation", "Condition"):
        assert client.get(f"/{t}").json()["total"] == 0


def test_transaction_rejects_wrong_bundle_type(client):
    r = client.post("/", json={"resourceType": "Bundle", "type": "collection", "entry": []})
    assert r.status_code == 400


# ---------- Conversión HL7 v2 ----------
ORU = (
    "MSH|^~\\&|LIS|DEMO|API|API|20261118091000||ORU^R01|MSG1|P|2.5\r"
    "PID|1||999^^^DEMO^MR||Soto^Luis||19800101|M\r"
    "PV1|1|O\r"
    "OBX|1|NM|8867-4^Frecuencia cardiaca^LN||80|/min|60-100|N|||F\r"
)


def test_convert_endpoint_only_converts(client):
    r = client.post("/convert/hl7v2", content=ORU)
    assert r.status_code == 200
    assert r.json()["type"] == "transaction"
    assert client.get("/Patient").json()["total"] == 0


def test_convert_endpoint_with_store(client):
    r = client.post("/convert/hl7v2", params={"store": "true"}, content=ORU)
    assert r.status_code == 201
    assert client.get("/Patient").json()["total"] == 1
    assert client.get("/Observation").json()["total"] == 1
    assert client.get("/Encounter").json()["total"] == 1


def test_convert_store_reuses_existing_patient(client):
    client.post("/convert/hl7v2", params={"store": "true"}, content=ORU)
    client.post("/convert/hl7v2", params={"store": "true"}, content=ORU)
    assert client.get("/Patient").json()["total"] == 1
    assert client.get("/Observation").json()["total"] == 2
    pid = client.get("/Patient").json()["entry"][0]["resource"]["id"]
    assert client.get(f"/Patient/{pid}/$everything").json()["total"] == 5  # 1 paciente + 2x(encuentro+obs)


def test_convert_endpoint_rejects_garbage(client):
    r = client.post("/convert/hl7v2", content="esto no es hl7")
    assert r.status_code == 400
    assert r.json()["resourceType"] == "OperationOutcome"
