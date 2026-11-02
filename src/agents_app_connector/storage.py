"""Attachment bytes on the PVC: <root>/<device_id>/<attachment_id>. Metadata is in the DB."""

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredFile:
    path: str
    sha256: str
    size: int


class AttachmentStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _resolve(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if not full.is_relative_to(self.root.resolve()):
            raise ValueError("path escapes the store root")
        return full

    def put(self, device_id: uuid.UUID, attachment_id: uuid.UUID, data: bytes) -> StoredFile:
        rel = f"{device_id}/{attachment_id}"
        final = self.root / rel
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f"{attachment_id}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return StoredFile(path=rel, sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def read(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def delete(self, path: str) -> None:
        self._resolve(path).unlink(missing_ok=True)
