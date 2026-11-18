from enum import StrEnum


class SourceKind(StrEnum):
    LIVE = "live"
    RECORDING = "recording"
    URL = "url"
    FILE = "file"


class ProcessingMode(StrEnum):
    LIVE = "live"
    FAST = "fast"
    REALTIME = "realtime"


class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
