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

To package a standalone Windows executable (`dist\Ultimate Audio Studio.exe`) locally using PyInstaller:

```powershell
.\build_exe.ps1
```

> **Note**: Requires genuine `ffmpeg.exe` and `ffprobe.exe` binaries (>10 MB) in the repository root or system PATH so PyInstaller can bundle them.

## License

This project is licensed under the terms of the GNU General Public License v3 (GPL-3.0). See the [LICENSE](LICENSE) file for details.


