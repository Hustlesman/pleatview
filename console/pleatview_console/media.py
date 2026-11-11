"""The media library: uploads in any format, made playable on every screen with ffmpeg.

Each upload is read by ffprobe, then copied, remuxed or converted into one file that every
PleatView screen plays: H.264 and AAC in MP4 for video, AAC or MP3 for audio, JPEG, PNG,
WebP or GIF for pictures. ffmpeg only opens the uploaded file, with a list of container
formats it may read, so a playlist or other indirection can't make it read anything else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import shutil
from typing import Any

from .store import Store

_LOGGER = logging.getLogger(__name__)

# Demuxers ffmpeg may use on uploads: real containers only, no playlists (HLS, concat, ...),
# no image sequences, nothing that opens another file or address.
FORMATS = ",".join([
    "mov", "mp4", "m4a", "3gp", "matroska", "webm", "avi", "asf", "mpegts", "mpeg", "flv",
    "ogg", "wav", "w64", "aiff", "caf", "mp3", "flac", "aac", "ac3", "eac3", "dts", "wv", "ape",
    "amr", "mxf", "dv", "m4v", "h264", "hevc", "ivf", "obu",
    "png_pipe", "jpeg_pipe", "webp_pipe", "gif", "bmp_pipe", "tiff_pipe", "pgm_pipe", "ppm_pipe",
    "svg_pipe", "qoi_pipe", "jpegxl_pipe",
])
SAFE_INPUT = ["-protocol_whitelist", "file", "-format_whitelist", FORMATS]
IMAGE_TYPES = {"png_pipe": "png", "jpeg_pipe": "jpg", "webp_pipe": "webp", "gif": "gif"}
PLAYABLE_TYPES = {
    "mp4": "video/mp4", "m4a": "audio/mp4", "mp3": "audio/mpeg",
    "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
}
MAX_SIDE = 1920
MAX_IMAGE_SIDE = 3840


def folder(data: Path, tenant_id: str, media_id: int) -> Path:
    return data / "tenants" / tenant_id / "media" / str(media_id)


def summary(probe: dict[str, Any]) -> dict[str, Any]:
    """What the upload is, for people: container, codecs, size and length."""
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "container": (probe.get("format") or {}).get("format_long_name") or (probe.get("format") or {}).get("format_name"),
        "video": video and {"codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height")},
        "audio": audio and {"codec": audio.get("codec_name"), "channels": audio.get("channels")},
    }


def plan(probe: dict[str, Any]) -> tuple[str, str, list[str]]:
    """(kind, playable file name, ffmpeg output arguments); arguments None means copy as is."""
    fmt = (probe.get("format") or {}).get("format_name", "")
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((probe.get("format") or {}).get("duration") or 0)
    still = video is not None and (fmt in IMAGE_TYPES or fmt.endswith("_pipe") or duration == 0) and not audio

    if video and still:
        if fmt in IMAGE_TYPES and fmt != "gif" or fmt == "gif":
            return "image", f"play.{IMAGE_TYPES[fmt]}", []
        scale = f"scale='min({MAX_IMAGE_SIDE},iw)':'min({MAX_IMAGE_SIDE},ih)':force_original_aspect_ratio=decrease"
        return "image", "play.png", ["-frames:v", "1", "-vf", scale, "-f", "image2", "-c:v", "png"]

    if video:
        out = ["-map", "0:v:0", "-sn", "-dn", "-map_metadata", "-1"]
        h264_fits = (
            video.get("codec_name") == "h264"
            and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
            and (video.get("width") or 0) <= MAX_SIDE and (video.get("height") or 0) <= 1088
            and (video.get("level") or 0) <= 42
            and video.get("profile") in ("Constrained Baseline", "Baseline", "Main", "High")
        )
        if h264_fits:
            out += ["-c:v", "copy"]
        else:
            scale = (f"scale='min({MAX_SIDE},iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
                     "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")
            out += ["-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                    "-profile:v", "high", "-level:v", "4.1", "-fpsmax", "60"]
        if audio:
            out += ["-map", "0:a:0"]
            if audio.get("codec_name") == "aac" and (audio.get("channels") or 2) <= 6:
                out += ["-c:a", "copy"]
            else:
                out += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        out += ["-movflags", "+faststart", "-f", "mp4"]
        return "video", "play.mp4", out

    if audio:
        base = ["-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1"]
        if audio.get("codec_name") == "mp3":
            return "audio", "play.mp3", base + ["-c:a", "copy", "-f", "mp3"]
        if audio.get("codec_name") == "aac":
            return "audio", "play.m4a", base + ["-c:a", "copy", "-movflags", "+faststart", "-f", "mp4"]
        return "audio", "play.m4a", base + ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4"]

    raise ValueError("No picture or sound found in the file")


async def _run(*args: str, timeout: float, on_line=None) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def read_out() -> bytes:
        chunks = []
        assert process.stdout
        async for line in process.stdout:
            chunks.append(line)
            if on_line:
                on_line(line.decode(errors="replace").strip())
        return b"".join(chunks)

    try:
        out, err = await asyncio.wait_for(asyncio.gather(read_out(), process.stderr.read()), timeout)  # type: ignore[union-attr]
        await process.wait()
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        return process.returncode or 1, err.decode(errors="replace")[-500:]
    return 0, out.decode(errors="replace")


async def probe(path: Path) -> dict[str, Any]:
    code, out = await _run("ffprobe", "-v", "error", *SAFE_INPUT, "-show_format", "-show_streams",
                           "-of", "json", str(path), timeout=120)
    if code != 0:
        raise ValueError("Not a picture, video or sound file that the console can read")
    return json.loads(out)


class Library:
    """Converts uploads one at a time, so the machine stays usable."""

    def __init__(self, store: Store, on_change=None) -> None:
        self.store = store
        self.on_change = on_change or (lambda tenant_id, media_id: None)
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        # Anything a restart interrupted starts again.
        for tenant in self.store.tenants():
            for item in self.store.tenant(tenant["id"]).media():
                if item["status"] in ("waiting", "converting"):
                    self.queue.put_nowait((tenant["id"], item["id"]))
        self._task = asyncio.create_task(self._work())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def add(self, tenant_id: str, media_id: int) -> None:
        self.queue.put_nowait((tenant_id, media_id))

    def remove(self, tenant_id: str, media_id: int) -> None:
        self.store.tenant(tenant_id).delete_media(media_id)
        shutil.rmtree(folder(self.store.data, tenant_id, media_id), ignore_errors=True)

    async def _work(self) -> None:
        while True:
            tenant_id, media_id = await self.queue.get()
            try:
                await self.convert(tenant_id, media_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad file must not stop the library
                _LOGGER.exception("Converting media %s failed", media_id)

    async def convert(self, tenant_id: str, media_id: int) -> None:
        try:
            tenant = self.store.tenant(tenant_id)
        except KeyError:
            return
        item = tenant.media_item(media_id)
        if not item:
            return
        where = folder(self.store.data, tenant_id, media_id)
        source = where / "original"

        def update(**fields: Any) -> None:
            tenant.update_media(media_id, **fields)
            self.on_change(tenant_id, media_id)

        try:
            info = await probe(source)
            kind, name, args = plan(info)
        except (ValueError, json.JSONDecodeError) as err:
            update(status="failed", error=str(err))
            return
        duration = float((info.get("format") or {}).get("duration") or 0)
        update(status="converting", kind=kind, duration=duration or None, summary=summary(info), progress=0)
        for old in where.glob("play.*"):
            old.unlink()
        target = where / name
        if not args:
            shutil.copyfile(source, target)
        else:
            def progress(line: str) -> None:
                if duration and line.startswith("out_time_us="):
                    value = line.split("=", 1)[1]
                    if value.isdigit():
                        tenant.update_media(media_id, progress=min(int(value) / 1e6 / duration, 0.99))
            last = [0.0]

            def report(line: str) -> None:
                progress(line)
                if line.startswith("progress="):
                    current = tenant.media_item(media_id)["progress"]
                    if current - last[0] >= 0.05:
                        last[0] = current
                        self.on_change(tenant_id, media_id)
            code, err = await _run(
                "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
                *SAFE_INPUT, "-i", str(source), *args, "-progress", "pipe:1", "-nostats", str(target),
                timeout=max(900.0, duration * 6), on_line=report)
            if code != 0:
                target.unlink(missing_ok=True)
                update(status="failed", error=f"ffmpeg couldn't convert it: {err.strip().splitlines()[-1] if err.strip() else code}")
                return
        await self._thumbnail(source if kind == "image" else target, where / "thumb.jpg", kind, duration)
        update(status="ready", progress=1, playable=name, error=None)

    @staticmethod
    async def _thumbnail(source: Path, target: Path, kind: str, duration: float) -> None:
        if kind == "audio":
            return
        seek = ["-ss", f"{duration * 0.1:.2f}"] if kind == "video" and duration > 1 else []
        await _run("ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *seek, *SAFE_INPUT, "-i", str(source),
                   "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2", "-c:v", "mjpeg", str(target), timeout=120)
