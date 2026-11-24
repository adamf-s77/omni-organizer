"""Read-only full-PC directory tree, size, large-file, and duplicate-candidate auditor."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKIP_EXPAND_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".next",
    ".nuxt",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    "site-packages",
    ".gradle",
    ".idea",
    ".vs",
    "obj",
    "bin",
    "dist",
    "build",
    "target",
    "vendor",
    "Pods",
    ".dart_tool",
    "Cache",
    "Code Cache",
    "GPUCache",
    "Service Worker",
    "IndexedDB",
    "ChromeData",
    "User Data",
    "playwright-browsers",
    "ms-playwright",
}


def fmt_size(b: float) -> str:
    """Format byte count into human-readable units."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}PB"


def fmt_ts(ts: float) -> str:
    """Format POSIX timestamp as YYYY-MM-DD."""
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "-"


def is_reparse_or_cloud(entry):
    """Detect NTFS reparse points / junctions and OneDrive cloud-only placeholders."""
    try:
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        is_reparse = bool(attrs & 0x400)
        is_cloud = bool(attrs & (0x1000 | 0x400000 | 0x40000))
        return is_reparse, is_cloud, st
    except Exception:
        return False, False, None


def quick_du(root_path: str, max_files: int = 200000):
    """Fast iterative size/file/dir/mtime aggregation for folded subtrees."""
    total_size = 0
    total_files = 0
    total_dirs = 0
    latest = 0.0
    stack = [root_path]
    while stack:
        curr = stack.pop()
        try:
            with os.scandir(curr) as it:
                for entry in it:
                    is_reparse, _, st = is_reparse_or_cloud(entry)
                    if st is None:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not is_reparse:
                            total_dirs += 1
                            stack.append(entry.path)
                    else:
                        total_size += st.st_size
                        total_files += 1
                        if st.st_mtime > latest:
                            latest = st.st_mtime
                        if total_files >= max_files:
                            return total_size, total_files, total_dirs, latest
        except Exception:
            continue
    return total_size, total_files, total_dirs, latest


def scan_dir_stats(
    path: str,
    max_depth: int = 4,
    current_depth: int = 0,
    collect_files=None,
    large_files=None,
    large_thresh: int = 50 * 1024 * 1024,
):
    """Recursively gather size, counts, extensions, and tree nodes without following links."""
    res = {
        "name": os.path.basename(path) or path,
        "path": path,
        "size": 0,
        "files": 0,
        "dirs": 0,
        "latest": 0.0,
        "ext_counts": defaultdict(int),
        "ext_sizes": defaultdict(int),
        "subdirs": [],
        "direct_files": [],
        "folded": False,
        "error": None,
    }
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except Exception as e:
        res["error"] = str(e)
        return res

    entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))

    for entry in entries:
        is_reparse, is_cloud, st = is_reparse_or_cloud(entry)
        if st is None:
            continue
        if entry.is_dir(follow_symlinks=False):
            if is_reparse:
                res["subdirs"].append(
                    {
                        "name": entry.name + " [LINK/JUNCTION]",
                        "path": entry.path,
                        "size": 0,
                        "files": 0,
                        "dirs": 0,
                        "latest": st.st_mtime,
                        "subdirs": [],
                        "direct_files": [],
                        "folded": True,
                        "error": None,
                    }
                )
                continue
            res["dirs"] += 1
            fold = (entry.name in SKIP_EXPAND_DIRS) or (current_depth >= max_depth)
            if fold:
                sub_size, sub_files, sub_dirs, sub_latest = quick_du(entry.path)
                res["size"] += sub_size
                res["files"] += sub_files
                res["dirs"] += sub_dirs
                if sub_latest > res["latest"]:
                    res["latest"] = sub_latest
                res["subdirs"].append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "size": sub_size,
                        "files": sub_files,
                        "dirs": sub_dirs,
                        "latest": sub_latest,
                        "subdirs": [],
                        "direct_files": [],
                        "folded": True,
                        "error": None,
                    }
                )
            else:
                sub = scan_dir_stats(
                    entry.path,
                    max_depth,
                    current_depth + 1,
                    collect_files,
                    large_files,
                    large_thresh,
                )
                res["size"] += sub["size"]
                res["files"] += sub["files"]
                res["dirs"] += sub["dirs"]
                if sub["latest"] > res["latest"]:
                    res["latest"] = sub["latest"]
                for k, v in sub["ext_counts"].items():
                    res["ext_counts"][k] += v
                for k, v in sub["ext_sizes"].items():
                    res["ext_sizes"][k] += v
                res["subdirs"].append(sub)
        else:
            sz = st.st_size
            mt = st.st_mtime
            res["size"] += sz
            res["files"] += 1
            if mt > res["latest"]:
                res["latest"] = mt
            ext = os.path.splitext(entry.name)[1].lower() or "(no ext)"
            res["ext_counts"][ext] += 1
            res["ext_sizes"][ext] += sz
            res["direct_files"].append(
                {
                    "name": entry.name + (" [CLOUD]" if is_cloud else ""),
                    "size": sz,
                    "mtime": mt,
                }
            )
            if large_files is not None and sz >= large_thresh:
                large_files.append({"path": entry.path, "size": sz, "mtime": mt})
            if collect_files is not None:
                collect_files.append({"name": entry.name, "path": entry.path, "size": sz, "mtime": mt})
    return res


