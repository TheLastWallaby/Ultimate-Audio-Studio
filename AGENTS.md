# AGENTS.md: Developer Governance & Quality Standards

Welcome to the **Ultimate Audio Studio** repository. All automated AI agents and contributors operating in this codebase must strictly adhere to the operational charter, design conventions, and execution workflows outlined below.

---

## 1. Operational Charter

### A. Strict Typing
- All newly authored or modified functions, methods, and classes must include comprehensive type annotations (parameters and return types).
- Use of `Any` is strongly discouraged; when external libraries lack type stubs, isolate untyped interactions using explicit stubs, `type: ignore[code]`, or typed adapter wrappers.
- Strive for compatibility with `mypy --strict`. Use modern PEP 604 type unions (`T | None`, `A | B`) instead of `typing.Optional` and `typing.Union`.

### B. Atomic Diffs
- Limit modifications strictly to the files, functions, and lines relevant to the requested task.
- Avoid repo-wide formatting passes or indiscriminate touch-ups on unrelated files.
- Preserve existing comments, docstrings, and architectural structure unless explicitly tasked with updating them.
- Ensure every commit or edit represents an isolated, logical, and testable change.

### C. Zero-Hallucination Package Policy
- **Never** introduce or attempt to install unverified packages.
- Always check that proposed third-party dependencies exist on PyPI and are compatible with Python 3.10 through 3.13 before modifying `requirements.txt`.
- Never invent non-existent APIs, methods, or CLI flags. Verify library signatures directly against installed packages or standard documentation.

---

## 2. Toolchain Execution Matrix

Execute all auditing and testing tools via the active Python environment (`python -m <tool>`).

| Tool | Scope & Purpose | Execution Command | Blocking Criteria (Quality Gate) | Remediation |
| :--- | :--- | :--- | :--- | :--- |
| **Ruff (Lint)** | Fast AST linting, import sorting, and code hygiene | `python -m ruff check .` | Any errors (`E`, `F`, `B`, `UP`). Warning count must not increase. | Run `python -m ruff check --fix .` |
| **Ruff (Format)** | Code style and formatting | `python -m ruff format --check .` | Any unformatted files. | Run `python -m ruff format .` |
| **Mypy** | Static type checking and interface verification | `python -m mypy app/ --strict` | Any type errors in modified modules. | Add precise type annotations, generics, or type narrowing. |
| **Bandit** | AST-based security vulnerability scanning | `python -m bandit -r app/ -ll -ii` | Any High or Medium severity/confidence security alerts. | Refactor unsafe constructs (remove `shell=True`, sanitize inputs). |
| **Pip-Audit** | Known CVE vulnerability scanning for dependencies | `python -m pip_audit -r requirements.txt` | Any known vulnerabilities with active fixes. | Upgrade vulnerable package in `requirements.txt`. |
| **Pytest** | Automated unit, integration, and regression testing | `python -m pytest` | Any failing test or unexpected exception. | Fix underlying logic defects before submitting changes. |

---

## 3. Python Design Conventions

All code contributed to this codebase must adhere to modern Python design conventions:

### A. Modern File & Path Management (`pathlib.Path`)
- **Do not use** `os.path` functions (`os.path.join`, `os.path.exists`, `os.path.splitext`, `os.path.basename`).
- **Always use** `pathlib.Path`:
  ```python
  # ✅ Correct
  from pathlib import Path
  data_file = Path("data") / "tracks" / "sample.mp3"
  if data_file.is_file():
      content = data_file.read_bytes()
  ```

### B. Structured Logging
- **Do not use** bare `print()` statements for diagnostic, error, or operational output.
- **Always use** standard library `logging`:
  ```python
  # ✅ Correct
  import logging
  logger = logging.getLogger(__name__)

  logger.info("Loading track from path: %s", track_path)
  logger.warning("Playback buffer under-run detected on channel: %s", channel_id)
  ```

### C. Resource Context Managers
- External and operating-system resources (files, sockets, audio devices, temporary directories, child processes, threading locks) must be managed using explicit context managers (`with` / `async with`):
  ```python
  # ✅ Correct
  with open(file_path, "rb") as audio_stream:
      header = audio_stream.read(128)
  ```
- Use `contextlib.suppress` instead of empty `try...except Pass` blocks for expected non-fatal errors:
  ```python
  import contextlib
  with contextlib.suppress(FileNotFoundError):
      cache_file.unlink()
  ```

