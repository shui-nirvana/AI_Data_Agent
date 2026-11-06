"""Manage private server-assigned input and temporary paths; an independent Publisher handles validated publication.


"""

import os
from pathlib import Path
from uuid import UUID


class InputFiles:
    """Manage input file paths."""
    def __init__(self, root: Path) -> None:
        """Initialize the input storage root.




        """
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # Restrict directory permissions on non-Windows systems.
        if os.name != "nt":
            self.root.chmod(0o700)

    def input_path(self, task_id: str) -> Path:
        """Return the task input CSV path, normalizing the UUID to prevent traversal.


        """
        return self.root / f"{UUID(task_id)}.csv"

    def temporary_path(self, task_id: str) -> Path:
        """Return the task's temporary upload path.

        """
        return self.root / f"{UUID(task_id)}.part"
