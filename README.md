# Ultimate Audio Studio

A senior-friendly, linear-workflow desktop audio application designed for simple YouTube downloading, audio playback, waveform clipping, and USB/CD export.

## Features

- **Step 1: Download & Library**
  - Download audio from YouTube (via yt-dlp) directly to your music directory.
  - Automatically loads and manages local audio tracks.
- **Step 2: Player & Clipper**
  - Linear playback controls with waveform visualization.
  - Set start and end points with single clicks to test and save custom audio clips.
- **Step 3: Playlists & Export**
  - Organize songs into multiple playlists.
  - Export playlists to external drives (USB) or target folders.
- **Automatic Updates on Launch**
  - Silently checks GitHub for new releases on startup.
  - Accessible update modal with live download progress bar.
  - Windows safe in-place replacement: updates and relaunches seamlessly without manual reinstallation.
- **Accessibility & Senior-Friendly UI**
  - Large typography, high-contrast controls, clear error reporting, and no command-line interaction required.

## Requirements

- Python 3.10+ (tested with Python 3.13)
- `ffmpeg` and `ffprobe` binaries in the project root or system PATH.

## Installation

```bash
pip install -r requirements.txt
```

## Running the Application

```bash
python simple_audio_clipper.py
```

## Building the Executable

To build a standalone Windows executable (`Ultimate Audio Studio.exe`) via PyInstaller:

```powershell
.\build_exe.ps1
```

The output executable will be created in `dist\Ultimate Audio Studio.exe`.

## Releasing & Auto-Updating

This project uses GitHub Actions to automate building and publishing releases for your users.

### Publishing a New Release

1. Update the version number in `app/core/config.py` (e.g. `app_version: str = Field(default="1.1.3")`).
2. Create and push a git tag matching the version:

```bash
git tag v1.1.3
git push origin v1.1.3
```

3. GitHub Actions (`.github/workflows/release.yml`) will automatically:
   - Spin up a clean Windows runner.
   - Install Python 3.13 and dependencies.
   - Bundle `ffmpeg` and compile `Ultimate Audio Studio.exe`.
   - Publish a GitHub Release with the executable attached.

The next time users launch their app (or click **Check for Updates** in the status bar), it will automatically detect the new release, download it with a progress bar, and restart directly into the updated version—with zero manual configuration.

## License

This project is licensed under the terms of the GNU General Public License v3 (GPL-3.0). See the [LICENSE](LICENSE) file for details.

