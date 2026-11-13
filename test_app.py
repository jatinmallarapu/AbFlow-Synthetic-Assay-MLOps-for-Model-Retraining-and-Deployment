import os

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app import Batch, api, connect, process
from pipeline import demo_batch, ingest


def test_validator_rejects_extra_fields_and_duplicates():
    payload = demo_batch(101, .3)
    payload["observations"][0]["unexpected"] = 1
    with pytest.raises(ValidationError):
        Batch.model_validate(payload)
    payload = demo_batch(101, .3)
    payload["observations"][0] = payload["observations"][1]
    with pytest.raises(ValidationError, match="Duplicate design"):
        Batch.model_validate(payload)


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Requires PostgreSQL")
def test_pipeline_and_serving(monkeypatch):
    with connect() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS abflow_test")
    from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
    parts = urlsplit(os.environ["DATABASE_URL"])
    query = dict(parse_qsl(parts.query))
    query["options"] = "-c search_path=abflow_test"
    monkeypatch.setenv("DATABASE_URL", urlunsplit(parts._replace(query=urlencode(query, quote_via=quote))))
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS models, observations, batches")
    client = TestClient(api)
    assert client.post("/predict", json={"features": [0, 0, 0]}).status_code == 503
    initial = ingest(demo_batch(101, .3))
    assert initial["promoted"]
    replay = process(Batch.model_validate(demo_batch(101, .3)))
    assert replay == initial
    with pytest.raises(ValueError, match="already belongs"):
        process(Batch.model_validate(demo_batch(101, .2)))
    improved = ingest(demo_batch(102, 0))
    assert improved["promoted"] and improved["candidate_rmse"] < initial["candidate_rmse"]
    rejected = ingest(demo_batch(103, 3))
    assert not rejected["promoted"] and rejected["champion_version"] == improved["champion_version"]
    response = client.post("/predict", json={"features": [0, 0, 0]})
    assert response.status_code == 200
    assert response.json()["model_version"] == improved["champion_version"]
    assert client.post("/predict", json={"features": [0, 0]}).status_code == 422
