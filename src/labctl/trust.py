"""Secure persistence of explicitly accepted external definition digests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class TrustStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _digests(self) -> set[str]:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        values = data.get("digests", []) if isinstance(data, dict) else []
        return {item for item in values if isinstance(item, str)}

    def accepted(self, digest: str) -> bool:
        return digest in self._digests()

    def accept(self, digest: str) -> None:
        values = self._digests()
        values.add(digest)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=".trust-")
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump({"schema_version": 1, "digests": sorted(values)}, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
