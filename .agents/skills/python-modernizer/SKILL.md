---
name: python-modernizer
description: >-
  Guide code refactoring to modern Python conventions (Python 3.10+ and Python 3.12+).
  Use this skill when updating legacy Python codebases, refactoring type annotations,
  implementing structural typing with Protocol, adding slots for performance, adopting
  asyncio.TaskGroup for structured concurrency, and ensuring explicit exception chaining.
---

# Modern Python Refactoring Guide & Conventions

This skill provides normative patterns, code transformation recipes, and refactoring guidelines for bringing Python codebases up to modern conventions (Python 3.10 through 3.13).

---

## 1. Core Modernization Patterns

### Pattern 1: PEP 604 Type Unions & Built-in Generics (PEP 585)

Modern Python eliminates the need to import `Union`, `Optional`, `List`, `Dict`, `Tuple`, and `Set` from the `typing` module.

#### Rules:
- Replace `Union[A, B]` with `A | B`.
- Replace `Optional[A]` with `A | None`.
- Replace `typing.List[T]`, `typing.Dict[K, V]`, `typing.Tuple[T, ...]`, `typing.Set[T]` with built-in generics: `list[T]`, `dict[K, V]`, `tuple[T, ...]`, `set[T]`.
- For type aliases in Python 3.12+, use the `type` statement (PEP 695); for Python 3.10-3.11, use `TypeAlias`.

#### Refactoring Example:

```python
# ❌ Legacy Pattern (Python < 3.10)
from typing import Dict, List, Optional, Tuple, Union

TrackID = Union[str, int]

def find_tracks(
    queries: List[str],
    limit: Optional[int] = None,
    metadata: Optional[Dict[str, Union[str, int]]] = None
) -> Tuple[List[str], Optional[int]]:
    ...
```

```python
# ✅ Modern Pattern (Python 3.10+)
type TrackID = str | int  # Or TrackID: TypeAlias = str | int

def find_tracks(
    queries: list[str],
    limit: int | None = None,
    metadata: dict[str, str | int] | None = None
) -> tuple[list[str], int | None]:
    ...
```

---

### Pattern 2: Structural Subtyping with `typing.Protocol`

Use `Protocol` (PEP 544) to specify structural interfaces (duck typing with static verification) without coupling classes to rigid inheritance hierarchies.

#### Rules:
- Favor `Protocol` over Abstract Base Classes (`ABC`) when defining caller-side interfaces and decoupling modules.
- Annotate with `@runtime_checkable` if `isinstance()` checks are required at runtime.
- Define minimal, focused protocols representing specific capabilities (Single Responsibility).

#### Refactoring Example:

```python
# ❌ Brittle Pattern (Tight Coupling / Concrete Inheritance)
class ConcreteAudioPlayer:
    def play(self, file_path: str) -> None: ...
    def stop(self) -> None: ...

class PlaybackController:
    def __init__(self, player: ConcreteAudioPlayer) -> None:
        self.player = player  # Hard dependency on concrete class
```

```python
# ✅ Modern Pattern (Decoupled with Protocol)
from typing import Protocol, runtime_checkable
from pathlib import Path

@runtime_checkable
class Playable(Protocol):
    """Structural interface for any audio playback engine."""
    def play(self, file_path: Path) -> None: ...
    def stop(self) -> None: ...
    def is_playing(self) -> bool: ...

class PlaybackController:
    """Accepts any engine that satisfies the Playable interface."""
    def __init__(self, player: Playable) -> None:
        self.player = player
```

---

### Pattern 3: Memory Efficiency & Safe Attributes with `slots=True`

Classes and dataclasses that represent high-volume data models (such as audio metadata, playlist entries, or waveform points) should enforce slots.

#### Rules:
- For dataclasses, use `@dataclass(slots=True, frozen=True)` (or `frozen=False` if mutation is required).
- Slots eliminate `__dict__` overhead, drastically reducing memory footprint and preventing accidental attribute creation bugs.
- For standard classes, declare `__slots__ = ("_field1", "_field2")`.

#### Refactoring Example:

```python
# ❌ Legacy Pattern (High memory overhead, allows typo attributes)
from dataclasses import dataclass

@dataclass
class AudioMetadata:
    title: str
    duration_seconds: float
    bitrate: int
    artist: str = "Unknown"
```

