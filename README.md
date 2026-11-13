# getdatjawn

Grab a track from a Spotify, YouTube Music, Apple Music, or Tidal URL and get a
properly tagged local audio file out the other end.

```
Spotify URL
   ↓
Resolver (Spotify / YouTube Music / Apple Music / Tidal) ──► track metadata
   ↓
Matcher ───────────► searches YouTube and ranks candidate videos
   ↓
Downloader ────────► retrieves the chosen media (yt-dlp)
   ↓
Processor ─────────► FFmpeg converts / normalizes audio
   ↓
Tagger ────────────► writes metadata and embedded artwork (mutagen)
   ↓
Output ────────────► organized audio files + job state
```

## Install

```bash
pip install getdatjawn
```

That gives you both `getdatjawn` and the short alias `jawn` on your PATH,
everywhere (pip generates the launchers for you, Windows included). You still
need `ffmpeg` on your system:

```bash
# Windows:  winget install ffmpeg      macOS:  brew install ffmpeg
# Debian/Ubuntu:  sudo apt install ffmpeg
```

**No pip / from source (still one step, any device):** clone or download this
repo, then run `python getdatjawn.py install` (Windows: double-click
`install.bat`). The installer copies the tool to a stable bin directory and
adds it to your **user PATH** automatically:

```powershell
# Windows: double-click install.bat, or run:
python getdatjawn.py install
```

```bash
# macOS / Linux:
python3 getdatjawn.py install
```

That's it. The installer:

- **Windows**: copies the tool to `%LOCALAPPDATA%\getdatjawn`, writes
  `getdatjawn.cmd` and `jawn.cmd` launchers that call *your* Python
  interpreter, adds the folder to your **user PATH** via the registry (no
  admin needed), and broadcasts the change so new terminals see it
  immediately. Open a **new** PowerShell window and both
  `getdatjawn` and the short alias `jawn` work from anywhere.
- **macOS / Linux**: copies to `~/.local/bin`, makes it executable, and
  appends a PATH line to `.bashrc`/`.zshrc` if needed. Works in new shells.

Then install the runtime deps (and ffmpeg) once:

```bash
pip install yt-dlp mutagen requests
# ffmpeg:  winget install ffmpeg  (or)  brew install ffmpeg  (or)  sudo apt install ffmpeg
```

Re-running `install` after updating `getdatjawn.py` refreshes the installed
copy (it's idempotent).

**Manual alternative**

- Windows: run `.\getdatjawn.bat` from the folder, or
  `python .\getdatjawn.py <args>`.
- macOS / Linux: `chmod +x getdatjawn.py && ./getdatjawn.py <args>`.

## Usage

Note: `<url>` below means "paste your real URL here", not the literal text.
In PowerShell, always wrap URLs in quotes.

```powershell
getdatjawn "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
getdatjawn "url1" "url2" "url3"                # batch
getdatjawn "url" -f flac                      # mp3 (default), m4a, opus, flac, wav
getdatjawn "url" -b 192k                      # bitrate for lossy formats
getdatjawn "url" --no-normalize               # skip loudness normalization
getdatjawn "url" --flat                       # no artist/album folders
getdatjawn "url" -o ~/Music/library           # custom output root

getdatjawn install                             # auto-PATH install (see above)
getdatjawn list                               # show job history
getdatjawn resume                             # retry failed/queued jobs
getdatjawn sync                               # re-fetch done jobs whose file vanished
```

The short alias `jawn` does the same thing: `jawn "https://..."`.

`list`, `resume`, and `sync` are three separate subcommands, not a pipeline;
run them one at a time.

Output lands as `~/Music/getdatjawn/<Artist>/<NN>. <Title>.mp3` by default
(album folders when the resolver exposes one). Job state is kept at
`~/.config/getdatjawn/state.json` and is used for dedupe (already-done URLs are
skipped), `resume`, and `sync`.

## How each stage works

- **Resolver**: parses the URL to detect the service. Spotify uses the public
  embed-page hydration blob (no API key); Apple Music uses the page's schema.org
  JSON-LD; Tidal uses page/og metadata; YouTube Music resolves via yt-dlp.
  All produce a `Track` with title, artists, album, duration, and artwork URL.
- **Matcher**: searches YouTube (`ytsearch`) and scores candidates on title
  similarity, channel/artist match, duration proximity, and bonuses/penalties
  for keywords like "official audio" vs "cover / sped up / nightcore".
- **Downloader**: yt-dlp `bestaudio` into a temp dir.
- **Processor**: ffmpeg encode to the chosen format, with optional two-pass
  loudness normalization to -14 LUFS for lossy formats.
- **Tagger**: mutagen writes title/artist/album/date/track and embeds the
  artwork for MP3 (ID3v2.3), M4A, and FLAC.
- **Output / state**: organized paths plus a JSON job ledger for dedupe,
  resume, and sync.

## Notes

- Spotify/Apple/Tidal resolvers need no accounts or API keys, but they scrape
  public pages, so they can break if the services change their markup.
- Only download content you have the right to download; respect the terms of
  the platforms involved.
