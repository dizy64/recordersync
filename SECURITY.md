# 보안과 개인정보 정책

## 지원 범위

현재 보안 수정은 최신 `main`과 최신 공개 release를 대상으로 한다. 과거 개발 버전과
이전 minor 계열은 별도 합의가 없으면 지원하지 않는다.

## 취약점 보고

민감한 보안 문제는 공개 이슈에 exploit, 실제 경로, 미디어, 토큰을 첨부하지 않는다.
GitHub 저장소의 private vulnerability reporting을 우선 사용한다. 재현에는 합성 파일과
삭제 가능한 임시 경로를 사용한다.

일반 correctness 버그는 공개 이슈로 보고할 수 있지만 실제 사용자 리포트와 미디어는
익명화한다.

## 비밀정보

RecorderSync는 API key, OAuth token, 비밀번호를 요구하지 않는다. 다음 항목을 저장소에
추가하지 않는다.

- `.env`, access token, SSH private key, credential file
- 실제 사용자 홈 경로가 담긴 로그와 JSON 리포트
- 원본 영상·오디오·썸네일·파형
- 클라우드 업로드 URL 또는 공유 링크

새 외부 서비스 연동이 필요해지면 환경변수 또는 운영체제 secret store를 사용하고,
테스트에서는 명백한 가짜 값만 사용한다. E2E 미디어는 고정 seed의 합성 noise와 단색
영상으로 임시 디렉터리에 만들며 네트워크나 사용자 파일을 참조하지 않는다.

## 미디어 개인정보

오디오와 영상에는 음성, 얼굴, 위치, 행사명, 생성 시각 등 개인정보가 포함될 수 있다.
RecorderSync는 로컬 처리만 수행하며 네트워크로 미디어를 전송하지 않는다.

JSON 리포트에도 절대 경로와 파일명이 포함된다. 이는 비밀키는 아니지만 사용자 이름,
장소, 날짜를 노출할 수 있다. 공개 공유 전 다음을 치환한다.

- `/Users/<name>` 같은 홈 경로
- 행사·장소·사람 이름이 포함된 디렉터리와 파일명
- 내부 볼륨 이름과 프로젝트 이름

## subprocess와 입력 경계

- FFmpeg/ffprobe는 `shell=False` 인자 배열로 호출한다.
- 사용자 경로를 쉘 명령 문자열로 보간하지 않는다.
- concat 매니페스트의 작은따옴표를 escape하고 절대 경로를 사용한다.
- 지원 확장자와 첫 오디오/영상 stream을 명시적으로 검사한다.
- 외부 프로세스 timeout을 유지하고 stderr만 필요한 범위로 노출한다.
- 환경변수 전체나 subprocess command에 포함될 수 있는 민감값을 로그로 남기지 않는다.

## 데이터 안전

- 원본은 읽기 전용이며 수정·삭제·이동하지 않는다.
- 최종 출력은 임시 파일 성공 후 원자적으로 publish한다.
- `--overwrite` 없이는 기존 결과를 보존한다.
- 출력 파일명 affix의 경로 구분자를 거부하고 원본과 동일한 출력 경로는 항상 거부한다.
- 실패한 하드웨어 렌더의 임시 출력은 폴백 전에 제거한다.
- 자동 매칭이 애매하면 안전하게 출력하지 않는다.
- 출력 디렉터리와 리포트의 백업·삭제 책임은 사용자에게 있다.

원본을 파괴할 수 있는 기능 요청은 별도 승인, dry-run, 복구 전략, 테스트가 없으면
구현하지 않는다.

## 의존성

```bash
uv run pip-audit
uv lock --check
```

NumPy, SciPy, FFmpeg 및 build/dev 도구의 업데이트는 Python 3.14 호환성, 라이선스,
lock diff, 단위 테스트, 성능 벤치를 함께 검토한다. 보안 경고를 단순 ignore하지 않고
직접 영향과 임시 완화책을 기록한다.

## 공개 전 검사

```bash
git status --short
git ls-files
git grep -nEi \
  '(api[_-]?key|client[_-]?secret|password|private[_-]?key|BEGIN .* PRIVATE KEY|github_pat_|ghp_|sk-)'
git ls-files | rg -i '(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|id_rsa|id_ed25519)'
```

정규식 검사는 보조 수단이다. diff를 사람이 읽고 합성 fixture만 포함되었는지 확인한다.
