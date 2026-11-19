"""Tests del dashboard clínico y de los datos de demostración."""


def test_dashboard_page(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Dashboard clínico" in r.text


def test_empty_state(client):
    d = client.get("/dashboard/data").json()
    assert d["totals"]["encounters"] == 0
    assert d["kpis"]["encounters"]["value"] == 0
    assert d["alerts"] == []


def test_seed_and_data(client):
    r = client.post("/dashboard/seed?patients=200")
    assert r.status_code == 200
    assert r.json()["created"]["Patient"] == 200

    d = client.get("/dashboard/data?days=30&capacity=60").json()
    assert d["days"] == 30
    assert d["kpis"]["encounters"]["value"] > 0
    assert len(d["daily"]["dates"]) == 30
    assert len(d["daily"]["EMER"]) == 30
    assert len(d["heatmap"]["matrix"]) == 7 and len(d["heatmap"]["matrix"][0]) == 24
    assert len(d["age_bands"]) == 5
    assert d["synthetic_resources"] > 0
    assert 0 <= d["kpis"]["occupancy_now"]["value"]


def test_seed_is_deterministic_and_idempotent(client):
    a = client.post("/dashboard/seed?patients=100").json()["created"]
    b = client.post("/dashboard/seed?patients=100").json()
    assert b["removed"] > 0               # reemplaza, no duplica
    assert b["created"] == a              # misma semilla, mismos datos
    assert client.get("/Patient?_count=1").json()["total"] == 100


def test_capacity_changes_occupancy(client):
    client.post("/dashboard/seed?patients=100")
    small = client.get("/dashboard/data?capacity=10").json()["kpis"]["occupancy_now"]["value"]
    big = client.get("/dashboard/data?capacity=1000").json()["kpis"]["occupancy_now"]["value"]
    assert small > big


def test_clear_only_removes_synthetic(client):
    client.post("/dashboard/seed?patients=100")
    real = client.post("/Patient", json={"resourceType": "Patient", "name": [{"family": "Real"}]})
    assert real.status_code == 201
    r = client.delete("/dashboard/seed")
    assert r.status_code == 200
    assert client.get("/dashboard/data").json()["synthetic_resources"] == 0
    assert client.get(f"/Patient/{real.json()['id']}").status_code == 200


def test_synthetic_imaging_hidden_from_dicom_list(client):
    client.post("/dashboard/seed?patients=100")
    assert client.get("/dicom/studies").json() == []


def test_query_validation(client):
    assert client.get("/dashboard/data?days=3").status_code == 422
    assert client.get("/dashboard/data?capacity=0").status_code == 422
    assert client.post("/dashboard/seed?patients=5").status_code == 422
