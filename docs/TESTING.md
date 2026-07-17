# 테스트 전략과 케이스 지도

## 원칙

RecorderSync의 비즈니스 정책은 단위 테스트로 검증한다. FFmpeg·ffprobe·파일 시스템·
네트워크는 도메인 정책과 분리하고 목/스텁으로 대체한다. 별도의 E2E는 공개 가능한
합성 미디어를 임시 디렉터리에 만들고 실제 CLI와 FFmpeg 경계만 확인한다.

현재 기준은 단위 테스트 96개, E2E 3개, 단위 커버리지 90%다. 새 변경은 커버리지를 85% 아래로
떨어뜨리지 않고, 변경된 비즈니스 분기의 정상·경계·오류를 직접 검증해야 한다.

## 현재 테스트 지도

| 파일 | 개수 | 책임 |
|---|---:|---|
| `test_models.py` | 2 | 부분 구간 정렬·겹침·영상 경계 불변식 |
| `test_sessions.py` | 7 | 자연 정렬, 연속 조각, gap, 스트림 불일치, 복사 시각 |
| `test_matching.py` | 12 | 전체/부분/다중 구간, 전역 재탐색 상한, 입력 검증, drift, 다중 카메라 |
| `test_media.py` | 7 | 탐색, ffprobe 파싱, PCM, 실패, 조각 frame padding |
| `test_render.py` | 16 | 출력명·경로 안전, 다중 구간 폴백, crossfade, 프로파일, 원자 출력 |
| `test_pipeline.py` | 6 | 배치 분석, matched/partial 렌더 정책, 잘못된 구간의 영상별 오류 처리 |
| `test_cli.py` | 23 | 도움말, 부분 opt-in, 모드 추천, fallback, 사람용/JSON 리포트, 종료 코드 |
| `test_api.py` | 4 | 외부 소비자용 세션·매칭·다중 세션 렌더 계획 API |
| `test_report.py` | 9 | JSON v2, 부분 구간/사용률, 한국어·영어 목록, 실패 사유 |
| `test_recommendation.py` | 10 | replace/fallback 추천과 신뢰도·커버리지·연속 길이 보류 경계 |
| `e2e/test_cli_pipeline.py` | 3 | 분할 WAV, 세로 해상도/FPS, mix와 다중 구간 fallback 렌더 |

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
- 외부 녹음이 영상 중간에만 있으면 해당 연속 구간만 `partial`로 승인하는가
- 영상 중간의 단절과 다른 세션 재시작이 서로 겹치지 않는 여러 구간으로 나뉘는가
- 최소 부분 길이보다 짧거나 후보가 애매한 창은 승인하지 않는가
- flat band가 NaN이나 무한대를 만들지 않는가

특징 테스트는 고정 seed를 사용하고, 테스트가 통과하기 쉬운 임계값으로 결과를 조작하지
않는다. 기본 임계값을 변경한다면 반복/무관/정확 매칭 세 종류를 함께 재검증한다.

### 렌더와 데이터 안전

- concat 경로에 공백과 작은따옴표가 있어도 escaping되는가
- 매니페스트 경로가 임시 디렉터리가 아닌 절대 원본 경로인가
- replace에 `amix`가 없고 mix에는 요청한 카메라/외부 볼륨이 있는가
- fallback이 일치 구간마다 올바른 세션 concat 입력을 쓰고 나머지는 카메라음인가
- 다중 구간 경계를 기본 50ms crossfade로 연결하고 출력 길이를 영상에 맞추는가
- fallback에 카메라 오디오가 없거나 구간이 겹치면 렌더 전에 거부하는가
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
- `analyze --partial`과 `process --mode fallback`만 부분 매칭을 활성화하는가
- partial은 fallback process에서만 성공 출력으로 취급되는가
- 부분 성공이 종료 코드 2인가
- 치명적 입력 오류가 종료 코드 1인가
- JSON 필드와 null 가능성이 REPORT_VERSION 계약과 일치하는가
- 기본 한국어와 명시적 영어 사유가 출력되고 미지원 언어는 거부되는가
- 알 수 없는 코덱·FFmpeg 진단이 번역 과정에서 손실되지 않는가
- 사용자 입력 오류에 비밀값이나 전체 subprocess 환경을 출력하지 않는가
- 인자 없는 실행과 `--help`가 대표 명령과 두 오디오 볼륨을 안내하는가
- 기본 `analyze`는 핵심 사람용 목록이고 `--json`에서만 상세 JSON인가
- `--report` 파일과 `process` stdout은 기존 JSON 계약을 유지하는가
- 전체 일치는 replace, 안전 기준을 통과한 부분 일치는 fallback을 추천하는가
- 짧고 분산되거나 커버리지가 낮은 부분 일치는 처리 보류로 안내하는가
- 추천이 상태, 종료 코드, process 모드를 자동으로 바꾸지 않는가
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
uv run python scripts/benchmark_matcher.py --partial
```

기본 입력은 50ms hop의 12시간 특징과 60초 영상 200개다. 결과에는 총시간,
영상당 p95, peak RSS가 나온다. `--partial`은 서로 멀리 떨어진 24초 구간 두 개 사이에
12초 무관 구간을 넣어 전체 매칭을 실패시키고, 부분 전역 재탐색과 연속 창 로컬 추적을
실제로 수행한다. 알고리즘 변경 검토 시 두 모드를 측정하고 p99도 기록한다.

2026-07-17 Apple Silicon 재측정 기준:

```text
mode              total      p95      p99      peak RSS
default          31.623 s   0.159 s  0.160 s   288.3 MB
--partial       160.084 s   0.805 s  0.810 s   288.5 MB
```

CI 환경 차이 때문에 엄격한 wall-clock 단위 테스트로 만들지 않는다. 대신 알고리즘 변경
PR에 동일 하드웨어 전후 수치를 기록한다.

## 합성 FFmpeg E2E

실제 파일을 저장소에 넣지 않는다. `mktemp -d` 아래에서 고정 seed의 합성 noise를 만들고
분할 레코더·카메라 영상을 구성한다. 부분 폴백 fixture는 22초 영상 안에 서로 떨어진
6초 레코더 구간 두 개와 무관한 카메라음 구간을 배치한다.

`bash scripts/test-e2e.sh`가 다음을 자동 검증한다.

- 두 조각이 한 세션이 되는가
- 시작점이 약 3.00초인가
- 기본 임계값에서 `matched`인가
- process 결과가 180×320 세로, 24fps, HEVC 10-bit, AAC 48kHz를 유지하는가
- 원본/외부 볼륨 0.2/0.8 mix가 실제 렌더되는가
- 두 부분 구간만 레코더음으로 교체되고 사이·앞뒤는 카메라음으로 남는가
- stereo 카메라음과 mono 레코더음의 채널 레이아웃 차이에도 렌더되는가
- 구간별 RMS가 설정한 레코더/카메라 볼륨 차이를 반영하는가
- 부분 리포트가 v2 `segments`와 `coverage_ratio`를 제공하는가
- 사람용과 JSON 분석 출력 모두에서 전체 매칭은 `replace`, 기준을 통과한 부분 매칭은
  `fallback` 추천을 영상별로 제공하는가
- 선택 파일과 세 단계 진행률, 한국어 JSON 리포트가 분리 출력되는가
- 임시 디렉터리 삭제 후 저장소에 미디어가 남지 않는가

수동으로 매체를 살펴볼 때만 [OPERATIONS.md](OPERATIONS.md)의 smoke 절을 사용한다.
