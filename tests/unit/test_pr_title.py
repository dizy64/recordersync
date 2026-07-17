"""PR 제목의 한글 포함 정책."""

from __future__ import annotations

import pytest

from scripts.check_pr_title import contains_hangul, main


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("분석 출력을 개선", True),
        ("recordersync CI 이름을 한국어로 변경", True),
        ("Make CI labels Korean", False),
        ("", False),
    ],
)
def test_contains_hangul_validates_pull_request_title(title: str, expected: bool) -> None:
    assert contains_hangul(title) is expected


def test_main_accepts_korean_pull_request_title(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PR_TITLE", "지속적 통합 이름을 한국어로 변경")

    assert main() == 0
    assert "통과했습니다" in capsys.readouterr().out


def test_main_rejects_english_only_pull_request_title(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PR_TITLE", "Use Korean CI labels")

    assert main() == 1
    assert "한글을 한 글자 이상" in capsys.readouterr().err
