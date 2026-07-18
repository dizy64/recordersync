"""TubeArchive 호환 렌더 계획과 FFmpeg 명령 테스트."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from recordersync.media import VideoInfo
from recordersync.models import AudioChunk, RecordingSession
from recordersync.render import (
    FFmpegCommandBuilder,
    FFmpegRenderer,
    RenderMode,
    RenderPlan,
    RenderSegment,
    build_concat_manifest,
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


def _video(*, portrait: bool = False, hdr: bool = False) -> VideoInfo:
    return VideoInfo(
        path=Path("clip.mov"),
        duration_seconds=30.0,
        width=1080 if portrait else 3840,
        height=1920 if portrait else 2160,
        has_audio=True,
        color_transfer="arib-std-b67" if hdr else "bt709",
    )


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
    assert "amix" not in joined
    assert "scale=" not in joined
    assert "pad=" not in joined


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
        overwrite=True,
    )

    command = FFmpegCommandBuilder().build(plan, Path("concat.txt"))
    joined = " ".join(command)

    assert "-y" in command[:8]
    assert "volume=0.08" in joined
    assert "volume=0.65,atempo=1" in joined
    assert "amix=inputs=2" in joined
    assert "scale=" not in joined
    assert "pad=" not in joined
    assert "colorspace=all=bt709:iall=bt2020:dither=fsb" in joined


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
