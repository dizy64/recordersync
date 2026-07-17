"""PR 제목에 한글 음절이 포함됐는지 검사한다."""

from __future__ import annotations

import os
import sys

HANGUL_SYLLABLE_START = "가"
HANGUL_SYLLABLE_END = "힣"


def contains_hangul(title: str) -> bool:
    """완성형 한글 음절이 하나 이상 있으면 참을 반환한다."""

    return any(HANGUL_SYLLABLE_START <= character <= HANGUL_SYLLABLE_END for character in title)


def main() -> int:
    title = os.environ.get("PR_TITLE", "")
    if contains_hangul(title):
        print("PR 제목 한글 검사를 통과했습니다.")
        return 0
    print("PR 제목에 한글을 한 글자 이상 포함해야 합니다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
