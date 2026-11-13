#!/usr/bin/env python3
"""
getdatjawn - grab a track from a streaming-service URL and get a tagged local audio file.

Pipeline:
    URL -> resolver (Spotify / YouTube Music / Apple Music / Tidal) -> track metadata
        -> matcher (YouTube search + candidate ranking)
        -> downloader (yt-dlp)
        -> processor (FFmpeg convert / optional loudness normalize)
        -> tagger (metadata + embedded artwork)
        -> output (organized files + job state)

Usage:
    getdatjawn <url> [url ...] [options]
    getdatjawn resume [--failed]
    getdatjawn sync
    getdatjawn list
"""


from __future__ import annotations
__version__ = "0.1.0"

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, quote_plus

try:
    import requests
except ImportError:  # checked lazily when a resolver runs; `install` must work without it
    requests = None  # type: ignore

try:
    import mutagen
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TRCK, TDRC, APIC, USLT, COMM
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:  # pragma: no cover
    mutagen = None

DEFAULT_STATE = "~/.config/getdatjawn/state.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

log = logging.getLogger("getdatjawn")


# ----------------------------------------------------------------------------
# Track metadata model
# ----------------------------------------------------------------------------

@dataclass
class Track:
    source: str = ""                 # spotify | apple | ytmusic | tidal | youtube
    source_id: str = ""               # service-native id
    source_url: str = ""
    title: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_artists: list[str] = field(default_factory=list)
    duration: float = 0.0            # seconds
    artwork_url: str = ""
    genre: str = ""
    track_number: int = 0
    disc_number: int = 0
    year: str = ""
    isrc: str = ""

    @property
    def artist_str(self) -> str:
        if self.album_artists:
            return ", ".join(self.album_artists)
        return ", ".join(self.artists) if self.artists else "Unknown Artist"

    @property
    def search_query(self) -> str:
        base = f"{self.artist_str} {self.title}".strip()
        if self.album and "official" not in self.title.lower():
            base += f" {self.album}"
        return re.sub(r"\s+", " ", base)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ----------------------------------------------------------------------------
# Stage 1: Resolver
# ----------------------------------------------------------------------------

