#!/usr/bin/env python3
"""
BVT Failure Analyzer — triage.py
Mirrors the Bob BVT Failure Analyzer skill classification logic (Steps 4-8).

Inputs (via environment variables set by action.yml):
  RUN_ID        - GitHub Actions run ID
  WORKFLOW      - Name of the failing CI workflow
  PR_NUMBER     - PR number (may be empty)
  REPO          - owner/repo
  ARTIFACT_DIR  - path to downloaded artifacts root
  ANNOTATIONS   - path to annotations.json
  CHANGED_FILES - path to newline-separated list of changed files

Output: Markdown triage report written to stdout.
"""

import json
import os
import re
import sys
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# ── Environment inputs ────────────────────────────────────────────────────────
RUN_ID        = os.environ.get("RUN_ID", "unknown")
WORKFLOW      = os.environ.get("WORKFLOW", "unknown")
PR_NUMBER     = os.environ.get("PR_NUMBER", "").strip()
REPO          = os.environ.get("REPO", "ibm-webmethods/esb")
ARTIFACT_DIR  = Path(os.environ.get("ARTIFACT_DIR", "/tmp/bvt_triage/artifacts"))
ANNOTATIONS   = Path(os.environ.get("ANNOTATIONS", "/tmp/bvt_triage/annotations.json"))
CHANGED_FILES = Path(os.environ.get("CHANGED_FILES", "/tmp/bvt_triage/changed_files.txt"))

# ── Classification patterns (from BVT Failure Analyzer skill — Step 7) ───────
# Ordered: first match wins. Infra patterns are checked before code patterns.
INFRA_PATTERNS = [
    (r"nRealmUnreachableException",                            "INFRA: UM Realm Unreachable"),
    (r"Realm was still unreachable after max retry",           "INFRA: UM Realm Unreachable"),
    (r"no consumers listening on path",                        "INFRA: Cascade (no consumers)"),
    (r"Failed to allocate session",                            "INFRA: Cascade (session alloc)"),
    (r"IntegrationLiveException",                              "INFRA: IntegrationLive failure"),
    (r"Connection (?:refused|timed out|reset)",                "INFRA: Network / connectivity"),
    (r"URI is not absolute",                                   "INFRA: JVM/image flake (URI)"),
    (r"MalformedURLException",                                 "INFRA: JVM/image flake (URL)"),
    (r"java\.net\.SocketTimeoutException",                     "INFRA: Socket timeout"),
    (r"java\.net\.ConnectException",                           "INFRA: Connect refused"),
    (r"org\.opentest4j\.AssertionFailedError.*timed? ?out",    "INFRA: Test timeout"),
    (r"Process completed with exit code 1.*timeout",           "INFRA: Runner timeout"),
]


def classify(title: str, message: str, path: str, changed_modules: set) -> tuple:
    """
    Return (category: str, caused_by_change: bool | None).
    None = cannot determine.
    """
    combined = f"{title} {message}"

    for pattern, label in INFRA_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return label, False

    # Gradle / JUnit: correlate with PR diff via module name
    if path:
        mod = extract_module(path)
        if mod and mod in changed_modules:
            return f"CODE CHANGE: regression in `{mod}` (module in PR diff)", True

    # Venus: annotation title contains suite name — check module proximity
    if title:
        for mod in changed_modules:
            if mod.lower() in title.lower():
                return f"CODE CHANGE: suite name matches changed module `{mod}`", True

    # NullPointerException in any test is suspicious if the PR touched Java
    if re.search(r"NullPointerException", combined):
        if changed_modules:
            return "CODE CHANGE (likely): NullPointerException — check PR diff", True
        return "UNKNOWN: NullPointerException — no diff correlation", None

    return "UNKNOWN: needs manual review", None


def extract_module(path: str) -> str:
    """Extract the Gradle/Maven module from a file path (segment before src/)."""
    parts = Path(path).parts
    for i, p in enumerate(parts):
        if p == "src" and i > 0:
            return parts[i - 1]
    return parts[0] if parts else ""


