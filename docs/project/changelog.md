# 변경 이력

RecorderSync의 사용자 동작과 공개 연동 계약 변경을 버전별로 기록한다. GitHub Release의
자동 생성 노트는 커밋·PR 탐색용이며, 이 문서를 제품 변경의 요약 정본으로 사용한다.

## Unreleased

### 추가

- `replace`에서 목표 LUFS, 최대 dBTP, 출력 채널 정책, LU 허용 오차를 모두 명시하는
  opt-in 음량 안전 처리 추가
- gain 전 float EBU R128 분석, static gain/true-peak 충돌 차단, 최종 AAC 재디코딩 검증
- JSON v2 영상별 optional `audio_levels`와 stderr `[음량 검증]` 요약
- `mix`의 카메라 1.0, 외부 `-12dB` 상당, 외부 HP80 보수 기본값과 HPF override
- component 처리 후 합산 float 신호의 음량 측정, 합산 뒤 static gain, 최종 AAC 검증

### 호환성

- 사용자가 단독 사용하므로 기존 mix의 카메라 0.1/외부 1.0 기본값 호환성을 유지하지 않는다. 기존 비율이 필요하면 두 volume 옵션으로 명시한다.
- replace 음량 안전 옵션에는 기본값이나 자동 추론이 없다. mix는 모드를 명시한 경우에만 문서화된 기본 음량·HPF·출력 계약을 적용한다.
- limiter/compressor를 자동 적용하지 않고, gain 충돌 또는 최종 검증 실패 파일은
  최종 경로에 게시하지 않는다.
- JSON 계약은 additive optional 필드이므로 v2를 유지한다.

## 0.4.2 - 2026-07-26

### 수정

- 30초 이상 영상의 끝 특징이 반복 음악의 앞선 구간과 잘못 매칭되어 외부 음원에 과도한
  `tempo_ratio`를 적용하던 문제 수정
- 앞·뒤 정렬 기준점의 신뢰도, 중간 기준점 일관성, 0.99~1.01 clock drift 안전 범위를
  모두 통과한 경우에만 속도 보정 적용
- 실사례형 반복 끝 구간을 실제 FFmpeg 입력으로 재현하는 합성 E2E 회귀 추가

### 호환성

- CLI와 JSON v2 필드 계약은 변경되지 않는다.
- 안전하지 않은 drift 추정은 coarse offset과 `tempo_ratio=1.0`을 사용하므로 이전
  버전에서 눈에 띄게 빠르거나 느려진 출력은 다시 분석·렌더해야 한다.

## 0.4.1 - 2026-07-18

### 수정

- 이름이 `.md`로 끝나는 디렉터리를 Markdown 문서로 오인하지 않도록 링크 검사기를 보완
- 재사용 분석 리포트를 배포 wheel의 v2 JSON Schema로 검증해 unknown 필드, 범위 밖
  수치와 유한하지 않은 JSON 수치를 입력 지문 검사 전에 거부

### 호환성

- JSON 리포트 계약은 v2를 유지하며, 기존 계약을 벗어난 입력의 검증만 엄격해진다.

## 0.4.0 - 2026-07-18

0.4.0은 첫 태그 기반 공개 릴리스다. 이전 개발 버전의 상세 이력은 Git 기록에서 확인한다.

### 추가

- `analyze --report` 결과를 `process --analysis-report`에서 입력 지문 검증 후 재사용
- JSON 리포트 v2의 Draft 2020-12 Schema와 배포 wheel 내 스키마 리소스
- 저장소 내부 Markdown 링크 검사와 CI 품질 게이트
- 태그·패키지 버전 검증 후 wheel과 sdist를 게시하는 GitHub Release 자동화

### 변경

- 공개 Python API가 영상별 probe·특징 추출·매칭·I/O 오류를 격리해 나머지 영상을 계속 분석
- 분석 리포트나 원본 입력에 접근할 수 없으면 일관된 입력 오류로 보고
- dry-run과 실제 process가 같은 렌더 대상 판정 정책을 사용
- 테스트 현황 숫자는 문서에 고정하지 않고 로컬 검사와 CI 결과를 정본으로 사용

### 호환성

- Python 3.14 이상과 FFmpeg/ffprobe가 필요하다.
- JSON 리포트 계약은 v2를 유지한다.
- PyPI에는 게시하지 않으며 Git 태그 또는 GitHub Release 산출물로 배포한다.
