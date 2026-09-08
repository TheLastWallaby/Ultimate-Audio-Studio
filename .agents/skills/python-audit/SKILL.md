---
name: python-audit
description: >-
  Run comprehensive Python code quality, security, typing, and dead-code audits.
  Use this skill whenever assessing repository health, verifying pull requests,
  evaluating security risks, running lint and type-checking suites, or triaging code quality issues.
---

# Python Quality & Security Audit Skill

This skill prescribes standardized workflows and exact command-line steps for auditing Python repositories. It covers static analysis, type checking, security vulnerability scanning, dead code elimination, code complexity metrics, and test coverage.

---

## 1. Toolchain Overview

The Python audit pipeline incorporates the following developer tools:

| Category | Tool | Primary Purpose |
| :--- | :--- | :--- |
| **Linting & Formatting** | `ruff` | Fast AST linting, import sorting, and code formatting |
| **Static Typing** | `mypy` | Type consistency, null safety, and strict interface validation |
| **Security Scanning** | `bandit` | AST-based security vulnerability and anti-pattern scanner |
| **Dependency Auditing** | `pip-audit` | Known CVE vulnerability detection in installed packages and requirements |
| **Dead Code Detection** | `vulture` | Unreachable code, unused functions, classes, and variables |
| **Complexity & Metrics** | `radon` | Cyclomatic complexity (CC) and Maintainability Index (MI) |
| **Testing & Coverage** | `pytest`, `pytest-cov` | Functional verification, regression testing, and branch coverage |

---

## 2. CLI Execution Matrix

Run these commands from the repository root. Since developer tools are installed in the Python environment, invoke tools via `python -m <module>` to ensure matching interpreter alignment and avoid Windows PATH resolution discrepancies.

### A. Linting and Formatting (`ruff`)

```bash
# Check for lint violations across all rules (E, F, W, B, I, UP, S, C4, etc.)
python -m ruff check .

# Automatically apply safe fixes for fixable violations
python -m ruff check --fix .

# Verify code formatting without modifying files (Audit / CI mode)
python -m ruff format --check .

# Auto-format all Python files in place
python -m ruff format .
```

### B. Static Type Checking (`mypy`)

```bash
# Strict type audit on the primary application package
python -m mypy app/ --strict

# Audit entire repository with error codes and missing third-party stubs handled
python -m mypy . --show-error-codes --ignore-missing-imports
```

### C. Security Vulnerability Scanning (`bandit`)

```bash
# Scan application code recursively for Medium and High severity & confidence issues
python -m bandit -r app/ -ll -ii

# Comprehensive scan across all repository files (excluding tests and scratch scripts)
python -m bandit -r . -x "./tests,./scratch,./build,./dist" -ll
```

### D. Dependency Vulnerability Auditing (`pip-audit`)

```bash
# Audit installed environment packages against OSV / PyPA vulnerability databases
python -m pip_audit

# Audit specific dependencies declared in requirements.txt
python -m pip_audit -r requirements.txt
```

### E. Dead Code Detection (`vulture`)

```bash
# Scan application modules for unused code with an 80% confidence threshold
python -m vulture app/ --min-confidence 80

# Audit repository root excluding build artifacts and scratch files
python -m vulture . --min-confidence 80 --exclude build,dist,scratch,simple_audio_clipper.py.bak
```

### F. Code Complexity & Maintainability (`radon`)

```bash
# Cyclomatic Complexity (CC): calculate average complexity and list blocks scoring B or worse (score > 5)
python -m radon cc app/ -a -nb

# Maintainability Index (MI): identify modules with maintainability concerns (score < 20)
python -m radon mi app/ -nb

# Raw code metrics (LOC, LLOC, SLOC, comments, blanks)
python -m radon raw app/
```

### G. Test Suite & Coverage (`pytest`)

```bash
# Run full automated test suite with verbose output
python -m pytest -v

# Run tests with branch coverage reporting on the core app package
python -m pytest --cov=app --cov-report=term-missing
```

---

## 3. Triage Criteria & Severity Levels

Audit findings must be categorized and addressed using the following severity hierarchy:

### Severity 1: Blocker (Immediate Remediation Required)
- **pip-audit**: Critical or High severity CVEs in direct dependencies with known active exploits.
- **bandit**: High severity / High confidence security violations (e.g., hardcoded secrets/passwords, `shell=True` subprocess calls with unescaped input, insecure deserialization).
- **ruff**: Fatal syntax errors (`E999`) or undefined variable names (`F821`).
- **pytest**: Any failing automated unit or integration test.

### Severity 2: High Priority (Must Fix Prior to Merge / Release)
- **mypy**: Type mismatches in core business logic or public function signatures, unchecked `None` dereferencing.
- **bandit**: Medium severity issues (e.g., weak hash algorithms such as MD5 for integrity, permissive file permissions, unverified SSL contexts).
- **ruff**: Bugbear warnings (`B006` mutable default arguments, `B008` function call in default arg).
- **radon**: Functions with Cyclomatic Complexity rank **E** or **F** (CC > 30) - high risk of defects, refactoring required.

### Severity 3: Medium Priority (Technical Debt)
- **vulture**: Dead methods, obsolete imports, or abandoned variables.  
  *Rule*: Always check for dynamic event bindings (e.g., Tkinter `command=`, protocol callbacks) before removing code.
- **mypy**: Untyped function definitions or untyped third-party library calls.
- **radon**: Maintainability Index rank **C** (score < 10) or CC rank **C/D** (CC 11-25).
- **pytest-cov**: Critical code branches lacking test coverage.

### Severity 4: Low Priority / Style
- **ruff format**: Code formatting and whitespace discrepancies.
- **ruff**: Import sorting violations (`I001`).
- Modernization opportunities (e.g., modern type unions `int | None` instead of `Optional[int]`).

---

## 4. Standard Audit Protocol (Step-by-Step)

When performing a repository health audit, execute the following steps in sequence:

1. **Verify Environment Integrity**:
   ```bash
   python -c "import ruff, mypy, bandit, pip_audit, vulture, radon, pytest; print('Audit toolchain ready')"
   ```
2. **Execute Static Linting & Formatting**:
   Run `python -m ruff check .` followed by `python -m ruff format --check .`.
3. **Execute Static Type Checking**:
   Run `python -m mypy app/ --strict` and note all reported type discrepancies.
4. **Execute Security & CVE Audits**:
   Run `python -m bandit -r app/ -ll -ii` and `python -m pip_audit -r requirements.txt`.
5. **Execute Dead Code & Complexity Scans**:
   Run `python -m vulture app/ --min-confidence 80` and `python -m radon cc app/ -a -nb`.
6. **Execute Functional Tests & Coverage**:
   Run `python -m pytest -v --cov=app --cov-report=term-missing`.
7. **Synthesize Findings**:
   Compile results into an audit summary organized by severity (Blocker, High, Medium, Low) with concrete code diff recommendations.
