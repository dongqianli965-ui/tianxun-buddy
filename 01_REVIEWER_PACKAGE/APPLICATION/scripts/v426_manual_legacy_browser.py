"""Run the unchanged four legacy browser specs against the new isolated copy."""
from pathlib import Path
import v426_frontend_verify

if __name__ == '__main__':
    v426_frontend_verify.OUT = Path(__file__).resolve().parents[1] / 'reports/v426_manual/regression'
    raise SystemExit(v426_frontend_verify.run('final'))
