"""VideoStorage protocol interface."""

from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable


@runtime_checkable
class VideoStorage(Protocol):
    """Abstract interface for video file storage backends."""

    def save(self, task_id: str, src: Path, lease_token: int | None = None) -> str:
        """Save a video file and return its storage key.

        Args:
            task_id: Unique task identifier.
            src: Local path to the source video file.
            lease_token: Optional strict-lease token (R20). S3 backends record
                it as object metadata so a stale owner's artifact is identifiable;
                the authoritative fence lives in ``app.workers.tasks.fence_artifact``.

        Returns:
            Storage key (e.g., ``<task_id>.mp4`` or an S3 object key).
        """
        ...

    def open(self, key: str) -> tuple[BinaryIO, int]:
        """Open a stored video for reading.

        Args:
            key: Storage key returned by :meth:`save`.

        Returns:
            A tuple of ``(file-like object, file_size_in_bytes)``.
        """
        ...

    def delete(self, key: str) -> None:
        """Delete a stored video file.

        Args:
            key: Storage key returned by :meth:`save`.
        """
        ...

    def exists(self, key: str) -> bool:
        """Check whether a stored video exists."""
        ...

    def presigned_url(self, key: str, expires: int = 3600) -> str | None:
        """Return a time-limited download URL, or ``None`` if unsupported.

        S3 backends return a signed URL; local/NFS backends return ``None``
        so callers fall back to streaming (scale-multi-instance R4).
        """
        ...
