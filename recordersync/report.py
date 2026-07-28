"""사람용 분석 목록과 자동화 가능한 JSON 리포트."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from recordersync.audio_levels import AudioLevelMetrics, AudioLevelPolicy, AudioLevelReport
from recordersync.mix_analysis import MixRecommendation, MixSourceMetrics
from recordersync.models import AudioMatch, MatchStatus, RecordingSession
from recordersync.recommendation import (
    ModeRecommendation,
    RecommendationReason,
    recommend_mode,
)

REPORT_VERSION = 2


class ReportLanguage(StrEnum):
    """사람이 읽는 리포트 사유의 지원 언어."""

    KO = "ko"
    EN = "en"


_KOREAN_REASONS = {
    "All recording sessions are shorter than the video feature": "모든 녹음 세션이 영상 오디오 특징보다 짧습니다.",
    "Match confidence is below the configured threshold": "매칭 신뢰도가 설정된 기준보다 낮습니다.",
    "Best match is not sufficiently distinct from the runner-up": (
        "최상위 후보와 차순위 후보의 차이가 충분하지 않습니다."
    ),
    "Camera audio is required for automatic matching": "자동 매칭에는 카메라 오디오가 필요합니다.",
    "Matched result is missing render metadata": "매칭 결과에 렌더 메타데이터가 없습니다.",
    "Match does not belong to the supplied recording session": "매칭이 제공된 녹음 세션에 속하지 않습니다.",
    "Match does not belong to the supplied recording sessions": "매칭이 제공된 녹음 세션들에 속하지 않습니다.",
    "Session mapping keys must match RecordingSession.id": ("세션 인덱스 키는 RecordingSession.id와 일치해야 합니다."),
    "Match video path does not match supplied video": "매칭 영상 경로가 제공된 영상과 일치하지 않습니다.",
    "Output path must not overwrite the source video": "출력 경로는 원본 영상을 덮어쓸 수 없습니다.",
    "FFmpeg reported success but produced no output file": (
        "FFmpeg가 성공을 보고했지만 출력 파일을 만들지 않았습니다."
    ),
    "mix mode requires camera audio": "mix 모드에는 카메라 오디오가 필요합니다.",
    "fallback mode requires camera audio": "fallback 모드에는 카메라 오디오가 필요합니다.",
    "Only part of the camera audio matched the external recording": "카메라 오디오의 일부만 외부 녹음과 일치합니다.",
    "Loudness target conflicts with true-peak ceiling": "목표 음량과 true peak 제한이 충돌합니다.",
    "Input audio analysis failed": "입력 오디오 음량 분석에 실패했습니다.",
    "Final AAC validation failed": "최종 AAC 음량 검증에 실패했습니다.",
    "Automatic mix analysis failed": "자동 mix 분석에 실패했습니다.",
}

_KOREAN_REASON_PREFIXES = {
    "Output already exists: ": "출력 파일이 이미 존재합니다: ",
    "No audio stream found: ": "오디오 스트림을 찾을 수 없습니다: ",
    "No video stream found: ": "비디오 스트림을 찾을 수 없습니다: ",
    "Timed out probing media: ": "미디어 정보를 읽는 중 시간 제한을 초과했습니다: ",
    "Failed to probe ": "미디어 정보를 읽지 못했습니다: ",
    "Invalid ffprobe JSON for ": "ffprobe JSON이 올바르지 않습니다: ",
    "Invalid ffprobe payload for ": "ffprobe 결과가 올바르지 않습니다: ",
    "Timed out extracting audio features: ": "오디오 특징을 추출하는 중 시간 제한을 초과했습니다: ",
    "Failed to decode audio from ": "오디오를 디코딩하지 못했습니다: ",
    "Decoded audio is empty: ": "디코딩한 오디오가 비어 있습니다: ",
    "FFmpeg render failed with VideoToolbox and libx265: ": (
        "VideoToolbox와 libx265 FFmpeg 렌더가 모두 실패했습니다: "
    ),
    "Invalid duration: ": "영상 길이가 올바르지 않습니다: ",
}

_RECOMMENDATION_REASONS = {
    ReportLanguage.KO: {
        RecommendationReason.FULL_MATCH: "카메라 오디오 전체가 외부 녹음과 일치합니다.",
        RecommendationReason.RELIABLE_PARTIAL: "충분히 길고 넓은 부분 매칭이 확인되었습니다.",
        RecommendationReason.LOW_CONFIDENCE: "부분 매칭 신뢰도가 안전 추천 기준보다 낮습니다.",
        RecommendationReason.LOW_PEAK_MARGIN: "부분 매칭 후보가 다른 후보와 충분히 구분되지 않습니다.",
        RecommendationReason.LOW_COVERAGE: "일치 구간이 영상의 10%보다 적어 오탐 가능성이 있습니다.",
        RecommendationReason.SHORT_SEGMENTS: "연속 일치 구간이 추천에 필요한 길이보다 짧습니다.",
        RecommendationReason.UNMATCHED: "신뢰할 수 있는 일치 구간이 없습니다.",
        RecommendationReason.AMBIGUOUS: "후보가 불분명해 자동 처리를 권장하지 않습니다.",
        RecommendationReason.ERROR: "분석 오류가 있어 처리를 권장하지 않습니다.",
    },
    ReportLanguage.EN: {
        RecommendationReason.FULL_MATCH: "The full camera audio matches the external recording.",
        RecommendationReason.RELIABLE_PARTIAL: "A sufficiently long and well-covered partial match is available.",
        RecommendationReason.LOW_CONFIDENCE: "Partial-match confidence is below the safe recommendation threshold.",
        RecommendationReason.LOW_PEAK_MARGIN: "The partial match is not sufficiently distinct from other candidates.",
        RecommendationReason.LOW_COVERAGE: (
            "Matched segments cover less than 10% of the video and may be false positives."
        ),
        RecommendationReason.SHORT_SEGMENTS: (
            "Contiguous matched segments are too short for an automatic recommendation."
        ),
        RecommendationReason.UNMATCHED: "No reliable matching segment is available.",
        RecommendationReason.AMBIGUOUS: "The candidates are ambiguous, so automatic processing is not recommended.",
        RecommendationReason.ERROR: "An analysis error prevents an automatic processing recommendation.",
    },
}


def _translate_reason(reason: str | None, language: ReportLanguage) -> str | None:
    if reason is None or language is ReportLanguage.EN:
        return reason
    translated = _KOREAN_REASONS.get(reason)
    if translated is not None:
        return translated
    for prefix, translated_prefix in _KOREAN_REASON_PREFIXES.items():
        if reason.startswith(prefix):
            return f"{translated_prefix}{reason.removeprefix(prefix)}"
    return reason


def _recommendation_reason(
    recommendation: ModeRecommendation,
    language: ReportLanguage,
) -> str:
    return _RECOMMENDATION_REASONS[language][recommendation.reason]


def _recommendation_options(recommendation: ModeRecommendation) -> dict[str, float]:
    if recommendation.minimum_contiguous_seconds is None:
        return {}
    return {"min_partial_seconds": recommendation.minimum_contiguous_seconds}


def _audio_metrics_payload(metrics: AudioLevelMetrics) -> dict[str, object]:
    return {
        "channels": metrics.channels,
        "sample_rate": metrics.sample_rate,
        "integrated_loudness_lufs": metrics.integrated_loudness_lufs,
        "loudness_range_lu": metrics.loudness_range_lu,
        "sample_peak_dbfs": metrics.sample_peak_dbfs,
        "true_peak_dbtp": metrics.true_peak_dbtp,
        "duration_seconds": metrics.duration_seconds,
        "codec": metrics.codec,
        "decoder_error": metrics.decoder_error,
    }


def _audio_level_policy_payload(policy: AudioLevelPolicy) -> dict[str, object]:
    return {
        "target_lufs": policy.target_lufs,
        "maximum_true_peak_dbtp": policy.maximum_true_peak_dbtp,
        "output_channel_layout": policy.output_channel_layout.value,
        "loudness_tolerance_lu": policy.loudness_tolerance_lu,
        "dynamics": "none",
    }


def _audio_level_payload(report: AudioLevelReport) -> dict[str, object]:
    decision = report.decision
    return {
        "policy": _audio_level_policy_payload(report.policy),
        "input": _audio_metrics_payload(report.input_metrics) if report.input_metrics is not None else None,
        "decision": (
            {
                "requested_gain_db": decision.requested_gain_db,
                "maximum_safe_gain_db": decision.maximum_safe_gain_db,
                "applied_gain_db": decision.applied_gain_db,
                "expected_true_peak_dbtp": decision.expected_true_peak_dbtp,
                "conflict_db": decision.conflict_db,
                "limiter_free_lufs": decision.limiter_free_lufs,
            }
            if decision is not None
            else None
        ),
        "output": _audio_metrics_payload(report.output_metrics) if report.output_metrics is not None else None,
        "validation": {
            "passed": report.passed,
            "failures": list(report.validation_failures),
        },
    }


def _mix_source_payload(source: MixSourceMetrics) -> dict[str, object]:
    return {
        "audio": _audio_metrics_payload(source.audio_levels),
        "low_frequency_energy_ratio": source.low_frequency_energy_ratio,
        "spectral_centroid_hz": source.spectral_centroid_hz,
        "stereo_correlation": source.stereo_correlation,
        "stereo_side_to_mid_db": source.stereo_side_to_mid_db,
    }


def _mix_recommendation_payload(recommendation: MixRecommendation) -> dict[str, object]:
    policy = recommendation.policy
    status = (
        "application_error"
        if policy is not None and recommendation.failures
        else ("error" if recommendation.failures else ("applied" if recommendation.applied else "recommended"))
    )
    return {
        "status": status,
        "camera": _mix_source_payload(recommendation.camera) if recommendation.camera is not None else None,
        "external": _mix_source_payload(recommendation.external) if recommendation.external is not None else None,
        "policy": (
            {
                "camera_audio_volume": policy.camera_audio_volume,
                "external_audio_volume": policy.external_audio_volume,
                "external_gain_db": recommendation.external_gain_db,
                "external_highpass_hz": policy.external_highpass_hz,
                "audio_level_policy": _audio_level_policy_payload(policy.audio_level_policy),
            }
            if policy is not None
            else None
        ),
        "reasons": list(recommendation.reasons),
        "failures": list(recommendation.failures),
    }


def _match_payload(
    match: AudioMatch,
    language: ReportLanguage,
    audio_levels: AudioLevelReport | None = None,
    mix_recommendation: MixRecommendation | None = None,
) -> dict[str, object]:
    recommendation = recommend_mode(match)
    payload: dict[str, object] = {
        "video": str(match.video_path),
        "status": match.status.value,
        "session_id": match.session_id,
        "external_start_seconds": match.external_start_seconds,
        "duration_seconds": match.duration_seconds,
        "tempo_ratio": match.tempo_ratio,
        "correlation": match.correlation,
        "peak_margin": match.peak_margin,
        "confidence": match.confidence,
        "coverage_ratio": match.coverage_ratio,
        "segments": [
            {
                "session_id": segment.session_id,
                "video_start_seconds": segment.video_start_seconds,
                "external_start_seconds": segment.external_start_seconds,
                "duration_seconds": segment.duration_seconds,
                "tempo_ratio": segment.tempo_ratio,
                "correlation": segment.correlation,
                "peak_margin": segment.peak_margin,
                "confidence": segment.confidence,
            }
            for segment in match.segments
        ],
        "reason": _translate_reason(match.reason, language),
        "output": str(match.output_path) if match.output_path else None,
        "recommended_mode": recommendation.mode.value if recommendation.mode else None,
        "recommendation_reason": _recommendation_reason(recommendation, language),
        "recommended_options": _recommendation_options(recommendation),
    }
    if audio_levels is not None:
        payload["audio_levels"] = _audio_level_payload(audio_levels)
    if mix_recommendation is not None:
        payload["mix_recommendation"] = _mix_recommendation_payload(mix_recommendation)
    return payload


def format_audio_level_summary(
    match: AudioMatch,
    report: AudioLevelReport,
) -> str:
    """CLI stderr에 표시할 영상별 음량 검증 한 줄 요약."""

    input_metrics = report.input_metrics
    decision = report.decision
    output_metrics = report.output_metrics
    status = "통과" if report.passed else "실패"
    output = (
        f"{output_metrics.integrated_loudness_lufs:.1f} LUFS / {output_metrics.true_peak_dbtp:.1f} dBTP"
        if output_metrics is not None
        else "없음"
    )
    if input_metrics is None or decision is None:
        summary = f"{match.video_path.name} | 음량 검증: {status} | 입력: 측정 실패 | 출력: {output}"
        if report.validation_failures:
            summary += f" | 실패: {'; '.join(report.validation_failures)}"
        return summary
    prefix = (
        f"{match.video_path.name} | 음량 검증: {status} | "
        f"입력: {input_metrics.integrated_loudness_lufs:.1f} LUFS / "
        f"{input_metrics.true_peak_dbtp:.1f} dBTP"
    )
    applied_gain = decision.applied_gain_db
    if applied_gain is None:
        return (
            f"{prefix} | 목표 gain: {decision.requested_gain_db:+.1f} dB | "
            f"안전 gain: {decision.maximum_safe_gain_db:+.1f} dB | "
            f"초과: {decision.conflict_db:.1f} dB | "
            f"limiter 없이 가능한 음량: {decision.limiter_free_lufs:.1f} LUFS | "
            f"출력: {output}"
        )
    summary = f"{prefix} | gain: {applied_gain:+.1f} dB | 출력: {output}"
    if report.validation_failures:
        summary += f" | 실패: {'; '.join(report.validation_failures)}"
    return summary


def format_mix_recommendation_summary(
    match: AudioMatch,
    recommendation: MixRecommendation,
) -> str:
    """CLI stderr에 표시할 자동 mix 추천 한 줄 요약."""

    if recommendation.policy is not None and recommendation.failures:
        return f"{match.video_path.name} | 상태: 적용 실패 | {'; '.join(recommendation.failures)}"
    if not recommendation.passed or recommendation.policy is None:
        failures = "; ".join(recommendation.failures) or "unknown analysis failure"
        return f"{match.video_path.name} | 상태: 실패 | {failures}"
    status = "적용" if recommendation.applied else "추천"
    gain = cast(float, recommendation.external_gain_db)
    highpass = recommendation.policy.external_highpass_hz
    highpass_label = "해제" if highpass is None else f"{highpass:g} Hz"
    return f"{match.video_path.name} | 상태: {status} | 외부 gain: {gain:+.1f} dB | 외부 HPF: {highpass_label}"


@dataclass(frozen=True, slots=True)
class MatchReport:
    sessions: tuple[RecordingSession, ...]
    matches: tuple[AudioMatch, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    recommended_command: tuple[str, ...] | None = None
    include_recommended_command: bool = False
    audio_levels: tuple[AudioLevelReport | None, ...] = ()
    mix_recommendations: tuple[MixRecommendation | None, ...] = ()

    def __post_init__(self) -> None:
        if self.audio_levels and len(self.audio_levels) != len(self.matches):
            raise ValueError("audio_levels must be empty or align with matches")
        if self.mix_recommendations and len(self.mix_recommendations) != len(self.matches):
            raise ValueError("mix_recommendations must be empty or align with matches")

    def _summary(self) -> dict[str, int]:
        return {
            "total": len(self.matches),
            "matched": sum(match.status is MatchStatus.MATCHED for match in self.matches),
            "partial": sum(match.status is MatchStatus.PARTIAL for match in self.matches),
            "unmatched": sum(match.status is MatchStatus.UNMATCHED for match in self.matches),
            "ambiguous": sum(match.status is MatchStatus.AMBIGUOUS for match in self.matches),
            "error": sum(match.status is MatchStatus.ERROR for match in self.matches),
        }

    def to_dict(self, *, language: ReportLanguage = ReportLanguage.KO) -> dict[str, object]:
        audio_levels = self.audio_levels or (None,) * len(self.matches)
        mix_recommendations = self.mix_recommendations or (None,) * len(self.matches)
        payload: dict[str, object] = {
            "version": REPORT_VERSION,
            "language": language.value,
            "created_at": self.created_at.isoformat(),
            "summary": self._summary(),
            "audio_sessions": [
                {
                    "id": session.id,
                    "duration_seconds": session.duration_seconds,
                    "chunks": [str(chunk.path) for chunk in session.chunks],
                }
                for session in self.sessions
            ],
            "matches": [
                _match_payload(match, language, match_audio_levels, match_mix_recommendation)
                for match, match_audio_levels, match_mix_recommendation in zip(
                    self.matches,
                    audio_levels,
                    mix_recommendations,
                    strict=True,
                )
            ],
        }
        if self.include_recommended_command:
            payload["recommended_command"] = (
                list(self.recommended_command) if self.recommended_command is not None else None
            )
        return payload

    def to_json(self, *, language: ReportLanguage = ReportLanguage.KO) -> str:
        return json.dumps(self.to_dict(language=language), ensure_ascii=False, indent=2)

    def to_text(self, *, language: ReportLanguage = ReportLanguage.KO) -> str:
        """영상별 핵심 매칭 결과만 사람이 읽기 쉬운 목록으로 반환한다."""

        summary = self._summary()
        total = summary["total"]
        matched = summary["matched"]
        partial = summary["partial"]
        overall_rate = matched / total * 100 if total else 0.0

        if language is ReportLanguage.KO:
            summary_line = (
                f"분석 결과: {matched}/{total}개 전체 매칭, {partial}개 부분 매칭"
                if partial
                else f"분석 결과: {matched}/{total}개 매칭 ({overall_rate:.1f}%)"
            )
            matched_label, yes, partial_value, no = "매칭 여부", "성공", "부분", "실패"
            confidence_label, reason_label = "매칭률", "사유"
            coverage_label, segment_label = "레코더 사용", "구간"
            recommendation_label, hold = "추천", "처리 보류"
            recommendation_reason_label = "추천 사유"
            missing_reason = "사유를 확인할 수 없습니다."
        else:
            summary_line = (
                f"Analysis result: {matched}/{total} fully matched, {partial} partially matched"
                if partial
                else f"Analysis result: {matched}/{total} matched ({overall_rate:.1f}%)"
            )
            matched_label, yes, partial_value, no = "matched", "yes", "partial", "no"
            confidence_label, reason_label = "match confidence", "reason"
            coverage_label, segment_label = "recorder coverage", "segments"
            recommendation_label, hold = "recommendation", "hold"
            recommendation_reason_label = "recommendation reason"
            missing_reason = "reason unavailable"

        lines = [summary_line]
        for match in self.matches:
            recommendation = recommend_mode(match)
            is_matched = match.status is MatchStatus.MATCHED
            is_partial = match.status is MatchStatus.PARTIAL
            match_value = partial_value if is_partial else (yes if is_matched else no)
            line = (
                f"- {match.video_path.name} | {matched_label}: {match_value} | "
                f"{confidence_label}: {match.confidence * 100:.1f}%"
            )
            if is_partial:
                segment_count = (
                    f"{len(match.segments)}개" if language is ReportLanguage.KO else str(len(match.segments))
                )
                line = (
                    f"{line} | {coverage_label}: {match.coverage_ratio * 100:.1f}% | {segment_label}: {segment_count}"
                )
            elif not is_matched:
                reason = _translate_reason(match.reason, language) or missing_reason
                line = f"{line} | {reason_label}: {reason}"
            recommendation_value = recommendation.mode.value if recommendation.mode is not None else hold
            line = f"{line} | {recommendation_label}: {recommendation_value}"
            if is_partial:
                line = f"{line} | {recommendation_reason_label}: {_recommendation_reason(recommendation, language)}"
            lines.append(line)
        if self.include_recommended_command:
            lines.append("")
            if self.recommended_command is not None:
                command_label = "추천 실행" if language is ReportLanguage.KO else "Recommended command"
                lines.extend((f"{command_label}:", f"  {shlex.join(self.recommended_command)}"))
            elif language is ReportLanguage.KO:
                lines.append("추천 실행 명령 없음: 신뢰할 수 있는 매칭이 없습니다.")
            else:
                lines.append("No recommended command: no reliable match is available.")
        return "\n".join(lines)

    def write(self, path: Path, *, language: ReportLanguage = ReportLanguage.KO) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.to_json(language=language)}\n", encoding="utf-8")
