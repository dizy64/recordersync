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
- `ambiguous`, `unmatched`, `error` 결과는 렌더하지 않는다.
- 사용자가 명시하지 않은 mix나 overwrite를 자동 선택하지 않는다.
- 레코더 조각은 중간 대용량 파일로 합치지 않고 논리 세션과 concat 입력으로 다룬다.
- 여러 영상이 같은 외부 오디오 구간에 매칭되는 것을 허용한다.
- 렌더는 임시 출력 성공 후 최종 경로로 원자 이동한다.
- 사용자 미디어·리포트·절대 경로·비밀정보를 저장소나 테스트 fixture에 넣지 않는다.
- subprocess는 인자 배열과 `shell=False`를 유지한다.

## 목표 프로파일

- 출력: MP4, 회전 적용 후 원본 표시 해상도 유지, 30000/1001fps
- 영상: HEVC 10-bit, VideoToolbox 50Mbps, BT.709 SDR, `hvc1`
- 오디오: AAC 48kHz 256kbps
- VideoToolbox 실패 시 libx265 10-bit 폴백

렌더 필터에 고정 `scale`, `pad`, `crop`, 배경 `overlay`를 추가하지 않는다. 세로 영상은
세로 픽셀 배열로 출력하고, 가로·세로 모두 원본 표시 해상도를 보존한다.

## 검증 명령

```bash
uv run pytest tests/unit -q
uv run mypy recordersync
uv run ruff check recordersync tests
uv run ruff format --check recordersync tests
uv run pip-audit
uv run python scripts/benchmark_matcher.py
```

비즈니스 로직 변경은 관련 단위 테스트 RED를 먼저 확인한 뒤 최소 구현으로 GREEN을
만든다. 알고리즘 변경은 전체 테스트와 벤치를 함께 실행하고, 단순 CLI·문서 변경은
영향받는 단위 테스트와 전체 정적 검사를 실행한다. 작업 종료 시 변경·테스트·성능·보안,
남은 위험, 문서 개선 제안을 보고한다.
