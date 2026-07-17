# 테스트 전략과 케이스 지도

## 원칙

RecorderSync의 비즈니스 정책은 단위 테스트로 검증한다. FFmpeg·ffprobe·파일 시스템·
네트워크는 도메인 정책과 분리하고 목/스텁으로 대체한다. 별도의 E2E는 공개 가능한
합성 미디어를 임시 디렉터리에 만들고 실제 CLI와 FFmpeg 경계만 확인한다.

현재 기준은 단위 테스트 74개, E2E 2개, 단위 커버리지 90%다. 새 변경은 커버리지를 85% 아래로
떨어뜨리지 않고, 변경된 비즈니스 분기의 정상·경계·오류를 직접 검증해야 한다.

## 현재 테스트 지도

| 파일 | 개수 | 책임 |
|---|---:|---|
| `test_sessions.py` | 7 | 자연 정렬, 연속 조각, gap, 스트림 불일치, 복사 시각 |
| `test_matching.py` | 8 | 특징, 정확한 구간, 반복/겹침, 무관 음원, drift, 다중 카메라 |
| `test_media.py` | 7 | 탐색, ffprobe 파싱, PCM, 실패, 조각 frame padding |
| `test_render.py` | 14 | 출력명·경로 안전, 해상도, concat, 프로파일, 두 음량, 폴백, 원자 출력 |
| `test_pipeline.py` | 3 | 배치 분석, 카메라음 없음, matched만 렌더 |
| `test_cli.py` | 19 | 도움말, 기본값, 사람용 분석, opt-in JSON, 파일 리포트, 종료 코드 |
| `test_api.py` | 3 | 외부 소비자용 세션·매칭·렌더 계획 API |
| `test_report.py` | 7 | JSON 계약, 한국어·영어 사람용 목록, 빈 결과, 실패 사유 |
| `test_pr_title.py` | 6 | 한글·혼용·영문·빈 PR 제목과 검사 명령 종료 코드 |
| `e2e/test_cli_pipeline.py` | 2 | 사람용/JSON 분석, 분할 WAV, 세로 해상도/FPS, mix 렌더 |

개수는 구현 기준선이며 새 테스트가 추가되면 표도 갱신한다.

## RED 테스트 설계 예시

### 세션 정책

- `REC_2.wav`가 `REC_10.wav`보다 먼저 정렬되는가
- 2GB 조각의 시작 시각이 앞 조각 종료와 허용 범위 내인가
- 큰 양수 gap이 새 세션을 만드는가
- 복사 때문에 모든 birthtime이 같아도 자연 파일명 순서로 합쳐지는가
- sample rate/channel/codec 변경이 세션을 나누는가
- 조각마다 마지막 50ms가 누적 손실되지 않는가

### 매칭 정책

- 세션 임의 위치에 삽입한 특징을 50ms 이내에서 찾는가
- 동일 패턴이 두 곳에 있으면 `ambiguous`인가
- 영상 길이 안에서 겹치는 반복 패턴도 차순위로 검출하는가
- 무관한 랜덤 특징이 `unmatched`인가
- 세션이 영상보다 짧으면 안전하게 거부하는가
- 두 영상이 같은 외부 위치를 독립적으로 선택할 수 있는가
- 앞/뒤 특징 위치 차이로 기대 `tempo_ratio`를 구하는가
- flat band가 NaN이나 무한대를 만들지 않는가

특징 테스트는 고정 seed를 사용하고, 테스트가 통과하기 쉬운 임계값으로 결과를 조작하지
않는다. 기본 임계값을 변경한다면 반복/무관/정확 매칭 세 종류를 함께 재검증한다.

### 렌더와 데이터 안전

- concat 경로에 공백과 작은따옴표가 있어도 escaping되는가
- 매니페스트 경로가 임시 디렉터리가 아닌 절대 원본 경로인가
- replace에 `amix`가 없고 mix에는 요청한 카메라/외부 볼륨이 있는가
- VideoToolbox 실패 후 libx265 명령을 만드는가
- 소프트웨어 폴백도 실패하면 최종 파일이 남지 않는가
- 기존 출력이 `--overwrite` 없이 보존되는가
- 기본 출력명에 자동 suffix가 없고 명시한 prefix/suffix만 적용되는가
- affix의 경로 구분자를 거부하고 원본과 같은 출력 경로는 overwrite도 거부하는가
- 가로·세로 입력 모두 고정 scale/pad/crop/overlay 없이 원본 표시 해상도를 유지하는가
- 고정 `-r` 없이 `-fps_mode:v passthrough`로 원본 프레임 타임스탬프를 유지하는가
- HLG/PQ 입력이 설치된 FFmpeg에 없는 `zscale`을 요구하지 않는가

