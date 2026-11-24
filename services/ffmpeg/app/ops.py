"""Operations: validate request options and build the FFmpeg command line.

`validate()` runs in the API (fail fast with 400) and returns normalised options.
`command()` runs in the worker and turns normalised options into an argv list.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass


class OptionError(ValueError):
    """Invalid operation or option; the message is returned to the client."""


VIDEO_FORMATS = {"mp4": ("h264", "aac"), "mov": ("h264", "aac"), "mkv": ("h264", "aac"),
                 "webm": ("vp9", "opus"), "avi": ("mpeg4", "mp3"), "ts": ("h264", "aac"),
                 "flv": ("h264", "aac")}
AUDIO_FORMATS = {"mp3": "mp3", "m4a": "aac", "aac": "aac", "wav": "pcm", "flac": "flac",
                 "ogg": "vorbis", "opus": "opus"}
IMAGE_FORMATS = ("jpg", "png", "webp")
VIDEO_CODECS = {"h264": "libx264", "h265": "libx265", "hevc": "libx265", "vp9": "libvpx-vp9",
                "av1": "libsvtav1", "mpeg4": "mpeg4", "copy": "copy"}
AUDIO_CODECS = {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis",
                "flac": "flac", "pcm": "pcm_s16le", "copy": "copy"}
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
CONTENT_TYPES = {"mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska", "webm": "video/webm",
                 "avi": "video/x-msvideo", "ts": "video/mp2t", "flv": "video/x-flv", "mp3": "audio/mpeg",
                 "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav", "flac": "audio/flac",
                 "ogg": "audio/ogg", "opus": "audio/opus", "gif": "image/gif", "jpg": "image/jpeg",
                 "png": "image/png", "webp": "image/webp", "zip": "application/zip", "json": "application/json"}
OPERATIONS = {
    "convert": "Transcode audio/video/images: format, video_codec, audio_codec, crf, preset, width, height, fps, "
               "video_bitrate, audio_bitrate, audio_channels, sample_rate, start, duration, no_audio, no_video.",
    "extract_audio": "Audio track only: format (mp3), bitrate, audio_channels, sample_rate, start, duration.",
    "thumbnail": "One frame as an image: time (seconds, default 1), width, height, format (jpg/png/webp).",
    "frames": "Frames as images in a ZIP: fps (default 1), max_frames (100), width, height, format, start, duration.",
    "gif": "Animated GIF: start, duration (default 5, max 60), fps (10), width (480).",
    "probe": "ffprobe metadata (format and streams) as JSON; no output file.",
    "custom": "Your own FFmpeg arguments between input and output: args (list or string), "
              "input_args (before -i), format (output extension).",
}
# Options that FFmpeg must never receive from a client: extra inputs/outputs, protocol and
# file-access switches, overwrite flags (the worker controls input and output paths).
FORBIDDEN_FLAGS = {"-i", "-y", "-n", "-protocol_whitelist", "-protocol_blacklist", "-filter_script",
                   "-filter_complex_script", "-dump_attachment", "-attach", "-progress", "-vstats_file",
                   "-report", "-passlogfile", "-sdp_file", "-init_hw_device", "-filter_hw_device", "-hwaccel"}
FORBIDDEN_FILTERS = re.compile(r"\b(movie|amovie|sendcmd|asendcmd|zmq|azmq|subtitles|ass|lensfun|"
                               r"frei0r|ladspa|lv2|ocr|sofalizer|signature|vidstab\w*)\s*=", re.I)
TIME = re.compile(r"^(\d+(\.\d+)?|\d{1,3}:[0-5]?\d(:[0-5]?\d)?(\.\d+)?)$")
BITRATE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")
EXT = re.compile(r"^[a-z0-9]{1,8}$")
RESERVED = {"operation", "options", "url", "file", "wait", "callback_url", "filename"}


@dataclass(frozen=True)
class Plan:
    input_args: tuple[str, ...]
    output_args: tuple[str, ...]
    extension: str
    multi_file: bool = False  # frames: output pattern in a directory, zipped afterwards


def _time(opts: dict, key: str) -> str | None:
    value = opts.get(key)
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not TIME.match(value):
        raise OptionError(f"{key} must be seconds (12.5) or HH:MM:SS, got {value!r}")
    return value


def _int(opts: dict, key: str, low: int, high: int, default: int | None = None) -> int | None:
    value = opts.get(key)
    if value in (None, ""):
        return default
    try:
        number = int(float(str(value)))
    except ValueError:
        raise OptionError(f"{key} must be a number") from None
    if not low <= number <= high:
        raise OptionError(f"{key} must be between {low} and {high}")
    return number


def _choice(opts: dict, key: str, choices, default: str | None = None) -> str | None:
    value = opts.get(key)
    if value in (None, ""):
        return default
    value = str(value).strip().lower()
    if value not in choices:
        raise OptionError(f"{key} must be one of {', '.join(sorted(choices))}")
    return value


def _bitrate(opts: dict, key: str) -> str | None:
    value = opts.get(key)
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not BITRATE.match(value):
        raise OptionError(f"{key} must look like 128k or 2M")
    return value


def _bool(opts: dict, key: str, default: bool = False) -> bool:
    value = opts.get(key)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _args(value, key: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            value = json.loads(value)
        except ValueError:
            raise OptionError(f"{key}: not a valid JSON list") from None
    if isinstance(value, str):
        try:
            tokens = shlex.split(value)
        except ValueError as err:
            raise OptionError(f"{key}: {err}") from None
    elif isinstance(value, list) and all(isinstance(t, (str, int, float)) for t in value):
        tokens = [str(t) for t in value]
    else:
        raise OptionError(f"{key} must be a list of strings or one string")
    if len(tokens) > 200:
        raise OptionError(f"{key}: at most 200 arguments")
    for token in tokens:
        if token.split(":")[0] in FORBIDDEN_FLAGS or token in FORBIDDEN_FLAGS:
            raise OptionError(f"{key}: {token} is not allowed (the API sets input and output itself)")
        if "://" in token or ".." in token or re.search(r"(^|[=:,'\"\s])/[A-Za-z_]", token):
            raise OptionError(f"{key}: URLs and file paths are not allowed ({token[:40]})")
        if FORBIDDEN_FILTERS.search(token):
            raise OptionError(f"{key}: filters that read files or sockets are not allowed")
    return tokens


def _scale(width: int | None, height: int | None) -> str | None:
    if width and height:
        return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if width:
        return f"scale={width}:-2"
    if height:
        return f"scale=-2:{height}"
    return None


def validate(operation: str, options: dict, allow_custom: bool = True) -> dict:
    """Return normalised options (only known keys, typed) or raise OptionError."""
    op = (operation or "").strip().lower().replace("-", "_")
    if op == "transcode":
        op = "convert"
    if op not in OPERATIONS:
        raise OptionError(f"operation must be one of {', '.join(OPERATIONS)}")
    o = options or {}
    out: dict = {"operation": op}
    if op in ("convert", "extract_audio", "frames", "gif", "custom"):
        out["start"] = _time(o, "start")
        out["duration"] = _time(o, "duration")
    if op == "convert":
        fmt = _choice(o, "format", set(VIDEO_FORMATS) | set(AUDIO_FORMATS) | set(IMAGE_FORMATS) | {"jpeg"})
        if not fmt:
            raise OptionError("format is required for convert (e.g. mp4, webm, mp3, png)")
        fmt = "jpg" if fmt == "jpeg" else fmt
        out.update(format=fmt,
                   video_codec=_choice(o, "video_codec", VIDEO_CODECS),
                   audio_codec=_choice(o, "audio_codec", AUDIO_CODECS),
                   crf=_int(o, "crf", 0, 63), preset=_choice(o, "preset", PRESETS),
                   width=_int(o, "width", 16, 7680), height=_int(o, "height", 16, 4320),
                   fps=_int(o, "fps", 1, 120), video_bitrate=_bitrate(o, "video_bitrate"),
                   audio_bitrate=_bitrate(o, "audio_bitrate"), audio_channels=_int(o, "audio_channels", 1, 8),
                   sample_rate=_int(o, "sample_rate", 8000, 192000), no_audio=_bool(o, "no_audio"),
                   no_video=_bool(o, "no_video"))
    elif op == "extract_audio":
        out.update(format=_choice(o, "format", AUDIO_FORMATS, "mp3"),
                   audio_bitrate=_bitrate(o, "bitrate") or _bitrate(o, "audio_bitrate"),
                   audio_channels=_int(o, "audio_channels", 1, 8), sample_rate=_int(o, "sample_rate", 8000, 192000))
    elif op == "thumbnail":
        out.update(time=_time(o, "time") or "1", width=_int(o, "width", 16, 7680), height=_int(o, "height", 16, 4320),
                   format=_choice(o, "format", set(IMAGE_FORMATS) | {"jpeg"}, "jpg").replace("jpeg", "jpg"))
    elif op == "frames":
        out.update(fps=_int(o, "fps", 1, 60, 1), max_frames=_int(o, "max_frames", 1, 1000, 100),
                   width=_int(o, "width", 16, 7680), height=_int(o, "height", 16, 4320),
                   format=_choice(o, "format", set(IMAGE_FORMATS) | {"jpeg"}, "jpg").replace("jpeg", "jpg"))
    elif op == "gif":
        out.update(fps=_int(o, "fps", 1, 30, 10), width=_int(o, "width", 16, 1920, 480))
        out["duration"] = out["duration"] or "5"
        if _seconds(out["duration"]) > 60:
            raise OptionError("duration for gif must be at most 60 seconds")
    elif op == "custom":
        if not allow_custom:
            raise OptionError("custom FFmpeg arguments are disabled on this server (ALLOW_CUSTOM_ARGS=false)")
        fmt = str(o.get("format", "")).strip().lower().lstrip(".")
        if not EXT.match(fmt):
            raise OptionError("format (output file extension such as mp4) is required for custom")
        out.update(format=fmt, args=_args(o.get("args"), "args"), input_args=_args(o.get("input_args"), "input_args"))
    return {k: v for k, v in out.items() if v is not None and v is not False}


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def output_extension(opts: dict) -> str:
    op = opts["operation"]
    if op == "probe":
        return "json"
    if op == "frames":
        return "zip"
    if op == "gif":
        return "gif"
    return opts["format"]


def plan(opts: dict, threads: int) -> Plan:
    """Build input/output arguments for FFmpeg (without the input/output paths)."""
    op = opts["operation"]
    pre: list[str] = []
    post: list[str] = []
    if opts.get("start"):
        pre += ["-ss", opts["start"]]
    if opts.get("duration"):
        post += ["-t", opts["duration"]]
    if op == "convert":
        fmt = opts["format"]
        if fmt in IMAGE_FORMATS:
            post += ["-frames:v", "1", "-an"]
            vf = _scale(opts.get("width"), opts.get("height"))
            if vf:
                post += ["-vf", vf]
            if fmt == "jpg":
                post += ["-q:v", "2"]
            return Plan(tuple(pre), tuple(post), fmt)
        audio_only = fmt in AUDIO_FORMATS or opts.get("no_video")
        if audio_only:
            post += ["-vn"]
        else:
            vcodec = opts.get("video_codec") or VIDEO_FORMATS[fmt][0]
            post += ["-c:v", VIDEO_CODECS[vcodec]]
            if vcodec != "copy":
                vf = [f for f in (_scale(opts.get("width"), opts.get("height")),) if f]
                if opts.get("fps"):
                    post += ["-r", str(opts["fps"])]
                if vf:
                    post += ["-vf", ",".join(vf)]
                if opts.get("video_bitrate"):
                    post += ["-b:v", opts["video_bitrate"]]
                if vcodec in ("h264", "h265", "hevc"):
                    post += ["-preset", opts.get("preset") or "fast", "-pix_fmt", "yuv420p"]
                    if opts.get("crf") is not None and not opts.get("video_bitrate"):
                        post += ["-crf", str(min(opts["crf"], 51))]
                    if vcodec in ("h265", "hevc") and fmt in ("mp4", "mov"):
                        post += ["-tag:v", "hvc1"]
                elif vcodec == "vp9":
                    post += ["-deadline", "good", "-cpu-used", "4", "-row-mt", "1"]
                    if not opts.get("video_bitrate"):
                        post += ["-crf", str(opts.get("crf", 32)), "-b:v", "0"]
                elif vcodec == "av1":
                    post += ["-preset", "8"]
                    if not opts.get("video_bitrate"):
                        post += ["-crf", str(opts.get("crf", 35))]
                elif vcodec == "mpeg4":
                    post += ["-q:v", "4"]
        if opts.get("no_audio") and not audio_only:
            post += ["-an"]
        else:
            acodec = opts.get("audio_codec") or (AUDIO_FORMATS[fmt] if fmt in AUDIO_FORMATS else VIDEO_FORMATS[fmt][1])
            post += ["-c:a", AUDIO_CODECS[acodec]]
            if acodec != "copy":
                if opts.get("audio_bitrate"):
                    post += ["-b:a", opts["audio_bitrate"]]
                if opts.get("audio_channels"):
                    post += ["-ac", str(opts["audio_channels"])]
                if opts.get("sample_rate"):
                    post += ["-ar", str(opts["sample_rate"])]
        if fmt in ("mp4", "mov", "m4a"):
            post += ["-movflags", "+faststart"]
        post += ["-threads", str(threads)]
        return Plan(tuple(pre), tuple(post), fmt)
    if op == "extract_audio":
        fmt = opts["format"]
        post += ["-vn", "-c:a", AUDIO_CODECS[AUDIO_FORMATS[fmt]]]
        if opts.get("audio_bitrate"):
            post += ["-b:a", opts["audio_bitrate"]]
        if opts.get("audio_channels"):
            post += ["-ac", str(opts["audio_channels"])]
        if opts.get("sample_rate"):
            post += ["-ar", str(opts["sample_rate"])]
        if fmt == "m4a":
            post += ["-movflags", "+faststart"]
        return Plan(tuple(pre), tuple(post), fmt)
    if op == "thumbnail":
        pre = ["-ss", opts["time"]]
        post = ["-frames:v", "1", "-an", "-update", "1"]
        vf = _scale(opts.get("width"), opts.get("height"))
        if vf:
            post += ["-vf", vf]
        if opts["format"] == "jpg":
            post += ["-q:v", "2"]
        return Plan(tuple(pre), tuple(post), opts["format"])
    if op == "frames":
        vf = ",".join(f for f in (f"fps={opts['fps']}", _scale(opts.get("width"), opts.get("height"))) if f)
        post += ["-vf", vf, "-frames:v", str(opts["max_frames"]), "-an"]
        if opts["format"] == "jpg":
            post += ["-q:v", "3"]
        return Plan(tuple(pre), tuple(post), opts["format"], multi_file=True)
    if op == "gif":
        post += ["-vf", f"fps={opts['fps']},scale={opts['width']}:-1:flags=lanczos,split[a][b];"
                        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer", "-loop", "0", "-an"]
        return Plan(tuple(pre), tuple(post), "gif")
    if op == "custom":
        return Plan(tuple(pre + opts.get("input_args", [])), tuple(post + opts.get("args", [])), opts["format"])
    raise OptionError(f"operation {op} has no FFmpeg command")
