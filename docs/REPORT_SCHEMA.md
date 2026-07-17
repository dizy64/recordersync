# JSON 리포트 계약

## 버전 정책

현재 `version`은 `2`다. v2는 `partial` 상태와 다중 `segments`,
`coverage_ratio`를 추가한다. 필드 추가처럼 기존 소비자가 무시할 수 있는 변경은 같은
버전에서 가능하지만, 이름·타입·상태 의미·null 가능성 변경은 REPORT_VERSION 증가와
호환성 테스트가 필요하다.

JSON 키와 `status` 값은 번역하지 않는다. `reason`만 `language`에 따라 한국어 또는
영어로 직렬화한다. CLI 기본값은 `ko`이며 `--report-language en`으로 바꿀 수 있다.
소비자는 표시 문구인 `reason`으로 분기하지 말고 `status`와 수치 필드를 사용한다.

`analyze`의 기본 stdout은 사람용 목록이다. 기존 JSON 계약이 필요한 소비자는
`analyze --json`을 사용한다. `--report`가 있으면 화면 형식과 무관하게 JSON 파일을 쓰며,
`process`는 stdout에 JSON을 출력하고 별도 경로가 없으면 output dir의
`recordersync-report.json`에도 쓴다.

사람용 목록은 안정적인 API 스키마가 아니다. 자동화는 표시 문구를 파싱하지 말고 반드시
`--json` 또는 `--report` 파일과 아래 버전 계약을 사용한다.

## 최상위 필드

| 필드 | 타입 | 의미 |
|---|---|---|
| `version` | integer | 스키마 버전 |
| `language` | string | `reason` 언어: `ko` 또는 `en` |
| `created_at` | ISO 8601 string | UTC 리포트 생성 시각 |
| `summary` | object | `total`, `matched`, `partial`, `unmatched`, `ambiguous`, `error` 개수 |
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
| `status` | string | `matched`, `partial`, `unmatched`, `ambiguous`, `error` |
| `session_id` | string/null | 전체 일치 또는 모든 부분 구간이 같은 녹음 세션. 다중 세션이면 null |
| `external_start_seconds` | number/null | 전체 일치 시작점. 부분 일치는 첫 승인 구간 시작점 |
| `duration_seconds` | number | 영상 duration |
| `tempo_ratio` | number | FFmpeg `atempo`에 전달할 drift 비율 |
| `correlation` | number | 최고 NCC peak |
| `peak_margin` | number | 최고와 차순위 peak 차이 |
| `confidence` | number | 상관도·유일성을 결합한 0~1 점수 |
| `coverage_ratio` | number | 영상 길이 중 승인된 외부 녹음 구간의 0~1 비율 |
| `segments` | array | 승인된 구간. 영상 시간순이며 서로 겹치지 않음 |
| `reason` | string/null | 선택한 언어의 승인 거부·오류 설명 |
| `output` | string/null | 성공한 결과 또는 dry-run 예상 경로 |

`matched`와 `partial` 분석 결과는 `output`이 null일 수 있다. 부분 일치의 실제 위치와
구간별 drift는 최상위 호환 필드가 아니라 항상 `segments`를 사용한다.
`process --mode fallback --dry-run`은 렌더하지 않아도 부분
일치에 예상 output을 넣는다. `error`의 duration은 probe 단계 실패 시 0일 수 있다.
v1 소비자는 새 상태를 알 수 없으므로 v2를 명시적으로 지원해야 한다.

## segments

| 필드 | 타입 | 의미 |
|---|---|---|
| `session_id` | string | 해당 구간이 참조하는 녹음 세션 |
| `video_start_seconds` | number | 영상 시간축의 구간 시작점 |
| `external_start_seconds` | number | 논리 녹음 세션 시간축의 시작점 |
| `duration_seconds` | number | 영상 시간축에서 승인된 구간 길이 |
| `tempo_ratio` | number | 해당 구간의 FFmpeg `atempo` drift 비율 |
| `correlation` | number | 해당 구간 최고 NCC peak |
| `peak_margin` | number | 해당 구간 최고/차순위 peak 차이 |
| `confidence` | number | 해당 구간의 0~1 신뢰도 |

구간은 `video_start_seconds` 오름차순이며 겹치지 않는다. 인접 구간 사이와 영상 앞뒤의
빈 부분은 fallback 렌더에서 카메라음을 사용한다. `matched`는 전체 영상을 덮는 한 구간을
가질 수 있고, `unmatched`, `ambiguous`, `error`의 `segments`는 빈 배열이다.

## 공개 가능한 합성 예시

아래 경로와 이름은 예시이며 실제 사용자 정보를 포함하지 않는다.

```json
{
  "version": 2,
  "language": "ko",
  "created_at": "2026-07-17T00:00:00+00:00",
  "summary": {
    "total": 2,
    "matched": 0,
    "partial": 1,
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
      "status": "partial",
      "session_id": "session-001",
      "external_start_seconds": 123.45,
      "duration_seconds": 60.0,
      "tempo_ratio": 1.0,
      "correlation": 0.91,
      "peak_margin": 0.22,
      "confidence": 0.94,
      "coverage_ratio": 0.5,
      "segments": [
        {
          "session_id": "session-001",
          "video_start_seconds": 5.0,
          "external_start_seconds": 123.45,
          "duration_seconds": 15.0,
          "tempo_ratio": 1.0001,
          "correlation": 0.91,
          "peak_margin": 0.22,
          "confidence": 0.94
        },
        {
          "session_id": "session-001",
          "video_start_seconds": 35.0,
          "external_start_seconds": 153.45,
          "duration_seconds": 15.0,
          "tempo_ratio": 1.0001,
          "correlation": 0.90,
          "peak_margin": 0.20,
          "confidence": 0.92
        }
      ],
      "reason": "카메라 오디오의 일부만 외부 녹음과 일치합니다.",
      "output": "/example/video/replace/clip-001.mp4"
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
      "coverage_ratio": 0.0,
      "segments": [],
      "reason": "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다.",
      "output": null
    }
  ]
}
```

정의된 공통 사유와 오류 접두사는 번역한다. 코덱이나 FFmpeg가 반환한 알 수 없는 진단은
정보 손실을 피하기 위해 원문으로 남을 수 있다.

## 개인정보 주의

실제 리포트의 절대 경로와 파일명은 사용자 이름, 장소, 행사명 등 개인정보를 드러낼
수 있다. 비밀키가 없더라도 공개 저장소·이슈·로그 수집 시스템에 원본 리포트를 올리지
않는다. 공유가 필요하면 경로와 파일명을 합성 값으로 치환한다.
