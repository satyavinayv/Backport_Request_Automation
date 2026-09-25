"""
Phase 4 — Repo file-path search.

When all earlier ID-extraction phases fail, this module takes the list of
changed file paths from the MR diff, infers a search scope from each path,
and searches the GitLab project for .feature files that reference the filename.
TC/Xray IDs are extracted directly from those feature file contents.
"""
import os
import re

from clients.gitlab import search_blobs, fetch_raw_file_content
from utils.log import info, warn

_TC_ID_RE = re.compile(r"\bTC-\d+\b")
_XRAY_ID_RE = re.compile(r"\bDEV-\d+\b")

# Walk up from the filename; stop at the first of these tokens and keep one level below.
_SCOPE_BOUNDARIES = {"src", "test", "tests", "resources", "features", "main", "java"}

_MAX_FEATURE_FILES = 20   # cap per changed file to avoid runaway searches

# File types that are never referenced by name inside .feature files.
# Searching for these in the repo would only produce false positives.
_PHASE4_SKIP_EXTENSIONS = {
    ".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".pptx", ".ppt", ".ods", ".odt",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".ico", ".tiff", ".tif", ".webp",
    ".mp3", ".mp4", ".avi", ".wav", ".mov", ".mkv", ".flv", ".webm",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".jar", ".war", ".ear",
    ".class", ".bin", ".exe", ".dll", ".so", ".dylib", ".pyc",
    ".ttf", ".woff", ".woff2", ".eot",
    ".xml",  # XML config/data files are not referenced directly in feature steps
}


def infer_search_scope(file_path: str) -> str:
    """
    Derive the folder scope to search within for a given changed file path.

    e.g.  Vault-Platform-Tests/src/test/resources/Platform/files/bulk.xlsx
          → Vault-Platform-Tests/src/test/resources/Platform
    """
    parts = file_path.replace("\\", "/").split("/")
    for i in range(len(parts) - 2, -1, -1):
        if parts[i].lower() in _SCOPE_BOUNDARIES:
            end = min(i + 2, len(parts) - 1)
            return "/".join(parts[:end])
    return "/".join(parts[:-1]) if len(parts) > 1 else ""


def _extract_ids_from_content(content: str):
    tc_ids = sorted({m.group(0) for m in _TC_ID_RE.finditer(content)})
    xray_ids = sorted({m.group(0) for m in _XRAY_ID_RE.finditer(content)})
    return tc_ids, xray_ids


def search_repo_for_file_usages(project_id_encoded, file_path, ref="develop"):
    """
    Phase 4: find .feature files in the repo that reference the changed filename,
    then extract TC/Xray IDs from their content.

    Returns (tc_ids, xray_ids, details, total_feature_hits)
      details: list of {file, tc_ids, xray_ids} for each matched feature file
      total_feature_hits: raw hit count before the 20-file cap
    """
    _, ext = os.path.splitext(file_path.lower())
    if ext in _PHASE4_SKIP_EXTENSIONS:
        info(f"  Phase 4: skipping '{file_path}' — binary/document/config files are never referenced in feature steps.")
        return [], [], [], 0

    filename = os.path.basename(file_path)
    scope = infer_search_scope(file_path)

    info(f"  Phase 4: searching for .feature files referencing '{filename}' (scope: '{scope or 'entire repo'}') ...")

    try:
        blobs = search_blobs(project_id_encoded, filename, ref=ref)
    except Exception as exc:
        warn(f"  Phase 4: GitLab blob search failed: {exc}")
        return [], [], [], 0

    feature_hits = [
        b for b in blobs
        if b.get("path", "").endswith(".feature")
        and (not scope or b.get("path", "").startswith(scope))
    ]
    total_hits = len(feature_hits)

    if total_hits == 0:
        return [], [], [], 0

    if total_hits > _MAX_FEATURE_FILES:
        warn(
            f"  Phase 4: {total_hits} feature files reference '{filename}' in scope. "
            f"Checking first {_MAX_FEATURE_FILES} — results may be partial."
        )
        feature_hits = feature_hits[:_MAX_FEATURE_FILES]

    seen_tcs: set = set()
    seen_xrays: set = set()
    all_tc_ids = []
    all_xray_ids = []
    details = []

    for blob in feature_hits:
        fpath = blob.get("path", "")
        content = fetch_raw_file_content(project_id_encoded, fpath, ref)
        if not content:
            continue
        tcs, xrays = _extract_ids_from_content(content)
        new_tcs = [t for t in tcs if t not in seen_tcs]
        new_xrays = [x for x in xrays if x not in seen_xrays]
        if new_tcs or new_xrays:
            details.append({"file": fpath, "tc_ids": new_tcs, "xray_ids": new_xrays})
            all_tc_ids.extend(new_tcs)
            all_xray_ids.extend(new_xrays)
            seen_tcs.update(new_tcs)
            seen_xrays.update(new_xrays)

    return sorted(all_tc_ids), sorted(all_xray_ids), details, total_hits
