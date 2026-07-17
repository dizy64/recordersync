# 설치·실행·운영 가이드

## 시스템 준비

```bash
brew install uv ffmpeg
python3 --version
uv --version
ffmpeg -version
ffmpeg -hide_banner -encoders | rg 'hevc_videotoolbox|libx265'
```

Python 3.14 이상과 `ffmpeg`, `ffprobe`가 PATH에 있어야 한다. VideoToolbox가 없으면
하드웨어 렌더는 실패하고 libx265 폴백을 시도하지만 처리 시간이 크게 늘어난다.

## 저장소 개발 환경

```bash
git clone git@github.com:dizy64/recordersync.git
cd recordersync
brew bundle
uv sync
uv run recordersync --version
```

이 방식은 저장소 안의 `.venv`를 사용한다. 명령 앞에 `uv run`이 필요하다.

## 전역 명령 설치

`uv tool install`은 RecorderSync 전용 격리 환경을 만들고 실행 파일을 uv tool bin
디렉터리에 둔다. 일반 셸에서 `uv run` 없이 호출하려면 다음을 실행한다.

```bash
uv tool install --python 3.14 /absolute/path/to/recordersync
uv tool dir --bin
uv tool update-shell
exec zsh
command -v recordersync
recordersync --version
```

이 저장소의 기본 로컬 경로를 사용하는 예:

```bash
uv tool install --python 3.14 /Users/zero/Workspaces/dizy64/recordersync
```

기본 실행 디렉터리는 현재 macOS 환경에서 `~/.local/bin`이다. `command -v` 결과가
없으면 `uv tool update-shell` 후 새 셸을 열거나 다음을 확인한다.

```bash
uv tool dir --bin
type -a recordersync
```

### 로컬 저장소 변경 후 강제 업데이트

RecorderSync의 버전이 아직 같아도 최신 로컬 소스로 반드시 다시 설치하려면
`--force --reinstall`을 함께 사용한다.

```bash
cd /Users/zero/Workspaces/dizy64/recordersync
git pull --ff-only origin main
uv tool install --python 3.14 --force --reinstall \
  /Users/zero/Workspaces/dizy64/recordersync
recordersync --version
```

의존성 캐시까지 새로 확인해야 할 때만 `--refresh`를 추가한다.

```bash
uv tool install --python 3.14 --force --reinstall --refresh \
  /Users/zero/Workspaces/dizy64/recordersync
```

### GitHub에서 직접 설치·강제 업데이트

로컬 clone 없이 SSH 접근 권한을 사용해 `main`을 설치할 수 있다.

```bash
uv tool install --python 3.14 \
  'recordersync @ git+ssh://git@github.com/dizy64/recordersync.git@main'
```

같은 버전 번호로 main이 갱신된 경우에도 다시 가져오려면 다음을 사용한다.

```bash
uv tool install --python 3.14 --force --reinstall --refresh \
  'recordersync @ git+ssh://git@github.com/dizy64/recordersync.git@main'
```

일반 업그레이드는 설치 시 기록된 source를 기준으로 실행할 수 있다.

```bash
uv tool upgrade --reinstall recordersync
```

재현 가능한 운영 설치가 필요하면 `@main` 대신 release tag 또는 commit SHA를 지정한다.

```bash
uv tool install --force --reinstall \
  'recordersync @ git+ssh://git@github.com/dizy64/recordersync.git@<tag-or-sha>'
```

### 개발용 editable 전역 설치

소스 수정이 전역 명령에 즉시 반영되어야 하는 로컬 개발에서만 사용한다.

```bash
uv tool install --python 3.14 --force --editable \
  /Users/zero/Workspaces/dizy64/recordersync
```

의존성이나 entry point가 바뀌면 editable이어도 `--force --reinstall`이 필요하다. 운영
사용에서는 소스 이동·삭제에 취약한 editable 설치를 피한다.

### 상태 확인과 제거

```bash
uv tool list
command -v recordersync
recordersync --version
uv tool uninstall recordersync
```

전역 Python tool을 제거해도 Homebrew FFmpeg나 사용자 미디어는 삭제되지 않는다.

## 안전한 실행 순서

