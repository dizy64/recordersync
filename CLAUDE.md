# CLAUDE.md

## 프로젝트 개요

`recordersync`는 분할된 보이스레코더 녹음을 세션별로 구성하고, 영상의 내장
오디오와 일치하는 외부 오디오 구간을 찾아 교체하는 macOS CLI 도구다.

- Python 3.14 이상, `uv` 사용
- FFmpeg/ffprobe 시스템 의존성
- 비즈니스 로직은 I/O와 분리하고 단위 테스트로 검증
- Plan -> Test(RED) -> Code(GREEN) -> Refactor 순서 준수
- 최소 변경, strict mypy, ruff, pytest, pip-audit 통과
- 코드 변경은 `../.worktrees/recordersync/{name}` worktree에서 수행
- `main`에 직접 commit/push/merge하지 않고 작업 브랜치의 PR과 필수 CI를 통해 병합
- `AGENTS.md`는 이 파일을 가리키는 심볼릭 링크로 유지

## 작업 전 읽기 순서

1. `docs/CONCEPT.md`: 제품 범위와 안전 정책
2. `docs/ARCHITECTURE.md`: 알고리즘, 모듈 책임, 성능 기준
3. `CONTRIBUTING.md`: worktree와 TDD 절차
4. `docs/TESTING.md`: 테스트 지도와 목 작성 규칙
5. `docs/HANDOFF.md`: 알려진 한계와 다음 우선순위

설치·운영 변경은 `docs/OPERATIONS.md`, 외부 JSON 변경은
`docs/REPORT_SCHEMA.md`, 개인정보·취약점 처리는 `SECURITY.md`를 함께 읽는다.

## 핵심 계약

- `--audio-dir` 생략 시 `VIDEO_DIR`에서 레코더 오디오도 찾는다.
- 원본 미디어를 수정·이동·삭제하지 않는다.
- `ambiguous`, `unmatched`, `error` 결과는 렌더하지 않는다. `partial`은 사용자가
  `fallback`을 명시한 경우에만 렌더한다.
- 사용자가 명시하지 않은 mix, fallback, overwrite를 자동 선택하지 않는다.
- 레코더 조각은 중간 대용량 파일로 합치지 않고 논리 세션과 concat 입력으로 다룬다.
- 여러 영상이 같은 외부 오디오 구간에 매칭되는 것을 허용한다.
- 렌더는 임시 출력 성공 후 최종 경로로 원자 이동한다.
- 사용자 미디어·리포트·절대 경로·비밀정보를 저장소나 테스트 fixture에 넣지 않는다.
- subprocess는 인자 배열과 `shell=False`를 유지한다.
- 선택 파일과 진행률은 stderr에 출력한다. `analyze` stdout은 기본 사람용 목록이고
  `--json`에서만 전체 JSON이며, `process` stdout과 `--report` 파일은 JSON을 유지한다.
- `analyze`의 처리 모드 추천은 안내만 제공한다. 추천이 process 모드, 종료 코드, 렌더
  허용 여부를 자동으로 바꾸면 안 된다.
- 원본/외부 오디오 볼륨은 각각 0.0~1.0이다. 원본 볼륨 기본값은 mix 0.1,
  fallback 1.0이며 replace에서는 적용하지 않는다.

## 목표 프로파일

- 출력: MP4, 회전 적용 후 원본 표시 해상도와 프레임 타임스탬프/VFR 유지
- 영상: HEVC 10-bit, VideoToolbox 50Mbps, BT.709 SDR, `hvc1`
- 오디오: AAC 48kHz 256kbps
- VideoToolbox 실패 시 libx265 10-bit 폴백

렌더 필터에 고정 `scale`, `pad`, `crop`, 배경 `overlay`를 추가하지 않는다. 세로 영상은
세로 픽셀 배열로 출력하고, 가로·세로 모두 원본 표시 해상도를 보존한다. 고정 `-r`을
추가하지 않고 `-fps_mode:v passthrough`를 유지한다. JSON 필드와 상태값은 번역하지 않고
`reason`만 `ko/en`으로 직렬화하며 CLI 기본값은 `ko`다.

부분 매칭은 opt-in이다. `analyze --partial`은 부분 구간을 진단만 하고,
`process --mode fallback`은 일치 구간에 레코더음을 쓰고 나머지 구간에 카메라음을 쓴다.
승인 구간은 영상 타임라인에서 정렬되고 겹치지 않아야 하며, 기본 50ms crossfade로
경계 클릭을 완화한다. JSON 리포트 v2는 `partial`, `coverage_ratio`, `segments`를 제공한다.

기본 출력 파일명은 `<원본 stem>.mp4`이며 자동 접두사·접미사를 붙이지 않는다. 사용자가
명시한 `--output-prefix/--output-suffix`만 적용하고 경로 구분자를 거부한다. 어떤
경우에도 출력 경로가 원본 영상과 같으면 `--overwrite` 여부와 무관하게 렌더하지 않는다.

## 검증 명령

```bash
bash scripts/check.sh
bash scripts/test-e2e.sh
uv run python scripts/benchmark_matcher.py
```

비즈니스 로직 변경은 관련 단위 테스트 RED를 먼저 확인한 뒤 최소 구현으로 GREEN을
만든다. 알고리즘 변경은 전체 테스트와 벤치를 함께 실행하고, 단순 CLI·문서 변경은
영향받는 단위 테스트와 전체 정적 검사를 실행한다. 작업 종료 시 변경·테스트·성능·보안,
남은 위험, 문서 개선 제안을 보고한다.

E2E는 사용자가 명시적으로 요청한 실제 도구 경계 검증이다. `tests/e2e/`는 고정 seed의
합성 미디어만 임시 디렉터리에 생성하고 네트워크·사용자 파일을 금지한다.

## PR 병합 절차

1. 최신 `main`에서 worktree 작업 브랜치를 만든다.
2. RED→GREEN→Refactor와 로컬 검증을 완료하고 push한다.
3. `gh pr create`로 PR을 만들고 변경·테스트·성능·보안 영향을 기록한다.
4. GitHub Actions의 Unit Tests, Synthetic FFmpeg E2E, Quality가 모두 통과해야 한다.
5. 미해결 리뷰 대화를 정리한 뒤 GitHub PR을 통해 병합하고 작업 브랜치를 삭제한다.

긴급 상황에서도 branch protection을 우회하지 않는다. 보호 규칙 변경이 필요하면 이유와
복구 시점을 먼저 기록하고 별도 승인을 받는다.
