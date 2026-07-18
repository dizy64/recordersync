#!/usr/bin/env python3
"""릴리스 태그와 패키지 버전이 일치하는지 검사한다."""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as file:
        document: dict[str, Any] = tomllib.load(file)
    version = document.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml의 project.version을 찾을 수 없습니다")
    return version


def _package_version(root: Path) -> str:
    source = (root / "recordersync" / "__init__.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    for statement in module.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value = statement.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and target.id == "__version__"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    raise ValueError("recordersync/__init__.py에서 __version__ 선언을 찾을 수 없습니다")


def validate_release_version(root: Path, tag: str | None = None) -> str:
    """프로젝트·패키지와 선택적으로 태그 버전이 같으면 버전을 반환한다."""

    project_version = _project_version(root)
    package_version = _package_version(root)
    if project_version != package_version:
        raise ValueError(f"버전 불일치: pyproject.toml {project_version}, recordersync {package_version}")

    if tag is not None:
        expected_tag = f"v{project_version}"
        if tag != expected_tag:
            raise ValueError(f"릴리스 태그 {tag}가 예상 {expected_tag}와 다릅니다")
    return project_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="릴리스 태그와 패키지 버전의 일치 여부를 검사합니다.")
    parser.add_argument("tag", nargs="?", help="검사할 Git 태그(예: v0.4.0)")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="저장소 루트(기본: 이 스크립트의 저장소)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        version = validate_release_version(args.root.resolve(), args.tag)
    except (OSError, SyntaxError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"릴리스 버전 검사 실패: {error}", file=sys.stderr)
        return 1
    tag_summary = f"태그 {args.tag}, " if args.tag is not None else ""
    print(f"릴리스 버전 검사 통과: {tag_summary}패키지 {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