```python
# ✅ Modern Pattern (Slotted & Immutable)
from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class AudioMetadata:
    """Memory-optimized, immutable audio metadata record."""
    title: str
    duration_seconds: float
    bitrate: int
    artist: str = "Unknown"
```

---

### Pattern 4: Structured Concurrency with `asyncio.TaskGroup`

Python 3.11+ introduces `asyncio.TaskGroup` (PEP 654), which guarantees structured concurrency: if any child task fails, remaining tasks are cancelled, preventing leaked coroutines and silent failures.

#### Rules:
- Replace `asyncio.gather()` or floating `asyncio.create_task()` with `async with asyncio.TaskGroup() as tg:`.
- Handle multi-exception failures using `except*` (PEP 654 Exception Groups).
- Retain tasks on the group scope so results can be evaluated upon successful exit.

#### Refactoring Example:

```python
# ❌ Brittle Pattern (Leaked tasks if one fails, loose exception handling)
import asyncio

async def fetch_all(urls: list[str]) -> list[str]:
    tasks = [asyncio.create_task(fetch_url(url)) for url in urls]
    return await asyncio.gather(*tasks)  # Unhandled cancellation or leaks
```

```python
# ✅ Modern Pattern (TaskGroup with structured scope)
import asyncio

async def fetch_all(urls: list[str]) -> list[str]:
    results: list[str] = []
    
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(fetch_url(url)) for url in urls]
            
        results = [t.result() for t in tasks]
    except* TimeoutError as eg:
        logger.error("Some downloads timed out: %s", eg.exceptions)
        raise
    except* Exception as eg:
        logger.error("Download failed in group: %s", eg.exceptions)
        raise
        
    return results
```

---

### Pattern 5: Explicit Exception Chaining & Diagnostics

Never swallow underlying causes or raise unchained exceptions when catching one exception and raising another.

#### Rules:
- Always use `raise NewException(...) from err` (PEP 3134) to preserve full causal traceback.
- Use `raise NewException(...) from None` only when intentionally hiding sensitive internal implementation details from end-users.
- Use `exc.add_note(...)` (PEP 678 in Python 3.11+) to attach runtime debugging metadata without corrupting exception messages.

#### Refactoring Example:

```python
# ❌ Anti-pattern (Lost original traceback context)
try:
    with open(config_path, "r") as f:
        data = json.load(f)
except Exception as e:
    raise ConfigurationError(f"Failed to load config: {e}")  # Drops original stack trace
```

```python
# ✅ Modern Pattern (Explicit Chaining & Notes)
from pathlib import Path
import json

try:
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as err:
    exc = ConfigurationError("Failed to parse configuration file")
    exc.add_note(f"Target path was: {Path(config_path).resolve()}")
    raise exc from err  # Preserves causal chain
```

---

## 2. Complementary Modern Python Conventions

| Feature | Modern Standard | Legacy Alternative (Avoid) |
| :--- | :--- | :--- |
| **Path Handling** | `pathlib.Path` (`path / "subdir"`, `path.read_text()`) | `os.path.join`, `os.path.exists`, `os.path.basename` |
| **String Formatting** | Formatted string literals (`f"Value: {val:.2f}"`) | `%` formatting, `str.format()` |
| **Context Suppression** | `with contextlib.suppress(FileNotFoundError):` | Bare `try: ... except FileNotFoundError: pass` |
| **Pattern Matching** | `match / case` (structural pattern matching) | Long cascading `if/elif/elif` chains checking types/shapes |
| **Logging** | Structured logging with `logging.getLogger(__name__)` | Bare `print()` statements |

---

## 3. Step-by-Step Modernization Workflow

When modernizing an existing Python module:

1. **Baseline Check**: Run automated tests first to establish a green baseline:
   ```bash
   python -m pytest
   ```
2. **Automated Upgrade via Ruff**: Run modern syntax upgrades:
   ```bash
   python -m ruff check --select UP --fix .
   ```
3. **Manual Idiom Refactoring**:
   - Upgrade dataclasses with `slots=True, frozen=True`.
   - Replace abstract classes or duct-typed parameters with `typing.Protocol`.
   - Update coroutine orchestration to `asyncio.TaskGroup`.
   - Add explicit `from err` exception chaining and `exc.add_note()`.
   - Convert string file paths to `pathlib.Path`.
4. **Strict Type Verification**:
   ```bash
   python -m mypy <module_or_file> --strict
   ```
5. **Format & Verify**:
   ```bash
   python -m ruff format .
   python -m pytest
   ```