def load_changed_modules() -> set:
    if not CHANGED_FILES.exists():
        return set()
    modules = set()
    for line in CHANGED_FILES.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        m = extract_module(line)
        if m and m not in (".", ""):
            modules.add(m)
    return modules


def load_annotations() -> list:
    if not ANNOTATIONS.exists():
        return []
    try:
        data = json.loads(ANNOTATIONS.read_text(errors="replace"))
        return [a for a in data if a.get("annotation_level") == "failure"]
    except Exception as e:
        print(f"# Warning: could not parse annotations.json: {e}", file=sys.stderr)
        return []


def strip_html_to_text(html: str) -> str:
    """Simple tag-stripping fallback when bs4 is unavailable."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(l.strip() for l in text.split("\n") if l.strip())


# ── Venus BVT parser (IS_BVT_Reports / MSR_BVT_Reports) ─────────────────────
def parse_venus_reports(artifact_root: Path) -> list:
    results = []
    for index_html in artifact_root.rglob("testResults/index.html"):
        source_label = "BVTs on IS" if "IS_BVT" in str(artifact_root) else "BVTs on MSR"
        results.extend(_parse_venus_index(index_html, source_label))
    return results


def _parse_venus_index(index_html: Path, source_label: str) -> list:
    failures = []
    try:
        content = index_html.read_text(errors="replace")
    except Exception:
        return failures

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.DOTALL)
    for row in rows:
        cols = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cols) < 7:
            continue
        try:
            name    = re.sub(r"<[^>]+>", "", cols[0]).strip()
            elapsed = re.sub(r"<[^>]+>", "", cols[2]).strip()
            total   = int(re.sub(r"[^\d]", "", cols[4]) or "0")
            passed  = int(re.sub(r"[^\d]", "", cols[5]) or "0")
            failed  = int(re.sub(r"[^\d]", "", cols[6]) or "0")
            if failed > 0:
                # Attempt to read the suite HTML for error messages
                href_m = re.search(r"href='([^']+)'", cols[0])
                error_msg = ""
                if href_m:
                    import urllib.parse
                    href_decoded = urllib.parse.unquote(href_m.group(1))
                    suite_file = index_html.parent / href_decoded
                    if suite_file.exists():
                        error_msg = _extract_venus_suite_error(suite_file)
                failures.append({
                    "suite":      name,
                    "path":       "",
                    "msg":        error_msg,
                    "failed":     failed,
                    "total":      total,
                    "elapsed":    elapsed,
                    "source":     source_label,
                })
        except Exception:
            continue
    return failures


def _extract_venus_suite_error(suite_file: Path) -> str:
    try:
        html = suite_file.read_text(errors="replace")
        text = strip_html_to_text(html)
        # Look for "Received Exception" pattern which is diagnostic
        m = re.search(r"Received Exception[:\s]+(.{0,300})", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # Fallback: first 200 chars after "Exception"
        m = re.search(r"Exception[:\s]+(.{0,200})", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


# ── Gradle HTML parser (JUnitAndIntegrationTestReports / KarateTestReports) ──
def parse_gradle_reports(artifact_root: Path) -> list:
    failures = []
    for index_html in artifact_root.rglob("index.html"):
        try:
            text = strip_html_to_text(index_html.read_text(errors="replace"))
        except Exception:
            continue
        m = re.search(r"(\d+)\s+failure", text, re.IGNORECASE)
        if not m or int(m.group(1)) == 0:
            continue
        fail_count = int(m.group(1))
        # Walk sibling classes/ directory for individual test details
        classes_dir = index_html.parent / "classes"
        if classes_dir.exists():
            for class_html in classes_dir.glob("*.html"):
                class_failures = _parse_gradle_class(class_html, str(artifact_root))
                failures.extend(class_failures)
        else:
            # No classes dir — create a summary entry from the index
            failures.append({
                "suite":   str(index_html.relative_to(artifact_root)),
                "path":    "",
                "msg":     f"{fail_count} failure(s) — see artifact for details",
                "failed":  fail_count,
                "total":   0,
                "elapsed": "",
                "source":  "Unit & Integration Tests",
            })
    return failures


def _parse_gradle_class(class_html: Path, artifact_root_str: str) -> list:
    failures = []
    try:
        text = strip_html_to_text(class_html.read_text(errors="replace"))
    except Exception:
        return failures

    # Extract class name from filename (com.example.FooTest.html → com.example.FooTest)
    class_name = class_html.stem
    java_path  = class_name.replace(".", "/") + ".java"

    # Find test method names paired with failures
    # Gradle test HTML typically lists "method name ... FAILED"
    failed_methods = re.findall(r"(\w[\w$]*)\s+FAILED", text)
    if not failed_methods:
        # Try to detect at least one failure
        if not re.search(r"failure|FAILED|Error", text, re.IGNORECASE):
            return failures
        failed_methods = [f"{class_name} (method unknown)"]

    # Extract first meaningful exception from the text
    exc_m = re.search(
        r"((?:java\.\w+\.\w+Exception|AssertionError|NullPointerException)"
        r"[^\n]{0,300})",
        text
    )
    error_msg = exc_m.group(1).strip() if exc_m else ""

    for method in set(failed_methods):
        failures.append({
            "suite":   f"{class_name} > {method}",
            "path":    java_path,
            "msg":     error_msg[:250],
            "failed":  1,
            "total":   1,
            "elapsed": "",
            "source":  "Unit & Integration Tests",
        })
    return failures


# ── Karate report parser ──────────────────────────────────────────────────────
def parse_karate_reports(artifact_root: Path) -> list:
    failures = []
    for summary_file in artifact_root.rglob("karate-summary-json.txt"):
        try:
            data = json.loads(summary_file.read_text(errors="replace"))
        except Exception:
            continue
        for feat in data.get("featureSummary", []):
            if feat.get("scenariosfailed", 0) > 0:
                failures.append({
                    "suite":   feat.get("name", "unknown"),
                    "path":    feat.get("relativePath", ""),
                    "msg":     f"{feat.get('scenariosfailed')} scenario(s) failed",
                    "failed":  feat.get("scenariosfailed", 1),
                    "total":   feat.get("scenarios", 1),
                    "elapsed": "",
                    "source":  "Karate Tests",
                })
    return failures


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    changed_modules = load_changed_modules()
    annotations     = load_annotations()
    failures        = []

    # Priority 1: annotations from GitHub check runs (fastest, no HTML needed)
    if annotations:
        print(f"# Using {len(annotations)} failure annotations from check runs",
              file=sys.stderr)
        for ann in annotations:
            title = ann.get("title", "")
            msg   = ann.get("message", "")
            path  = ann.get("path", "")
            cat, by_change = classify(title, msg, path, changed_modules)
            failures.append({
                "suite":   (title or "unknown")[:80],
                "path":    path,
                "msg":     msg[:200],
                "failed":  1,
                "total":   1,
                "elapsed": "",
                "source":  "Annotation",
                "category":  cat,
                "by_change": by_change,
            })

    # Priority 2: artifact HTML reports
    if not failures and ARTIFACT_DIR.exists():
        print("# Falling back to artifact HTML reports", file=sys.stderr)

        for artifact_name in ["IS_BVT_Reports", "MSR_BVT_Reports"]:
            art_dir = ARTIFACT_DIR / artifact_name
            if art_dir.exists():
                for f in parse_venus_reports(art_dir):
                    cat, by_change = classify(f["suite"], f["msg"], f["path"], changed_modules)
                    f["category"]  = cat
                    f["by_change"] = by_change
                    failures.append(f)

        for artifact_name in ["JUnitAndIntegrationTestReports"]:
            art_dir = ARTIFACT_DIR / artifact_name
            if art_dir.exists():
                for f in parse_gradle_reports(art_dir):
                    cat, by_change = classify(f["suite"], f["msg"], f["path"], changed_modules)
                    f["category"]  = cat
                    f["by_change"] = by_change
                    failures.append(f)

        for artifact_name in ["KarateTestReports"]:
            art_dir = ARTIFACT_DIR / artifact_name
            if art_dir.exists():
                for f in parse_karate_reports(art_dir):
                    cat, by_change = classify(f["suite"], f["msg"], f["path"], changed_modules)
                    f["category"]  = cat
                    f["by_change"] = by_change
                    failures.append(f)

    # ── Compute overall verdict ───────────────────────────────────────────────
    verdicts = [f.get("by_change") for f in failures if f.get("by_change") is not None]
    if not failures:
        verdict = "NO_FAILURES_DETECTED — artifacts may not have been uploaded"
    elif not verdicts:
        verdict = "UNKNOWN — could not classify any failure"
    elif all(v is False for v in verdicts):
        verdict = "ALL_INFRA ✅ — safe to re-run, no code fix required"
    elif all(v is True for v in verdicts):
        verdict = "ALL_CODE ❌ — fix required before re-run"
    else:
        code_count  = sum(1 for v in verdicts if v is True)
        infra_count = sum(1 for v in verdicts if v is False)
        verdict = f"MIXED ⚠️ — {code_count} code regression(s), {infra_count} infra/flaky failure(s)"

    # ── Render Markdown ───────────────────────────────────────────────────────
    pr_ref  = f"PR #{PR_NUMBER}" if PR_NUMBER and PR_NUMBER not in ("", "null") else "manual run"
    run_url = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"

    lines = [
        "<!-- bvt-triage -->",
        f"## 🔍 BVT Triage Report — {pr_ref}",
        "",
        "| | |",
        "|---|---|",
        f"| **Workflow** | `{WORKFLOW}` |",
        f"| **Run** | [{RUN_ID}]({run_url}) |",
        f"| **Changed modules** | {', '.join(f'`{m}`' for m in sorted(changed_modules)) or '_none detected_'} |",
        f"| **Failures found** | {len(failures)} |",
        "",
        "---",
        "",
        "### Failing Tests / Suites",
        "",
        "| Test / Suite | Source | Failed/Total | Category | Caused by change? |",
        "|---|---|---|---|---|",
    ]

    for f in failures[:60]:  # cap at 60 rows to stay within comment size limits
        icon = ("✅ Yes" if f.get("by_change") is True
                else "❌ No" if f.get("by_change") is False
                else "❓ Unknown")
        suite_label = f["suite"][:70].replace("|", "\\|")
        lines.append(
            f"| `{suite_label}` | {f['source']} | {f.get('failed',1)}/{f.get('total',1)} "
            f"| {f.get('category','UNKNOWN')} | {icon} |"
        )

    if len(failures) > 60:
        lines.append(f"\n> ⚠️ _Showing 60 of {len(failures)} failures. "
                     f"[View full run]({run_url}) for the complete list._")

    lines += [
        "",
        "---",
        "",
        "### Overall Verdict",
        "",
        f"> **`{verdict}`**",
        "",
    ]

    # Add a brief explanation for each root-cause group
    groups: dict = {}
    for f in failures:
        cat = f.get("category", "UNKNOWN")
        groups.setdefault(cat, []).append(f)

    if groups:
        lines.append("### Root-cause groups")
        lines.append("")
        for cat, items in sorted(groups.items()):
            icon = "❌" if any(i.get("by_change") for i in items) else "✅"
            lines.append(f"**{icon} {cat}** — {len(items)} failure(s)")
            # Show one representative message
            rep = next((i["msg"] for i in items if i.get("msg")), "")
            if rep:
                lines.append(f"> `{rep[:200]}`")
            lines.append("")

    lines.append(
        "_Generated automatically by "
        "[BVT Triage Action]"
        f"(https://github.com/{REPO}/tree/main/.github/actions/bvt-triage)_"
    )

    print("\n".join(lines))


if __name__ == "__main__":
    main()
