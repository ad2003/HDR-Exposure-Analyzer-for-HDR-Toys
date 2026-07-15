# hdr-exposure-analyzer

Ever wondered why some HDR movies look great on your display while others
are frustratingly dark — and what number to actually type into your config?

This tool measures how dark an HDR10 movie's master really is, **relative to
a reference movie that already looks perfect on your setup**, and tells you
the exact `auto_exposure_limit_postive` value to use with
[hdr-toys](https://github.com/natural-harmonia-gropius/hdr-toys) (astra
tone mapping) in mpv.

It replicates astra's own metering math offline (PQ-domain luma,
center-weighted averaging, Jzazbz anchor derivation), samples only keyframes
for speed, strips hard-coded letterbox bars, and separates *intentionally
dark* scenes (space, night, fades) from *lit* scenes — because a suggestion
should fix underexposed corridors, not "rescue" outer space.

```
Lit median:       5.39 EV  (lit scenes >=1 nit = 44% of samples)
Reference delta:  +2.85 EV vs. your reference movie
Suggestion:       auto_exposure_limit_postive=2.9
```

A ~2 h 4K remux analyzes in a few minutes (seconds-per-minute territory
with `--hwaccel`).

## Requirements

- Python 3.8+ with `numpy` (`pip install numpy`)
- `ffmpeg` and `ffprobe` (5.1+) in PATH — or simply place `ffmpeg.exe` and
  `ffprobe.exe` next to the script
- mpv with hdr-toys / astra (for actually *using* the suggested value)

## How to use

Follow these steps in order — step 2 only ever happens once.

1. **Know your display peak.** Use the same nits value you set as
   `reference_white` in your hdr-toys config (e.g. `431`). Every run
   requires it via `--white`.

2. **Calibrate your reference (one time).** Pick one HDR10 movie that
   already looks *perfectly exposed* on your setup — well-lit, no tweaking
   needed. Run:

   ```
   python hdr_exposure_analyzer.py "Reference Movie.mkv" --white 431
   ```

   Without `--ref-median` the tool runs in calibration mode and prints your
   personal reference value, e.g. `lit median = 2.54`. Write it down.

3. **Analyze any movie against your reference:**

   ```
   python hdr_exposure_analyzer.py "Some Dark Movie.mkv" --white 431 --ref-median 2.54 --hwaccel
   ```

   The report ends with a `Suggestion:` line — that number is the
   `auto_exposure_limit_postive` value for *this* movie.

4. **Apply the value in mpv.** Either put it in your config for the session,
   or better: bind a runtime hotkey (see `exposure_control.lua` in this
   repo) and dial the suggested value in while watching. Sanity-check with
   your eyes: shadow structures visible, blacks still black, highlights not
   clipping.

5. **Optional — drag & drop:** save this next to the script as
   `analyze.bat` (Windows), then drop one or more files onto it:

   ```bat
   @echo off
   cd /d "%~dp0"
   for %%F in (%*) do (
       python "%~dp0hdr_exposure_analyzer.py" "%%~F" --white 431 --ref-median 2.54 --hwaccel
   )
   pause
   ```

   Each run also writes `<movie>.hdr_analysis.txt` plus one line into
   `hdr_analysis_log.txt` — over time that log becomes a comparison table
   of how bright different masters are graded.

## How it works

One sequential ffmpeg pass decodes **keyframes only** (`-skip_frame nokey`,
~1 % of all frames), downscales each to a tiny luma thumbnail and pipes it
to Python. Per sample, the script strips hard-coded black bars, applies
astra's center-weighted averaging (tan-distorted grid, identical to
`map_coords`), converts the PQ average to absolute nits and computes the EV
distance to astra's anchor (derived via Jzazbz from your `--white`, exactly
like `get_ev()`). The suggestion is simply
`lit_median(movie) − lit_median(reference)` (+ optional `--base-limit`
offset). Because both sides are measured with the same method, systematic
approximation bias largely cancels out.

## Options

| Flag | Meaning |
|---|---|
| `--white NITS` | your display peak / `reference_white` (**required**) |
| `--ref-median EV` | your calibrated reference value; omit to calibrate |
| `--base-limit EV` | offset added to the suggestion (default 0) |
| `--anchor` | `auto_exposure_anchor` if you changed it (default 0.6) |
| `--hwaccel` | GPU decoding — big speedup on 4K HEVC |
| `--precise` | fixed time grid instead of keyframes (slow; automatic fallback exists) |
| `--interval` | sample spacing in `--precise` mode (default 10 s) |
| `--no-crop` | disable letterbox removal |
| `--start` / `--duration` | analyze a time window (diagnostics) |
| `--no-log` | don't write report/log files |

## Limitations (honest edition)

- **HDR10/PQ input only.** SDR content goes through mpv's inverse tone
  mapping, not astra — the tool warns and the numbers don't apply.
- **Dolby Vision profile 5 is unusable** (IPT signal, no HDR10 layer);
  the tool detects and refuses it. Profiles 7/8 work (HDR10 base layer).
- Measures the video's Y' (non-constant-luminance luma) instead of
  PQ(Y of linear RGB), skips astra's temporal smoothing, and samples at
  keyframe positions. All of this cancels in the *relative* comparison
  but makes absolute numbers approximate.
- The suggestion is a **starting point**, not gospel. Movies that are
  mostly dark by intent get a warning (`lit scenes < 40%`) — trust your
  eyes for the final 0.5 EV.

## License

MIT
