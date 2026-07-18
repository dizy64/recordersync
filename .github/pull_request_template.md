## 문제와 범위

- 해결하는 문제:
- 범위에 포함하지 않은 항목:
- 관련 이슈:

## Plan / RED / GREEN / Refactor

- Plan과 실패 조건:
- RED 재현 테스트:
- 최소 구현과 주요 결정:
- 리팩터링 내용:

## 검증

- [ ] `bash scripts/check.sh`
- [ ] `bash scripts/test-e2e.sh` 또는 미실행 사유 기록
- [ ] 성능 경로 변경 시 benchmark 전후 p95/p99/RSS 기록
- [ ] 원본 미디어와 기존 출력 보존 확인

결과:

```text
commands and results
```

## 보안·데이터 안전

- [ ] 비밀정보, 실제 사용자 미디어, 실제 JSON 리포트 없음
- [ ] 원본 파일을 수정·삭제·이동하지 않음
- [ ] ambiguous/unmatched/error 출력 거부 정책 유지
- [ ] overwrite와 임시 파일 원자성 유지
- [ ] subprocess `shell=False`와 경로 escaping 유지

## 호환성과 문서

- [ ] CLI/API/JSON 변경 시 관련 문서 갱신
- [ ] REPORT_VERSION 변경 필요 여부 검토
- [ ] TubeArchive 연동 영향 검토
- [ ] `CLAUDE.md` 또는 `docs/project/handoff.md` 갱신 필요 여부 검토

## 남은 위험

-
