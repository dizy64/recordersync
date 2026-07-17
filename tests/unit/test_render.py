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


def test_build_concat_manifest_escapes_paths() -> None:
    manifest = build_concat_manifest(_session())
    first = str(Path("part 1.wav").resolve())
    second = str(Path("part'2.wav").resolve()).replace("'", "'\\''")

    assert f"file '{first}'" in manifest
    assert f"file '{second}'" in manifest


def test_resolve_output_path_uses_replace_dir_and_mp4() -> None:
    assert resolve_output_path(Path("/video/clip.mov"), Path("/video/replace")) == Path(
        "/video/replace/clip.mp4"
    )


def test_resolve_output_path_applies_requested_prefix_and_suffix() -> None:
    assert resolve_output_path(
        Path("/video/clip.mov"),
        Path("/video/replace"),
        prefix="final_",
        suffix="_synced",
    ) == Path("/video/replace/final_clip_synced.mp4")


@pytest.mark.parametrize("affix", ["../escape", "nested/name", "nested\\name"])
def test_resolve_output_path_rejects_path_separators(affix: str) -> None:
    with pytest.raises(ValueError, match="path separator"):
        resolve_output_path(Path("clip.mov"), Path("replace"), suffix=affix)


def test_build_replace_command_uses_tubearchive_profile() -> None:
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


def test_build_portrait_command_preserves_source_dimensions() -> None:
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


def test_build_mix_command_keeps_camera_audio_at_requested_volume() -> None:
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


def test_render_plan_rejects_invalid_camera_volume() -> None:
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


def test_render_plan_rejects_invalid_external_audio_volume() -> None:
    with pytest.raises(ValueError, match="external_audio_volume"):
        RenderPlan(
            video=_video(),
            session=_session(),
            output_path=Path("out.mp4"),
            external_start_seconds=0,
            tempo_ratio=1,
            external_audio_volume=1.1,
        )


def test_renderer_falls_back_to_libx265_and_atomically_publishes(tmp_path: Path) -> None:
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


def test_renderer_preserves_existing_output_without_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"original")
    plan = RenderPlan(_video(), _session(), output, 0, 1, overwrite=False)

    with pytest.raises(FileExistsError, match="already exists"):
        FFmpegRenderer().render(plan)

    assert output.read_bytes() == b"original"


def test_renderer_never_overwrites_source_video() -> None:
    plan = RenderPlan(_video(), _session(), Path("clip.mov"), 0, 1, overwrite=True)
    renderer = FFmpegRenderer()

    with (
        patch.object(renderer, "_run") as run,
        pytest.raises(ValueError, match="source video"),
    ):
        renderer.render(plan)

    run.assert_not_called()
