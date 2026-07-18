# 릴리스 절차

RecorderSync는 기능 PR을 버전별 release 브랜치에 모은 뒤 main PR로 병합하고, main의 병합
커밋에 태그를 붙여 GitHub Release를 생성한다. PyPI 배포는 현재 범위에 포함하지 않는다.

## 1. release 브랜치 준비

기능과 수정은 각각 독립 PR로 `release/<version>`에 병합한다. 릴리스 메타데이터 PR에서
다음을 함께 갱신한다.

- `pyproject.toml`의 `project.version`
- `recordersync/__init__.py`의 `__version__`
- `uv.lock`
- [변경 이력](../project/changelog.md)

버전과 예정 태그는 다음처럼 검사한다.

```bash
release_version=0.4.0
uv lock --check
uv run python scripts/check_release_version.py "v${release_version}"
bash scripts/check.sh
bash scripts/test-e2e.sh
```

## 2. main 병합

`release/<version>`에서 `main`으로 PR을 만들고 Unit Tests, Synthetic FFmpeg E2E,
Lint/Types/Security/Complexity/Build 체크와 미해결 리뷰를 모두 확인한다. main에 직접
commit하거나 release 브랜치를 강제로 push하지 않는다.

## 3. 태그와 GitHub Release

main PR 병합 커밋을 로컬에서 확인한 뒤 해당 커밋에 annotated tag를 만든다.

```bash
release_version=0.4.0
git fetch origin main --tags
release_commit=$(git rev-parse origin/main)
git tag -a "v${release_version}" "${release_commit}" -m "RecorderSync ${release_version}"
git push origin "v${release_version}"
```

`.github/workflows/release.yml`은 태그와 두 패키지 버전이 정확히 일치하는지 검증하고,
전체 품질 검사 후 `dist/*.whl`과 `dist/*.tar.gz`를 GitHub Release에 첨부한다. 태그가
잘못됐거나 검사가 실패하면 릴리스를 생성하지 않는다.

## 4. 완료 확인

```bash
gh run list --workflow Release --limit 5
gh release view "v${release_version}"
```

Release 페이지의 태그·제목·산출물 두 개와 설치 후 버전을 확인한다. 이미 공개한 태그를
다른 커밋으로 옮기지 않는다. 릴리스 이후 수정은 새 patch 버전으로 진행한다.
