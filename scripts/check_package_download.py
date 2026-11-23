#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026-present LibreELEC (https://libreelec.tv)

"""
Check package downloads and checksum verification for LibreELEC packages.
Uses LibreELEC's existing download scripts (scripts/get) to verify package
availability and hash consistency.
"""

import argparse
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


def parse_package_mk(mk_path: str) -> Dict[str, str]:
    """Extract metadata from package.mk using regex and basic expansion."""
    info = {
        "name": os.path.basename(os.path.dirname(mk_path)),
        "version": "",
        "url": "",
        "sha256": "",
        "git_sha": "",
    }

    try:
        with open(mk_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Extract basic variables
        name_m = re.search(r'^\s*PKG_NAME=["\']?([^"\']+)["\']?', content, re.MULTILINE)
        if name_m:
            info["name"] = name_m.group(1).strip()

        ver_m = re.search(r'^\s*PKG_VERSION=["\']?([^"\']+)["\']?', content, re.MULTILINE)
        if ver_m:
            info["version"] = ver_m.group(1).strip()

        sha_m = re.search(r'^\s*PKG_SHA256=["\']?([^"\']+)["\']?', content, re.MULTILINE)
        if sha_m:
            info["sha256"] = sha_m.group(1).strip()

        git_m = re.search(r'^\s*PKG_GIT_SHA=["\']?([^"\']+)["\']?', content, re.MULTILINE)
        if git_m:
            info["git_sha"] = git_m.group(1).strip()

        url_m = re.search(r'^\s*PKG_URL=["\']?([^"\']+)["\']?', content, re.MULTILINE)
        if url_m:
            raw_url = url_m.group(1).strip()
            # Basic variable substitution
            raw_url = raw_url.replace("${PKG_NAME}", info["name"])
            raw_url = raw_url.replace("${PKG_VERSION}", info["version"])
            info["url"] = raw_url
    except Exception as e:
        info["parse_error"] = str(e)

    return info


def patch_libreelec_scripts(le_dir: str, primary_only: bool) -> None:
    """Optimize get_archive for download checking and enforce primary URL if requested."""
    get_archive_path = os.path.join(le_dir, "scripts", "get_archive")
    if not os.path.isfile(get_archive_path):
        return

    try:
        with open(get_archive_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Reduce curl retries from 10 to 1 loop so dead URLs fail quickly
        content = re.sub(r"\bNBGET=10\b", "NBGET=1", content)
        content = re.sub(r"\bNBCHKS=2\b", "NBCHKS=1", content)

        if primary_only:
            # Only loop over primary PKG_URL, skip PACKAGE_MIRROR
            content = re.sub(
                r'for url in "\${PKG_URL}" "\${PACKAGE_MIRROR}"; do',
                'for url in "${PKG_URL}"; do',
                content,
            )

        with open(get_archive_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[setup] scripts/get_archive patched (primary_only=%s, NBGET=1)" % primary_only)
    except Exception as e:
        print("[setup] Warning: failed to patch scripts/get_archive: %s" % e, file=sys.stderr)


def discover_packages(le_dir: str, scope: str, pattern: Optional[str] = None) -> List[Tuple[str, str]]:
    """
    Find all package.mk files in packages/ matching scope and pattern.
    Returns sorted list of (package_name, relative_package_path).
    """
    packages_dir = os.path.join(le_dir, "packages")
    if not os.path.isdir(packages_dir):
        raise FileNotFoundError(f"Directory not found: {packages_dir}")

    discovered = []
    for root, _, files in os.walk(packages_dir):
        if "package.mk" in files:
            rel_path = os.path.relpath(root, le_dir)
            pkg_name = os.path.basename(root)

            is_addon = "packages/addons" in rel_path or rel_path.startswith("packages/addons")

            if scope == "core" and is_addon:
                continue
            if scope == "addons" and not is_addon:
                continue

            if pattern:
                pattern_lower = pattern.lower().strip()
                if not fnmatch.fnmatch(pkg_name.lower(), pattern_lower) and pattern_lower not in pkg_name.lower():
                    continue

            discovered.append((pkg_name, rel_path))

    # Sort deterministically by package name
    discovered.sort(key=lambda x: x[0])
    return discovered


def clean_sources(le_dir: str, pkg_name: str) -> None:
    """Remove downloaded package sources to conserve runner disk space."""
    sources_dir = os.path.join(le_dir, "sources")
    pkg_source_dir = os.path.join(sources_dir, pkg_name)

    if os.path.isdir(pkg_source_dir):
        shutil.rmtree(pkg_source_dir, ignore_errors=True)

    # Also clean any stamp or temp files
    for f in glob.glob(os.path.join(sources_dir, f"{pkg_name}*")):
        try:
            if os.path.isdir(f):
                shutil.rmtree(f, ignore_errors=True)
            else:
                os.remove(f)
        except OSError:
            pass


def check_single_package(le_dir: str, pkg_name: str, rel_path: str) -> Dict[str, str]:
    """Run ./scripts/get <package> and evaluate the download & hash verification."""
    mk_path = os.path.join(le_dir, rel_path, "package.mk")
    pkg_meta = parse_package_mk(mk_path)

    result = {
        "package": pkg_name,
        "path": rel_path,
        "version": pkg_meta.get("version", ""),
        "url": pkg_meta.get("url", ""),
        "expected_hash": pkg_meta.get("sha256") or pkg_meta.get("git_sha", ""),
        "calculated_hash": "",
        "status": "UNKNOWN",
        "detail": "",
    }

    if not pkg_meta.get("url"):
        # Package has no URL (meta package or local source)
        result["status"] = "SKIPPED"
        result["detail"] = "No PKG_URL defined (local/virtual package)"
        return result

    env = os.environ.copy()
    env["PROJECT"] = "Generic"
    env["ARCH"] = "x86_64"
    env["LC_ALL"] = "C"

    cmd = ["./scripts/get", pkg_name]
    try:
        proc = subprocess.run(
            cmd,
            cwd=le_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
        )
        output = proc.stdout or ""
    except subprocess.TimeoutExpired:
        result["status"] = "DOWNLOAD_FAILED"
        result["detail"] = "Timeout after 300s"
        clean_sources(le_dir, pkg_name)
        return result
    except Exception as e:
        result["status"] = "DOWNLOAD_FAILED"
        result["detail"] = f"Execution error: {e}"
        clean_sources(le_dir, pkg_name)
        return result

    # Try to extract the resolved URL from script output
    url_match = re.search(r"GET\s+\S+\s+\[(?:primary|mirror)\]\s+(\S+)", output)
    if url_match:
        result["url"] = url_match.group(1).strip()

    # Try to extract calculated checksum
    calc_match = re.search(r"Calculated checksum:\s+([a-fA-F0-9]{64})", output)
    if calc_match:
        result["calculated_hash"] = calc_match.group(1).strip()

    if proc.returncode == 0:
        result["status"] = "SUCCESS"
        result["detail"] = "Download OK, hash verified"
    else:
        # Check for CRC/checksum mismatch
        mismatch_match = re.search(
            r"Incorrect checksum calculated on downloaded file:\s*got\s*([a-fA-F0-9]+)\s*wanted\s*([a-fA-F0-9]+)",
            output,
        )
        git_mismatch = re.search(
            r"Incorrect git hash in repository:\s*got\s*([a-fA-F0-9]+),\s*wanted\s*([a-fA-F0-9]+)",
            output,
        )

        if mismatch_match:
            result["status"] = "CHECKSUM_MISMATCH"
            result["calculated_hash"] = mismatch_match.group(1).strip()
            result["expected_hash"] = mismatch_match.group(2).strip()
            result["detail"] = f"Calculated: {result['calculated_hash']} != Expected: {result['expected_hash']}"
        elif git_mismatch:
            result["status"] = "CHECKSUM_MISMATCH"
            result["calculated_hash"] = git_mismatch.group(1).strip()
            result["expected_hash"] = git_mismatch.group(2).strip()
            result["detail"] = f"Git SHA: {result['calculated_hash']} != Expected: {result['expected_hash']}"
        else:
            result["status"] = "DOWNLOAD_FAILED"
            # Extract meaningful error reason
            if "The requested URL returned error: 404" in output or "404 Not Found" in output:
                result["detail"] = "HTTP 404 Not Found"
            elif "Could not resolve host" in output:
                result["detail"] = "DNS resolution failed"
            elif "Connection timed out" in output or "connect-timeout" in output:
                result["detail"] = "Connection timed out"
            elif "SSL certificate problem" in output:
                result["detail"] = "SSL certificate validation error"
            elif "There is no commit" in output:
                cm = re.search(r"There is no commit [^\n]+", output)
                result["detail"] = cm.group(0) if cm else "Git commit not found"
            else:
                last_lines = [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("*")]
                result["detail"] = last_lines[-1] if last_lines else "Download failed"

    # Conserve disk space immediately
    clean_sources(le_dir, pkg_name)
    return result


def generate_markdown_report(results: List[Dict[str, str]]) -> str:
    """Format check results into a clear GitHub Markdown summary table."""
    total = len(results)
    success = sum(1 for r in results if r["status"] == "SUCCESS")
    mismatches = [r for r in results if r["status"] == "CHECKSUM_MISMATCH"]
    failures = [r for r in results if r["status"] == "DOWNLOAD_FAILED"]
    skipped = sum(1 for r in results if r["status"] == "SKIPPED")

    md = []
    md.append("## 📦 LibreELEC Package Download & Checksum Verification")
    md.append("")
    md.append("| Metric | Count | Status |")
    md.append("| :--- | :---: | :---: |")
    md.append(f"| 📦 **Total Packages Checked** | **{total}** | - |")
    md.append(f"| ✅ **Download & Checksum OK** | **{success}** | {'🟢 Pass' if success == (total - skipped) else 'ℹ️'} |")
    md.append(f"| ❌ **Download Unavailable / Failed** | **{len(failures)}** | {'🔴 Failure' if failures else '🟢 None'} |")
    md.append(f"| ⚠️ **CRC / Hash Mismatch** | **{len(mismatches)}** | {'🟠 Mismatch' if mismatches else '🟢 None'} |")
    md.append(f"| ⏭️ **Skipped (No URL / Local)** | **{skipped}** | ⚪ |")
    md.append("")

    issues = failures + mismatches
    if issues:
        md.append("### 🚨 Detected Issues")
        md.append("")
        md.append("| Status | Package | Version | Source URL | Expected Hash | Detail / Calculated |")
        md.append("| :---: | :--- | :--- | :--- | :--- | :--- |")
        for item in issues:
            icon = "❌ Failed" if item["status"] == "DOWNLOAD_FAILED" else "⚠️ CRC Error"
            pkg = f"`{item['package']}`"
            ver = item.get("version", "-") or "-"
            url = f"[Link]({item['url']})" if item.get("url") else "-"
            exp = f"`{item['expected_hash'][:16]}...`" if item.get("expected_hash") else "-"
            detail = item.get("detail", "")
            if item["status"] == "CHECKSUM_MISMATCH" and item.get("calculated_hash"):
                detail = f"Got: `{item['calculated_hash'][:16]}...`"
            md.append(f"| {icon} | {pkg} | {ver} | {url} | {exp} | {detail} |")
        md.append("")
    else:
        md.append("### 🎉 All verified packages downloaded successfully with matching checksums!")
        md.append("")

    return "\n".join(md)


def run_shard(args: argparse.Namespace) -> None:
    """Execute checking for the assigned shard."""
    le_dir = os.path.abspath(args.le_dir)
    primary_only = str(args.primary_only).lower() in ("true", "1", "yes")

    patch_libreelec_scripts(le_dir, primary_only=primary_only)

    all_packages = discover_packages(le_dir, scope=args.scope, pattern=args.filter)
    total_pkgs = len(all_packages)

    shard = max(1, args.shard)
    total_shards = max(1, args.total_shards)

    assigned = [p for idx, p in enumerate(all_packages) if (idx % total_shards) == (shard - 1)]

    print(f"=== Shard {shard}/{total_shards}: Checking {len(assigned)} of {total_pkgs} packages (scope: {args.scope}) ===")

    results = []
    for i, (pkg_name, rel_path) in enumerate(assigned, 1):
        print(f"[{i}/{len(assigned)}] Checking {pkg_name} ({rel_path})...", flush=True)
        res = check_single_package(le_dir, pkg_name, rel_path)
        print(f"    -> {res['status']}: {res['detail']}")
        results.append(res)

    if args.output_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Saved shard JSON results to {args.output_json}")

    if args.output_md:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_md)), exist_ok=True)
        with open(args.output_md, "w", encoding="utf-8") as f:
            f.write(generate_markdown_report(results))
        print(f"Saved shard Markdown report to {args.output_md}")


