from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO


@dataclass
class Context:
    config_dir: Path
    state_dir: Path
    host: Any
    stdin: TextIO
    stdout: TextIO
    interactive: bool
    locks: list[Any] = field(default_factory=list)  # released when a command ends
