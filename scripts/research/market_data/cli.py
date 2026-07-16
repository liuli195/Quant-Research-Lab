from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .storage import audit_store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="共享行情中心维护工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit", help="只读校验全部行情批次和快照")
    audit.add_argument("--root", type=Path, required=True, help="行情中心根目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "audit":
        print(
            json.dumps(
                audit_store(root=arguments.root),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError(f"unhandled command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())
