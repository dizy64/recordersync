# 변경 이력

RecorderSync의 사용자 동작과 공개 연동 계약 변경을 버전별로 기록한다. GitHub Release의
자동 생성 노트는 커밋·PR 탐색용이며, 이 문서를 제품 변경의 요약 정본으로 사용한다.

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
