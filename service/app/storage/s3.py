"""S3-compatible object storage backend (AWS S3 / MinIO).

Implements the :class:`~app.storage.base.VideoStorage` protocol. The boto3
client is injectable so tests can drive it with an in-memory fake instead of a
real bucket (design source R4 / R10).

``boto3`` is imported lazily (only when a real client is constructed) so the
module imports cleanly in environments where S3 is not used.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import BinaryIO

from app.config import settings
from app.storage.base import VideoStorage


class S3VideoStorage:
    """Store videos as objects in an S3-compatible bucket."""

    def __init__(
        self,
        client=None,
        bucket: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._bucket = bucket or settings.s3_bucket
        self._endpoint = endpoint or settings.s3_endpoint
        if client is None:
            import boto3
            import botocore

            # Bound the client timeouts so an unreachable/slow S3 endpoint fails
            # fast (seconds, not the 60s boto3 default). This keeps /healthz and
            # normal storage ops from hanging when MinIO is down (R8/R11).
            client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                config=botocore.config.Config(connect_timeout=3, read_timeout=5),
            )
        self._client = client

    def save(self, task_id: str, src: Path) -> str:
        key = f"{task_id}.mp4"
        with open(src, "rb") as fh:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=fh.read())
        return key

    def open(self, key: str) -> tuple[BinaryIO, int]:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        data = resp["Body"].read()
        size = resp["ContentLength"]
        return io.BytesIO(data), size

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def presigned_url(self, key: str, expires: int = 3600) -> str | None:
        """Return a time-limited download URL, or None if it cannot be built."""
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires,
            )
        except Exception:
            return None
