"""TubeArchive 호환 렌더 계획과 FFmpeg 명령 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

from recordersync.audio_levels import (
    AudioLevelMetrics,
    AudioLevelPolicy,
    OutputChannelLayout,
)
from recordersync.media import VideoInfo
from recordersync.models import AudioChunk, AudioMatch, MatchStatus, RecordingSession
from recordersync.render import (
    AudioLevelRenderError,
    FFmpegAudioAnalyzer,
    FFmpegCommandBuilder,
    FFmpegRenderer,
    MixPolicy,
    RenderError,
    RenderMode,
    RenderPlan,
    RenderSegment,
    build_concat_manifest,
    build_render_plan,
    resolve_output_path,
)


def _session() -> RecordingSession:
    return RecordingSession(
        id="session-001",
        chunks=(
            AudioChunk(Path("part 1.wav"), 60, 48_000, 2, "pcm_f32le", None),
            AudioChunk(Path("part'2.wav"), 60, 48_000, 2, "pcm_f32le", None),
        ),
    )


def _video(*, portrait: bool = False, hdr: bool = False, audio_channels: int = 2) -> VideoInfo:
    return VideoInfo(
        path=Path("clip.mov"),
        duration_seconds=30.0,
        width=1080 if portrait else 3840,
        height=1920 if portrait else 2160,
        has_audio=True,
        color_transfer="arib-std-b67" if hdr else "bt709",
        audio_channels=audio_channels,
    )


def _audio_level_policy(
    *,
    target_lufs: float = -16.0,
    maximum_true_peak_dbtp: float = -1.0,
) -> AudioLevelPolicy:
    return AudioLevelPolicy(
        target_lufs=target_lufs,
        maximum_true_peak_dbtp=maximum_true_peak_dbtp,
        output_channel_layout=OutputChannelLayout.STEREO,
        loudness_tolerance_lu=0.5,
    )


def _audio_metrics(
    *,
    integrated_loudness_lufs: float,
    true_peak_dbtp: float,
) -> AudioLevelMetrics:
    return AudioLevelMetrics(
        channels=2,
        sample_rate=48_000,
        integrated_loudness_lufs=integrated_loudness_lufs,
        loudness_range_lu=7.0,
        sample_peak_dbfs=true_peak_dbtp - 0.1,
        true_peak_dbtp=true_peak_dbtp,
        duration_seconds=30.0,
        codec="aac",
    )


def _ebur128_summary() -> str:
    return """
[Parsed_ebur128_0] Summary:

  Integrated loudness:
    I:         -16.1 LUFS

  Loudness range:
    LRA:         7.0 LU

  Sample peak:
    Peak:       -1.2 dBFS

  True peak:
    Peak:       -1.1 dBFS