class Resolver:
    """Turns a streaming-service URL into Track metadata."""

    def resolve(self, url: str) -> Track:
        if requests is None:
            sys.exit("getdatjawn: missing dependency 'requests' (pip install requests)")
        host = urlparse(url).netloc.lower()
        if "spotify.com" in host:
            track = self._resolve_spotify(url)
        elif "music.apple.com" in host or "itunes.apple.com" in host:
            track = self._resolve_apple(url)
        elif "tidal.com" in host:
            track = self._resolve_tidal(url)
        elif "music.youtube.com" in host or "youtube.com" in host or "youtu.be" in url:
            track = self._resolve_youtube(url)
        else:
            raise ValueError(f"unrecognized service in URL: {url}")
        track.source_url = url
        if not track.title:
            raise ValueError(f"could not resolve metadata for: {url}")
        return track

    # -- Spotify -------------------------------------------------------------

    def _resolve_spotify(self, url: str) -> Track:
        m = re.search(r"(?:spotify\.com/(?:intl-[a-z]{2}/)?track|open\.spotify\.com/track)/([A-Za-z0-9]+)", url)
        if not m:
            raise ValueError(f"not a Spotify track URL: {url}")
        tid = m.group(1)

        # Embed page ships a hydration JSON blob with full metadata, no API key needed.
        r = requests.get(f"https://open.spotify.com/embed/track/{tid}",
                         headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        m = re.search(r'id="resource"[\s\S]*?>([\s\S]*?)<\/script>', r.text)
        blob = {}
        if m:
            try:
                blob = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        if not blob:
            # Fallback: oEmbed gives at least a title and artwork.
            oe = requests.get("https://open.spotify.com/oembed",
                              params={"url": f"https://open.spotify.com/track/{tid}"}, timeout=15).json()
            blob = {"name": oe.get("title", ""), "coverArt": {"sources": [{"url": oe.get("thumbnail_url")}]}}
            if " - " in blob["name"]:
                a, t = blob["name"].split(" - ", 1)
                blob.setdefault("artists", [{"name": a}])
                blob["name"] = t

        artists = [a.get("name", "") for a in blob.get("artists", []) if a.get("name")]
        return Track(
            source="spotify", source_id=tid,
            title=blob.get("name", ""),
            artists=artists,
            album=(blob.get("albumOfTrack") or {}).get("name", ""),
            album_artists=[(blob.get("albumOfTrack") or {}).get("artists", "")] if isinstance((blob.get("albumOfTrack") or {}).get("artists"), str) else [],
            duration=float(blob.get("duration", 0)) / 1000.0,
            artwork_url=(((blob.get("coverArt") or {}).get("sources") or [{}])[-1]).get("url", ""),
        )

    # -- Apple Music ----------------------------------------------------------

    def _resolve_apple(self, url: str) -> Track:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        # Schema.org JSON-LD is embedded on every Apple Music page.
        m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise ValueError("could not parse Apple Music page")
        ld = json.loads(m.group(1))
        t = Track(source="apple", source_url=url)
        t.title = ld.get("name", "")
        dur = ld.get("duration") or ld.get("track", {}).get("duration") if isinstance(ld.get("track"), dict) else ld.get("duration")
        if dur and re.match(r"^PT[\dHMS.]+$", str(dur)):
            h = re.search(r"(\d+)H", dur); mi = re.search(r"(\d+)M", dur); s = re.search(r"([\d.]+)S", dur)
            t.duration = int(h.group(1)) * 3600 if h else 0
            t.duration += int(mi.group(1)) * 60 if mi else 0
            t.duration += float(s.group(1)) if s else 0
        art = ld.get("byArtist") or ld.get("artist") or {}
        if isinstance(art, dict):
            art = art.get("name", "")
        if isinstance(art, list):
            art = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in art)
        t.artists = [a.strip() for a in str(art).split(",") if a.strip()]
        al = ld.get("inAlbum") or {}
        if isinstance(al, dict):
            t.album = al.get("name", "")
        ia = ld.get("image") or ""
        t.artwork_url = ia if isinstance(ia, str) else (ia[0] if ia else "")
        # Upscale: Apple serves e.g. .../200x200bb.jpg; request higher res.
        t.artwork_url = t.artwork_url.replace("200x200bb", "1000x1000bb")
        t.source_id = ld.get("url", url).rsplit("/", 1)[-1].split("?")[0]
        return t

    # -- Tidal ----------------------------------------------------------------

    def _resolve_tidal(self, url: str) -> Track:
        m = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", url)
        if not m:
            raise ValueError(f"not a Tidal track URL: {url}")
        tid = m.group(1)
        t = Track(source="tidal", source_id=tid)
        # Try the public oEmbed-style listen page first.
        r = requests.get(f"https://tidal.com/browse/track/{tid}", headers={"User-Agent": UA}, timeout=15)
        if r.ok:
            mm = re.search(r'<meta property="og:title" content="([^"]*)"', r.text)
            if mm:
                title = mm.group(1)
                if " - " in title:
                    a, title = title.split(" - ", 1)
                    t.artists = [a.strip()]
                t.title = title.strip()
            mm = re.search(r'<meta property="og:image" content="([^"]*)"', r.text)
            if mm:
                t.artwork_url = mm.group(1)
            mm = re.search(r'"duration"\s*:\s*(\d+)', r.text)
            if mm:
                t.duration = float(mm.group(1))
            mm = re.search(r'"album"\s*:\s*\{[^}]*"title"\s*:\s*"([^"]*)"', r.text)
            if mm:
                t.album = mm.group(1)
        if not t.title:
            # fall back to generic oembed
            oe = requests.get("https://oembed.tidal.com/1.0/oembed",
                              params={"url": f"https://tidal.com/track/{tid}"}, timeout=15).json()
            t.title = oe.get("title", "")
            t.artists = t.artists or [oe.get("author_name", "")]
            t.artwork_url = t.artwork_url or oe.get("thumbnail_url", "")
        return t

    # -- YouTube / YouTube Music ----------------------------------------------

    def _resolve_youtube(self, url: str) -> Track:
        vid = self._yt_id(url)
        info = self._ytdlp().extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        t = Track(source="youtube", source_id=vid)
        t.title = info.get("title", "")
        t.artists = [info.get("artist") or info.get("uploader") or "Unknown Artist"]
        t.album = info.get("album") or ""
        t.duration = float(info.get("duration") or 0)
        t.artwork_url = info.get("thumbnail") or ""
        if info.get("track") and info.get("track") != t.title:
            # YouTube Music data: prefer the proper track title.
            t.title = info["track"]
        if info.get("release_year"):
            t.year = str(info["release_year"])
        return t

    @staticmethod
    def _yt_id(url: str) -> str:
        if "youtu.be/" in url:
            return url.split("youtu.be/")[1].split("?")[0]
        qs = parse_qs(urlparse(url).query)
        if "v" in qs:
            return qs["v"][0]
        m = re.search(r"(?:embed|shorts|watch)/([A-Za-z0-9_-]{11})", url)
        if m:
            return m.group(1)
        raise ValueError(f"not a YouTube URL: {url}")

    @staticmethod
    def _ytdlp():
        try:
            import yt_dlp
        except ImportError:
            sys.exit("getdatjawn: missing dependency 'yt-dlp' (pip install yt-dlp)")
        return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True})


