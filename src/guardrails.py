"""Immutable safety guardrails, privacy redlines, and project protection for Omni-Organizer v3."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Sequence

from models import DestinationOption

HARDCODED_FORBIDDEN_PREFIXES: tuple[str, ...] = tuple(
    os.path.normcase(os.path.abspath(p))
    for p in (
        r"C:\Windows",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        r"C:\$Recycle.Bin",
        r"C:\Recovery",
        r"C:\System Volume Information",
        r"D:\$RECYCLE.BIN",
        r"D:\Recovery",
        r"D:\System Volume Information",
    )
)

FORBIDDEN_PATH_SEGMENTS = {
    "appdata",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
}

PROJECT_MARKERS = {
    ".git",
    "package.json",
    "pyproject.toml",
    ".venv",
    "venv",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
}

IGNORED_EXTENSIONS = {
    ".crdownload",
    ".tmp",
    ".part",
    ".download",
    ".opdownload",
    ".partial",
    ".!ut",
    ".aria2",
    ".filepart",
    ".lnk",
    ".ini",
    ".rdp",
    ".bat",
    ".cmd",
    ".vbs",
    ".ps1",
    ".log",
    ".sys",
    ".swp",
}

IGNORED_FILENAMES = {
    "desktop.ini",
    "thumbs.db",
    "ntuser.dat",
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "dumpstack.log",
    "dumpstack.log.tmp",
    ".ds_store",
    ".localized",
}

SENSITIVE_NAME_REGEX = re.compile(
    r"(?i)(passwd|password|pwd|密碼|帳密|secret|token|api[_-]?key|credential|"
    r"private[_-]?key|id_rsa|id_ed25519|證件|身分證|身份證|健保卡|護照|存摺|"
    r"銀行|信用卡|稅單|報稅|薪資|扣繳|契約|合約|租賃|醫療|病歷)"
)

SENSITIVE_EXTENSIONS = {
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".ovpn",
    ".ppk",
    ".jks",
}

SENSITIVE_CONTENT_REGEX = re.compile(
    r"(?i)(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|"
    r"AIza[0-9A-Za-z\-_]{30,}|"
    r"sk-[0-9A-Za-z]{20,}|"
    r"ghp_[0-9A-Za-z]{20,}|"
    r"密碼\s*[:：]|"
    r"身分證字號\s*[:：]|"
    r"銀行帳號\s*[:：])"
)


def _norm_abs(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _norm_real(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _prefix_match(norm: str, prefix: str) -> bool:
    return norm == prefix or norm.startswith(prefix + os.sep)


def is_sensitive_file(path: Path | str) -> tuple[bool, str]:
    """Return (True, reason) if filename or extension indicates sensitive/secret content."""
    p = Path(path)
    name = p.name
    name_lower = name.lower()
    ext_lower = p.suffix.lower()

    if name_lower == ".env" or name_lower.startswith(".env."):
        return True, "sensitive_env_file"
    if ext_lower in SENSITIVE_EXTENSIONS:
        return True, f"sensitive_extension:{ext_lower}"
    if SENSITIVE_NAME_REGEX.search(name):
        return True, "sensitive_filename_keyword"
    return False, ""


def has_sensitive_content(text: str) -> bool:
    """Secondary PII/secret scan on extracted text snippets before any AI dispatch."""
    if not text:
        return False
    return bool(SENSITIVE_CONTENT_REGEX.search(text))


def is_hidden_or_system(path: Path | str) -> bool:
    """Detect OS hidden/system files and temporary Office lock files."""
    p = Path(path)
    name = p.name
    name_lower = name.lower()

    if name.startswith("~$") or name_lower in {".ds_store", ".localized"}:
        return True

    if os.name == "nt" and p.exists():
        try:
            attrs = os.stat(p).st_file_attributes
            hidden_mask = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2) | getattr(
                stat, "FILE_ATTRIBUTE_SYSTEM", 0x4
            )
            if attrs & hidden_mask:
                return True
        except OSError:
            pass
    elif name.startswith(".") and not is_sensitive_file(p)[0]:
        return True

    return False


def is_inside_project_dir(path: Path | str, stop_at: Path | None = None) -> bool:
    """Return True if path resides inside a directory containing project markers."""
    p = Path(os.path.abspath(str(path)))
    home_norm = _norm_abs(Path.home())
    stop_norm = _norm_abs(stop_at) if stop_at is not None else None

    if p.exists() and p.is_dir():
        curr = p
    else:
        curr = p.parent

    while True:
        curr_norm = _norm_abs(curr)
        # Never treat drive roots, user home, or stop_at boundary itself as a project root.
        if curr.parent == curr or curr_norm == home_norm:
            break
        if stop_norm is not None and curr_norm == stop_norm:
            break

        if curr.exists() and curr.is_dir():
            for marker in PROJECT_MARKERS:
                if (curr / marker).exists():
                    return True
        curr = curr.parent

    return False


def is_forbidden_path(
    path: Path | str,
    scope_root: Path | None = None,
    extra_forbidden: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Return (True, reason) if path hits system/user redlines, symlinks, or project dirs."""
    p = Path(str(path))
    try:
        if p.is_symlink():
            return True, "symlink_rejected"
    except OSError:
        return True, "symlink_check_error"

    norm = _norm_abs(p)
    real_norm = _norm_real(p)

    for candidate in (norm, real_norm):
        for prefix in HARDCODED_FORBIDDEN_PREFIXES:
            if _prefix_match(candidate, prefix):
                return True, f"hardcoded_forbidden_prefix:{prefix}"
        if extra_forbidden:
            for raw_extra in extra_forbidden:
                if not raw_extra:
                    continue
                extra_norm = _norm_abs(os.path.expanduser(raw_extra))
                if _prefix_match(candidate, extra_norm):
                    return True, f"user_protected_prefix:{extra_norm}"

        parts = {part.lower() for part in Path(candidate).parts}
        hit_segments = sorted(parts & FORBIDDEN_PATH_SEGMENTS)
        if hit_segments:
            return True, f"forbidden_segment:{hit_segments[0]}"

    if is_inside_project_dir(Path(norm), stop_at=scope_root):
        return True, "inside_project_directory"

    return False, ""


def is_ignored_file(path: Path | str) -> tuple[bool, str]:
    """Return (True, reason) for temporary downloads, OS files, and ignored extensions."""
    p = Path(path)
    name_lower = p.name.lower()
    ext_lower = p.suffix.lower()

    if name_lower in IGNORED_FILENAMES:
        return True, f"ignored_filename:{name_lower}"
    if ext_lower in IGNORED_EXTENSIONS:
        return True, f"ignored_extension:{ext_lower}"
    if is_hidden_or_system(p):
        return True, "hidden_or_system_file"

    return False, ""


def is_valid_destination(
    dest_path: Path | str,
    allowed_dests: list[DestinationOption],
    extra_forbidden: Sequence[str] | None = None,
) -> bool:
    """Verify dest_path matches an allowed destination whitelist entry and is safe."""
    dest_norm = _norm_abs(dest_path)
    forbidden, _ = is_forbidden_path(dest_norm, extra_forbidden=extra_forbidden)
    if forbidden:
        return False

    allowed_norms = {_norm_abs(os.path.expanduser(d.path)) for d in allowed_dests}
    return dest_norm in allowed_norms