영상과 레코더 파일이 같은 디렉터리에 있으면 `--audio-dir`를 생략한다. 별도
디렉터리인 경우에만 `--audio-dir /path/to/recorder-files`를 추가한다.

```bash
recordersync
recordersync --help
recordersync process --help
```

인자 없이 호출하면 대표 명령을 안내하고 종료 코드 0으로 끝난다.

### 1. 분석

```bash
recordersync analyze /path/to/media \
  --report /safe/private/path/analysis.json
```

터미널에는 다음처럼 파일별 핵심 결과만 표시된다.

```text
분석 결과: 1/2개 매칭 (50.0%)
- clip-001.mov | 매칭 여부: 성공 | 매칭률: 94.0%
- clip-002.mov | 매칭 여부: 실패 | 매칭률: 71.0% | 사유: 최상위 후보와 차순위 후보의 차이가 충분하지 않습니다.
```

전체 세션·offset·상관도 등 기계 판독 필드는 `--json`을 명시한다.

```bash
recordersync analyze /path/to/media --json >analysis.json 2>progress.log
```

`--report PATH`는 기본 사람용 화면을 유지하면서 해당 파일에 전체 JSON을 저장한다.
리포트의 `reason`은 기본 한국어다. 영어가 필요한 연동이나 공유에서는 다음을 추가한다.

```bash
recordersync analyze /path/to/media --report-language en
```

별도 디렉터리 예:

```bash
recordersync analyze /path/to/videos --audio-dir /path/to/recorder-files
```

사람용 목록에서 실패 파일과 사유를 먼저 확인한다. 상세 JSON의 `summary`,
`audio_sessions`, 각 영상의 `confidence`, `peak_margin`, 시작점은 자동화나 심층 진단에만
사용한다. JSON에는 절대 경로가 들어가므로 저장소나 공개 이슈에 커밋하지 않는다.

### 2. dry run

```bash
recordersync process /path/to/media \
  --output-dir /path/to/output \
  --dry-run
```

렌더 없이 예상 출력 경로를 확인한다.

### 3. 렌더

```bash
recordersync process /path/to/media \
  --output-dir /path/to/output
```

기본 결과는 `/path/to/output/<원본 stem>.mp4`다. 이름을 구분해야 할 때만 접두사나
접미사를 추가한다.

```bash
recordersync process /path/to/media \
  --output-prefix final_ \
  --output-suffix _synced
```

접두사·접미사에는 경로 구분자를 사용할 수 없다. `--output-dir`를 원본 디렉터리로
지정해 계산된 출력 경로가 원본 MP4와 같아지는 경우에는 `--overwrite`도 허용되지 않는다.

현장음을 남기려면 명시적으로 mix를 사용한다.

```bash
recordersync process /path/to/media \
  --mode mix \
  --camera-audio-volume 0.20 \
  --external-audio-volume 0.80
```

두 값은 0.0~1.0 범위의 독립적인 FFmpeg 볼륨 배수다. 외부 음량은 replace와 mix 모두에
적용되고, 카메라 음량은 mix에서만 적용된다. 합이 1일 필요는 없으며 큰 합은 clipping을
유발할 수 있으므로 결과를 청취한다.

실행 중 선택된 파일 목록과 `[오디오 분석]`, `[영상 매칭]`, `[영상 렌더]` 진행률은
stderr로 출력된다. stdout JSON만 저장하려면 다음처럼 분리한다.

```bash
recordersync process /path/to/media >result.json 2>progress.log
```

`--overwrite`는 기존 결과를 교체해도 되는 경우에만 사용한다. 50Mbps 영상은 이론상
시간당 약 22.5GB이므로 출력 디스크 여유 공간을 먼저 확인한다.

## 임계값 조정

기본값은 confidence 0.75, peak margin 0.05다.

- 정확한 음원인데 `unmatched`: `--min-confidence`를 조금 낮추기 전에 카메라음 유무,
  세션 구성, 조각 순서, 반복 패턴을 먼저 확인한다.
- 동일한 후렴이나 박수가 반복되어 `ambiguous`: 임계값을 낮추지 말고 입력 세션을
  촬영 단위로 좁히거나 `--session-gap-seconds`를 조정한다.
