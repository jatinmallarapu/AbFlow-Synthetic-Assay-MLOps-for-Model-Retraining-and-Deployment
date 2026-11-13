"""Run a Prefect flow on a batch JSON file or two deterministic demo batches."""
import argparse
import json
from pathlib import Path
from uuid import UUID

import numpy as np
from prefect import flow, task
from app import Batch, process


@task
def validate(payload):
    return Batch.model_validate(payload)


@task
def retrain_and_gate(batch):
    return process(batch)


@flow(name="synthetic-observation-ingestion")
def ingest(payload):
    return retrain_and_gate(validate(payload))


def demo_batch(seed, bias):
    features = np.random.default_rng(seed).uniform(-1, 1, (32, 3))
    targets = features @ np.array([2., -1., .75]) + .4 + bias
    return {"id": str(UUID(int=seed)), "observations": [
        {"features": row.tolist(), "target": float(target)}
        for row, target in zip(features, targets)
    ]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path)
    args = parser.parse_args()
    payloads = [json.loads(args.file.read_text(encoding="utf-8"))] if args.file else [
        demo_batch(101, .3), demo_batch(102, 0),
    ]
    print(json.dumps([ingest(payload) for payload in payloads], indent=2))
