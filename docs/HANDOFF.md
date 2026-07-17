# 인수인계와 다음 작업

## 현재 상태

- 저장소: `git@github.com:dizy64/recordersync.git`
- 기본 브랜치: `main`
- 패키지/CLI 버전: `0.1.3`
- 최초 기능 완료 커밋: `3873f62`
- Python: 3.14+
- 플랫폼: macOS
- 자동 테스트: 단위 테스트 56개, 기준 커버리지 88%
- 성능 기준: 12시간·영상 200개, 총 약 31.7초, p95 0.159초, p99 0.160초,
  peak RSS 약 322MB(2026-07-17 Apple Silicon)

현재 main은 분할 녹음 세션 구성, 영상별 FFT NCC 매칭, 반복 후보 거부, clock drift,
replace/mix, VideoToolbox/libx265 렌더, JSON 리포트, 공개 Python API를 포함한다.
TubeArchive 저장소는 아직 이 패키지를 호출하지 않는다.

## 먼저 읽을 문서

1. [CONCEPT.md](CONCEPT.md): 제품 범위와 안전 정책
2. [ARCHITECTURE.md](ARCHITECTURE.md): 알고리즘, 모듈 경계, 불변식
3. [../CONTRIBUTING.md](../CONTRIBUTING.md): worktree, TDD, 검증
4. [TESTING.md](TESTING.md): 테스트 케이스 작성법
5. [OPERATIONS.md](OPERATIONS.md): 전역 설치, 실행, 문제 해결
6. [REPORT_SCHEMA.md](REPORT_SCHEMA.md): 외부 연동 JSON 계약
7. [../SECURITY.md](../SECURITY.md): 미디어·리포트 개인정보와 보안

## 변경하면 안 되는 핵심 불변식

- 원본 영상과 오디오를 수정·이동·삭제하지 않는다.
- `ambiguous`, `unmatched`, `error`에는 결과 영상을 만들지 않는다.
- 사용자가 요청하지 않은 mix 또는 overwrite를 자동 선택하지 않는다.
- 기본 출력명은 `<원본 stem>.mp4`이고, 명시된 prefix/suffix만 적용한다.
- 출력 경로와 원본 경로가 같으면 overwrite가 있어도 거부한다.
- 서로 다른 영상이 같은 외부 구간에 매칭되는 것을 허용한다.
- 2GB 조각을 대용량 중간 오디오로 물리 결합하지 않는다.
- concat 매니페스트에는 절대 경로를 사용한다.
- 조각 특징 길이를 duration에 맞춰 경계 offset 누적을 막는다.
- 렌더는 임시 파일 성공 후 최종 경로로 원자 이동한다.
- subprocess는 인자 배열과 `shell=False`를 사용한다.
- 자동 테스트에 실제 사용자 미디어나 네트워크를 넣지 않는다.

이 불변식을 바꾸는 요구는 단순 리팩터가 아니라 제품 정책 변경이다. 별도 합의, RED
테스트, 문서와 REPORT_VERSION 영향을 먼저 정리한다.

## 알려진 한계

### 입력과 세션

- 디렉터리 바로 아래만 스캔하며 재귀 탐색하지 않는다.
- CLI에서 `--audio-dir`를 생략하면 `VIDEO_DIR`를 오디오 입력으로도 사용한다.
- creation_time이 없고 여러 별도 녹음이 동일한 복사 시각과 연속 파일명을 가지면 자동
  세션 경계를 알 수 없다.
- 동일 세션 안 codec/sample rate/channel이 바뀌면 별도 세션으로 분리한다.
- 사용자가 명시적으로 세션 파일 목록이나 순서를 제공하는 manifest 기능은 없다.

### 매칭

- 기본 임계값은 합성 fixture와 제한된 smoke를 기준으로 하며 다양한 실제 레코더/카메라
  조합에 대한 대규모 calibration은 아직 없다.
- 매우 반복적인 음악, 장시간 일정한 소리, 거의 무음인 카메라음은 자동 매칭이 어렵다.
- 특징과 ffprobe 결과를 캐시하지 않아 재실행할 때 모든 오디오를 다시 디코딩한다.
- drift는 클립 앞뒤 특징 위치의 선형 비율 하나로 보정하며 구간별 비선형 drift는 다루지
  않는다.

### 렌더와 운영

- 출력 해상도·방향·프레임 타임스탬프/VFR은 원본을 유지하지만 BT.709 HEVC
  10-bit/AAC 프로파일과 영상 bitrate는 고정되어 있다.
