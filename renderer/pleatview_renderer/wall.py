"""A wall's clock: the pieces of a wall (session.Tile) are separate streams, and the screens
that play them keep in step by the time each segment was drawn.

ffmpeg writes each piece's playlist with the time its segments start
(EXT-X-PROGRAM-DATE-TIME), but each piece's from the moment its own writer started, a few
milliseconds apart. Every piece is cut from the same frames, with a keyframe every segment,
so segment n of every piece starts on the same frame; their timestamps (PTS) can still
differ by a constant (the piece with the sound starts its timestamps at its first sound).
So the first piece's playlist sets the wall's clock (the time at its timestamp zero, at the
first segment the renderer reads), each other piece's clock is that plus the difference
between the two pieces' first pictures' timestamps at a segment both have, and every segment
gets its time from its first sample's timestamp by its piece's clock (the first sample, as
HLS has it: in the piece with the sound, that can be a sound a little before or after the
first picture, and players count from it).

Each playlist also says how far behind that time screens should play
(EXT-X-SERVER-CONTROL:HOLD-BACK), and, for players that see timestamps but not playlists'
times (mpv), the time at timestamp zero (EXT-X-PLEATVIEW-CLOCK). A player that keeps to
the delay by its own clock (kept right by NTP) shows the same frame as the screens beside it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import time

# Seconds behind the time a segment was drawn that screens play: three segments (the least
# HLS allows), and one to spare for a slow network.
HOLD_BACK = 4.0
# MPEG-TS timestamps: 90 kHz, 33 bits.
TICKS = 90_000
WRAP = 1 << 33
PACKET = 188
# Enough of a segment's start to find its first picture's timestamp.
HEAD_BYTES = 64 * 1024

_PDT = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(.+)")


def stamps(data: bytes) -> dict[str, int]:
    """The presentation timestamps (PTS) of the first picture and the first sound in an MPEG-TS
    segment ("video", "audio"), found from its program tables; those it hasn't, left out."""
    pmt_pid = None
    kinds: dict[int, str] = {}
    found: dict[str, int] = {}
    for at in range(0, len(data) - PACKET + 1, PACKET):
        packet = data[at:at + PACKET]
        if packet[0] != 0x47:
            break
        start = bool(packet[1] & 0x40)
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        control = (packet[3] >> 4) & 0x3
        payload = 4
        if control & 0x2:
            payload += 1 + packet[4]
        if not control & 0x1 or payload >= PACKET:
            continue
        body = packet[payload:]
        if pid == 0 and start and pmt_pid is None:  # the program association table
            body = body[1 + body[0]:]
            if len(body) >= 12:
                pmt_pid = ((body[10] & 0x1F) << 8) | body[11]
        elif pid == pmt_pid and start and not kinds:  # the program map table
            body = body[1 + body[0]:]
            if len(body) < 12:
                continue
            end = 3 + (((body[1] & 0x0F) << 8) | body[2]) - 4
            entry = 12 + (((body[10] & 0x0F) << 8) | body[11])
            while entry + 5 <= min(end, len(body)):
                kind = body[entry]
                elementary = ((body[entry + 1] & 0x1F) << 8) | body[entry + 2]
                if kind in (0x1B, 0x24):  # H.264, H.265
                    kinds[elementary] = "video"
                elif kind in (0x0F, 0x03, 0x04, 0x81):  # AAC, MP3, AC-3
                    kinds[elementary] = "audio"
                entry += 5 + (((body[entry + 3] & 0x0F) << 8) | body[entry + 4])
        elif pid in kinds and start and kinds[pid] not in found:
            if body[:3] != b"\x00\x00\x01" or len(body) < 14 or not body[7] & 0x80:
                continue
            p = body[9:14]
            found[kinds[pid]] = (((p[0] >> 1) & 0x07) << 30) | (p[1] << 22) | ((p[2] >> 1) << 15) | (p[3] << 7) | (p[4] >> 1)
            if len(found) == len(set(kinds.values())):
                break
    return found


def first_pts(data: bytes) -> int | None:
    """The timestamp of a segment's first picture: the same frame has the same in every piece
    (but for a constant), so pieces are lined up by it."""
    return stamps(data).get("video")