### CLI와 리포트

- `--audio-dir` 생략 시 `VIDEO_DIR`가 분석에 전달되는가
- `analyze`가 미디어를 쓰지 않는가
- `process --dry-run`이 렌더러를 호출하지 않는가
- 부분 성공이 종료 코드 2인가
- 치명적 입력 오류가 종료 코드 1인가
- JSON 필드와 null 가능성이 REPORT_VERSION 계약과 일치하는가
- 기본 한국어와 명시적 영어 사유가 출력되고 미지원 언어는 거부되는가
- 알 수 없는 코덱·FFmpeg 진단이 번역 과정에서 손실되지 않는가
- 사용자 입력 오류에 비밀값이나 전체 subprocess 환경을 출력하지 않는가
- 인자 없는 실행과 `--help`가 대표 명령과 두 오디오 볼륨을 안내하는가
- 기본 `analyze`는 핵심 사람용 목록이고 `--json`에서만 상세 JSON인가
- `--report` 파일과 `process` stdout은 기존 JSON 계약을 유지하는가
- 선택 파일·진행률은 stderr에 유지되는가

## 목과 fixture 작성 규칙

```python
rng = np.random.default_rng(20260717)
features = rng.normal(size=(6, 100)).astype(np.float32)
```

- NumPy 배열은 `float32`, shape은 `band x frame`을 사용한다.
- `FFmpegTools`와 `FFmpegRenderer`는 `MagicMock(spec=...)`으로 잘못된 메서드 호출을
  조기에 잡는다.
- subprocess 결과는 `CompletedProcess`로 성공·실패·stderr를 명시한다.
- 파일은 `tmp_path`에 만들고 사용자 홈의 실제 미디어를 참조하지 않는다.
- 날짜는 timezone-aware UTC를 사용한다.
- 예외 타입뿐 아니라 운영자가 이해할 수 있는 핵심 메시지도 확인한다.

## 실행 순서

```bash
# RED를 빠르게 확인
uv run pytest tests/unit/test_matching.py::test_specific_case -q

# 관련 모듈 GREEN
uv run pytest tests/unit/test_matching.py -q

# 전체 회귀
uv run pytest tests/unit -q

# 실제 CLI/FFmpeg 합성 회귀(coverage 계측 제외)
bash scripts/test-e2e.sh

# 커버리지 상세
uv run pytest tests/unit --cov=recordersync --cov-report=term-missing
```

## 성능 테스트

```bash
uv run python scripts/benchmark_matcher.py
```

기본 입력은 50ms hop의 12시간 특징과 60초 영상 200개다. 결과에는 총시간,
영상당 p95, peak RSS가 나온다. 알고리즘 변경 검토 시 별도 계측으로 p99도 기록한다.

2026-07-17 Apple Silicon 재측정 기준:

```text
total        31.645 s
per-video p95 0.159 s
per-video p99 0.161 s
peak RSS      287.9 MB
```

CI 환경 차이 때문에 엄격한 wall-clock 단위 테스트로 만들지 않는다. 대신 알고리즘 변경
PR에 동일 하드웨어 전후 수치를 기록한다.

## 합성 FFmpeg E2E

실제 파일을 저장소에 넣지 않는다. `mktemp -d` 아래에서 결정론적 합성 noise를 12초
만들고 4초 조각으로 나눈 뒤, 3~7초 구간을 카메라 영상 오디오로 사용한다.

`bash scripts/test-e2e.sh`가 다음을 자동 검증한다.

- 두 조각이 한 세션이 되는가
- 시작점이 약 3.00초인가
- 기본 임계값에서 `matched`인가
- process 결과가 180×320 세로, 24fps, HEVC 10-bit, AAC 48kHz를 유지하는가
- 원본/외부 볼륨 0.2/0.8 mix가 실제 렌더되는가
- 선택 파일과 세 단계 진행률, 한국어 JSON 리포트가 분리 출력되는가
- 임시 디렉터리 삭제 후 저장소에 미디어가 남지 않는가

수동으로 매체를 살펴볼 때만 [OPERATIONS.md](OPERATIONS.md)의 smoke 절을 사용한다.
