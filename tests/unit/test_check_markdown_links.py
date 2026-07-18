"""저장소 내부 Markdown 링크 검사기 계약."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_markdown_links import check_markdown_links, main


def test_링크_검사는_존재하는_상대_경로와_외부_URL을_허용한다(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# 사용법\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[가이드](docs/guide.md#사용법)\n"
        "[참조형 가이드][guide]\n"
        "[guide]: docs/guide.md\n"
        "[외부](https://example.com/docs)\n"
        "`[인라인 코드](missing-inline.md)`\n"
        "```markdown\n[코드 블록](missing-fence.md)\n```\n",
        encoding="utf-8",
    )

    issues = check_markdown_links(tmp_path)

    assert issues == ()


def test_링크_검사는_없는_로컬_대상과_줄번호를_보고한다(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# 문서\n[없는 문서](docs/missing.md)\n",
        encoding="utf-8",
    )

    issues = check_markdown_links(tmp_path)

    assert len(issues) == 1
    assert issues[0].source == Path("README.md")
    assert issues[0].line == 2
    assert issues[0].target == "docs/missing.md"
    assert issues[0].reason == "대상이 존재하지 않습니다"


def test_링크_검사는_저장소_밖을_가리키는_경로를_거부한다(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# 외부\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("[외부](../outside.md)\n", encoding="utf-8")

    issues = check_markdown_links(tmp_path)

    assert len(issues) == 1
    assert issues[0].reason == "저장소 밖을 가리킵니다"


def test_링크_검사_CLI는_실패_목록과_종료_코드를_제공한다(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "README.md").write_text("[없는 문서](missing.md)\n", encoding="utf-8")

    exit_code = main([str(tmp_path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "README.md:1: missing.md - 대상이 존재하지 않습니다" in captured.err
