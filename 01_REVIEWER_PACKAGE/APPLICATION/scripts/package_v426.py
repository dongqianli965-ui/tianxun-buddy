"""Build the V4.2.5.1/V4.2.6 source evidence packages without private data."""
from pathlib import Path, PurePosixPath
import argparse, datetime, hashlib, json, re, zipfile

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports/v426"
RELEASE = ROOT / "release"

PRIVATE_NAMES = {
    ".pg-dsn", ".pgpass", ".netrc", "credentials.json",
    "config.private.json", "users.private.json", "local_pilot_access.private.txt",
    ".npmrc", ".pypirc", "service-account.json", "storage-state.json", "auth-state.json",
}
PRIVATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".dump", ".zip", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pdf"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sensitive_name(path) -> bool:
    p = PurePosixPath(str(path).replace("\\", "/"))
    return (
        any(part.lower() in PRIVATE_NAMES or part.lower().startswith(".env") or ".private." in part.lower() or part.lower().endswith((".pg-dsn", ".secret", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm")) for part in p.parts)
        or p.suffix.lower() in PRIVATE_SUFFIXES
        or any(part.lower() in {".venv", "venv", "env", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist", "__pycache__", "test-results", "playwright-artifacts", "screenshots"} for part in p.parts)
    )


def scan_payload(path, data: bytes, private_values=()):
    p = PurePosixPath(str(path).replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or any(":" in part for part in p.parts):
        raise ValueError("UNSAFE_ARCHIVE_PATH:" + str(path))
    if sensitive_name(p):
        raise ValueError("SENSITIVE_PATH:" + str(path))
    for value in private_values:
        if value and len(value) >= 8 and value.encode("utf-8") in data:
            raise ValueError("PRIVATE_VALUE_FOUND:" + str(path))
    text = data.decode("utf-8", errors="ignore")
    key_pattern = r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{32,}"
    if re.search(key_pattern, text):
        raise ValueError("PRIVATE_KEY_CONTENT:" + str(path))
    dsn_pattern = re.compile(r"postgres(?:ql)?://([^:\s/@]+):([^@\s/]+)@([^/\s?#]+)", re.I)
    placeholders = {"user", "username", "pass", "password", "example", "dummy", "placeholder", "redacted", "xxx", "changeme"}
    for match in dsn_pattern.finditer(text):
        username, password, _host = (part.lower() for part in match.groups())
        # Only exempt an obviously synthetic credential pair. Unrelated words such
        # as "test" elsewhere in a file must not hide a real DSN.
        if username in placeholders and password in placeholders:
            continue
        raise ValueError("UNREDACTED_DSN:" + str(path))


def private_values(root=ROOT):
    from package_v425 import private_values as previous_private_values
    values = previous_private_values(Path(root))
    for folder in (root.parent / "_CODEX_V424", root.parent / "_CODEX_V425"):
        cfg = folder / "local-pilot/config.private.json"
        if not cfg.exists():
            continue
        try:
            obj = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in ("TXBUDDY_JWT_SECRET", "TX_INTERNAL_TOKEN"):
            if obj.get(key):
                values.append(str(obj[key]))
    return values


def _add(files, root, path):
    path = Path(path)
    if path.is_file():
        rel = path.relative_to(root).as_posix()
        if not sensitive_name(rel):
            files[rel] = path.read_bytes()


def gather(root=ROOT, profile="V4.2.5.1 SecurityFix"):
    root = Path(root).resolve()
    files = {}
    # Source and tests needed to reproduce the fix. Generated dependencies, builds,
    # databases, raw originals, and authentication traces are intentionally absent.
    for directory in ("backend", "frontend", "sql", "schemas", "workflows"):
        base = root / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(x in rel.split("/") for x in ("node_modules", "dist", "__pycache__", "data", "test-results", "playwright-artifacts")):
                continue
            if path.suffix.lower() in {".pyc", ".tsbuildinfo", ".sqlite3", ".db", ".dump"} or sensitive_name(rel):
                continue
            if path.suffix.lower() in {".py", ".ts", ".tsx", ".vue", ".mjs", ".json", ".toml", ".sql", ".yaml", ".yml", ".css", ".html", ".md", ".txt"}:
                files[rel] = path.read_bytes()
    for path in (root / "scripts").rglob("*.py"):
        if path.is_file():
            _add(files, root, path)
    for path in (root / "scripts/tests").glob("test_v426_*.py"):
        _add(files, root, path)
    for relative in ("V426_EXECUTION_STATE.json", "EXECUTION_STATE.md", "scripts/V426_TASK_QUEUE.json"):
        _add(files, root, root / relative)
    for path in (root / "docs").glob("V4.2.*"):
        _add(files, root, path)
    for path in (root / "reports/v426").iterdir():
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".xml", ".log"} and not sensitive_name(path.name):
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    profile_text = (
        f"# {profile}\n\n"
        "This is a source-and-evidence package. Production acceptance is false.\n"
        "Human OCR review, professional signoff, current formal pricing, cross-region validation,"
        " independent quantity accuracy, and PostgreSQL production migration remain BLOCKED.\n"
        "Private configuration, credentials, database files, raw source PDFs/OCR full text, browser traces, and old release archives are excluded.\n"
    ).encode("utf-8")
    files["V426_DELIVERY_PROFILE.md"] = profile_text
    return files


def _read(name, root=ROOT):
    return json.loads((Path(root) / "reports/v426" / name).read_text(encoding="utf-8"))


def source_manifest(root=ROOT):
    """Match the source scope captured by v426_verify.py, without private inputs."""
    root = Path(root)
    result = {}
    for directory in ("backend", "frontend", "scripts", "sql", "schemas", "workflows"):
        for path in (root / directory).rglob("*"):
            if not path.is_file() or any(x in path.parts for x in (".venv", "node_modules", "__pycache__", "dist", "data", ".pytest_cache")):
                continue
            if path.suffix.lower() in (".py", ".ts", ".vue", ".mjs", ".sql", ".yaml", ".json", ".toml", ".txt", ".ps1", ".cmd", ".css", ".html") and not path.name.endswith((".lock", ".pg-dsn")) and ".private." not in path.name:
                result[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return result


def validate_gate(root=ROOT):
    backend = _read("FINAL_RESULT.json", root)
    frontend = _read("FINAL_FRONTEND_RESULT.json", root)
    inventory = _read("FINAL_DATA_INVENTORY.json", root)
    prices = _read("MATERIAL_PRICE_VALIDATION.json", root)
    boq = _read("BOQ_VALIDATION.json", root)
    ocr = _read("OCR_HUMAN_REVIEW_READINESS.json", root)
    dedup = _read("OCR_CONTENT_DEDUP.json", root)
    if not (backend["exit_code"] == 0 and backend["failures"] == 0 and backend["errors"] == 0 and backend["skipped"] == 0 and backend["source_unchanged_during_run"] and backend["working_source_matches_tested_copy"]):
        raise ValueError("FINAL_BACKEND_GATE_FAILED")
    manifest_now = source_manifest(root)
    tested_map = backend.get("tested_source_sha256") or {}
    if any(tested_map.get(k) != v for k, v in manifest_now.items() if k in tested_map):
        raise ValueError("SOURCE_CHANGED_SINCE_FINAL_TEST")
    stats = frontend.get("stats", {})
    if not (frontend["build_exit_code"] == 0 and frontend["browser_exit_code"] == 0 and stats.get("expected", 0) >= 4 and stats.get("unexpected", 1) == 0 and stats.get("skipped", 1) == 0 and stats.get("flaky", 1) == 0 and frontend["source_unchanged_during_run"]):
        raise ValueError("FINAL_FRONTEND_GATE_FAILED")
    if frontend.get("tested_source_sha256") != backend.get("tested_source_sha256"):
        raise ValueError("FRONTEND_BACKEND_SOURCE_MISMATCH")
    summary = _read("TEST_SUMMARY.json", root)
    for key, expected in {"backend_tests": backend["tests"], "backend_failures": 0, "backend_errors": 0, "backend_skipped": 0, "frontend_browser_tests": stats["expected"], "frontend_browser_failures": 0, "frontend_build_exit_code": 0, "production_accepted": False, "accuracy_E": "BLOCKED"}.items():
        if summary.get(key) != expected:
            raise ValueError("TEST_SUMMARY_MISMATCH:" + key)
    if not all(inventory.get(k) is True for k in ("original_files_unchanged", "live_databases_logically_unchanged", "sqlite_integrity_passed")):
        raise ValueError("FINAL_DATA_INVENTORY_GATE_FAILED")
    if prices.get("total") != 10989 or prices.get("unverified_source_count") != 0:
        raise ValueError("MATERIAL_PRICE_GATE_FAILED")
    if boq.get("total_rows") != 148 or boq.get("independently_calculated") is not False or boq.get("accuracy_verification") != "BLOCKED":
        raise ValueError("BOQ_BOUNDARY_GATE_FAILED")
    if ocr.get("task_count") != 10 or ocr.get("human_reviews_created") != 0 or ocr.get("professional_signoffs_created") != 0 or ocr.get("human_review_status") != "BLOCKED":
        raise ValueError("OCR_REVIEW_BOUNDARY_GATE_FAILED")
    if dedup.get("assets_verified_unchanged") is not True or dedup.get("human_reviews_created") != 0:
        raise ValueError("OCR_DEDUP_GATE_FAILED")
    return {"backend": backend, "frontend": frontend, "inventory": inventory, "prices": prices, "boq": boq, "ocr": ocr, "dedup": dedup}


def write_archive(files, target, private_values_=()):
    target = Path(target)
    if target.exists():
        raise FileExistsError("NEVER_OVERWRITE_EXISTING_RELEASE")
    for rel, data in files.items():
        scan_payload(rel, data, private_values_)
    manifest = "".join(f"{sha(data)}  {rel}\n" for rel, data in sorted(files.items()))
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        raise FileExistsError("STAGING_PATH_ALREADY_EXISTS")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for rel, data in sorted(files.items()):
                archive.writestr(rel, data)
            archive.writestr("SHA256SUMS", manifest.encode("utf-8"))
        with zipfile.ZipFile(tmp) as archive:
            if archive.testzip() is not None:
                raise ValueError("ARCHIVE_CRC_FAILED")
            for rel, data in files.items():
                actual = archive.read(rel)
                scan_payload(rel, actual, private_values_)
                if sha(actual) != sha(data):
                    raise ValueError("ARCHIVE_SHA_FAILED:" + rel)
        tmp.replace(target)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return manifest


def package(profile, root=ROOT):
    root = Path(root).resolve()
    evidence = validate_gate(root)
    files = gather(root, profile)
    release = root / "release"
    release.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y%m%dT%H%M%S%f")
    slug = "V4.2.5.1_SecurityFix" if profile.startswith("V4.2.5.1") else "V4.2.6_BusinessValidation"
    target = release / f"TianxunBuddy_{slug}_{stamp}.zip"
    manifest = write_archive(files, target, private_values(root))
    digest = sha(target.read_bytes())
    report = {
        "profile": profile, "archive": str(target), "sha256": digest,
        "bytes": target.stat().st_size, "payload_files": len(files),
        "archive_entries": len(files) + 1, "payload_hashes_verified": True,
        "sensitive_findings": 0, "production_accepted": False,
        "accuracy_E": "BLOCKED", "backend_tests": evidence["backend"]["tests"],
        "frontend_tests": evidence["frontend"]["stats"]["expected"],
        "excluded": ["private credentials/config", "SQLite/PostgreSQL databases and dumps", "raw PDF/OCR full text", "browser auth traces", "old release archives"],
        "manifest_sha256": sha(manifest.encode("utf-8")),
    }
    report_dir = release / "v426"
    report_dir.mkdir(exist_ok=True)
    (report_dir / f"{target.stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / f"{target.stem}.md").write_text(
        f"# {profile} 安全交付包\n\n- 文件：`{target}`\n- SHA256：`{digest}`\n- 载荷：{len(files)} 个文件，CRC 与逐文件 SHA256 已校验。\n- 后端：{evidence['backend']['tests']} passed；前端：{evidence['frontend']['stats']['expected']} passed。\n- 生产验收：`false`；能力精度 E：`BLOCKED`。\n", encoding="utf-8")
    (report_dir / f"{target.stem}.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("security", "business", "all"), default="all")
    args = parser.parse_args()
    profiles = {"security": ["V4.2.5.1 SecurityFix"], "business": ["V4.2.6 BusinessValidation"], "all": ["V4.2.5.1 SecurityFix", "V4.2.6 BusinessValidation"]}[args.profile]
    print(json.dumps([package(profile) for profile in profiles], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
