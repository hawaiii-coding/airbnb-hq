"""GCS-backed backup sink and state store (lazy google-cloud-storage import; `client` injectable for tests)."""
from __future__ import annotations

import json
from typing import Optional

from . import writer


def _client(client):
    if client is not None:
        return client
    from google.cloud import storage  # pragma: no cover
    return storage.Client()


def split_uri(uri: str):
    assert uri.startswith("gs://"), uri
    bucket, _, prefix = uri[5:].partition("/")
    return bucket, prefix.strip("/")


class GcsBackupSink:
    """Writes the tab as JSON, reads it back and compares before reporting success."""

    def __init__(self, uri: str, client=None):
        self.bucket_name, self.prefix = split_uri(uri)
        self.client = _client(client)

    def __call__(self, label, rows):
        blob = self.client.bucket(self.bucket_name).blob(f"{self.prefix}/backups/{label}.json")
        payload = json.dumps({"header": writer.HEADER, "rows": rows})
        blob.upload_from_string(payload, content_type="application/json")
        if json.loads(blob.download_as_text())["rows"] != rows:
            raise RuntimeError("GCS backup verification failed")
        return f"gs://{self.bucket_name}/{blob.name}"


class GcsState:
    def __init__(self, uri: str, client=None):
        self.bucket_name, self.prefix = split_uri(uri)
        self.client = _client(client)

    def _blob(self):
        return self.client.bucket(self.bucket_name).blob(f"{self.prefix}/state.json")

    def load(self) -> dict:
        b = self._blob()
        return json.loads(b.download_as_text()) if b.exists() else {"miss_counts": {}}

    def save(self, state: dict):
        self._blob().upload_from_string(json.dumps(state), content_type="application/json")
