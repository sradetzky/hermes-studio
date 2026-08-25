from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp.movie_store import MovieStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assemble one immutable Hermes Studio movie export contract")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()

    contract = json.loads(args.contract)
    result = MovieStore().export(args.project, contract, args.job_id)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