def render_tree(node, indent: int = 0, lines=None, max_direct_files: int = 25):
    """Render scanned tree dict into indented readable lines."""
    if lines is None:
        lines = []
    prefix = "  " * indent
    fold_tag = " [FOLDED]" if node.get("folded") else ""
    err_tag = f" [ERR: {node['error']}]" if node.get("error") else ""
    lines.append(
        f"{prefix}[D] {node['name']}/  ({fmt_size(node['size'])}, {node['files']}f, {node['dirs']}d, last:{fmt_ts(node['latest'])}){fold_tag}{err_tag}"
    )
    for sub in node.get("subdirs", []):
        render_tree(sub, indent + 1, lines, max_direct_files)
    dfiles = node.get("direct_files", [])
    if dfiles:
        shown = dfiles[:max_direct_files]
        for f in shown:
            lines.append(f"{prefix}  [F] {f['name']}  ({fmt_size(f['size'])}, {fmt_ts(f['mtime'])})")
        if len(dfiles) > max_direct_files:
            rem = dfiles[max_direct_files:]
            rem_sz = sum(x["size"] for x in rem)
            lines.append(f"{prefix}  ... (+{len(rem)} more files, total {fmt_size(rem_sz)})")
    return lines


def get_audit_targets(extra_targets: list[str] | None = None) -> list[tuple[str, str, int]]:
    """Return generic user directories plus optional local/CLI audit targets."""
    user_home = Path.home()
    targets: list[tuple[str, str, int]] = [
        ("Desktop", str(user_home / "Desktop"), 4),
        ("Downloads", str(user_home / "Downloads"), 3),
        ("Documents", str(user_home / "Documents"), 3),
        ("Pictures", str(user_home / "Pictures"), 3),
        ("Videos", str(user_home / "Videos"), 3),
        ("Projects", str(user_home / "Projects"), 3),
    ]
    local_file = PROJECT_ROOT / "config" / "audit_targets.local.json"
    if local_file.is_file():
        try:
            raw = json.loads(local_file.read_text(encoding="utf-8"))
            for item in raw:
                if isinstance(item, dict) and item.get("path"):
                    targets.append((str(item.get("label") or Path(item["path"]).name), str(item["path"]), int(item.get("depth", 3))))
        except Exception:
            pass
    for idx, t in enumerate(extra_targets or []):
        targets.append((f"Custom_{idx + 1}", os.path.abspath(os.path.expanduser(t)), 3))
    return targets


