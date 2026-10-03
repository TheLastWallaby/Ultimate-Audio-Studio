# AGENTS.md: Ultimate Audio Studio

Ultimate Audio Studio is a Windows desktop app (Tkinter, pygame, FFmpeg, yt-dlp) that lets seniors download songs, trim them, build playlists, and export them to a USB drive or CD. It ships as a single auto-updating `.exe`.

**How to read this file.** Sections 3 and 4 describe how code must be written from now on. Parts of the existing code predate these standards (string paths via `os.path`, `log_error()`, bare `except: pass`, loose `Any`). Do not copy those patterns into new work, and do not treat them as precedent. When this file and the surrounding code disagree, this file wins.

---

## 1. Product

- **User**: seniors (about 65+) with little technical experience. Nobody is available to help them when something goes wrong.
- **Layout**: three columns read left to right, plus a status bar.
  1. *Library*: YouTube download to MP3 (`noplaylist`), file import, and the song list (`MyAudioDownloads` in the user's Music folder).
  2. *Player*: Play/Pause/Stop, volume, the Start/Progress/End timeline, and clipping (Set Start/End, Test Clip, Save Clip, Restore Original).
  3. *Playlists and export*: several playlists stored as JSON, Previous/Next, and USB or CD export.
- **Interface rules**:
  - Large fonts and high-contrast controls. Fonts and colors come from `app/ui/theme.py`.
  - Every dialog goes through `app/ui/dialogs.py` (`show_info`, `show_warning`, `ask_yes_no`, `ask_choice`, and `ask_text` for typing a name or a time) or `show_friendly_error` in `app/ui/error_dialog.py`. Do not call `tkinter.messagebox` or `tkinter.simpledialog`.
  - Label buttons with what they do ("Replace the old songs", "Keep them and add these"), not "Yes"/"No"/"OK".
  - Error messages use plain words, say what happened, and say what to do next. Raw exception text is mapped to friendly text in `app/core/errors.py`.
  - Ask as few questions as possible. Prefer a safe automatic choice with Undo over another dialog.
  - The user never needs a command line, a config file, or an account.

---

## 2. Code map

| Path | Holds |
| :--- | :--- |
| `simple_audio_clipper.py` | Entry point; delegates to `app.main`. |
| `app/main.py` | Main window: layout, settings, status bar, undo, shutdown. |
| `app/ui/features/` | One mixin per feature (library, download, player, clip editor, playlists, export, updates). `base.py` declares the state and methods they share. |
| `app/ui/views/` | Widget construction for the three columns. |
| `app/controllers/` | State and decisions with no widgets (playback, playlists, library, export, download, update). |
| `app/services/` | Work that runs on worker threads: `downloader`, `clipper`, `exporter`, `updater`. |
| `app/core/` | `audio_engine`, `task_manager`, `metadata`, `waveform`, `cache_manager`, `process_utils`, `file_utils`, `errors`, and `config` (pydantic settings, `UAS_*` overrides). |
| `app/config.py` | Constants, tool paths, `settings_mgr`, logging setup. |
| `app/platform_utils.py` | Windows-specific calls (drives, eject, mutex, drag and drop). |
| `tests/` | Pytest suite. `conftest.py` isolates it from the real Music folder (see Section 6). |

Keep the layering: services and controllers never import Tkinter widgets; UI code never runs FFmpeg, network, or large file operations directly.

---

## 3. Standards for new and changed code

**Scope.** Apply these to every function you write or modify. If you change a function, bring that whole function up to standard. Leave functions you are not otherwise touching alone: no repo-wide conversions, no reformatting of unrelated files, no rewritten comments. One logical, testable change per commit.

### A. Typing
- Annotate every parameter and return type. The whole package must pass `mypy --strict`; CI enforces it.
- Use PEP 604 unions (`str | None`), built-in generics (`list[str]`), and `collections.abc` for `Callable`, `Sequence`, `Mapping`.
- Do not add `Any`. Where an untyped boundary forces it (yt-dlp info dicts, parsed JSON, Tk callbacks), narrow to a real type at that boundary and keep `Any` out of signatures.
- Use `# type: ignore[code]` with the specific code, never a bare ignore.
- Start each module with `from __future__ import annotations`.

### B. Paths
- Use `pathlib.Path` for path handling. Do not add `os.path` calls.
- New functions take and return `Path`. When a new function must be called from older string-based code, accept `str | Path` and convert on the first line.
  ```python
  def backup_path(song: str | Path) -> Path:
      song = Path(song)
      return song.with_name(song.name + ".original.bak")
  ```
- Compare Windows paths case-insensitively on the absolute path (see `_same_file` in `app/controllers/playlist_controller.py`).

### C. Logging
- Use a module logger. No `print()`, and no new `log_error()` calls (it remains only for existing call sites).
  ```python
  import logging

  logger = logging.getLogger(__name__)

  logger.warning("Backup of %s failed: %s", song.name, err)
  ```
- Loggers under the `app.` namespace write to the rotating log file through the handler that `app.config.setup_logging()` installs.
- Pass values as arguments (`%s`), not f-strings. Use `logger.exception(...)` inside an `except` block when the traceback matters.
- Logging is for the developer. If the user needs to know, also tell them in the UI.

### D. Errors and resources
- Catch the narrowest exception that can occur (`OSError`, `ValueError`), not `Exception`, unless the code is a last-resort guard around a worker or callback.
- Never swallow an error silently. Either handle it, log it, or use `contextlib.suppress(SpecificError)` for a failure that is expected and harmless.
  ```python
  with contextlib.suppress(FileNotFoundError):
      temp_file.unlink()
  ```
- A function that can fail returns or raises something the caller can act on. Do not log a failure and then continue as if it succeeded.
- Chain causes: `raise ExportError("...") from err`.
- Manage files, locks, temp directories, and subprocesses with `with`.

### E. Data and interfaces
- Use `@dataclass(slots=True, frozen=True)` for values passed between layers (see `PendingUpdate`, `InstallResult`).
- Use `typing.Protocol` for an interface a caller depends on (see `CancelToken` in `app/core/process_utils.py`).
- Prefer a small result type over a tuple of loosely related values or a set of callbacks that can each be `None`.

### F. Comments and docstrings
- Give every module and public function a short docstring.
- Comments explain why a choice was made or what would break without it. Do not narrate what the code does.

### G. Dependencies and Python version
- The `.exe` is built and tested on Python 3.13. Code must still be valid on 3.10 (`requires-python = ">=3.10"`, Ruff `target-version = "py310"`), so do not use 3.11+ only features such as `typing.Self`, `tomllib`, or `StrEnum`.
- Before adding a dependency, confirm it exists on PyPI, supports 3.10 to 3.13, and bundles cleanly with PyInstaller. Add it to both `requirements.txt` and `pyproject.toml`.
- Never invent an API, option, or CLI flag. Check signatures against the installed package or its documentation.

---

## 4. Invariants (never regress)

### A. Audio engine
- **Mixer**: initialized once at startup with `pygame.mixer.pre_init(44100, -16, 2, 8192)`. Never call `pygame.mixer.quit()` or re-initialize per track. The 8192-sample buffer prevents crackling on slow PCs.
- **Playback position**: computed from `time.monotonic()` and the start offset (`AudioEngine.current_play_seconds()`). Never use `pygame.mixer.music.get_pos()`, which drifts on VBR MP3s, and never use `time.time()`, which jumps when the system clock changes.
- **Duration**: read with `tinytag` through `read_track_metadata()`. When the header has no length, the `ffprobe`/`ffmpeg` fallback (`probe_audio_duration`) may run, but only on a worker thread; on the UI thread pass `probe_fallback=False` / `probe=False`.
- **Unsupported formats** (M4A, WMA, and so on) are converted to a cached WAV before playback. The conversion runs on a worker (`_when_playable`), never on the UI thread.

### B. Threading
- The Tkinter main loop must never block. Downloads, FFmpeg, encoding, file copies, drive access, network calls, and `ffprobe` all run on workers.
- Start background work with `task_mgr.submit_task(...)` from `app/core/task_manager.py`. Do not create ad-hoc `threading.Thread` objects for one-off jobs.
- Workers never touch widgets. Return to the UI thread with `self._safe_after(0, callback, *args)`.
- Long jobs accept a cancel token and check it between steps; pass it to `run_ffmpeg(..., cancel_event=...)` so Stop takes effect immediately.
- Workers check `_is_shutting_down` and stop quietly when the app is closing.
- A result that arrives late must be dropped if the user has moved on (see the token check in `_when_playable` and the request ids for waveform and cover art).

### C. Subprocesses
- Run FFmpeg through `run_ffmpeg()` in `app/core/process_utils.py` unless you need to stream its raw output. Use `ffmpeg_path` and `ffprobe_path` from `app.config`; never hardcode a tool path.
- Always pass an argument list. Never use `shell=True`. Use `CREATE_NO_WINDOW` so no console window flashes.
- Every subprocess call has a timeout.

### D. The user's files
The user's songs, playlists, and exports are the only things that cannot be regenerated. Any change that writes, replaces, or deletes them must follow these rules.
- **Write, then swap.** Write to a temporary file in the same folder, verify it is non-empty, then `os.replace` it into place. Use `atomic_save_json` and `copy_file_atomic` from `app/core/file_utils.py`. Never write directly onto a file the user already has.
- **Back up before replacing.** Do not replace an original unless a complete backup exists. If the backup fails, stop and report it.
- **Ask everything first.** Finish all checks and questions before the first destructive step, so that Cancel always leaves things as they were.
- **Deleting a song** goes through the undo staging area and then the Recycle Bin (`send2trash`), never `os.remove`.
- **On USB drives and in export folders**, delete only files this app created (`find_previous_export`). Leave everything else alone.
- **Unreadable data files** (playlists, settings) are set aside for recovery, not overwritten with defaults.
- **Report truthfully.** If a step fell back to something weaker (copied instead of normalized, skipped, partly done), say so in the result. "Finished" means everything requested was done.
- Sanitize any name that comes from the network or the user with `sanitize_filename()` before it becomes a file name.

---

## 5. Quality gates

CI (`.github/workflows/ci.yml`) runs all of these on every push and pull request. Any finding fails the build, whether or not your change introduced it.

Install the tools once:

```bash
python -m pip install -r requirements.txt ruff mypy bandit pip-audit pytest
```

| Gate | Command | Fix |
| :--- | :--- | :--- |
| Lint | `python -m ruff check .` | `python -m ruff check --fix .` |
| Format | `python -m ruff format --check .` | `python -m ruff format <files you changed>` |
| Types | `python -m mypy --strict app/ .github/scripts/bump_version.py` | Add annotations or narrow types. |
| Security | `python -m bandit -r app/ -ll -ii -q` | Remove the unsafe construct. Use `# nosec BXXX` only with a stated reason. |
| Dependencies | `python -m pip_audit -r requirements.txt` | Raise the affected requirement. |
| Tests | `python -m pytest -q` | Fix the code, not the test. |

Run all six before you call a change finished.

---

## 6. Testing

- Every bug fix comes with a test that fails without the fix. Every new behavior comes with a test.
- Test failure paths, not only success: a locked file, a full or removed drive, a failed FFmpeg run, Cancel pressed at each step, and shutdown during a job.
- Use `tmp_path` for files. Tests must never touch the real Music folder, a real drive, or the network.
- Do not assert on a mock alone when the outcome can be checked on disk.
- `conftest.py` sets up the following for every test; do not work around it:
  - **A throwaway Music folder.** The `UAS_*` paths (Library, playlists, settings, error log) point at a temporary folder, set before anything imports `app`.
  - **Dialogs never block.** Every dialog from `app/ui/dialogs.py` returns its cancel value, and error dialogs are dismissed. Patch the dialog to test another answer, or mark the test `@pytest.mark.real_dialogs`.
  - **A running worker pool.** Closing a window shuts the shared `task_mgr` down; it is restarted before each test.
  - **No real sound device.** `SDL_AUDIODRIVER` is set to SDL's silent `dummy` driver, so the mixer starts at once on any machine. Without it, every window opened on a runner without audio waits 8 s for pygame to give up.
  - **Window start-up is retried.** `tk.Tk()` tries again when Tcl fails with "Can't find a usable init.tcl", an intermittent error on the Windows CI runners.
- Use the `studio` fixture for a real, hidden main window. It closes the window afterwards.
- Background results reach the window through its event loop, so a test must pump it (`root.update()`) until the result arrives.

---

## 7. Build and packaging

- Build locally with `.\build_exe.ps1`. It installs `requirements.txt` and `requirements-build.txt`, locates genuine `ffmpeg.exe`/`ffprobe.exe` (over 10 MB; shims are rejected), and runs PyInstaller on `simple_audio_clipper.spec`. Output: `dist\Ultimate Audio Studio.exe`.
- All PyInstaller settings live in `simple_audio_clipper.spec`. Do not pass packaging options on the command line.
  - One-file, windowed, `upx=False`, with a splash screen that `app.main` closes.
  - Bundled binaries: `ffmpeg.exe`, `ffprobe.exe`, and the Deno runtime that yt-dlp needs for YouTube.
  - `collect_all` for `yt_dlp`, `yt_dlp_ejs`, `certifi`, and `tinytag`.
- A module that is imported dynamically must be added to `hiddenimports`, or it will be missing from the `.exe`.
- When frozen, bundled files are found under `sys._MEIPASS` (`BASE_PATH` in `app/config.py`). SSL uses the bundled `certifi` certificates.
- CI runs the built `.exe` with `--self-test` (`app/self_test.py`). When you add a bundled tool or dependency, add a check for it there.
- Never commit `ffmpeg.exe`, `ffprobe.exe`, `dist/`, or `build/`.

---

## 8. Releases

- Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`: it checks that the tag matches `app_version`, runs the tests, builds, self-tests the `.exe`, and publishes the GitHub Release.
- The version is declared in four places that must agree: `pyproject.toml`, `app/core/config.py` (twice), and `tests/test_settings.py`. `python .github/scripts/bump_version.py` bumps the patch version in all four and refuses to run if they disagree.
- Manual release: bump the version, commit and push to `main`, then tag and push the tag.
  ```bash
  git tag vX.Y.Z
  git push origin vX.Y.Z
  ```
- When a yt-dlp requirement change lands on `main` (normally a merged Dependabot PR), `.github/workflows/yt-dlp-autorelease.yml` bumps the patch version, tags, and calls `release.yml` with no manual steps. Keep yt-dlp current: YouTube changes regularly break old versions.
- Every release reaches users' machines automatically. Do not tag anything you would not want installed unattended.

---

## 9. Auto-updater and security

See [SECURITY.md](SECURITY.md) for the full policy.

- The update check (`check_latest_release()`) runs on a background thread at launch and never delays the window or playback.
- Updates need no account, token, or setup. The token setting is optional and not needed for the public repository.
- A newer release is downloaded in the background and staged with its SHA-256. It is checked for minimum size, a valid `MZ` header, and GitHub's published digest when one exists.
- Requests use HTTPS only. The `Authorization` header is stripped on any redirect away from `github.com`.
- Install: rename the running `sys.executable` to `.old`, move the new file into place, start it, and wait for it to signal `UPDATE_OK_EVENT_NAME` once its window is up. If it exits first, or has still not signalled when the start timeout ends (it is then stopped), restore the previous version and record the tag so it is not retried automatically. Leftover `.old` files are removed at the next start.
- The app sends no telemetry. Do not add network calls other than YouTube search and download and the GitHub release check.

---

## 10. Before you finish

1. The change does what was asked and nothing else; `git diff` contains no unrelated edits or stray files.
2. Every function you touched meets Section 3.
3. No invariant in Section 4 is weakened.
4. New behavior and fixed bugs have tests, including a failure path.
5. All six gates in Section 5 pass.
6. Anything the user sees is in plain language and says what to do next.