### D. Modern Data Classes & Protocols
- Use `@dataclass(slots=True, frozen=True)` for metadata and transfer objects to optimize memory and enforce immutability.
- Use `typing.Protocol` for defining decoupled component interfaces rather than tight concrete class coupling.
- Preserve full exception causality via `raise TargetException(...) from original_error`.

---

## 4. The 5-Step Agent Execution Loop

Agents operating within this repository must systematically follow this five-step cycle:

```mermaid
flowchart LR
    A["1. Analyze"] --> B["2. Plan"]
    B --> C["3. Edit"]
    C --> D["4. Test"]
    D --> E["5. Review"]
```

1. **Analyze**:
   - Inspect the codebase context and locate relevant modules and existing tests.
   - Read and clarify constraints, dependencies, and potential side effects before touching code.
2. **Plan**:
   - Devise a minimal, atomic set of modifications.
   - Confirm which tools and tests in the Toolchain Matrix will validate the changes.
3. **Edit**:
   - Implement surgical changes adhering to the Operational Charter and Python Design Conventions.
   - Ensure imports, types, and resource management conform to modern standards.
4. **Test**:
   - Run the automated toolchain: `pytest`, `ruff check`, `mypy`, and `bandit`.
   - Address all regressions or new warnings introduced by the changes.
5. **Review**:
   - Perform a clean diff review (`git diff`).
   - Verify that changes are strictly scoped, documentation remains intact, and no extraneous artifacts are committed.

---

## 5. Domain Constraints & Architectural Invariants

### A. Target Audience & UI Philosophy
- **Target User**: Seniors (~65+) with limited technical familiarity.
- **UI Design**: Linear left-to-right 3-column layout plus status bar:
  1. *Column 1*: YouTube MP3 download (`noplaylist`) and library manager (`~/Music/MyAudioDownloads`).
  2. *Column 2*: Playback controls (Play/Pause/Stop), volume, timeline sliders (Start/Progress/End), clipping tools (Set Start/End, Test Clip, Save Clip).
  3. *Column 3*: Multi-playlist JSON management, Previous/Next navigation, and USB/CD export.
- **Visuals**: Large fonts, high-contrast controls, intuitive error dialogs with recovery steps, and strictly zero CLI interaction required.

### B. Core Audio Engine & Threading Rules (Never Regress)
- **Mixer Initialization**: Initialize once at startup with `pygame.mixer.pre_init(44100, -16, 2, 8192)`. Never call `quit()` or re-initialize `pygame.mixer` per track.
- **Timeline Tracking**: Track playback position using `time.time()` calculated from the start offset. **Never** use `pygame.mixer.music.get_pos()` (drifts significantly with VBR MP3 files).
- **Duration Probing**: Use `tinytag` exclusively for track duration. **Never** call `pydub.utils.mediainfo` (spawns subprocesses that freeze the UI thread).
- **Background Workflows**: Network downloads (`yt-dlp`) and audio encoding (`pydub.export`) must always run on background threads; never block the Tkinter main loop.

### C. Build & Packaging Requirements
- **Frozen Environment Support**: `pydub` and `yt-dlp` must locate bundled `ffmpeg.exe` and `ffprobe.exe` via `sys._MEIPASS` when frozen.
- **PyInstaller Flags**: Build with `build_exe.ps1` using PyInstaller onefile, `--hidden-import audioop`, `collect_all` for `yt_dlp`, `upx=False`, and bundled `certifi` for SSL certs.
- **Python 3.13 Support**: Requires `audioop-lts` and explicit `--hidden-import audioop`.

### D. Auto-Updater & Security Governance
- **Non-blocking Startup**: Auto-update check (`check_latest_release()`) must run on a background thread on launch; never delay GUI presentation or audio playback.
- **Public Asset Streaming**: Stream release assets via GitHub API `/releases/assets/{id}` or release download URLs. Always strip any `Authorization` headers when redirected to external S3 storage URLs.
- **In-Place Executable Swap**: Rename running `sys.executable` to `.old`, move the new binary into place, spawn the updated `.exe`, and exit. Always clean up leftover `.old` binaries on subsequent launches.
- **Zero-Setup Public Updates**: Releases are publicly accessible from GitHub Releases. Updates require zero authentication, tokens, or manual configuration by end users. See [SECURITY.md](file:///c:/Users/niral/Downloads/AudioAppBuilder/SECURITY.md).

