"""패키지 버전과 릴리스 태그 일치 계약."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_release_version import validate_release_version


def _write_versions(root: Path, *, project: str, package: str) -> None:
    (root / "recordersync").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "recordersync"\nversion = "{project}"\n',
        encoding="utf-8",
    )
    (root / "recordersync" / "__init__.py").write_text(
        f'__version__ = "{package}"\n',
        encoding="utf-8",
    )


def test_릴리스_태그는_프로젝트와_패키지_버전이_같으면_통과한다(tmp_path: Path) -> None:
    _write_versions(tmp_path, project="0.4.0", package="0.4.0")

    version = validate_release_version(tmp_path, "v0.4.0")

    assert version == "0.4.0"


def test_일반_검사는_태그_없이_두_패키지_버전만_검증한다(tmp_path: Path) -> None:
    _write_versions(tmp_path, project="0.4.0", package="0.4.0")

    version = validate_release_version(tmp_path, None)

    assert version == "0.4.0"


def test_릴리스는_타입_표기된_패키지_버전도_검증한다(tmp_path: Path) -> None:
    _write_versions(tmp_path, project="0.4.0", package="0.4.0")
    (tmp_path / "recordersync" / "__init__.py").write_text(
        '__version__: str = "0.4.0"\n',
        encoding="utf-8",
    )

    version = validate_release_version(tmp_path, "v0.4.0")

    assert version == "0.4.0"


def test_릴리스_태그는_프로젝트_버전과_다르면_거부한다(tmp_path: Path) -> None:
    _write_versions(tmp_path, project="0.4.0", package="0.4.0")

    with pytest.raises(ValueError, match=r"태그 v0\.4\.1.*예상 v0\.4\.0"):
        validate_release_version(tmp_path, "v0.4.1")


def test_릴리스는_프로젝트와_패키지_버전이_다르면_거부한다(tmp_path: Path) -> None:
    _write_versions(tmp_path, project="0.4.0", package="0.3.0")

    with pytest.raises(
        ValueError,
        match=r"pyproject\.toml 0\.4\.0.*recordersync 0\.3\.0",
    ):
        validate_release_version(tmp_path, "v0.4.0")


def test_릴리스는_패키지_버전_선언이_없으면_거부한다(tmp_path: Path) -> None:
    _write_versions(tmp_path, project="0.4.0", package="0.4.0")
    (tmp_path / "recordersync" / "__init__.py").write_text(
        '"""패키지."""\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="__version__ 선언을 찾을 수 없습니다"):
        validate_release_version(tmp_path, "v0.4.0")
