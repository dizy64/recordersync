#!/usr/bin/env python3
"""Markdown 문서가 가리키는 저장소 내부 파일 경로를 검사한다."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_IGNORED_DIRECTORIES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist"}
)
_FENCE_PATTERN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
_INLINE_CODE_PATTERN = re.compile(r"(`+).*?\1")
_INLINE_LINK_PATTERN = re.compile(
    r"!?\[[^\]\n]*\]\(\s*"
    r"(?P<target><[^>\n]+>|(?:[^()\s]+|\([^()\n]*\))+?)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_REFERENCE_LINK_PATTERN = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(?P<target><[^>\n]+>|\S+)")


@dataclass(frozen=True, slots=True, order=True)
class LinkIssue:
    """한 Markdown 링크의 로컬 경로 검증 실패."""

    source: Path
    line: int
    target: str
    reason: str


def markdown_files(root: Path) -> tuple[Path, ...]:
    """생성물과 도구 캐시를 제외한 Markdown 파일을 정렬해 반환한다."""

    resolved_root = root.resolve()
    return tuple(
        sorted(
            path
            for path in resolved_root.rglob("*.md")
            if not _IGNORED_DIRECTORIES.intersection(path.relative_to(resolved_root).parts)
        )
    )


def _link_targets(lines: Iterable[str]) -> Iterable[tuple[int, str]]:
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(lines, start=1):
        fence_match = _FENCE_PATTERN.match(line)
        if fence_character is not None:
            if fence_match is not None:
                fence = fence_match.group("fence")
                if fence[0] == fence_character and len(fence) >= fence_length:
                    fence_character = None
                    fence_length = 0
            continue
        if fence_match is not None:
            fence = fence_match.group("fence")
            fence_character = fence[0]
            fence_length = len(fence)
            continue

        searchable = _INLINE_CODE_PATTERN.sub("", line)
        for match in _INLINE_LINK_PATTERN.finditer(searchable):
            yield line_number, match.group("target")
        reference_match = _REFERENCE_LINK_PATTERN.match(searchable)
        if reference_match is not None:
            yield line_number, reference_match.group("target")


def _clean_target(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return target.replace(r"\ ", " ")


def _validate_target(root: Path, source: Path, target: str) -> str | None:
    cleaned = _clean_target(target)
    parsed = urlsplit(cleaned)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None

    decoded_path = unquote(parsed.path)
    candidate = (
        root / decoded_path.lstrip("/")
        if decoded_path.startswith("/")
        else source.parent / decoded_path
    ).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return "저장소 밖을 가리킵니다"
    if not candidate.exists():
        return "대상이 존재하지 않습니다"
    return None


def check_markdown_links(root: Path) -> tuple[LinkIssue, ...]:
    """저장소 내부 Markdown 파일의 깨진 로컬 경로를 반환한다."""

    resolved_root = root.resolve()
    issues: list[LinkIssue] = []
    for source in markdown_files(resolved_root):
        text = source.read_text(encoding="utf-8")
        for line_number, target in _link_targets(text.splitlines()):
            reason = _validate_target(resolved_root, source, target)
            if reason is not None:
                issues.append(
                    LinkIssue(
                        source=source.relative_to(resolved_root),
                        line=line_number,
                        target=_clean_target(target),
                        reason=reason,
                    )
                )
    return tuple(sorted(issues))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="저장소 내부 Markdown 파일 링크를 검사합니다.")
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="검사할 저장소 루트(기본: 이 스크립트의 저장소)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    root = build_parser().parse_args(argv).root.resolve()
    if not root.is_dir():
        print(f"Markdown 링크 검사 실패: 디렉터리가 아닙니다: {root}", file=sys.stderr)
        return 2

    files = markdown_files(root)
    issues = check_markdown_links(root)
    if issues:
        print(f"Markdown 링크 검사 실패: {len(issues)}개", file=sys.stderr)
        for issue in issues:
            print(
                f"{issue.source}:{issue.line}: {issue.target} - {issue.reason}",
                file=sys.stderr,
            )
        return 1
    print(f"Markdown 링크 검사 통과: {len(files)}개 파일")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