- 여러 녹음이 한 세션으로 합쳐짐: gap을 줄인다.
- 2GB 조각이 여러 세션으로 잘못 분리됨: gap을 늘리고 ffprobe의 creation_time을
  확인한다.

임계값 변경은 오탐 위험을 늘릴 수 있다. 일부 샘플이 아니라 초반·중간·후반을 직접
검증하고 값을 기록한다.

## 종료 코드 처리

```bash
recordersync process ...
status=$?
case "$status" in
  0) echo "all matched" ;;
  1) echo "fatal input or pipeline error" ;;
  2) echo "partial result: inspect JSON report" ;;
esac
```

자동화에서는 종료 코드 2를 성공으로 덮어쓰지 않는다. 일부 파일만 생성된 정상적인
부분 결과이므로 리포트를 읽어 후속 정책을 결정한다.

## 자주 발생하는 문제

| 증상 | 확인 |
|---|---|
| `No supported audio files` | 확장자와 선택된 오디오 디렉터리 바로 아래 파일인지 확인. 생략 시 VIDEO_DIR 사용 |
| `Camera audio is required` | 카메라 원본에 첫 오디오 스트림이 있는지 ffprobe 확인 |
| 결과가 모두 ambiguous | 반복 음원, 너무 넓은 세션, peak margin 확인 |
| 결과가 이미 존재 | 다른 output dir 또는 의도적인 `--overwrite` 사용 |
| VideoToolbox 실패 | `ffmpeg -encoders`, macOS 권한, 디스크 공간 확인 |
| libx265도 실패 | JSON error와 FFmpeg stderr, 지원 filter/codec 확인 |
| 뒤쪽 싱크가 밀림 | `tempo_ratio`, 입력 클립 길이, 조각 경계 frame padding 회귀 확인 |
| 전역 명령이 없음 | `uv tool dir --bin`, `uv tool update-shell`, `type -a` 확인 |

## 공개 합성 smoke

자동 회귀 검증은 저장소 루트에서 다음 한 줄로 실행한다.

```bash
bash scripts/test-e2e.sh
```

아래 절차는 임시 디렉터리에서만 공개 가능한 pink noise를 만든다. 실제 사용자
미디어를 복사하거나 커밋하지 않는다.

```bash
smoke_dir=$(mktemp -d /tmp/recordersync-smoke.XXXXXX)
mkdir -p "$smoke_dir/audio" "$smoke_dir/video"

ffmpeg -hide_banner -loglevel error \
  -f lavfi -i 'anoisesrc=color=pink:seed=42:duration=12:sample_rate=48000' \
  -c:a pcm_s16le "$smoke_dir/recorder.wav"

ffmpeg -hide_banner -loglevel error \
  -i "$smoke_dir/recorder.wav" \
  -f segment -segment_time 4 -reset_timestamps 1 -c:a pcm_s16le \
  "$smoke_dir/audio/REC_%03d.wav"

ffmpeg -hide_banner -loglevel error \
  -ss 3 -t 4 -i "$smoke_dir/recorder.wav" \
  -f lavfi -i 'color=c=black:s=320x240:r=30000/1001:d=4' \
  -map 1:v:0 -map 0:a:0 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \
  "$smoke_dir/video/clip.mp4"

recordersync analyze "$smoke_dir/video" --audio-dir "$smoke_dir/audio"
recordersync process "$smoke_dir/video" --audio-dir "$smoke_dir/audio" \
  --output-dir "$smoke_dir/output"

ffprobe -v error -show_entries \
  stream=codec_name,width,height,pix_fmt,sample_rate,bit_rate \
  -of json "$smoke_dir/output/clip.mp4"
```

기대 시작점은 약 3.00초이며 출력 해상도는 원본과 같은 320×240이다. 스마트폰 회전
메타데이터가 있는 입력은 autorotate 적용 후의 표시 해상도와 가로·세로 방향이
유지되는지, 원본 frame rate/VFR이 불필요한 CFR로 바뀌지 않는지도 확인한다. 확인 후
임시 디렉터리는 삭제해도 된다.
