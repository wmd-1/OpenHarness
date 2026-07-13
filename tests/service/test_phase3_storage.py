"""Phase 3: object-storage abstraction + /file default 302 redirect.

TDD-first: this module exercises behavior that does not exist yet
(presigned_url on the Protocol + LocalVideoStorage, storage_for_kind, and the
302 redirect branch in download_video), so it fails red before the production
code lands.

Three concerns:
  1. S3VideoStorage.delete/exists/presigned_url all implemented (fake S3 client).
  2. LocalVideoStorage.presigned_url returns None.
  3. GET /v1/videos/{id}/file returns 302 + Location when storage_kind=s3
     (default mode=redirect), and falls back to a 200 stream for ?mode=stream
     or storage_kind=local / presigned=None.
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.deps import get_db
from app.main import app
from app.models import Base, TaskStatus, VideoTask
from app.storage.local import LocalVideoStorage
from app.storage.s3 import S3VideoStorage


# ---------------------------------------------------------------------------
# Fake S3 client (in-memory) for unit-testing S3VideoStorage without boto3/moto.
# ---------------------------------------------------------------------------


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body if isinstance(Body, (bytes, bytearray)) else Body.read()
        return {}

    def get_object(self, Bucket, Key):
        data = self.objects[Key]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Exception("NoSuchKey")

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=3600):
        p = Params or {}
        return f"https://s3.example/{p.get('Bucket')}/{p.get('Key')}?sig=xyz&e={ExpiresIn}"


def _make_s3() -> S3VideoStorage:
    return S3VideoStorage(client=FakeS3(), bucket="test-bucket", endpoint="http://minio")


# ---------------------------------------------------------------------------
# 1. S3VideoStorage unit tests (delete / exists / presigned_url).
# ---------------------------------------------------------------------------


def test_s3_save_open_roundtrip():
    s3 = _make_s3()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
        fh.write(b"VIDEO-DATA")
        path = fh.name
    try:
        key = s3.save("t1", __import__("pathlib").Path(path))
        assert key == "t1.mp4"
        assert s3.exists(key) is True
        obj, size = s3.open(key)
        assert obj.read() == b"VIDEO-DATA"
        assert size == len(b"VIDEO-DATA")
    finally:
        os.unlink(path)


def test_s3_delete():
    s3 = _make_s3()
    s3._client.objects["k.mp4"] = b"x"
    assert s3.exists("k.mp4") is True
    s3.delete("k.mp4")
    assert s3.exists("k.mp4") is False


def test_s3_exists_false_when_missing():
    s3 = _make_s3()
    assert s3.exists("nope.mp4") is False


def test_s3_presigned_url_returns_url():
    s3 = _make_s3()
    url = s3.presigned_url("t1.mp4", expires=1800)
    assert url is not None
    assert "t1.mp4" in url
    assert "e=1800" in url


# ---------------------------------------------------------------------------
# 2. LocalVideoStorage.presigned_url returns None.
# ---------------------------------------------------------------------------


def test_local_presigned_url_is_none(tmp_path):
    store = LocalVideoStorage(root=tmp_path)
    assert store.presigned_url("x.mp4") is None


# ---------------------------------------------------------------------------
# 3. GET /v1/videos/{id}/file redirect vs stream (endpoint via TestClient).
# ---------------------------------------------------------------------------


@pytest.fixture
def client_and_db():
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    make_session = async_sessionmaker(eng, expire_on_commit=False)

    async def _override_get_db():
        async with make_session() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _seed(kind: str):
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with make_session() as s:
            t = VideoTask(
                id=uuid.uuid4(),
                prompt="p",
                status=TaskStatus.SUCCEEDED,
                storage_kind=kind,
                output_path=f"{kind}-key.mp4",
            )
            s.add(t)
            await s.commit()
            return t.id, kind

    def seed(kind: str):
        return loop.run_until_complete(_seed(kind))

    with TestClient(app) as client:
        yield client, make_session, seed

    app.dependency_overrides.clear()
    loop.close()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _read(make_session, tid):
    async def _go():
        async with make_session() as s:
            return await s.get(VideoTask, tid)

    return asyncio.get_event_loop().run_until_complete(_go())


def test_file_redirects_when_s3(client_and_db, monkeypatch):
    client, make_session, seed = client_and_db
    tid, _kind = seed("s3")

    # Router resolves storage via storage_for_kind(task.storage_kind); give it
    # a fake S3 so presigned_url returns a URL (no real MinIO needed).
    fake_s3 = _make_s3()
    monkeypatch.setattr(
        "app.routers.videos.storage_for_kind",
        lambda k: fake_s3,
    )

    # NOTE: TestClient follows redirects by default; we must NOT follow here
    # or it will try to fetch the (unreachable) presigned URL and surface a
    # 404. We are asserting the API box returns the 302 itself.
    resp = client.get(f"/v1/videos/{tid}/file", follow_redirects=False)  # default mode=redirect
    assert resp.status_code == 302, resp.status_code
    assert resp.headers["Location"].startswith("https://s3.example/")
    assert "s3-key.mp4" in resp.headers["Location"]


def test_file_streams_when_mode_stream(client_and_db, monkeypatch):
    client, make_session, seed = client_and_db
    tid, kind = seed("s3")

    fake_s3 = _make_s3()
    # The streaming fallback reads the artifact back from S3, so the object
    # must exist in the fake bucket (otherwise open() raises -> 404/500).
    fake_s3._client.objects["s3-key.mp4"] = b"STREAM-DATA"
    monkeypatch.setattr(
        "app.routers.videos.storage_for_kind",
        lambda k: fake_s3,
    )

    resp = client.get(f"/v1/videos/{tid}/file?mode=stream")
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["Content-Type"] == "video/mp4"
    assert resp.content == b"STREAM-DATA"


def test_file_streams_when_local(client_and_db, monkeypatch, tmp_path):
    client, make_session, seed = client_and_db
    tid, kind = seed("local")

    store = LocalVideoStorage(root=tmp_path)
    (tmp_path / "local-key.mp4").write_bytes(b"LOCAL-VIDEO")

    monkeypatch.setattr(
        "app.routers.videos.storage_for_kind",
        lambda k: store,
    )

    resp = client.get(f"/v1/videos/{tid}/file")  # mode=redirect but kind=local
    assert resp.status_code == 200, resp.status_code
    assert int(resp.headers["Content-Length"]) == len(b"LOCAL-VIDEO")
