"""Extract a release outside the checkout and independently verify its evidence."""
from pathlib import Path, PurePosixPath
import argparse, datetime, json, stat, zipfile
from package_v426 import ROOT, gather, private_values, scan_payload, sha, source_manifest


def verify(report_path, destination, root=ROOT):
    root = Path(root).resolve()
    report = json.loads(Path(report_path).read_text(encoding='utf-8'))
    archive_path = Path(report['archive'])
    if sha(archive_path.read_bytes()) != report['sha256']:
        raise ValueError('ARCHIVE_SHA256_MISMATCH')
    destination = Path(destination).resolve()
    if destination == root or root in destination.parents:
        raise ValueError('EXTRACTION_MUST_BE_OUTSIDE_CHECKOUT')
    destination.mkdir(parents=True, exist_ok=False)
    secrets = private_values(root)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) != report['archive_entries']:
            raise ValueError('ENTRY_COUNT_OR_DUPLICATE_PATH')
        if archive.testzip() is not None:
            raise ValueError('CRC_FAILED')
        expected = {}
        manifest_bytes = archive.read('SHA256SUMS')
        if sha(manifest_bytes) != report['manifest_sha256']:
            raise ValueError('MANIFEST_SHA256_MISMATCH')
        for line in manifest_bytes.decode('utf-8').splitlines():
            digest, name = line.split('  ', 1)
            if name in expected:
                raise ValueError('DUPLICATE_MANIFEST_ENTRY')
            expected[name] = digest
        if set(expected) != set(names) - {'SHA256SUMS'} or len(expected) != report['payload_files']:
            raise ValueError('MANIFEST_COVERAGE_MISMATCH')
        for info in archive.infolist():
            pure = PurePosixPath(info.filename)
            if chr(92) in info.filename or pure.is_absolute() or '..' in pure.parts or any(':' in part for part in pure.parts) or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('UNSAFE_ARCHIVE_PATH')
            data = archive.read(info.filename)
            if info.filename != 'SHA256SUMS':
                scan_payload(info.filename, data, secrets)
                if sha(data) != expected[info.filename]:
                    raise ValueError('PAYLOAD_SHA256_MISMATCH:' + info.filename)
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    for name, digest in expected.items():
        if sha((destination / name).read_bytes()) != digest:
            raise ValueError('EXTRACTED_SHA256_MISMATCH:' + name)
    current_payload = gather(root, report['profile'])
    if {name: sha(data) for name, data in current_payload.items()} != expected:
        raise ValueError('PACKAGE_DIFFERS_FROM_DELIVERY_WORKTREE')
    read = lambda name: json.loads((destination / 'reports/v426' / name).read_text(encoding='utf-8'))
    backend, frontend, summary = read('FINAL_RESULT.json'), read('FINAL_FRONTEND_RESULT.json'), read('TEST_SUMMARY.json')
    current_source = source_manifest(root)
    if backend['tested_source_sha256'] != current_source or frontend['tested_source_sha256'] != current_source:
        raise ValueError('TESTED_SOURCE_MISMATCH')
    for name, digest in current_source.items():
        if name in expected and expected[name] != digest:
            raise ValueError('PACKAGED_SOURCE_MISMATCH:' + name)
    if not (report['backend_tests'] == backend['tests'] == summary['backend_tests'] and report['frontend_tests'] == frontend['stats']['expected'] == summary['frontend_browser_tests']):
        raise ValueError('TEST_COUNTS_MISMATCH')
    if report['production_accepted'] is not False or summary['production_accepted'] is not False or summary['accuracy_E'] != 'BLOCKED':
        raise ValueError('BUSINESS_BOUNDARY_MISMATCH')
    return {'archive': str(archive_path), 'archive_sha256': report['sha256'], 'archive_bytes': archive_path.stat().st_size, 'extracted_to': str(destination), 'payload_files': len(expected), 'archive_entries': len(names), 'crc_passed': True, 'manifest_verified': True, 'all_extracted_hashes_verified': True, 'sensitive_findings': 0, 'worktree_payload_matches': True, 'tested_source_matches': True, 'test_counts_match': True, 'production_accepted': False, 'accuracy_E': 'BLOCKED', 'passed': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%dT%H%M%S%f')
    base = ROOT.parent / '_CODEX_V426/package-verify' / stamp
    result = {'generated_at': stamp, 'packages': [verify(path, base / path.stem) for path in args.reports]}
    output = ROOT / 'release/v426' / ('PACKAGE_EXTERNAL_VERIFICATION_' + stamp + '.json')
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(output), **result}, ensure_ascii=False, indent=2))
