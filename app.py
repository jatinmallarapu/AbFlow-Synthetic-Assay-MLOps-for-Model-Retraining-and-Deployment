"""Versioned regression and promotion for numerical simulator observations."""
import hashlib
import json
import os
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException
import numpy as np
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

Number = Annotated[FiniteFloat, Field(strict=True, ge=-100, le=100)]
Vector = tuple[Number, Number, Number]
LOCK = 18733124

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id uuid PRIMARY KEY, digest text NOT NULL, report jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  batch_id uuid REFERENCES batches(id), ordinal integer,
  features jsonb NOT NULL, target double precision NOT NULL,
  PRIMARY KEY (batch_id, ordinal)
);
CREATE TABLE IF NOT EXISTS models (
  version bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES batches(id),
  rmse double precision NOT NULL, weights jsonb NOT NULL
);
"""


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: Vector
    target: Number


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    observations: Annotated[list[Observation], Field(min_length=8, max_length=1000)]

    @model_validator(mode="after")
    def reject_duplicate_designs(self):
        if len({row.features for row in self.observations}) != len(self.observations):
            raise ValueError("Duplicate design within batch")
        return self


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: Vector


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"])


def matrix(features):
    return np.c_[np.ones(len(features)), features]


def holdout():
    x = np.random.default_rng(313).uniform(-1, 1, size=(128, 3))
    return matrix(x), x @ np.array([2., -1., .75]) + .4


def process(batch):
    payload = batch.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(statement)
        existing = conn.execute("SELECT digest, report FROM batches WHERE id = %s", (batch.id,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Batch ID already belongs to different data")
            return existing[1]
        history = conn.execute("SELECT features, target FROM observations ORDER BY batch_id, ordinal").fetchall()
        incoming = [(row.features, row.target) for row in batch.observations]
        x, y = zip(*(history + incoming))
        weights = np.linalg.lstsq(matrix(x), np.array(y), rcond=None)[0]
        test_x, test_y = holdout()
        rmse = float(np.sqrt(np.mean((test_x @ weights - test_y) ** 2)))
        champion = conn.execute("SELECT version, rmse FROM models ORDER BY version DESC LIMIT 1").fetchone()
        promote = champion is None or rmse < champion[1] - .01
        drift = None
        if history:
            previous = np.array([row[0] for row in history])
            latest = np.array([row[0] for row in incoming])
            drift = float(np.max(abs(latest.mean(0) - previous.mean(0)) /
                                 np.maximum(previous.std(0), .001)))
        report = {"batch_id": str(batch.id), "candidate_rmse": rmse,
                  "training_rows": len(y), "promoted": promote, "max_standardized_mean_shift": drift,
                  "drift_flag": drift is not None and drift > 1.0}
        conn.execute("INSERT INTO batches VALUES (%s, %s, %s)", (batch.id, digest, Jsonb(report)))
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO observations VALUES (%s, %s, %s, %s)",
                               [(batch.id, i, Jsonb(list(a)), b) for i, (a, b) in enumerate(incoming)])
        if promote:
            champion_version = conn.execute(
                "INSERT INTO models (batch_id, rmse, weights) VALUES (%s, %s, %s) RETURNING version",
                (batch.id, rmse, Jsonb(weights.tolist())),
            ).fetchone()[0]
        else:
            champion_version = champion[0]
        report["champion_version"] = champion_version
        conn.execute("UPDATE batches SET report = %s WHERE id = %s", (Jsonb(report), batch.id))
        return report


api = FastAPI(title="AbFlow synthetic regression serving")


@api.get("/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@api.post("/predict")
def predict(request: PredictionRequest):
    with connect() as conn:
        try:
            row = conn.execute("SELECT version, weights FROM models ORDER BY version DESC LIMIT 1").fetchone()
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            row = None
        if row is None:
            raise HTTPException(503, "No champion model")
        value = float(np.array((1., *request.features)) @ np.array(row[1]))
        return {"model_version": row[0], "prediction": value}