def run_aggregate(args: argparse.Namespace) -> None:
    """Combine results from all shard JSON files into a consolidated report."""
    results = []
    seen = set()

    json_files = glob.glob(os.path.join(args.aggregate_dir, "**", "*.json"), recursive=True)
    if not json_files:
        print(f"Warning: No JSON files found in {args.aggregate_dir}", file=sys.stderr)

    for path in sorted(json_files):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        pkg = item.get("package")
                        if pkg and pkg not in seen:
                            seen.add(pkg)
                            results.append(item)
        except Exception as e:
            print(f"Error reading {path}: {e}", file=sys.stderr)

    results.sort(key=lambda x: x.get("package", ""))

    report_md = generate_markdown_report(results)
    print(report_md)

    if args.summary_file:
        try:
            with open(args.summary_file, "a", encoding="utf-8") as f:
                f.write(report_md + "\n")
            print(f"Appended report to summary file: {args.summary_file}")
        except Exception as e:
            print(f"Error writing summary file: {e}", file=sys.stderr)

    if args.output_md:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_md)), exist_ok=True)
        with open(args.output_md, "w", encoding="utf-8") as f:
            f.write(report_md + "\n")
        print(f"Saved report to {args.output_md}")

    if args.fail_on_errors:
        failures = sum(1 for r in results if r.get("status") in ("DOWNLOAD_FAILED", "CHECKSUM_MISMATCH"))
        if failures > 0:
            print(f"ERROR: {failures} package(s) failed download or checksum verification!", file=sys.stderr)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="LibreELEC package download and checksum verification tool")
    parser.add_argument("--le-dir", default="LibreELEC.tv", help="Path to LibreELEC.tv repository")
    parser.add_argument("--scope", choices=["core", "addons", "all"], default="core", help="Package scope to check")
    parser.add_argument("--shard", type=int, default=1, help="Current shard number (1-based)")
    parser.add_argument("--total-shards", type=int, default=1, help="Total number of shards")
    parser.add_argument("--primary-only", default="true", help="Test upstream primary URL only")
    parser.add_argument("--filter", default=None, help="Filter package name (pattern or substring)")
    parser.add_argument("--output-json", default=None, help="Path to save results as JSON")
    parser.add_argument("--output-md", default=None, help="Path to save results as Markdown")

    # Aggregation mode
    parser.add_argument("--aggregate-dir", default=None, help="Directory containing shard JSON results to aggregate")
    parser.add_argument("--summary-file", default=None, help="File to append Markdown summary (e.g. GITHUB_STEP_SUMMARY)")
    parser.add_argument("--fail-on-errors", action="store_true", help="Exit with code 1 if any failure or mismatch occurred")

    args = parser.parse_args()

    if args.aggregate_dir:
        run_aggregate(args)
    else:
        run_shard(args)


if __name__ == "__main__":
    main()
