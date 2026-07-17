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

## 목표 프로파일

- 출력: MP4, 3840x2160, 30000/1001fps
- 영상: HEVC 10-bit, VideoToolbox 50Mbps, BT.709 SDR, `hvc1`
- 오디오: AAC 48kHz 256kbps
- VideoToolbox 실패 시 libx265 10-bit 폴백

## 검증 명령

```bash
uv run pytest tests/unit -q
uv run mypy recordersync
uv run ruff check recordersync tests
uv run ruff format --check recordersync tests
uv run pip-audit
```
