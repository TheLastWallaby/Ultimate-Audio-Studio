# Security Policy & Architectural Decisions

## Scope & Purpose

This document outlines the security policies, architectural threat model, and intentional design decisions for **Ultimate Audio Studio**.

---

## 1. Public Repository Auto-Update Architecture (Zero-Setup Updates)

### Architectural Context
Ultimate Audio Studio is hosted as an open public GitHub repository and distributed as a standalone Windows desktop executable to non-technical users. To deliver seamless in-place updates without requiring end users to manually reinstall or configure Git/CLI tools:

- The application checks for new releases using GitHub's public REST API (`/releases/latest`).
- **Zero Authentication Required:** Because the repository is public, release queries and asset downloads do not require Personal Access Tokens (PAT) or credentials.
- Client executables can self-update out of the box on any Windows computer with zero configuration.

### Defense-in-Depth Safeguards
To safeguard this update mechanism, the following mitigations are enforced:
1. **HTTPS Enforcement:** The update URL is strictly validated to use secure `https://` transport.
2. **Binary Verification:** Downloaded update binaries are verified for minimum size and valid PE executable headers (`MZ`) before being applied.
3. **Safe In-Place Executable Swap:** The updater safely renames the running binary to `.old`, moves the new executable in place, releases single-instance mutexes, and restarts cleanly.
4. **Non-Blocking Background Execution:** All update checks and asset downloads run asynchronously without blocking the user interface.

---

## 2. Reporting a Vulnerability

If you discover a genuine security vulnerability (such as remote code execution, denial of service, or improper data handling), please report it responsibly by contacting the repository maintainer directly or opening a private security advisory on GitHub.

Please do not open public issues for sensitive security findings.
