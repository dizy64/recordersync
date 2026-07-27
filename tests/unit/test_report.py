"""분석·처리 결과 JSON 계약."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recordersync.audio_levels import (
    AudioLevelMetrics,
    AudioLevelPolicy,
    AudioLevelReport,
    OutputChannelLayout,
    decide_static_gain,
)
from recordersync.models import (
    AudioChunk,
    AudioMatch,
    AudioMatchSegment,
    MatchStatus,
    RecordingSession,
)
from recordersync.report import MatchReport, ReportLanguage, format_audio_level_summary


def test_매칭_리포트는_세션과_매칭과_요약을_직렬화한다() -> None:
    session = RecordingSession(
        "session-001",
        (AudioChunk(Path("a.wav"), 60, 48_000, 2, "pcm_f32le", None),),
    )
    matches = (
        AudioMatch(
            Path("a.mov"),
            10,
            MatchStatus.MATCHED,
            session_id="session-001",
            external_start_seconds=2.5,
            confidence=0.9,
            output_path=Path("replace/a.mp4"),
        ),
        AudioMatch(
            Path("b.mov"),
            8,
            MatchStatus.AMBIGUOUS,
            reason="Best match is not sufficiently distinct from the runner-up",
        ),
    )
    report = MatchReport(
        sessions=(session,),
        matches=matches,
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = json.loads(report.to_json())

    assert payload["version"] == 2
    assert payload["language"] == "ko"
    assert payload["summary"] == {
        "total": 2,
        "matched": 1,
        "partial": 0,
        "unmatched": 0,
        "ambiguous": 1,
        "error": 0,
    }
    assert payload["audio_sessions"][0]["chunks"] == ["a.wav"]
    assert payload["matches"][0]["external_start_seconds"] == 2.5
    assert payload["matches"][0]["recommended_mode"] == "replace"
    assert payload["matches"][0]["recommendation_reason"] == "카메라 오디오 전체가 외부 녹음과 일치합니다."
    assert payload["matches"][0]["recommended_options"] == {}
    assert payload["matches"][1]["reason"] == "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다."
    assert payload["matches"][1]["recommended_mode"] is None
    assert "audio_levels" not in payload["matches"][0]


def test_처리_리포트는_음량_측정_gain_결정과_최종_AAC_검증을_직렬화한다() -> None:
    match = AudioMatch(
        Path("clip.mov"),
        30,
        MatchStatus.MATCHED,
        session_id="session-001",
        external_start_seconds=2.5,
        output_path=Path("replace/clip.mp4"),
    )
    policy = AudioLevelPolicy(
        target_lufs=-16.0,
        maximum_true_peak_dbtp=-1.0,
        output_channel_layout=OutputChannelLayout.STEREO,
        loudness_tolerance_lu=0.5,
    )
    input_metrics = AudioLevelMetrics(2, 48_000, -20.0, 7.0, -8.1, -8.0, 30.0, "pcm_f32le")
    output_metrics = AudioLevelMetrics(2, 48_000, -16.2, 7.0, -1.2, -1.1, 30.0, "aac")
    audio_levels = AudioLevelReport(
        policy=policy,
        input_metrics=input_metrics,
        decision=decide_static_gain(input_metrics, policy),
        output_metrics=output_metrics,
    )
    report = MatchReport(
        sessions=(),
        matches=(match,),
        audio_levels=(audio_levels,),
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
    )

    payload = report.to_dict()

    assert payload["matches"][0]["audio_levels"] == {
        "policy": {
            "target_lufs": -16.0,
            "maximum_true_peak_dbtp": -1.0,
            "output_channel_layout": "stereo",
            "loudness_tolerance_lu": 0.5,
            "dynamics": "none",
        },
        "input": {
            "channels": 2,
            "sample_rate": 48_000,
            "integrated_loudness_lufs": -20.0,
            "loudness_range_lu": 7.0,
            "sample_peak_dbfs": -8.1,
            "true_peak_dbtp": -8.0,
            "duration_seconds": 30.0,
            "codec": "pcm_f32le",
            "decoder_error": None,
        },
        "decision": {
            "requested_gain_db": 4.0,
            "maximum_safe_gain_db": 7.0,
            "applied_gain_db": 4.0,
            "expected_true_peak_dbtp": -4.0,
            "conflict_db": 0.0,
            "limiter_free_lufs": -13.0,
        },
        "output": {
            "channels": 2,
            "sample_rate": 48_000,
            "integrated_loudness_lufs": -16.2,
            "loudness_range_lu": 7.0,
            "sample_peak_dbfs": -1.2,
            "true_peak_dbtp": -1.1,
            "duration_seconds": 30.0,
            "codec": "aac",
            "decoder_error": None,
        },
        "validation": {"passed": True, "failures": []},
    }
    assert format_audio_level_summary(match, audio_levels) == (
        "clip.mov | 음량 검증: 통과 | 입력: -20.0 LUFS / -8.0 dBTP | gain: +4.0 dB | 출력: -16.2 LUFS / -1.1 dBTP"
    )


def test_사람용_음량_요약은_peak_충돌의_필수_판정값을_표시한다() -> None:
    match = AudioMatch(Path("clip.mov"), 30, MatchStatus.ERROR)
    policy = AudioLevelPolicy(-7.3, -1.0, OutputChannelLayout.MONO, 0.5)
    input_metrics = AudioLevelMetrics(1, 48_000, -11.1, 10.6, 7.73, 7.7, 30.0, "aac")
    audio_levels = AudioLevelReport(
        policy=policy,
        input_metrics=input_metrics,
        decision=decide_static_gain(input_metrics, policy),
        validation_failures=("loudness target conflicts with true-peak ceiling",),
    )

    assert format_audio_level_summary(match, audio_levels) == (
        "clip.mov | 음량 검증: 실패 | 입력: -11.1 LUFS / 7.7 dBTP | "
        "목표 gain: +3.8 dB | 안전 gain: -8.7 dB | 초과: 12.5 dB | "
        "limiter 없이 가능한 음량: -19.8 LUFS | 출력: 없음"
    )


def test_사람용_음량_요약은_입력_분석_실패를_표시한다() -> None:
    match = AudioMatch(Path("clip.mov"), 30, MatchStatus.ERROR)
    policy = AudioLevelPolicy(-16.0, -1.0, OutputChannelLayout.MONO, 0.5)
    audio_levels = AudioLevelReport(
        policy=policy,
        validation_failures=("input analysis error: invalid frame",),
    )

    assert format_audio_level_summary(match, audio_levels) == (
        "clip.mov | 음량 검증: 실패 | 입력: 측정 실패 | 출력: 없음 | 실패: input analysis error: invalid frame"
    )


@pytest.mark.parametrize(
    ("reason", "translated"),
    [
        (
            "Loudness target conflicts with true-peak ceiling",
            "목표 음량과 true peak 제한이 충돌합니다.",
        ),
        ("Input audio analysis failed", "입력 오디오 음량 분석에 실패했습니다."),
        ("Final AAC validation failed", "최종 AAC 음량 검증에 실패했습니다."),
    ],
)
def test_음량_안전_오류_사유는_한국어로_직렬화한다(
    reason: str,
    translated: str,
) -> None:
    report = MatchReport(
        sessions=(),
        matches=(AudioMatch(Path("clip.mov"), 30, MatchStatus.ERROR, reason=reason),),
    )

    assert report.to_dict()["matches"][0]["reason"] == translated


def test_매칭_리포트는_부분_구간과_레코더_사용률을_표시한다() -> None:
    match = AudioMatch(
        Path("partial.mov"),
        10,
        MatchStatus.PARTIAL,
        confidence=0.85,
        peak_margin=0.1,
        reason="Only part of the camera audio matched the external recording",
        segments=(
            AudioMatchSegment("session-001", 1, 3, 3, confidence=0.9),
            AudioMatchSegment("session-002", 7, 4, 2, confidence=0.8),
        ),
    )
    report = MatchReport(
        sessions=(),
        matches=(match,),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = report.to_dict()

    assert payload["summary"] == {
        "total": 1,
        "matched": 0,
        "partial": 1,
        "unmatched": 0,
        "ambiguous": 0,
        "error": 0,
    }
    assert payload["matches"][0]["coverage_ratio"] == 0.5
    assert payload["matches"][0]["recommended_mode"] == "fallback"
    assert payload["matches"][0]["recommended_options"] == {"min_partial_seconds": 2.5}
    assert payload["matches"][0]["segments"][1] == {
        "session_id": "session-002",
        "video_start_seconds": 7,
        "external_start_seconds": 4,
        "duration_seconds": 2,
        "tempo_ratio": 1.0,
        "correlation": 0.0,
        "peak_margin": 0.0,
        "confidence": 0.8,
    }
    assert report.to_text().splitlines() == [
        "분석 결과: 0/1개 전체 매칭, 1개 부분 매칭",
        (
            "- partial.mov | 매칭 여부: 부분 | 매칭률: 85.0% | 레코더 사용: 50.0% | "
            "구간: 2개 | 추천: fallback | 추천 사유: 충분히 길고 넓은 부분 매칭이 "
            "확인되었습니다."
        ),
    ]


def test_매칭_리포트는_영문_사유를_렌더링할_수_있다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("clip.mov"),
                8,
                MatchStatus.UNMATCHED,
                reason="Match confidence is below the configured threshold",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = json.loads(report.to_json(language=ReportLanguage.EN))

    assert payload["language"] == "en"
    assert payload["matches"][0]["reason"] == "Match confidence is below the configured threshold"
    assert payload["matches"][0]["recommendation_reason"] == "No reliable matching segment is available."


def test_부분_매칭의_영문_목록은_한국어_단위를_포함하지_않는다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("partial.mov"),
                10,
                MatchStatus.PARTIAL,
                confidence=0.8,
                segments=(AudioMatchSegment("session-001", 2, 4, 5, confidence=0.8),),
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    rendered = report.to_text(language=ReportLanguage.EN)

    assert "segments: 1" in rendered
    assert "recommendation: hold" in rendered
    assert "recommendation reason: The partial match is not sufficiently distinct" in rendered
    assert "개" not in rendered


def test_매칭_리포트는_알려진_접두사를_번역하고_알_수_없는_사유는_보존한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("existing.mov"),
                8,
                MatchStatus.ERROR,
                reason="Output already exists: result.mp4",
            ),
            AudioMatch(
                Path("unknown.mov"),
                8,
                MatchStatus.ERROR,
                reason="codec-specific diagnostic",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = report.to_dict()

    assert payload["matches"][0]["reason"] == "출력 파일이 이미 존재합니다: result.mp4"
    assert payload["matches"][1]["reason"] == "codec-specific diagnostic"


def test_매칭_리포트는_렌더_계획_검증_사유를_번역한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("wrong-session.mov"),
                8,
                MatchStatus.ERROR,
                reason="Session mapping keys must match RecordingSession.id",
            ),
            AudioMatch(
                Path("wrong-video.mov"),
                8,
                MatchStatus.ERROR,
                reason="Match video path does not match supplied video",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    payload = report.to_dict()

    assert [match["reason"] for match in payload["matches"]] == [
        "세션 인덱스 키는 RecordingSession.id와 일치해야 합니다.",
        "매칭 영상 경로가 제공된 영상과 일치하지 않습니다.",
    ]


def test_매칭_리포트는_간결한_한국어_사람용_요약을_렌더링한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(Path("matched.mov"), 8, MatchStatus.MATCHED, confidence=0.9),
            AudioMatch(
                Path("unmatched.mov"),
                8,
                MatchStatus.UNMATCHED,
                confidence=0.42,
                reason="Match confidence is below the configured threshold",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    rendered = report.to_text()

    assert rendered.splitlines() == [
        "분석 결과: 1/2개 매칭 (50.0%)",
        "- matched.mov | 매칭 여부: 성공 | 매칭률: 90.0% | 추천: replace",
        (
            "- unmatched.mov | 매칭 여부: 실패 | 매칭률: 42.0% | "
            "사유: 매칭 신뢰도가 설정된 기준보다 낮습니다. | 추천: 처리 보류"
        ),
    ]
    assert "session_id" not in rendered
    assert "correlation" not in rendered


def test_매칭_리포트는_나눗셈_오류_없이_빈_사람용_요약을_렌더링한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text() == "분석 결과: 0/0개 매칭 (0.0%)"


def test_매칭_리포트는_실패한_사람용_매칭을_항상_설명한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(AudioMatch(Path("failed.mov"), 8, MatchStatus.ERROR),),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert "사유: 사유를 확인할 수 없습니다." in report.to_text()
    assert report.to_text().endswith("추천: 처리 보류")


def test_매칭_리포트는_영문_사람용_요약을_렌더링한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(
            AudioMatch(
                Path("clip.mov"),
                8,
                MatchStatus.UNMATCHED,
                confidence=0.3,
                reason="Match confidence is below the configured threshold",
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert report.to_text(language=ReportLanguage.EN).splitlines() == [
        "Analysis result: 0/1 matched (0.0%)",
        (
            "- clip.mov | matched: no | match confidence: 30.0% | "
            "reason: Match confidence is below the configured threshold | recommendation: hold"
        ),
    ]


def test_매칭_리포트는_추천_명령을_JSON과_셸_형식으로_렌더링한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(AudioMatch(Path("clip.mov"), 8, MatchStatus.MATCHED),),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
        recommended_command=("recordersync", "process", "/video dir"),
        include_recommended_command=True,
    )

    assert report.to_dict()["recommended_command"] == [
        "recordersync",
        "process",
        "/video dir",
    ]
    assert report.to_text().endswith("추천 실행:\n  recordersync process '/video dir'")


def test_매칭_리포트는_추천할_명령이_없음을_표시한다() -> None:
    report = MatchReport(
        sessions=(),
        matches=(AudioMatch(Path("clip.mov"), 8, MatchStatus.UNMATCHED),),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
        include_recommended_command=True,
    )

    assert report.to_dict()["recommended_command"] is None
    assert report.to_text().endswith("추천 실행 명령 없음: 신뢰할 수 있는 매칭이 없습니다.")
