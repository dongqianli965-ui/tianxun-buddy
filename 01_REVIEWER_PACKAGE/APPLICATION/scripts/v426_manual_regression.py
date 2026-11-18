"""Preserve prior v426 evidence while rerunning all backend checks in a new copy."""
from pathlib import Path
import v426_verify

if __name__ == '__main__':
    v426_verify.OUT = Path(__file__).resolve().parents[1] / 'reports/v426_manual/regression'
    raise SystemExit(v426_verify.run('final'))
