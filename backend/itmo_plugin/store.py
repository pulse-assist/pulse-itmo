"""Состояние входа: токены ITMO.ID и незавершённый вход — файл state.json в служебной папке плагина.

Как в itmo-mcp: запись атомарная (временный файл и rename), права 0600; новый refresh token сохраняется сразу —
старый после продления уже не годится.
"""

import json
import os
import tempfile
import threading
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "state.json"
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, **fields) -> dict:
        """Поменять поля состояния (None — удалить поле)."""
        with self._lock:
            data = self.load()
            for key, value in fields.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data
