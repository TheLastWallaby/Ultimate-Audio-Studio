# Security Policy & Architectural Decisions

## Scope & Purpose

This document outlines the security policies, architectural threat model, and intentional design decisions for **Ultimate Audio Studio**.

---

## 1. Supported Versions

Only the current major/minor release series receives active security updates and dependency patches:

| Version | Supported          |
| ------- | ------------------ |
| 1.1.x   | :white_check_mark: |
| < 1.1.0 | :x:                |

---

## 2. Privacy & Data Governance (Zero-Telemetry)

Ultimate Audio Studio is designed with a strict offline-first, local-only architecture:
- **Zero Telemetry / Analytics:** The application contains no tracking scripts, telemetry, or user behavior analytics.
- **Local Storage:** All downloaded audio, cached waveforms, playlists, and settings remain 100% on your local machine (`~/Music`).
- **Network Boundaries:** Network requests are limited strictly to user-initiated YouTube downloads (via `yt-dlp`) and checking for public updates via GitHub Releases.

---

## 3. Public Repository Auto-Update Architecture (Zero-Setup Updates)

### Architectural Context
Ultimate Audio Studio is hosted as an open-source public GitHub repository and distributed as a standalone Windows desktop executable. To deliver seamless in-place updates without requiring end users to reinstall software or manage dependencies manually:

- The application queries GitHub's public REST API (`/releases/latest`) over secure HTTPS to detect new releases.
- Client executables automatically check for updates and self-update out of the box on Windows with zero setup or configuration required.

### Defense-in-Depth Safeguards
To safeguard this update mechanism, the following mitigations are enforced:
1. **HTTPS Enforcement:** The update URL is strictly validated to use secure `https://` transport.
2. **Binary Verification:** Downloaded update binaries are verified for minimum size and valid PE executable headers (`MZ`) before being applied.
3. **Safe In-Place Executable Swap:** The updater safely renames the running binary to `.old`, moves the new executable in place, releases single-instance mutexes, and restarts cleanly.
4. **Non-Blocking Background Execution:** All update checks and asset downloads run asynchronously without blocking the user interface.

---

## 4. Subprocess Isolation & File Security

- **No Shell Execution:** All external tools (`ffmpeg`, `ffprobe`, `yt-dlp`) are invoked using explicit argument arrays (`shell=False`) with `CREATE_NO_WINDOW`, preventing command injection vulnerabilities.
- **Path Traversal Sanitization:** Filenames from remote sources and user input are sanitized against path traversal (`..`), invalid NTFS/FAT32 characters, and Windows DOS reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).

---

## 5. Reporting a Vulnerability

If you discover a potential security vulnerability, please report it responsibly:

- **Preferred:** Open a **[Private Vulnerability Report](https://github.com/TheLastWallaby/Ultimate-Audio-Studio/security/advisories/new)** directly through GitHub Security Advisories.
- **Alternative:** Contact the repository maintainer directly via GitHub profile contact options.

Please **do not** open public GitHub issues or discussions for sensitive security reports until they have been reviewed and resolved.
