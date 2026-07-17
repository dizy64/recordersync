# 개발 가이드

이 문서는 사람과 코딩 에이전트 모두에게 적용된다. 작업 전 `CLAUDE.md`와 관련
`docs/` 문서를 읽고, 한 번에 한 문제만 해결한다.

## 개발 환경

```bash
git clone git@github.com:dizy64/recordersync.git
cd recordersync
brew bundle
uv sync
uv run recordersync --version
```

필수 환경은 macOS, Python 3.14+, uv, VideoToolbox를 지원하는 FFmpeg/ffprobe다.
NumPy와 SciPy는 `uv.lock`으로 고정한다.

## Worktree 규칙

코드나 문서를 변경할 때 main 작업 트리에서 직접 수정하지 않는다.

```bash
mkdir -p ../.worktrees/recordersync
git worktree add -b feat/short-name \
  ../.worktrees/recordersync/short-name main
cd ../.worktrees/recordersync/short-name
```

완료 후 브랜치를 push하고 PR의 필수 CI가 통과한 뒤 GitHub에서 병합한다. 로컬에서
`main`으로 직접 병합하거나 push하지 않는다.

```bash
git push -u origin feat/short-name
gh pr create --fill
gh pr checks --watch
gh pr merge --delete-branch

cd /path/to/recordersync
git pull --ff-only origin main
git worktree remove ../.worktrees/recordersync/short-name
git worktree prune
```

동일 브랜치를 둘 이상의 worktree에서 열지 않는다. `.venv`와 사용자 미디어는 새
worktree로 복사하지 않는다.

## 필수 개발 순서

1. **Plan**: 입력·출력·실패·경계·성능·데이터 안전을 짧게 정리한다.
2. **RED**: 변경하려는 정책을 재현하는 단위 테스트를 먼저 작성하고 실패를 확인한다.
3. **GREEN**: 테스트를 통과시키는 최소 구현만 추가한다.
4. **Refactor**: 중복과 명명을 정리하되 테스트를 계속 통과시킨다.
5. **Review**: 범위 밖 변경, 원본 손상, 비밀정보, 성능 회귀를 확인한다.
6. **Commit**: 한 목적의 작은 커밋으로 저장한다.

버그 수정에서 테스트를 통과시키기 위해 기존 기대값을 바꾸지 않는다. 테스트 전제가
실제 FFmpeg 동작과 다른 경우에만 근거를 기록하고 사양을 수정한다.

## 테스트 케이스 작성

비즈니스 정책 테스트는 `tests/unit/`에 두고 실제 FFmpeg, ffprobe, 네트워크를
호출하지 않는다. 실제 CLI/코덱 경계는 `tests/e2e/`에서 공개 합성 미디어로만 검증하며
사용자의 미디어와 네트워크는 사용하지 않는다.

- 순수 정책은 작은 dataclass와 NumPy 배열로 검증한다.
- 파일 경계는 `tmp_path`를 사용한다.
- subprocess는 `unittest.mock.patch`로 `CompletedProcess`를 반환한다.
- 랜덤 특징은 고정된 `np.random.default_rng(seed)`를 사용한다.
- 정상, 경계, 오류를 최소 한 개씩 고정한다.
- 반복 음원, 무음/flat 특징, 짧은 세션, 조각 경계, clock drift처럼 오탐 위험이 큰
  경우를 우선한다.
- 여러 카메라가 같은 외부 구간에 매칭될 수 있다는 불변식을 유지한다.
- 출력 파일이 있는 경우 `--overwrite` 없이 보존되는지 검증한다.

상세 케이스 지도와 예시는 [docs/TESTING.md](docs/TESTING.md)를 따른다.

## 검증 명령

개발 중에는 가장 가까운 테스트부터 실행한다.

```bash
uv run pytest tests/unit/test_matching.py -q
uv run pytest tests/unit/test_matching.py::test_name -q
```

커밋 전에는 다음을 모두 실행한다.

```bash
bash scripts/check.sh
bash scripts/test-e2e.sh
git diff --check
```

`bash scripts/format.sh`는 Ruff 위반을 안전하게 자동 수정하고 포맷합니다. 최초 clone 뒤
`bash scripts/install-hooks.sh`를 실행하면 pre-commit에 lint/type/unit/audit 검사를,
pre-push에 단위 테스트를 연결합니다. E2E는 실행 시간이 길고 FFmpeg가 필요하므로
명시적 스크립트와 CI에서 수행합니다.

매칭 알고리즘·특징·성능 경로를 바꾼 경우 벤치도 실행한다.

```bash
uv run python scripts/benchmark_matcher.py
```

기준은 12시간·영상 200개에서 총 600초 이하, peak RSS 2GB 이하다. 기준을 넘으면
알고리즘을 병합하지 않고 원인을 기록한다.

## 의존성과 라이선스

- 기존 표준 라이브러리, NumPy, SciPy, FFmpeg로 해결 가능한지 먼저 확인한다.
- 런타임 의존성을 추가하기 전 유지보수 상태, Python 3.14 지원, 상업 사용 가능 여부,
  설치 크기를 확인한다.
- 강한 copyleft나 출처가 불명확한 코드를 복사하지 않는다.
- 의존성 변경은 `uv lock`, `pip-audit`, 패키지 빌드를 함께 검증한다.

## 데이터와 보안

- 실제 영상·녹음·리포트를 저장소에 커밋하지 않는다.
- JSON 리포트에는 절대 경로와 파일명이 포함되므로 공개 이슈에 그대로 첨부하지 않는다.
- API key, 토큰, 쿠키, 개인 경로를 fixture와 로그에 넣지 않는다.
- subprocess는 인자 배열과 `shell=False`를 유지한다.
- 원본을 수정·삭제·이동하는 기능은 추가하지 않는다.
- 파괴적 동작이 필요해지는 요구는 별도 승인과 설계를 먼저 받는다.

[SECURITY.md](SECURITY.md)의 공개 보고 정책과 개인정보 주의사항도 확인한다.

## 커밋과 PR

커밋 메시지는 이모지 없이 무엇과 이유를 표현한다.

```text
Add ambiguity regression coverage for repeated audio
Preserve logical offsets across recorder chunks
Document global uv tool installation workflow
```

PR에는 문제와 범위, RED 재현, 테스트 결과, 성능·보안·데이터 영향, 남은 위험을 적는다.
`.github/pull_request_template.md` 체크리스트를 모두 확인한다.
`main` 보호 규칙은 PR, 최신 main 기준 필수 CI, 미해결 대화 해소를 요구한다.

## 완료 정의

- 요청한 비즈니스 동작과 실패 정책이 단위 테스트로 고정됨
- CLI/FFmpeg 경계 변경은 공개 합성 E2E로 검증됨
- 전체 품질·보안 검증 통과
- 원본 미디어와 기존 출력의 안전성 유지
- 공개 API/JSON/CLI 변경 시 관련 문서 갱신
- 성능 경로 변경 시 p95/p99와 peak RSS 기록
- `CLAUDE.md` 또는 인수인계 문서에 새 불변식이 필요한지 검토