# ----------------------------------------------------------------------------
# Stage 2: Matcher - search YouTube and rank candidates
# ----------------------------------------------------------------------------

class Matcher:
    GOOD_WORDS = ("official audio", "official", "audio", "lyrics", "hq", "hd", "remaster")
    BAD_WORDS = ("cover", "live", "remix", "instrumental", "karaoke", "tutorial",
                 "reaction", "8d", "nightcore", "sped up", "slowed", "full album", "mix -")

    def __init__(self, max_candidates: int = 10):
        self.max_candidates = max_candidates

    def match(self, track: Track) -> dict:
        """Return the best YouTube candidate dict for a Track."""
        import yt_dlp
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                                "playlistend": self.max_candidates})
        query = quote_plus(track.search_query)
        data = ydl.extract_info(f"ytsearch{self.max_candidates}:{track.search_query}", download=False)
        entries = [e for e in (data.get("entries") or []) if e]
        if not entries:
            raise RuntimeError(f"no YouTube candidates found for: {track.search_query}")
        ranked = sorted(entries, key=lambda e: -self._score(track, e))
        best = ranked[0]
        log.debug("top candidate %s score=%.2f (%s)", best.get("id"), self._score(track, best), best.get("title"))
        return best

    def _score(self, track: Track, entry: dict) -> float:
        title = (entry.get("title") or "").lower()
        channel = (entry.get("uploader") or entry.get("channel") or "").lower()
        dur = float(entry.get("duration") or 0)

        # Title similarity to "artist(s) - title"
        target = f"{track.artist_str} {track.title}".lower()
        sim = SequenceMatcher(None, title, target).ratio()
        score = 2.5 * sim
        # Channel name matching an artist is a strong signal.
        if any(a.lower() in channel for a in track.artists):
            score += 1.2
        # Duration proximity (only meaningful when both known).
        if track.duration and dur:
            diff = abs(track.duration - dur)
            if diff <= 2:
                score += 2.0
            elif diff <= 5:
                score += 1.2
            elif diff <= 10:
                score += 0.4
            else:
                score -= min(1.5, (diff - 10) / 30.0)
        # Keyword bonuses / penalties.
        for w in self.GOOD_WORDS:
            if w in title or w in channel:
                score += 0.35
                break
        for w in self.BAD_WORDS:
            if w in title:
                score -= 0.8
        return score