def run_audit(out_dir: Path, extra_targets: list[str] | None = None) -> None:
    """Run read-only audit and save tree/summary reports under out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = get_audit_targets(extra_targets)

    all_large = []
    all_files_index = []
    summary = []

    for label, path, depth in targets:
        if not os.path.exists(path):
            summary.append(f"{label}: NOT FOUND ({path})")
            continue
        print(f"Scanning {label}: {path} ...", flush=True)
        tree = scan_dir_stats(
            path,
            max_depth=depth,
            collect_files=all_files_index,
            large_files=all_large,
        )
        lines = render_tree(tree, max_direct_files=40)
        ext_top = sorted(tree["ext_sizes"].items(), key=lambda x: x[1], reverse=True)[:10]
        lines.append("\n--- Top Extensions by Size ---")
        for ext, sz in ext_top:
            lines.append(f"  {ext}: {tree['ext_counts'][ext]} files, {fmt_size(sz)}")

        out_path = out_dir / f"tree_{label}.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        summary.append(
            f"{label:20s} | {fmt_size(tree['size']):>10s} | {tree['files']:>7d} files | {tree['dirs']:>5d} dirs | last:{fmt_ts(tree['latest'])} | {path}"
        )

    user_home = str(Path.home())
    top_summaries = []
    for root_label, root_path in [
        ("C_ROOT", r"C:\\"),
        ("C_USER_ROOT", user_home),
        ("C_APPDATA_LOCAL", os.path.join(user_home, "AppData", "Local")),
        ("C_APPDATA_ROAMING", os.path.join(user_home, "AppData", "Roaming")),
        ("D_ROOT", r"D:\\"),
        ("E_ROOT", r"E:\\"),
    ]:
        top_summaries.append(f"\n=== {root_label} ({root_path}) ===")
        rows = []
        try:
            with os.scandir(root_path) as it:
                for entry in it:
                    is_reparse, _, st = is_reparse_or_cloud(entry)
                    if st is None:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if is_reparse:
                            rows.append((0, 0, f"[LINK] {entry.name}"))
                        elif entry.name in (
                            "Windows",
                            "Program Files",
                            "Program Files (x86)",
                            "System Volume Information",
                            "$RECYCLE.BIN",
                            "$Recycle.Bin",
                            "ProgramData",
                        ):
                            if root_label in ("D_ROOT", "E_ROOT"):
                                sz, fc, _, _ = quick_du(entry.path, max_files=150000)
                                rows.append((sz, fc, f"[D] {entry.name}"))
                            else:
                                rows.append((-1, 0, f"[SYS-SKIP] {entry.name}"))
                        else:
                            sz, fc, _, _ = quick_du(entry.path, max_files=150000)
                            rows.append((sz, fc, f"[D] {entry.name}"))
                    else:
                        rows.append((st.st_size, 1, f"[F] {entry.name}"))
        except Exception as e:
            top_summaries.append(f"ERROR: {e}")
        rows.sort(key=lambda x: x[0], reverse=True)
        for sz, fc, nm in rows[:30]:
            sz_str = "SKIPPED" if sz < 0 else fmt_size(sz)
            top_summaries.append(f"  {sz_str:>10s} | {fc:>7d}f | {nm}")

    all_large.sort(key=lambda x: x["size"], reverse=True)
    large_lines = ["=== LARGE FILES (>= 50MB) ==="]
    for lf in all_large[:80]:
        large_lines.append(f"  {fmt_size(lf['size']):>10s} | {fmt_ts(lf['mtime'])} | {lf['path']}")

    dup_map = defaultdict(list)
    for fi in all_files_index:
        if fi["size"] >= 100 * 1024:
            dup_map[(fi["name"].lower(), fi["size"])].append(fi["path"])
    dups = [(k, v) for k, v in dup_map.items() if len(v) > 1]
    dups.sort(key=lambda x: x[0][1] * (len(x[1]) - 1), reverse=True)
    dup_lines = ["=== DUPLICATE CANDIDATES (same name & size >= 100KB) ==="]
    for (nm, sz), paths in dups[:60]:
        dup_lines.append(f"  [{len(paths)}x {fmt_size(sz)}] {nm}")
        for p in paths[:6]:
            dup_lines.append(f"      - {p}")

    summary_file = out_dir / "SUMMARY.txt"
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write("=== TARGET SUMMARY ===\n")
        f.write("\n".join(summary))
        f.write("\n\n")
        f.write("\n".join(top_summaries))
        f.write("\n\n")
        f.write("\n".join(large_lines))
        f.write("\n\n")
        f.write("\n".join(dup_lines))

    print(f"DONE! Output saved to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only full-PC file management auditor.")
    default_out = PROJECT_ROOT / "reports" / f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    parser.add_argument(
        "--out",
        type=Path,
        default=default_out,
        help="Directory to write SUMMARY.txt and per-target tree_*.txt files.",
    )
    parser.add_argument(
        "--targets",
        nargs="*",
        default=[],
        help="Optional extra directory paths to include in the read-only audit.",
    )
    args = parser.parse_args()
    run_audit(args.out, extra_targets=args.targets)


if __name__ == "__main__":
    main()