"""


def test_이어붙이기_목록_생성은_경로를_이스케이프한다() -> None:
    manifest = build_concat_manifest(_session())
    first = str(Path("part 1.wav").resolve())
    second = str(Path("part'2.wav").resolve()).replace("'", "'\\''")

    assert f"file '{first}'" in manifest
    assert f"file '{second}'" in manifest


def test_출력_경로_결정은_replace_디렉터리와_MP4를_사용한다() -> None:
    assert resolve_output_path(Path("/video/clip.mov"), Path("/video/replace")) == Path("/video/replace/clip.mp4")


def test_출력_경로_결정은_요청한_접두사와_접미사를_적용한다() -> None:
    assert resolve_output_path(
        Path("/video/clip.mov"),
        Path("/video/replace"),
        prefix="final_",
        suffix="_synced",
    ) == Path("/video/replace/final_clip_synced.mp4")


@pytest.mark.parametrize("affix", ["../escape", "nested/name", "nested\\name"])
def test_출력_경로_결정은_경로_구분자를_거부한다(affix: str) -> None:
    with pytest.raises(ValueError, match="path separator"):
        resolve_output_path(Path("clip.mov"), Path("replace"), suffix=affix)


def test_교체_명령_생성은_tubearchive_프로필을_사용한다() -> None:
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=Path("replace/clip.mp4"),
        external_start_seconds=65.25,
        tempo_ratio=1.0002,
        mode=RenderMode.REPLACE,
        camera_audio_volume=0.1,
        overwrite=False,
    )

    command = FFmpegCommandBuilder().build(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "-n" in command[:8]
    assert "-ss 65.25 -f concat -safe 0 -i concat.txt" in joined
    assert "-c:v hevc_videotoolbox" in joined
    assert "-b:v 50M" in joined
    assert "-pix_fmt p010le" in joined
    assert "-r" not in command
    assert "-fps_mode:v passthrough" in joined
    assert "-c:a aac -b:a 256k -ar 48000" in joined
    assert "[external]" in joined
    assert "volume=1,atempo=1.0002" in joined
    assert "aformat=channel_layouts=stereo" not in joined
    assert "amix" not in joined
    assert "scale=" not in joined
    assert "pad=" not in joined


def test_음량_안전_분석_명령은_실제_교체_구간과_float_채널_정책을_측정한다() -> None:
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=Path("replace/clip.mp4"),
        external_start_seconds=65.25,
        tempo_ratio=1.0002,
        audio_level_policy=_audio_level_policy(),
    )

    command = FFmpegCommandBuilder().build_audio_analysis(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "-xerror -err_detect explode" in joined
    assert "-ss 65.25 -f concat -safe 0 -i concat.txt" in joined
    assert "atempo=1.0002" in joined
    assert "apad,atrim=duration=30" in joined
    assert "aformat=channel_layouts=stereo" in joined
    assert "aformat=sample_fmts=fltp" in joined
    assert "ebur128=peak=sample+true:framelog=quiet" in joined
    assert "volume=" not in joined


def test_음량_안전_채널_정책은_mono를_gain_없이_dual_mono로_측정한다() -> None:
    mono_session = RecordingSession(
        id="session-mono",
        chunks=(AudioChunk(Path("mono.wav"), 60, 48_000, 1, "pcm_f32le", None),),
    )
    plan = RenderPlan(
        video=_video(),
        session=mono_session,
        output_path=Path("replace/clip.mp4"),
        external_start_seconds=0,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )

    command = FFmpegCommandBuilder().build_audio_analysis(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "pan=stereo|c0=c0|c1=c0" in joined
    assert "volume=" not in joined


def test_음량_안전_채널_정책은_stereo_downmix를_명시적인_반반_합으로_수행한다() -> None:
    policy = AudioLevelPolicy(
        target_lufs=-16.0,
        maximum_true_peak_dbtp=-1.0,
        output_channel_layout=OutputChannelLayout.MONO,
        loudness_tolerance_lu=0.5,
    )
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=Path("replace/clip.mp4"),
        external_start_seconds=0,
        tempo_ratio=1,
        audio_level_policy=policy,
    )

    command = FFmpegCommandBuilder().build_audio_analysis(plan, Path("concat.txt"))

    assert "pan=mono|c0=0.5*c0+0.5*c1" in " ".join(command)
    assert FFmpegCommandBuilder.expected_output_channels(plan) == 1


def test_최종_오디오_분석기는_container가_아닌_오디오_stream_길이를_측정한다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "clip.mp4"
    probe_payload = {
        "streams": [
            {
                "channels": 2,
                "sample_rate": "48000",
                "duration": "29.8",
                "codec_name": "aac",
            },
        ],
        "format": {"duration": "30.0"},
    }
    analyzer = FFmpegAudioAnalyzer()
    probe = CompletedProcess(["ffprobe"], 0, json.dumps(probe_payload), "")
    measure = CompletedProcess(["ffmpeg"], 0, "", _ebur128_summary())

    with patch.object(analyzer, "_run", side_effect=(probe, measure)) as run:
        metrics = analyzer.measure_output(output)

    probe_command = run.call_args_list[0].args[0]
    assert "-select_streams" in probe_command
    assert "a:0" in probe_command
    assert "-show_entries" in probe_command
    assert "-show_streams" not in probe_command
    assert metrics.duration_seconds == pytest.approx(29.8)
    assert metrics.integrated_loudness_lufs == pytest.approx(-16.1)
    assert metrics.true_peak_dbtp == pytest.approx(-1.1)
    assert metrics.decoder_error is None


def test_오디오_분석기는_실패_stderr의_마지막_오류_줄을_진단으로_선택한다() -> None:
    result = CompletedProcess(
        ["ffmpeg"],
        1,
        "",
        (
            "Invalid stream specifier while probing input\n"
            "Error while decoding stream #0:0: corrupt input packet\n"
            "Summary:\nPeak: -1.0 dBFS\n"
        ),
    )

    assert FFmpegAudioAnalyzer._decoder_error(result) == "Error while decoding stream #0:0: corrupt input packet"


def test_음량_안전_정책은_폴백_모드에서_허용하지_않는다() -> None:
    with pytest.raises(ValueError, match="replace or mix mode"):
        RenderPlan(
            video=_video(),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            mode=RenderMode.FALLBACK,
            audio_level_policy=_audio_level_policy(),
        )


def test_렌더러는_안전한_static_gain을_적용하고_최종_AAC를_검증한_뒤_공개한다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = _audio_metrics(
        integrated_loudness_lufs=-20.0,
        true_peak_dbtp=-8.0,
    )
    analyzer.measure_output.return_value = _audio_metrics(
        integrated_loudness_lufs=-16.2,
        true_peak_dbtp=-1.1,
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    def run(command: list[str]) -> CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"rendered")
        return CompletedProcess(command, 0, "", "")

    with patch.object(renderer, "_run", side_effect=run) as mocked_run:
        rendered = renderer.render_with_report(plan)

    assert rendered.output_path == output
    assert rendered.audio_levels is not None
    assert rendered.audio_levels.passed
    assert rendered.audio_levels.decision.applied_gain_db == pytest.approx(4.0)
    assert "volume=4dB" in " ".join(mocked_run.call_args.args[0])
    assert output.read_bytes() == b"rendered"
    analyzer.measure_output.assert_called_once()


def test_렌더러는_static_gain과_peak_제한이_충돌하면_렌더하지_않는다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(
            target_lufs=-7.3,
            maximum_true_peak_dbtp=-1.0,
        ),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = _audio_metrics(
        integrated_loudness_lufs=-11.1,
        true_peak_dbtp=7.7,
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(AudioLevelRenderError, match="conflicts") as error,
    ):
        renderer.render_with_report(plan)

    assert error.value.report.decision.conflict_db == pytest.approx(12.5)
    assert not output.exists()
    run.assert_not_called()
    analyzer.measure_output.assert_not_called()


def test_렌더러는_입력_디코더_오류가_있으면_렌더하지_않는다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = AudioLevelMetrics(
        channels=2,
        sample_rate=48_000,
        integrated_loudness_lufs=-20.0,
        loudness_range_lu=7.0,
        sample_peak_dbfs=-8.1,
        true_peak_dbtp=-8.0,
        duration_seconds=30.0,
        codec="pcm_f32le",
        decoder_error="Invalid data found when processing input",
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(AudioLevelRenderError, match="Input audio analysis failed") as error,
    ):
        renderer.render_with_report(plan)

    assert error.value.report.validation_failures == ("decoder error: Invalid data found when processing input",)
    assert not output.exists()
    run.assert_not_called()
    analyzer.measure_output.assert_not_called()


def test_렌더러는_입력_EBUR_요약_전_디코드_실패도_음량_보고서로_남긴다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.side_effect = RenderError(
        "Failed to measure audio levels: Invalid data found when processing input"
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(AudioLevelRenderError, match="Input audio analysis failed") as error,
    ):
        renderer.render_with_report(plan)

    assert error.value.report.input_metrics is None
    assert error.value.report.decision is None
    assert error.value.report.validation_failures == (
        "input analysis error: Failed to measure audio levels: Invalid data found when processing input",
    )
    assert not output.exists()
    run.assert_not_called()


def test_렌더러는_지원하지_않는_입력_채널도_음량_보고서로_남긴다(
    tmp_path: Path,
) -> None:
    multichannel_session = RecordingSession(
        id="session-multichannel",
        chunks=(AudioChunk(Path("surround.wav"), 60, 48_000, 3, "pcm_f32le", None),),
    )
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=multichannel_session,
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )
    renderer = FFmpegRenderer()

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(AudioLevelRenderError, match="Input audio analysis failed") as error,
    ):
        renderer.render_with_report(plan)

    assert error.value.report.input_metrics is None
    assert error.value.report.decision is None
    assert error.value.report.validation_failures == (
        "input analysis error: loudness safety supports mono or stereo recorder audio",
    )
    assert not output.exists()
    run.assert_not_called()


def test_렌더러는_최종_AAC가_peak_검증에_실패하면_임시_출력을_게시하지_않는다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = _audio_metrics(
        integrated_loudness_lufs=-20.0,
        true_peak_dbtp=-8.0,
    )
    analyzer.measure_output.return_value = _audio_metrics(
        integrated_loudness_lufs=-16.1,
        true_peak_dbtp=-0.4,
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    def run(command: list[str]) -> CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"unsafe")
        return CompletedProcess(command, 0, "", "")

    with (
        patch.object(renderer, "_run", side_effect=run),
        pytest.raises(AudioLevelRenderError, match="validation failed") as error,
    ):
        renderer.render_with_report(plan)

    assert error.value.report.validation_failures == ("true peak -0.4 dBTP exceeds -1.0 dBTP",)
    assert not output.exists()
    assert list(output.parent.iterdir()) == []


def test_렌더러는_음량_측정_후_인코더가_실패해도_부분_보고서를_남긴다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        audio_level_policy=_audio_level_policy(),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = _audio_metrics(
        integrated_loudness_lufs=-20.0,
        true_peak_dbtp=-8.0,
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)
    failed = CompletedProcess(["ffmpeg"], 1, "", "encoder failed")

    with (
        patch.object(renderer, "_run", return_value=failed) as run,
        pytest.raises(AudioLevelRenderError, match="VideoToolbox and libx265") as error,
    ):
        renderer.render_with_report(plan)

    assert run.call_count == 2
    assert error.value.report.input_metrics is not None
    assert error.value.report.decision is not None
    assert error.value.report.decision.applied_gain_db == pytest.approx(4.0)
    assert error.value.report.output_metrics is None
    assert error.value.report.validation_failures == (
        "render error: FFmpeg render failed with VideoToolbox and libx265: encoder failed",
    )
    assert not output.exists()
    analyzer.measure_output.assert_not_called()


def test_세로_영상_명령_생성은_원본_해상도를_보존한다() -> None:
    plan = RenderPlan(
        video=_video(portrait=True),
        session=_session(),
        output_path=Path("out.mp4"),
        external_start_seconds=0,
        tempo_ratio=1.0,
    )

    command = FFmpegCommandBuilder().build(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "scale=" not in joined
    assert "pad=" not in joined
    assert "crop=" not in joined
    assert "overlay=" not in joined
    assert "split=2" not in joined
    assert "-noautorotate" not in command


def test_믹스_명령_생성은_카메라_오디오를_요청한_볼륨으로_유지한다() -> None:
    plan = RenderPlan(
        video=_video(portrait=True, hdr=True),
        session=_session(),
        output_path=Path("out.mp4"),
        external_start_seconds=0,
        tempo_ratio=1.0,
        mode=RenderMode.MIX,
        camera_audio_volume=0.08,
        external_audio_volume=0.65,
        external_highpass_hz=100,
        overwrite=True,
    )

    command = FFmpegCommandBuilder().build(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "-y" in command[:8]
    assert "volume=0.08" in joined
    assert "volume=0.65,atempo=1" in joined
    assert "highpass=f=100" in joined
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0" in joined
    assert "scale=" not in joined
    assert "pad=" not in joined
    assert "colorspace=all=bt709:iall=bt2020:dither=fsb" in joined


def test_믹스_음량_분석은_보수적인_비율과_HP80을_합산한_float_신호를_측정한다() -> None:
    mono_session = RecordingSession(
        id="session-mono",
        chunks=(AudioChunk(Path("mono.wav"), 60, 48_000, 1, "pcm_f32le", None),),
    )
    plan = RenderPlan(
        video=_video(),
        session=mono_session,
        output_path=Path("out.mp4"),
        external_start_seconds=5,
        tempo_ratio=1.0001,
        mode=RenderMode.MIX,
        camera_audio_volume=1.0,
        external_audio_volume=10 ** (-12 / 20),
        external_highpass_hz=80,
        audio_level_policy=_audio_level_policy(),
    )

    command = FFmpegCommandBuilder().build_audio_analysis(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "-i clip.mov" in joined
    assert "-ss 5 -f concat -safe 0 -i concat.txt" in joined
    assert "volume=0.251188643" in joined
    assert "atempo=1.0001" in joined
    assert "highpass=f=80" in joined
    assert "pan=stereo|c0=c0|c1=c0" in joined
    assert "volume=1,aresample=48000" in joined
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0" in joined
    assert "aformat=sample_fmts=fltp" in joined
    assert "ebur128=peak=sample+true:framelog=quiet" in joined


def test_믹스_렌더는_측정한_static_gain을_합산_뒤에_적용한다(tmp_path: Path) -> None:
    output = tmp_path / "mix" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        mode=RenderMode.MIX,
        camera_audio_volume=1.0,
        external_audio_volume=10 ** (-12 / 20),
        external_highpass_hz=80,
        audio_level_policy=_audio_level_policy(),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = _audio_metrics(
        integrated_loudness_lufs=-14.9,
        true_peak_dbtp=-5.6,
    )
    analyzer.measure_output.return_value = _audio_metrics(
        integrated_loudness_lufs=-16.0,
        true_peak_dbtp=-6.7,
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    def run(command: list[str]) -> CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"rendered")
        return CompletedProcess(command, 0, "", "")

    with patch.object(renderer, "_run", side_effect=run) as mocked_run:
        rendered = renderer.render_with_report(plan)

    joined = " ".join(mocked_run.call_args.args[0])
    assert rendered.audio_levels is not None
    assert rendered.audio_levels.passed
    assert rendered.audio_levels.decision.applied_gain_db == pytest.approx(-1.1)
    assert joined.index("amix=inputs=2") < joined.index("volume=-1.1dB")


def test_믹스_렌더는_static_gain과_peak_제한이_충돌하면_렌더하지_않는다(tmp_path: Path) -> None:
    output = tmp_path / "mix" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        mode=RenderMode.MIX,
        camera_audio_volume=1.0,
        external_audio_volume=10 ** (-12 / 20),
        external_highpass_hz=80,
        audio_level_policy=_audio_level_policy(target_lufs=-7.3, maximum_true_peak_dbtp=-1.0),
    )
    analyzer = MagicMock(spec=FFmpegAudioAnalyzer)
    analyzer.measure_render_input.return_value = _audio_metrics(
        integrated_loudness_lufs=-11.1,
        true_peak_dbtp=7.7,
    )
    renderer = FFmpegRenderer(audio_analyzer=analyzer)

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(AudioLevelRenderError, match="conflicts") as error,
    ):
        renderer.render_with_report(plan)

    assert error.value.report.decision.conflict_db == pytest.approx(12.5)
    assert not output.exists()
    run.assert_not_called()
    analyzer.measure_output.assert_not_called()


def test_믹스_명령은_mono_카메라를_추가_gain_없이_dual_mono로_만든다() -> None:
    plan = RenderPlan(
        video=_video(audio_channels=1),
        session=_session(),
        output_path=Path("out.mp4"),
        external_start_seconds=1,
        tempo_ratio=1,
        mode=RenderMode.MIX,
        audio_level_policy=_audio_level_policy(),
        output_audio_gain_db=-3,
    )

    joined = " ".join(FFmpegCommandBuilder().build(plan, Path("concat.txt")))

    assert "[0:a:0]volume=1,aresample=48000" in joined
    assert "pan=stereo|c0=c0|c1=c0" in joined


def test_믹스_렌더_계획은_다채널_카메라의_암묵적_downmix를_거부한다() -> None:
    with pytest.raises(ValueError, match="mix mode supports mono or stereo camera audio"):
        RenderPlan(
            video=_video(audio_channels=6),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=1,
            tempo_ratio=1,
            mode=RenderMode.MIX,
        )


def test_렌더_계획은_잘못된_카메라_볼륨을_거부한다() -> None:
    with pytest.raises(ValueError, match="camera_audio_volume"):
        RenderPlan(
            video=_video(),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            mode=RenderMode.MIX,
            camera_audio_volume=1.1,
        )


def test_렌더_계획은_잘못된_외부_오디오_볼륨을_거부한다() -> None:
    with pytest.raises(ValueError, match="external_audio_volume"):
        RenderPlan(
            video=_video(),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            external_audio_volume=1.1,
        )


def test_렌더_계획은_지원_범위_밖의_highpass를_거부한다() -> None:
    with pytest.raises(ValueError, match="external_highpass_hz"):
        RenderPlan(
            video=_video(),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            mode=RenderMode.MIX,
            external_highpass_hz=10,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("camera_audio_volume", 1.1, "camera_audio_volume"),
        ("external_audio_volume", -0.1, "external_audio_volume"),
        ("external_highpass_hz", 10, "external_highpass_hz"),
    ],
)
def test_믹스_정책은_지원_범위_밖의_값을_거부한다(
    field: str,
    value: float,
    message: str,
) -> None:
    values: dict[str, object] = {
        "camera_audio_volume": 1.0,
        "external_audio_volume": 0.25,
        "external_highpass_hz": 80,
        "audio_level_policy": _audio_level_policy(),
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        MixPolicy(**values)  # type: ignore[arg-type]


def test_믹스_정책은_개별_옵션과_동시에_사용할_수_없다() -> None:
    match = AudioMatch(
        _video().path,
        _video().duration_seconds,
        MatchStatus.MATCHED,
        session_id=_session().id,
        external_start_seconds=1,
    )
    policy = MixPolicy(1.0, 0.25, 80, _audio_level_policy())

    with pytest.raises(ValueError, match="individual mix options"):
        build_render_plan(
            match,
            _video(),
            _session(),
            Path("replace"),
            mode=RenderMode.MIX,
            mix_policy=policy,
            external_audio_volume=0.5,
        )


def test_렌더러는_libx265로_대체하고_원자적으로_결과를_공개한다(tmp_path: Path) -> None:
    output = tmp_path / "replace" / "clip.mp4"
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=output,
        external_start_seconds=1,
        tempo_ratio=1,
        overwrite=False,
    )
    renderer = FFmpegRenderer()

    def run(command: list[str]) -> CompletedProcess[str]:
        if "hevc_videotoolbox" in command:
            return CompletedProcess(command, 1, "", "hardware failed")
        Path(command[-1]).write_bytes(b"rendered")
        return CompletedProcess(command, 0, "", "")

    with patch.object(renderer, "_run", side_effect=run) as mocked_run:
        rendered = renderer.render(plan)

    assert rendered == output
    assert output.read_bytes() == b"rendered"
    assert mocked_run.call_count == 2
    assert "libx265" in mocked_run.call_args_list[1].args[0]


def test_렌더러는_덮어쓰지_않고_기존_출력을_보존한다(tmp_path: Path) -> None:
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"original")
    plan = RenderPlan(_video(), _session(), output, 0, 1, overwrite=False)

    with pytest.raises(FileExistsError, match="already exists"):
        FFmpegRenderer().render(plan)

    assert output.read_bytes() == b"original"


def test_렌더러는_덮어쓰기_옵션으로_기존_출력을_원자적으로_교체한다(
    tmp_path: Path,
) -> None:
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"original")
    plan = RenderPlan(_video(), _session(), output, 0, 1, overwrite=True)
    renderer = FFmpegRenderer()

    def run(command: list[str]) -> CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"replacement")
        return CompletedProcess(command, 0, "", "")

    with patch.object(renderer, "_run", side_effect=run) as mocked_run:
        rendered = renderer.render(plan)

    assert rendered == output
    assert output.read_bytes() == b"replacement"
    assert "-y" in mocked_run.call_args.args[0]


def test_렌더러는_원본_영상을_절대_덮어쓰지_않는다() -> None:
    plan = RenderPlan(_video(), _session(), Path("clip.mov"), 0, 1, overwrite=True)
    renderer = FFmpegRenderer()

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(ValueError, match="source video"),
    ):
        renderer.render(plan)

    run.assert_not_called()


def test_폴백_명령은_다중_일치_구간_사이에_카메라음을_사용한다() -> None:
    second_session = RecordingSession(
        "session-002",
        (AudioChunk(Path("second.wav"), 60, 48_000, 2, "pcm_f32le", None),),
    )
    plan = RenderPlan(
        video=_video(),
        session=_session(),
        output_path=Path("out.mp4"),
        external_start_seconds=10.0,
        tempo_ratio=1.0,
        mode=RenderMode.FALLBACK,
        camera_audio_volume=0.4,
        external_audio_volume=0.9,
        segments=(
            RenderSegment(_session(), 2.0, 10.0, 3.0, 1.0),
            RenderSegment(second_session, 8.0, 5.0, 2.0, 1.0),
        ),
        crossfade_seconds=0.05,
    )

    command = FFmpegCommandBuilder().build(
        plan,
        {
            "session-001": Path("first.txt"),
            "session-002": Path("second.txt"),
        },
    )
    joined = " ".join(command)

    assert "-ss 10 -f concat -safe 0 -i first.txt" in joined
    assert "-ss 5 -f concat -safe 0 -i second.txt" in joined
    assert "atrim=start=0:end=2.05" in joined
    assert "atrim=start=4.95:end=8.05" in joined
    assert "atrim=start=9.95:end=30" in joined
    assert "volume=0.4" in joined
    assert "aresample=48000,aformat=channel_layouts=stereo,volume=0.4" in joined
    assert ("volume=0.9,aresample=48000,aformat=channel_layouts=stereo,atempo=1,atrim=duration=3") in joined
    assert joined.count("aformat=channel_layouts=stereo") == 5
    assert joined.count("acrossfade=d=0.05") == 4
    assert "amix" not in joined
    assert "atrim=duration=30" in joined


def test_폴백_렌더_계획은_카메라_오디오와_겹치지_않는_구간을_요구한다() -> None:
    with pytest.raises(ValueError, match="camera audio"):
        RenderPlan(
            video=VideoInfo(Path("silent.mov"), 10, 1920, 1080, False),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            mode=RenderMode.FALLBACK,
        )

    with pytest.raises(ValueError, match="must not overlap"):
        RenderPlan(
            video=_video(),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            mode=RenderMode.FALLBACK,
            segments=(
                RenderSegment(_session(), 1.0, 1.0, 4.0, 1.0),
                RenderSegment(_session(), 4.0, 8.0, 2.0, 1.0),
            ),
        )