# ----------------------------------------------------------------------------
# Stage 3: Downloader
# ----------------------------------------------------------------------------

class Downloader:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)

    def download(self, video_id: str) -> Path:
        import yt_dlp
        outtmpl = str(self.workdir / "%(id)s.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "overwrites": True,
        }
        ydl = yt_dlp.YoutubeDL(opts)
        ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        for p in self.workdir.glob(f"{video_id}.*"):
            if p.suffix.lower() != ".part":
                return p
        raise RuntimeError(f"download produced no file for {video_id}")


# ----------------------------------------------------------------------------
# Stage 4: Processor - FFmpeg conversion
# ----------------------------------------------------------------------------

class Processor:
    CODECS = {
        "mp3":  ("libmp3lame", ".mp3"),
        "m4a":  ("aac", ".m4a"),
        "opus": ("libopus", ".opus"),
        "flac": ("flac", ".flac"),
        "wav":  ("pcm_s16le", ".wav"),
    }

    def __init__(self, fmt: str = "mp3", bitrate: str = "320k", normalize: bool = False):
        if fmt not in self.CODECS:
            raise ValueError(f"unsupported format '{fmt}' (choose from {', '.join(self.CODECS)})")
        self.fmt, self.bitrate, self.normalize = fmt, bitrate, normalize
        if shutil.which("ffmpeg") is None:
            sys.exit("getdatjawn: ffmpeg not found on PATH (install ffmpeg)")

    def process(self, src: Path, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        codec, _ = self.CODECS[self.fmt]
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        if self.normalize and self.fmt in ("mp3", "m4a"):
            # two-pass loudness normalization to ~-14 LUFS (streaming standard)
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        cmd += ["-vn", "-c:a", codec]
        if self.fmt in ("mp3", "m4a"):
            cmd += ["-b:a", self.bitrate]
        cmd += [str(dest)]
        subprocess.run(cmd, check=True)
        return dest


# ----------------------------------------------------------------------------
# Stage 5: Tagger - metadata + artwork
# ----------------------------------------------------------------------------

class Tagger:
    def __init__(self, artwork_dir: Path):
        self.artwork_dir = artwork_dir
        if mutagen is None:
            log.warning("mutagen not installed; skipping tags (pip install mutagen)")

    def tag(self, path: Path, track: Track, chosen: Optional[dict] = None) -> None:
        if mutagen is None:
            return
        art = self._fetch_artwork(track)
        if path.suffix.lower() == ".mp3":
            self._tag_mp3(path, track, art, chosen)
        elif path.suffix.lower() in (".m4a", ".mp4"):
            self._tag_m4a(path, track, art)
        elif path.suffix.lower() == ".flac":
            self._tag_flac(path, track, art)

    def _fetch_artwork(self, track: Track) -> Optional[bytes]:
        if not track.artwork_url:
            return None
        try:
            r = requests.get(track.artwork_url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as e:
            log.debug("artwork fetch failed: %s", e)
            return None

    @staticmethod
    def _tag_mp3(path: Path, track: Track, art: Optional[bytes], chosen: Optional[dict]) -> None:
        id3 = ID3(path)
        if id3 is None:
            id3 = ID3()
        id3.setall("TIT2", [TIT2(encoding=3, text=track.title)])
        id3.setall("TPE1", [TPE1(encoding=3, text=track.artists or [track.artist_str])])
        id3.setall("TALB", [TALB(encoding=3, text=track.album or "Single")])
        id3.setall("TCON", [TCON(encoding=3, text=track.genre)] if track.genre else [])
        if track.track_number:
            id3.setall("TRCK", [TRCK(encoding=3, text=f"{track.track_number:02d}")])
        if track.year:
            id3.setall("TDRC", [TDRC(encoding=3, text=track.year)])
        if art:
            mime = "image/jpeg" if art[:3] == b"\xff\xd8\xff" else "image/png"
            id3.setall("APIC", [APIC(encoding=3, mime=mime, type=3, desc="Cover", data=art)])
        id3.save(path)

    @staticmethod
    def _tag_m4a(path: Path, track: Track, art: Optional[bytes]) -> None:
        audio = MP4(path)
        audio["\xa9nam"] = [track.title]
        audio["\xa9ART"] = [", ".join(track.artists) or track.artist_str]
        audio["\xa9alb"] = [track.album or "Single"]
        if track.year:
            audio["\xa9day"] = [track.year]
        if track.genre:
            audio["\xa9gen"] = [track.genre]
        if art:
            fmt = MP4Cover.FORMAT_JPEG if art[:3] == b"\xff\xd8\xff" else MP4Cover.FORMAT_PNG
            audio["covr"] = [MP4Cover(art, imageformat=fmt)]
        audio.save()

    @staticmethod
    def _tag_flac(path: Path, track: Track, art: Optional[bytes]) -> None:
        audio = mutagen.File(path)
        audio["title"] = track.title
        audio["artist"] = "; ".join(track.artists) or track.artist_str
        audio["album"] = track.album or "Single"
        if track.year:
            audio["date"] = track.year
        if art:
            p = mutagen.flac.Picture()
            p.type = 3
            p.mime = "image/jpeg" if art[:3] == b"\xff\xd8\xff" else "image/png"
            p.data = art
            audio.add_picture(p)
        audio.save()


# ----------------------------------------------------------------------------
# Stage 6: Output / job state
# ----------------------------------------------------------------------------

class JobState:
    """JSON-backed record of every job: resume, sync, and dedupe."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {"jobs": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    def get(self, key: str) -> Optional[dict]:
        return self.data["jobs"].get(key)

    def set(self, key: str, job: dict) -> None:
        self.data["jobs"][key] = job
        self.save()

    def all_jobs(self) -> dict:
        return self.data["jobs"]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


def safe(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip().strip(".")
    return s[:120] or "_"


class OutputOrganizer:
    def __init__(self, root: Path, flat: bool = False, template: str = None):
        self.root = root
        self.flat = flat
        self.template = template or "{nn}. {title}"

    def dest_for(self, track: Track, ext: str) -> Path:
        if self.flat:
            return self.root / f"{safe(track.artist_str)} - {safe(track.title)}{ext}"
        album = track.album or "Singles"
        name = self.template.format(
            artist=safe(track.artist_str), album=safe(album),
            nn=f"{track.track_number:02d}" if track.track_number else "01",
            title=safe(track.title),
        )
        return self.root / safe(track.artist_str) / f"{name}{ext}"


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

class GetDatJawn:
    def __init__(self, args):
        self.args = args
        self.state = JobState(Path(args.state).expanduser())
        self.resolver = Resolver()
        self.matcher = Matcher(args.max_candidates)
        self._processor: Optional[Processor] = None
        self.organizer = OutputOrganizer(Path(args.output).expanduser(), args.flat)
        self.tagger = Tagger(Path(args.output).expanduser() / ".artwork")
        self.tmp = Path(args.output).expanduser() / ".getdatjawn-tmp"

    @property
    def processor(self) -> Processor:
        if self._processor is None:
            self._processor = Processor(self.args.format, self.args.bitrate, not self.args.no_normalize)
        return self._processor

    def job_key(self, url: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", url)[:200]

    def run_url(self, url: str) -> Path:
        key = self.job_key(url)
        prev = self.state.get(key)
        if prev and prev.get("status") == "done" and not self.args.force:
            log.info("✓ already done: %s -> %s", url, prev.get("output"))
            return Path(prev["output"])
        if prev and prev.get("status") == "failed" and not self.args.retry_failed:
            log.info("✗ previously failed (use --retry-failed): %s", url)
            return None

        job = {"url": url, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "running"}
        self.state.set(key, job)
        try:
            # 1) Resolve
            log.info("🎧 resolving: %s", url)
            track = self.resolver.resolve(url)
            log.info("   %s - %s (%s)", track.artist_str, track.title, track.source)
            # 2) Match
            log.info("🔎 matching on YouTube...")
            best = self.matcher.match(track)
            log.info("   picked: %s (https://youtu.be/%s)", best.get("title"), best.get("id"))
            # 3) Download
            log.info("⬇  downloading...")
            dl = Downloader(self.tmp)
            raw = dl.download(best["id"])
            # 4) Process
            ext = Processor.CODECS[self.args.format][1]
            dest = self.organizer.dest_for(track, ext)
            log.info("🎛  converting to %s...", dest.name)
            self.processor.process(raw, dest)
            raw.unlink(missing_ok=True)
            # 5) Tag
            log.info("🏷  tagging...")
            self.tagger.tag(dest, track, best)
            # 6) Output + state
            job.update({"status": "done", "output": str(dest),
                        "track": track.to_dict(),
                        "video": {"id": best.get("id"), "title": best.get("title")},
                        "finished": time.strftime("%Y-%m-%dT%H:%M:%S")})
            self.state.set(key, job)
            log.info("✅ done: %s", dest)
            return dest
        except Exception as e:
            job.update({"status": "failed", "error": str(e)})
            self.state.set(key, job)
            log.error("❌ %s: %s", url, e)
            if self.args.debug:
                raise
            return None

    def cmd_sync(self) -> None:
        """Re-check done jobs; re-fetch any whose output file vanished."""
        for key, job in list(self.state.all_jobs().items()):
            if job.get("status") != "done":
                continue
            out = Path(job["output"])
            if out.exists():
                continue
            log.info("🔄 missing file, re-syncing: %s", job["url"])
            self.state.set(key, {**job, "status": "queued"})
            self.args.retry_failed = True
            self.run_url(job["url"])

    def cmd_resume(self, only_failed: bool = False) -> None:
        for key, job in list(self.state.all_jobs().items()):
            if only_failed and job.get("status") != "failed":
                continue
            if job.get("status") in ("failed", "queued"):
                self.args.retry_failed = True
                self.run_url(job["url"])

    def cmd_list(self) -> None:
        jobs = self.state.all_jobs()
        if not jobs:
            print("no jobs yet.")
            return
        for url, job in jobs.items():
            mark = {"done": "✅", "failed": "❌", "running": "⏳", "queued": "🕘"}.get(job["status"], "?")
            print(f"{mark} [{job['status']:7s}] {job.get('url')}")
            if job["status"] == "done":
                print(f"         -> {job.get('output')}")
            if job.get("error"):
                print(f"         !  {job['error']}")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="getdatjawn",
        description="Grab a tagged local audio file from a Spotify / Apple Music / Tidal / YouTube Music URL.")
    ap.add_argument("urls", nargs="*", help="track URL(s) to process")
    ap.add_argument("-o", "--output", default="~/Music/getdatjawn", help="output root (default ~/Music/getdatjawn)")
    ap.add_argument("-f", "--format", default="mp3", choices=list(Processor.CODECS), help="audio format (default mp3)")
    ap.add_argument("-b", "--bitrate", default="320k", help="bitrate for lossy formats (default 320k)")
    ap.add_argument("--no-normalize", action="store_true", help="skip loudness normalization")
    ap.add_argument("--flat", action="store_true", help="no artist/album folders; drop files in output root")
    ap.add_argument("--max-candidates", type=int, default=10, help="YouTube candidates to rank (default 10)")
    ap.add_argument("--state", default=DEFAULT_STATE, help=f"job state file (default {DEFAULT_STATE})")
    ap.add_argument("--force", action="store_true", help="re-process even if already done")
    ap.add_argument("--retry-failed", action="store_true", help="retry jobs previously marked failed")
    ap.add_argument("-v", "--debug", action="store_true", help="verbose logging")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("resume", help="retry failed/queued jobs from state")
    sub.add_parser("sync", help="re-download any done job whose file is missing")
    sub.add_parser("list", help="show job history")
    sub.add_parser("install", help="install so `getdatjawn`/`jawn` run from any terminal (auto-PATH)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(message)s")

    # subcommand mode
    if args.command == "list":
        GetDatJawn(args).cmd_list(); return 0
    if args.command == "sync":
        GetDatJawn(args).cmd_sync(); return 0
    if args.command == "resume":
        args.retry_failed = True
        GetDatJawn(args).cmd_resume(); return 0

    if args.command == "install":
        return _install_cmd([])

    if not args.urls:
        ap.print_help()
        return 1

    jawner = GetDatJawn(args)
    rc = 0
    for url in args.urls:
        if jawner.run_url(url) is None:
            rc = 1
    return rc




# ----------------------------------------------------------------------------
# Installer: makes `getdatjawn` (or `jawn`) work from any terminal
# ----------------------------------------------------------------------------

class Installer:
    """Copies the tool to a stable directory and puts it on the user PATH."""

    @staticmethod
    def install(symlink_src: Path) -> None:
        if sys.platform.startswith("win"):
            Installer._windows(symlink_src)
        else:
            Installer._unix(symlink_src)

    # -- Windows ---------------------------------------------------------------

    @staticmethod
    def _windows(src: Path) -> None:
        import winreg
        import ctypes

        dest_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "getdatjawn"
        dest_dir.mkdir(parents=True, exist_ok=True)
        py = src.read_text()
        (dest_dir / "getdatjawn.py").write_text(py)
        script_path = dest_dir / "getdatjawn.py"
        launcher = dest_dir / "getdatjawn.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n')
        # optional short alias: jawn.cmd
        (dest_dir / "jawn.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n')

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            try:
                cur, typ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                cur, typ = "", winreg.REG_EXPAND_SZ
            entries = [e for e in (cur or "").split(";") if e]
            if str(dest_dir).lower() not in [e.lower() for e in entries]:
                entries.append(str(dest_dir))
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(entries))
        finally:
            winreg.CloseKey(key)

        # Tell already-open apps (explorer, new terminals) about the PATH change.
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                                                 "Environment", SMTO_ABORTIFHUNG, 5000, None)
        print(f"✅ installed to {dest_dir}")
        print(f"   `getdatjawn` (and alias `jawn`) will work in NEW PowerShell/CMD windows.")
        print(f"   Current window: close and reopen, or run refreshenv / restart the terminal.")

    # -- macOS / Linux ----------------------------------------------------------

    @staticmethod
    def _unix(src: Path) -> None:
        dest_dir = Path.home() / ".local" / "bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        script = dest_dir / "getdatjawn"
        script.write_text(src.read_text())
        script.chmod(0o755)
        # short alias
        jawn = dest_dir / "jawn"
        jawn.write_text(src.read_text())
        jawn.chmod(0o755)

        path_val = os.environ.get("PATH", "")
        if str(dest_dir) not in path_val.split(":"):
            shell = os.environ.get("SHELL", "")
            rc = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
            with open(rc, "a") as f:
                f.write(f'\n# getdatjawn\nexport PATH="$HOME/.local/bin:$PATH"\n')
            print(f"✅ installed to {dest_dir}; added PATH line to {rc}")
        else:
            print(f"✅ installed to {dest_dir} (already on PATH)")
        print("   `getdatjawn` and `jawn` work in new shells.")


def _install_cmd(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="getdatjawn install",
                                 description="Install getdatjawn so it works from any terminal.")
    ap.add_argument("--src", default=None, help="path to getdatjawn.py (default: this file)")
    args = ap.parse_args(argv)
    src = Path(args.src).expanduser() if args.src else Path(__file__).resolve()
    if not src.exists():
        print(f"getdatjawn install: source not found: {src}", file=sys.stderr)
        return 1
    Installer.install(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
