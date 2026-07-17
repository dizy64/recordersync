# recordersync

길게 녹음된 보이스레코더 파일과 카메라 영상의 내장 오디오를 비교해, 영상별로
일치하는 외부 오디오 구간을 찾고 교체하는 macOS CLI 도구입니다.

보이스레코더가 2GB 단위로 나눈 파일은 녹음 시각과 자연 파일명 순서로 정렬해
여러 녹음 세션으로 자동 구성합니다. 조각들을 거대한 중간 파일로 합치지 않고
논리 타임라인으로 분석하므로 원본을 변경하지 않습니다.

## 요구 사항

- macOS
- Python 3.14 이상
- [uv](https://docs.astral.sh/uv/)
- FFmpeg 8 이상(`ffmpeg`, `ffprobe`, VideoToolbox 지원 빌드)

```bash
brew bundle
uv sync
```

## 빠른 시작

매칭 결과만 확인합니다. 파일을 만들지 않고 JSON을 표준 출력으로 내보냅니다.

```bash
uv run recordersync analyze ~/Videos/day1 \
  --audio-dir ~/Recordings/day1
```

매칭이 확실한 영상만 `~/Videos/day1/replace`에 생성합니다.

```bash
uv run recordersync process ~/Videos/day1 \
  --audio-dir ~/Recordings/day1
```

카메라 현장음을 10% 섞으려면 `mix` 모드를 명시합니다. 낮은 신뢰도의 매칭이
자동으로 mix로 전환되지는 않습니다.

```bash
uv run recordersync process ~/Videos/day1 \
  --audio-dir ~/Recordings/day1 \
  --mode mix \
  --camera-audio-volume 0.10
```

출력 위치와 리포트 위치를 지정할 수 있습니다.

```bash
uv run recordersync process ~/Videos/day1 \
  --audio-dir ~/Recordings/day1 \
  --output-dir ~/Videos/day1/final-audio \
  --report ~/Videos/day1/match-report.json
```

## 동작 방식

1. 오디오 파일을 `creation_time` → macOS 생성 시각 → 수정 시각 → 자연 파일명
   순서로 정렬합니다.
2. 앞 조각의 예상 종료와 다음 조각 시작 사이에 기본 10초를 넘는 양수 공백이
   있거나 스트림 규격이 바뀌면 새 녹음 세션으로 분리합니다.
3. FFmpeg로 8kHz mono PCM을 추출하고 6대역 log-energy 특징을 만듭니다.
4. FFT 기반 normalized cross-correlation으로 후보 구간을 찾고, 시작·끝 특징으로
   offset과 recorder clock drift를 보정합니다.
5. confidence 0.75 이상이고 차순위 peak와 0.05 이상 차이 나는 결과만 승인합니다.
6. 승인된 영상은 TubeArchive 호환 3840×2160 HEVC 10-bit/AAC MP4로 출력합니다.

기본 출력 이름은 `replace/<원본명>_replaced.mp4`입니다. 기존 출력은
`--overwrite` 없이는 덮어쓰지 않습니다.

### 매칭 상태

| 상태 | 의미 | 출력 생성 |
|---|---|---|
| `matched` | 신뢰도와 peak 유일성 기준 통과 | 예 |
| `unmatched` | 상관도 또는 confidence 부족 | 아니요 |
| `ambiguous` | 비슷한 후보가 둘 이상 존재 | 아니요 |
| `error` | 오디오 스트림 없음, probe/렌더 실패 등 | 아니요 |

종료 코드는 전체 성공 `0`, 일부 미매칭·애매함·오류 `2`, 입력이나 세션 분석의
치명적 실패 `1`입니다. `process`는 기본적으로 출력 디렉터리에
`recordersync-report.json`을 저장합니다.

## 주요 옵션

```text
--output-dir DIR              출력 디렉터리(기본: VIDEO_DIR/replace)
--report PATH                 JSON 리포트 저장 경로
--min-confidence 0.75         최소 종합 신뢰도
--min-peak-margin 0.05        최고/차순위 상관 peak 최소 차이
--session-gap-seconds 10      새 녹음 세션으로 나눌 시간 공백
--mode replace|mix            외부 음원 교체 또는 카메라음 혼합
--camera-audio-volume 0.10    mix 모드의 카메라 음량(0.0~1.0)
--dry-run                     process 계획만 출력
--overwrite                   기존 결과 덮어쓰기 허용
```

지원 오디오: AAC, AIF/AIFF, FLAC, M4A, MP3, WAV/WAVE

지원 영상: AVI, M2TS, M4V, MKV, MOV, MP4, MTS, WebM

## TubeArchive 연동 지점

v1은 독립 CLI만 제공하지만 Python API는 렌더와 분리되어 있습니다.

```python
from pathlib import Path

from recordersync.api import discover_sessions, match_videos

sessions = discover_sessions(Path("~/Recordings/day1").expanduser())
matches = match_videos(video_paths, sessions)
```

향후 TubeArchive는 `match_videos()`의 `session_id`, `external_start_seconds`,
`tempo_ratio`를 기존 Transcoder에 전달하고, 기존 Merger와 YouTube 업로더를 그대로
사용할 수 있습니다.

## 개발 및 검증

```bash
uv run pytest tests/unit -q
uv run mypy recordersync
uv run ruff check recordersync tests
uv run ruff format --check recordersync tests
uv run pip-audit
uv run python scripts/benchmark_matcher.py
```

비즈니스 로직 테스트는 FFmpeg와 파일 I/O를 목/스텁으로 격리한 단위 테스트입니다.
실제 운영 전에는 대표 보이스레코더 파일로 `analyze`를 먼저 실행하고, 결과 영상의
초반·중간·후반 싱크를 확인하는 것을 권장합니다.