- 실시간 진행률·취소 후 resume·디스크 사전 용량 검사는 없다.
- macOS 외 플랫폼은 지원 대상으로 검증하지 않았다.
- JSON은 `version: 1`이지만 JSON Schema 파일은 아직 없다.
- release tag와 자동 배포 파이프라인이 없다.

## 다음 우선순위

### P0: 대표 실파일 calibration

1. 사용자 로컬에서 레코더 모델별로 짧은 대표 작업을 `analyze`한다.
2. 정확 매칭, 반복 구간, 무관 구간을 구분해 correlation/margin/confidence 분포를 기록한다.
3. 원본 파일명·절대 경로·오디오를 저장소에 넣지 않고 익명화한 수치만 남긴다.
4. false positive가 있으면 임계값을 낮추지 말고 재현 가능한 합성 특징 테스트를 먼저
   만든다.
5. 기본값 변경 시 정확/반복/무관 세 범주와 12시간 벤치를 모두 재실행한다.

성공 기준은 false positive 0을 우선하고, 놓친 파일은 리포트로 복구 가능한 상태다.

### P1: TubeArchive 연동

권장 첫 단계는 RecorderSync CLI/API가 만든 표준 개별 MP4 목록을 TubeArchive의 기존
scan/merge/upload 경로에 입력하는 것이다. 이 방식은 양 프로젝트의 렌더 책임을
중복시키지 않고 이미 표준화된 파일을 concat할 수 있다.

TubeArchive가 `match_videos()` 결과를 직접 받아 기존 Transcoder에서 렌더하도록
통합한다면 다음을 함께 구현해야 한다.

- `session_id`에서 `RecordingSession.chunks`로 해석하는 경계
- 다중 조각 concat 매니페스트를 외부 audio input으로 받는 FFmpeg executor
- `external_start_seconds`, 영상 duration, `tempo_ratio` 전달
- 조각 경계를 넘는 영상의 회귀 테스트
- `ambiguous/unmatched/error`를 TubeArchive 병합에서 제외하는 정책
- RecorderSync REPORT_VERSION과 TubeArchive 저장 모델 호환성

첫 오디오 조각만 `external_audio_path`로 넘기는 구현은 금지한다.

### P2: 반복 실행 비용

- 파일 path, size, mtime, feature parameter version을 키로 특징 cache 설계
- cache 원자 쓰기와 손상 시 재생성
- 개인정보가 포함될 수 있는 cache 위치와 삭제 명령 문서화
- 12시간 재분석 wall time과 디스크 사용량 전후 측정

### P3: 운영성

- 진행률과 영상별 ETA
- 렌더 전 출력 예상 크기 및 디스크 여유 검사
- 중단된 배치 resume
- versioned JSON Schema
- release tag 기반 전역 설치와 changelog

## 새 작업 시작 체크리스트

```bash
git status --short --branch
git pull --ff-only origin main
mkdir -p ../.worktrees/recordersync
git worktree add -b <type>/<short-name> \
  ../.worktrees/recordersync/<short-name> main
cd ../.worktrees/recordersync/<short-name>
uv sync
uv run pytest tests/unit -q
```

그다음 관련 테스트 파일에서 RED를 만들고 구현한다. 완료 시 전체 검사, 벤치 필요 여부,
비밀정보·미디어 추적 여부, 문서 갱신 여부를 확인한다.

## 배포·전역 설치 인수인계

전역 명령 설치와 강제 갱신은 [OPERATIONS.md](OPERATIONS.md)를 따른다. main 변경 후
버전이 같아 uv가 생략할 수 있으므로 로컬 개발 배포는 다음 명령이 기준이다.

```bash
uv tool install --python 3.14 --force --reinstall \
  /Users/zero/Workspaces/dizy64/recordersync
recordersync --version
```

원격 main에서 설치할 때는 `--refresh`와 Git SSH source를 사용한다. 안정적인 운영
배포에는 main이 아니라 tag 또는 commit SHA를 고정한다.

## 작업 종료 보고 형식

- 변경 요약: 목표, 주요 결정, 변경 파일
- 테스트 결과: 케이스 수, 커버리지, 관련 smoke
- 성능 결과: 입력 규모, 총시간, p95/p99, peak RSS
- 보안·데이터: 비밀정보·사용자 미디어·절대 경로 포함 여부
- 남은 위험과 다음 작업
- CLAUDE.md/문서 개선 제안
