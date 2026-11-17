import gzip
import os
import zlib

ENABLED = os.environ.get("COMPRESS_UPLOADS", "1").lower() in ("1", "true", "yes")
LEVEL = int(os.environ.get("COMPRESS_LEVEL", 6))

MIN_GAIN = float(os.environ.get("COMPRESS_MIN_GAIN", 0.10))

PROBE_BYTES = int(os.environ.get("COMPRESS_PROBE_BYTES", 4 * 1024 * 1024))
PROBE_RATIO = float(os.environ.get("COMPRESS_PROBE_RATIO", 0.90))

SKIP_EXT = {
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst", ".lz4", ".br",
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv", ".ts",
    ".mp3", ".aac", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wma",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic", ".heif", ".jxl",
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub",
    ".apk", ".jar", ".whl", ".crx", ".appx", ".dmg", ".iso",
}

def worth_trying(filename : str) -> bool:
    if not ENABLED:
        return False
    return os.path.splitext(filename or "")[1].lower() not in SKIP_EXT


class Attempt:
    def __init__(self, path: str, level: int = LEVEL):
        self.path = path
        self.raw = 0
        self.alive = True
        self._probed = False
        self._fh = open(path, "wb")
        self._gz = gzip.GzipFile(fileobj=self._fh, mode="wb",
                                 compresslevel=level, mtime=0)

    def feed(self, chunk: bytes) -> None:
        if not self.alive:
            return
        self._gz.write(chunk)
        self.raw += len(chunk)
        if not self._probed and self.raw >= PROBE_BYTES:
            self._probed = True
            self._gz.flush(zlib.Z_SYNC_FLUSH)
            if self._fh.tell() / self.raw > PROBE_RATIO:
                self.abandon()
        
    def finish(self) -> int | None:
        if not self.alive:
            return None
        self._gz.close()
        self._fh.close()
        self._gz = self._fh = None
        return os.path.getsize(self.path)

    def abandon(self) -> None:
        if not self.alive:
            return
        self.alive = False
        for h in (self._gz, self._fh):
            try:
                h.close()
            except Exception:
                pass
        self._gz = self._fh = None
        try:
            os.remove(self.path)
        except OSError:
            pass

def accept(original: int, compressed: int | None) -> bool:
    if compressed is None or original <= 0: 
        return False
    return compressed < original * (1.0 - MIN_GAIN)
