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

영상과 레코더 파일이 같은 디렉터리에 있으면 `--audio-dir`를 생략합니다. 매칭 결과만
확인하는 `analyze`는 미디어 파일을 만들지 않고 JSON을 표준 출력으로 내보냅니다.

```bash
uv run recordersync analyze ~/Capture/day1
```

매칭이 확실한 영상만 `~/Capture/day1/replace`에 생성합니다.

```bash
uv run recordersync process ~/Capture/day1
```

레코더 파일이 별도 디렉터리에 있을 때만 경로를 지정합니다.

```bash
uv run recordersync process ~/Videos/day1 --audio-dir ~/Recordings/day1
```

카메라 현장음을 10% 섞으려면 `mix` 모드를 명시합니다. 낮은 신뢰도의 매칭이
자동으로 mix로 전환되지는 않습니다.

```bash
uv run recordersync process ~/Videos/day1 \
  --audio-dir ~/Recordings/day1 \
  --mode mix \
  --camera-audio-volume 0.20 \
  --external-audio-volume 0.80
```

두 볼륨은 각각 0.0~1.0 범위입니다. `replace`에서도 외부 음량을 줄일 수 있고,
원본 영상 음량은 `mix`에서만 사용됩니다.

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
6. 승인된 영상은 원본 표시 해상도, 가로·세로 방향, 프레임 타임스탬프를 유지한 HEVC
   10-bit/AAC MP4로 출력합니다. 스마트폰 회전 메타데이터와 VFR도 보존합니다.

기본 출력 이름은 `replace/<원본명>.mp4`입니다. 접두사·접미사가 필요할 때만
`--output-prefix`와 `--output-suffix`를 지정합니다. 기존 출력은 `--overwrite` 없이는
덮어쓰지 않으며, 출력 경로를 원본 MP4와 같게 지정하면 overwrite 여부와 관계없이
거부합니다.

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

선택된 오디오·영상 파일과 오디오 분석/영상 매칭/렌더 진행률은 표준 오류에 표시합니다.
JSON 결과는 표준 출력에만 기록하므로 파이프나 자동화에서 분리해 사용할 수 있습니다.

## 주요 옵션

```text
--audio-dir DIR               레코더 오디오 디렉터리(기본: VIDEO_DIR)
--output-dir DIR              출력 디렉터리(기본: VIDEO_DIR/replace)
--output-prefix TEXT          출력 파일명 앞에 붙일 문자열(기본: 없음)
--output-suffix TEXT          출력 파일명 뒤에 붙일 문자열(기본: 없음)
--report PATH                 JSON 리포트 저장 경로
--report-language ko|en       리포트 사유 언어(기본: ko)
--min-confidence 0.75         최소 종합 신뢰도
--min-peak-margin 0.05        최고/차순위 상관 peak 최소 차이
--session-gap-seconds 10      새 녹음 세션으로 나눌 시간 공백
--mode replace|mix            외부 음원 교체 또는 카메라음 혼합
--camera-audio-volume 0.10    mix 모드의 카메라 음량(0.0~1.0)
--external-audio-volume 1.0   외부 레코더 음량(0.0~1.0)
--dry-run                     process 계획만 출력
--overwrite                   기존 결과 덮어쓰기 허용
```

인자 없이 실행하면 대표 사용법을 표시합니다. 전체/하위 명령 도움말은 다음과 같습니다.

```bash
recordersync --help
recordersync process --help
```

JSON 키와 `matched` 같은 상태값은 자동화 호환성을 위해 영어로 고정되며, 사람이 읽는
`reason`만 기본 한국어 또는 `--report-language en`의 영어로 출력됩니다.

예를 들어 `clip.mov`를 `final_clip_synced.mp4`로 만들려면 다음처럼 실행합니다.

```bash
recordersync process ~/Capture/day1 --output-prefix final_ --output-suffix _synced
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

가장 단순한 연동은 RecorderSync가 만든 표준 개별 MP4 목록을 TubeArchive의 기존
병합·업로드 경로에 전달하는 것입니다. 매칭 결과를 Transcoder가 직접 소비하려면
`session_id`, `external_start_seconds`, `tempo_ratio`와 함께 여러 레코더 조각을 concat
입력으로 처리해야 합니다. 첫 오디오 조각만 전달하면 조각 경계를 넘는 영상이 깨집니다.

## 전역 설치와 업데이트

저장소 밖에서도 `recordersync`를 직접 호출하려면 uv의 격리된 tool 환경에 설치합니다.

```bash
uv tool install --python 3.14 /absolute/path/to/recordersync
uv tool update-shell
exec zsh
recordersync --version
```

버전이 같아도 현재 로컬 소스로 강제 갱신하려면 다음을 사용합니다.

```bash
uv tool install --python 3.14 --force --reinstall /absolute/path/to/recordersync
```

GitHub main 직접 설치, editable 개발 설치, 제거 방법은
[설치·실행·운영 가이드](docs/OPERATIONS.md)에 정리되어 있습니다.

## 개발 및 검증

```bash
bash scripts/format.sh    # Ruff 자동 수정·포맷
bash scripts/check.sh     # lint, format, mypy, 단위 테스트, 감사, 복잡도, build
bash scripts/test-e2e.sh  # 실제 FFmpeg 합성 E2E
uv run python scripts/benchmark_matcher.py
bash scripts/install-hooks.sh
```

비즈니스 로직 테스트는 FFmpeg와 파일 I/O를 목/스텁으로 격리한 단위 테스트입니다.
별도 E2E는 임시 디렉터리에 공개 가능한 합성 미디어만 만들며 실제 CLI와 FFmpeg로
분할 오디오 매칭, 세로 해상도/FPS, 렌더와 리포트를 검증합니다.

## 문서

- [제품 컨셉](docs/CONCEPT.md)
- [아키텍처와 알고리즘](docs/ARCHITECTURE.md)
- [개발·기여 방법](CONTRIBUTING.md)
- [테스트 전략](docs/TESTING.md)
- [설치·실행·운영](docs/OPERATIONS.md)
- [JSON 리포트 계약](docs/REPORT_SCHEMA.md)
- [인수인계와 다음 작업](docs/HANDOFF.md)
- [보안·개인정보](SECURITY.md)
