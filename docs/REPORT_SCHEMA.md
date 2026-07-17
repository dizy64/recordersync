# JSON 리포트 계약

## 버전 정책

현재 `version`은 `1`이다. 필드 추가처럼 기존 소비자가 무시할 수 있는 변경은 같은
버전에서 가능하지만, 이름·타입·상태 의미·null 가능성 변경은 REPORT_VERSION 증가와
호환성 테스트가 필요하다.

`analyze`는 JSON을 stdout으로 출력하고 `--report`가 있으면 파일에도 쓴다. `process`는
별도 경로가 없으면 output dir의 `recordersync-report.json`에 쓴다.

## 최상위 필드

| 필드 | 타입 | 의미 |
|---|---|---|
| `version` | integer | 스키마 버전 |
| `created_at` | ISO 8601 string | UTC 리포트 생성 시각 |
| `summary` | object | 상태별 영상 개수 |
| `audio_sessions` | array | 자동 구성된 녹음 세션 |
| `matches` | array | 입력 영상 순서의 매칭·렌더 결과 |

## audio_sessions

| 필드 | 타입 | 의미 |
|---|---|---|
| `id` | string | 실행 내 결정적 세션 ID, 예: `session-001` |
| `duration_seconds` | number | 조각 duration 합계 |
| `chunks` | string[] | 세션에 포함된 오디오 경로 순서 |

세션 ID는 실행 범위 안에서만 사용한다. 파일 추가·정렬 변경 후 영구 식별자로 저장하지
않는다.

## matches

| 필드 | 타입/null | 의미 |
|---|---|---|
| `video` | string | 입력 영상 경로 |
| `status` | string | `matched`, `unmatched`, `ambiguous`, `error` |
| `session_id` | string/null | 승인된 녹음 세션 |
| `external_start_seconds` | number/null | 논리 세션 안 외부 구간 시작점 |
| `duration_seconds` | number | 영상 duration |
| `tempo_ratio` | number | FFmpeg `atempo`에 전달할 drift 비율 |
| `correlation` | number | 최고 NCC peak |
| `peak_margin` | number | 최고와 차순위 peak 차이 |
| `confidence` | number | 상관도·유일성을 결합한 0~1 점수 |
| `reason` | string/null | 승인 거부나 오류 설명 |
| `output` | string/null | 성공한 결과 또는 dry-run 예상 경로 |

`matched` 분석 결과는 `output`이 null일 수 있다. `process --dry-run`은 렌더하지 않아도
예상 output을 넣는다. `error`의 duration은 probe 단계 실패 시 0일 수 있다.

## 공개 가능한 합성 예시

아래 경로와 이름은 예시이며 실제 사용자 정보를 포함하지 않는다.

```json
{
  "version": 1,
  "created_at": "2026-07-17T00:00:00+00:00",
  "summary": {
    "total": 2,
    "matched": 1,
    "unmatched": 0,
    "ambiguous": 1,
    "error": 0
  },
  "audio_sessions": [
    {
      "id": "session-001",
      "duration_seconds": 7200.0,
      "chunks": ["/example/audio/REC_001.wav", "/example/audio/REC_002.wav"]
    }
  ],
  "matches": [
    {
      "video": "/example/video/clip-001.mov",
      "status": "matched",
      "session_id": "session-001",
      "external_start_seconds": 123.45,
      "duration_seconds": 60.0,
      "tempo_ratio": 1.0001,
      "correlation": 0.91,
      "peak_margin": 0.22,
      "confidence": 0.94,
      "reason": null,
      "output": "/example/video/replace/clip-001_replaced.mp4"
    },
    {
      "video": "/example/video/clip-002.mov",
      "status": "ambiguous",
      "session_id": null,
      "external_start_seconds": null,
      "duration_seconds": 45.0,
      "tempo_ratio": 1.0,
      "correlation": 0.88,
      "peak_margin": 0.01,
      "confidence": 0.71,
      "reason": "Best match is not sufficiently distinct from the runner-up",
      "output": null
    }
  ]
}
```

## 개인정보 주의

실제 리포트의 절대 경로와 파일명은 사용자 이름, 장소, 행사명 등 개인정보를 드러낼
수 있다. 비밀키가 없더라도 공개 저장소·이슈·로그 수집 시스템에 원본 리포트를 올리지
않는다. 공유가 필요하면 경로와 파일명을 합성 값으로 치환한다.