def earliest_pts(data: bytes) -> int | None:
    """The timestamp of a segment's first sample, picture or sound: the moment its time
    (EXT-X-PROGRAM-DATE-TIME) is of, which players count from."""
    found = stamps(data)
    if not found:
        return None
    video = found.get("video")
    # The nearer way round from the picture's, should one of them have just wrapped.
    return min(found.values(), key=lambda pts: pts if video is None else video + ((pts - video + WRAP // 2) % WRAP - WRAP // 2))


def _parse_time(text: str) -> float | None:
    text = text.strip()
    text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _format_time(seconds: float) -> str:
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def segments(playlist: str) -> list[tuple[str, float | None, float]]:
    """A playlist's segments: (name, its time as ffmpeg wrote it, its seconds)."""
    out: list[tuple[str, float | None, float]] = []
    when: float | None = None
    length = 0.0
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                length = float(line[8:].split(",", 1)[0])
            except ValueError:
                length = 0.0
        elif match := _PDT.fullmatch(line):
            when = _parse_time(match.group(1))
        elif line and not line.startswith("#"):
            out.append((line, when, length))
            when, length = None, 0.0
    return out


def clock_from(playlist: str, pts_of) -> float | None:
    """The time at timestamp zero, by a piece's playlist as ffmpeg wrote it: the newest segment
    with a time whose first timestamp can be read. `pts_of(name)` reads one (None if gone)."""
    for name, when, _ in reversed(segments(playlist)):
        if when is None:
            continue
        pts = pts_of(name)
        if pts is not None:
            return when - pts / TICKS
    return None


def piece_clock(first_clock: float, playlist: str, pts_of, first_name) -> float | None:
    """Another piece's clock: the first piece's, moved by the difference between their
    timestamps at the same segment (the newest this piece has that both can read).
    `first_name(name)` is the first piece's segment of the same number."""
    for name, _, _ in reversed(segments(playlist)):
        mine, theirs = pts_of(name), pts_of(first_name(name))
        if mine is not None and theirs is not None:
            difference = (theirs - mine + WRAP // 2) % WRAP - WRAP // 2  # the nearer way round
            return first_clock + difference / TICKS
    return None


def unwrapped(clock: float, pts: int, now: float | None = None) -> float:
    """The time a timestamp stands for: of the timestamps that wrap every 26.5 hours, the one
    nearest now."""
    now = time.time() if now is None else now
    turns = round(((now - clock) * TICKS - pts) / WRAP)
    return clock + (pts + turns * WRAP) / TICKS


def in_step(playlist: str, clock: float, pts_of, now: float | None = None) -> str:
    """A piece's playlist as screens get it: each segment's time from its first sample's
    timestamp (`pts_of`) by the piece's clock (a segment that can't be read: its neighbour's,
    plus or less its seconds), the delay to keep, and the clock."""
    found = segments(playlist)
    times: list[float | None] = []
    for name, _, _ in found:
        pts = pts_of(name)
        times.append(unwrapped(clock, pts, now) if pts is not None else None)
    for i in range(1, len(times)):
        if times[i] is None and times[i - 1] is not None:
            times[i] = times[i - 1] + found[i - 1][2]
    for i in range(len(times) - 2, -1, -1):
        if times[i] is None and times[i + 1] is not None:
            times[i] = times[i + 1] - found[i][2]
    out: list[str] = []
    index = 0
    for line in playlist.splitlines():
        stripped = line.strip()
        if _PDT.fullmatch(stripped) or stripped.startswith(("#EXT-X-SERVER-CONTROL", "#EXT-X-PLEATVIEW-CLOCK")):
            continue
        if stripped and not stripped.startswith("#"):
            if index < len(times) and times[index] is not None:
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{_format_time(times[index])}")
            index += 1
        out.append(line)
        if stripped.startswith("#EXT-X-TARGETDURATION"):
            out.append(f"#EXT-X-SERVER-CONTROL:HOLD-BACK={HOLD_BACK:.3f}")
            out.append(f"#EXT-X-PLEATVIEW-CLOCK:{clock:.3f}")
    return "\n".join(out) + "\n"
