#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hdr_exposure_analyzer.py

Analyzes an HDR10/PQ movie (mkv/mp4) and estimates how many stops (EV)
its master sits below astra's exposure anchor, relative to a reference
movie of your choice. From that it derives a suggested value for
hdr-toys' auto_exposure_limit_postive parameter.

The measurement replicates astra's metering (hdr-toys, tone-mapping/astra.glsl):
  - intensity = PQ-encoded luma (absolute, 10000-nit scale)
  - center-weighted average via tan distortion (strength=2.0,
    identical to map_coords in the shader, incl. kaleidoscope mirroring)
  - anchor derivation via Jzazbz lightness, identical to get_ev()

Approximation, not exactness: we read the video's Y' signal
(non-constant-luminance luma) instead of PQ(Y of linear RGB) and skip
temporal smoothing. For an EV estimate within ~0.1-0.2 that is plenty --
and since the reference is measured the same way, systematic bias
largely cancels in the difference.

Requires: ffmpeg + ffprobe in PATH, numpy.

Examples:
  python hdr_exposure_analyzer.py movie.mkv --white 431
  python hdr_exposure_analyzer.py movie.mkv --white 431 --ref-median 2.54
  python hdr_exposure_analyzer.py movie.mkv --white 431 --no-crop
"""

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np

# ---------------------------------------------------------------- PQ / Jzazbz
# Constants exactly as in astra.glsl
M1 = 2610.0 / 4096.0 / 4.0
M2 = 2523.0 / 4096.0 * 128.0
C1 = 3424.0 / 4096.0
C2 = 2413.0 / 4096.0 * 32.0
C3 = 2392.0 / 4096.0 * 32.0
PW = 10000.0
M2_Z = 1.7 * M2
D = -0.56
D0 = 1.6295499532821566e-11


def pq_eotf(x):
    """PQ signal [0,1] -> nits."""
    t = np.power(np.maximum(x, 0.0), 1.0 / M2)
    return np.power(np.maximum(t - C1, 0.0) / (C2 - C3 * t), 1.0 / M1) * PW


def iz_eotf_inv(nits):
    t = (nits / PW) ** M1
    return ((C1 + C2 * t) / (1.0 + C3 * t)) ** M2_Z


def iz_eotf(iz):
    t = iz ** (1.0 / M2_Z)
    return (max(t - C1, 0.0) / (C2 - C3 * t)) ** (1.0 / M1) * PW


def anchor_nits(reference_white, anchor):
    """Replicates get_ev(): anchor via Jz lightness from reference_white."""
    ref_iz = iz_eotf_inv(reference_white)
    ref_j = ((1.0 + D) * ref_iz) / (1.0 + D * ref_iz) - D0
    a_j = anchor * ref_j
    a_iz = (a_j + D0) / (1.0 + D - D * (a_j + D0))
    return iz_eotf(a_iz)


# ------------------------------------------------------ Center-weight grid
def build_sample_grid(w=512, h=288, strength=2.0):
    """map_coords() from astra.glsl: uv grid -> distorted sample coordinates."""
    ys, xs = np.mgrid[0:h, 0:w]
    uv = np.stack([(xs + 0.5) / w, (ys + 0.5) / h], axis=-1)
    c = uv - 0.5
    r = np.linalg.norm(c, axis=-1)
    r_safe = np.where(r == 0.0, 1.0, r)
    dr = np.tan(np.minimum(r * strength, 1.55)) / strength  # clamp near tan pole
    d = c / r_safe[..., None] * dr[..., None]
    d = d / max(strength, 1.0)
    duv = d + 0.5
    # kaleidoscope mirroring into [0,1]; GLSL fract(x) = x - floor(x)
    fr = duv * 0.5 - np.floor(duv * 0.5)
    k = 1.0 - np.abs(fr * 2.0 - 1.0)
    return k  # (h, w, 2) in [0,1]


def weighted_mean(frame, grid):
    """Bilinear sampling of the frame at grid coordinates, then mean."""
    fh, fw = frame.shape
    x = grid[..., 0] * (fw - 1)
    y = grid[..., 1] * (fh - 1)
    x0 = np.clip(x.astype(int), 0, fw - 2)
    y0 = np.clip(y.astype(int), 0, fh - 2)
    fx = x - x0
    fy = y - y0
    v = (frame[y0, x0] * (1 - fx) * (1 - fy)
         + frame[y0, x0 + 1] * fx * (1 - fy)
         + frame[y0 + 1, x0] * (1 - fx) * fy
         + frame[y0 + 1, x0 + 1] * fx * fy)
    return float(v.mean())


def strip_bars(frame, thr=0.02):
    """Removes contiguous black border rows/columns (hard-coded letterbox/
    pillarbox). Returns (content, removed area fraction); None if the frame
    is essentially all black (fade/credits)."""
    h, w = frame.shape
    rm = frame.mean(axis=1)
    cm = frame.mean(axis=0)
    t = 0
    while t < h and rm[t] < thr:
        t += 1
    b = h - 1
    while b > t and rm[b] < thr:
        b -= 1
    l = 0
    while l < w and cm[l] < thr:
        l += 1
    r = w - 1
    while r > l and cm[r] < thr:
        r -= 1
    if (b - t + 1) < h * 0.3 or (r - l + 1) < w * 0.3:
        return None, 1.0
    content = frame[t:b + 1, l:r + 1]
    removed = 1.0 - content.size / frame.size
    return content, removed


# ------------------------------------------------------------------- ffprobe
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path):
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}\n"
                 "(Tip: drag & drop the file into the terminal to get the full path)")
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path])
    info = json.loads(r.stdout or "{}")
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"
            and s.get("disposition", {}).get("attached_pic", 0) == 0]
    if not vids:
        sys.exit("No video stream found.")
    # pick the base layer: largest resolution; prefer PQ on ties
    vids_sorted = sorted(
        vids, key=lambda s: (int(s.get("width", 0)) * int(s.get("height", 0)),
                             s.get("color_transfer") == "smpte2084"),
        reverse=True)
    v = vids_sorted[0]
    v_index = vids.index(v)  # index among video streams (for -map 0:v:N)
    dovi = None
    for sd in v.get("side_data_list", []) or []:
        if "DOVI" in str(sd.get("side_data_type", "")).upper() or "dv_profile" in sd:
            dovi = sd.get("dv_profile")
    return {
        "width": int(v.get("width", 0)),
        "height": int(v.get("height", 0)),
        "transfer": v.get("color_transfer", "?"),
        "primaries": v.get("color_primaries", "?"),
        "pix_fmt": v.get("pix_fmt", "?"),
        "range": v.get("color_range", "tv"),
        "duration": float(info.get("format", {}).get("duration", 0.0)),
        "v_index": v_index,
        "n_video": len(vids),
        "dovi_profile": dovi,
    }


def probe_hdr_metadata(path):
    """MaxCLL/FALL + mastering display from the first frame's side data."""
    r = run(["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-read_intervals", "%+#1", "-print_format", "json",
             "-show_frames", "-show_entries", "frame=side_data_list", path])
    out = {"max_cll": None, "max_fall": None, "md_max": None, "md_min": None}
    try:
        frames = json.loads(r.stdout).get("frames", [])
        for sd in frames[0].get("side_data_list", []):
            t = sd.get("side_data_type", "")
            if "Content light" in t:
                out["max_cll"] = sd.get("max_content")
                out["max_fall"] = sd.get("max_average")
            if "Mastering display" in t:
                for key, field in (("md_max", "max_luminance"), ("md_min", "min_luminance")):
                    raw = sd.get(field)
                    if raw is not None:
                        m = re.match(r"(\d+)(?:/(\d+))?", str(raw))
                        if m:
                            num = float(m.group(1))
                            den = float(m.group(2) or 1)
                            out[key] = num / den
    except (IndexError, KeyError, json.JSONDecodeError):
        pass
    return out


# ------------------------------------------------------------------ Sampling
def sample_frames(path, interval, crop, size=(256, 144), fast=True,
                  hwaccel=False, duration=0.0, progress=True, v_index=0,
                  start=0.0, window=0.0):
    """Generator over Y' frames (float [0,1], PQ signal), TV-range corrected.

    fast=True: decodes keyframes only (-skip_frame nokey).
    fast=False: precise time grid via fps filter (decodes everything).
    Progress via ffmpeg -progress on stderr (out_time vs. duration).
    """
    vf = []
    if crop:
        vf.append(f"crop={crop}")
    if not fast:
        vf.append(f"fps=1/{interval}")
    vf += ["extractplanes=y", f"scale={size[0]}:{size[1]}:flags=area",
           "format=gray16le"]

    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-progress", "pipe:2"]
    if hwaccel:
        cmd += ["-hwaccel", "auto"]
    if fast:
        cmd += ["-skip_frame", "nokey"]
    if start > 0:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if window > 0:
        cmd += ["-t", str(window)]
    cmd += ["-map", f"0:v:{v_index}",
            "-vf", ",".join(vf), "-fps_mode", "passthrough",
            "-f", "rawvideo", "-"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    state = {"t": 0.0, "err": []}

    def drain_stderr():
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    state["t"] = int(line.split("=")[1]) / 1e6
                except ValueError:
                    pass
            elif line and "=" not in line:
                state["err"].append(line)

    th = threading.Thread(target=drain_stderr, daemon=True)
    th.start()

    nbytes = size[0] * size[1] * 2
    n = 0
    last_draw = 0.0
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        n += 1
        f = np.frombuffer(buf, dtype="<u2").reshape(size[1], size[0]).astype(np.float64)
        # gray16 from 10-bit: values << 6. Limited range: 64..940 (10 bit)
        f = (f / 64.0 - 64.0) / (940.0 - 64.0)
        yield np.clip(f, 0.0, 1.0)

        now = time.time()
        if progress and duration > 0 and now - last_draw > 0.25:
            last_draw = now
            frac = min(state["t"] / duration, 1.0)
            bar = "#" * int(frac * 30)
            sys.stdout.write(f"\r  [{bar:<30}] {frac*100:5.1f}%   {n} samples ")
            sys.stdout.flush()

    proc.stdout.close()
    proc.wait()
    th.join(timeout=1.0)
    if progress and duration > 0:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    if state["err"]:
        print(f"[ffmpeg] {' | '.join(state['err'])[-400:]}", file=sys.stderr)


# ----------------------------------------------------------------- Reporting
def ascii_histogram(values, lo, hi, bins=12, width=28):
    counts, edges = np.histogram(np.clip(values, lo, hi), bins=bins, range=(lo, hi))
    peak = max(counts.max(), 1)
    lines = []
    for c, e0, e1 in zip(counts, edges[:-1], edges[1:]):
        bar = "#" * int(round(c / peak * width))
        lines.append(f"  {e0:5.1f}-{e1:5.1f} EV |{bar:<{width}}| {c}")
    return "\n".join(lines)


# --------------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser(
        description="astra-compatible exposure analysis for HDR10 movies "
                    "(companion tool for mpv + hdr-toys)")
    ap.add_argument("file")
    ap.add_argument("--white", type=float, required=True, metavar="NITS",
                    help="your display peak / reference_white in nits, same value "
                         "as in your hdr-toys config (required)")
    ap.add_argument("--ref-median", type=float, default=None, metavar="EV",
                    help="lit-median EV of your reference movie (from a previous "
                         "run). Omit on first run to calibrate.")
    ap.add_argument("--base-limit", type=float, default=0.0, metavar="EV",
                    help="offset added to the suggestion (default 0 = suggestion "
                         "equals the pure master difference)")
    ap.add_argument("--anchor", type=float, default=0.6,
                    help="auto_exposure_anchor (default 0.6 = astra default)")
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between samples in --precise mode (default 10)")
    ap.add_argument("--no-crop", action="store_true",
                    help="disable hard-coded letterbox removal")
    ap.add_argument("--precise", action="store_true",
                    help="fixed time grid instead of keyframes (slow, decodes everything)")
    ap.add_argument("--hwaccel", action="store_true",
                    help="try hardware decoding (-hwaccel auto)")
    ap.add_argument("--no-log", action="store_true",
                    help="do not write report/log files")
    ap.add_argument("--start", type=float, default=0.0,
                    help="analyze from second X (diagnostics)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="analyze only Y seconds (diagnostics)")
    args = ap.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ffmpeg/ffprobe not found in PATH.")

    meta = probe(args.file)
    if meta["transfer"] not in ("smpte2084", "?"):
        print(f"WARNING: transfer={meta['transfer']} -- not PQ/HDR10 material. "
              f"This analysis targets the astra chain (SDR goes through ITM).")
    hdr = probe_hdr_metadata(args.file)

    crop = None  # bars are removed per frame in numpy (see strip_bars)

    a_nits = anchor_nits(args.white, args.anchor)
    grid = build_sample_grid()

    title = os.path.splitext(os.path.basename(args.file))[0]
    L = []
    L.append(f"File:             {args.file}")
    L.append(f"Resolution:       {meta['width']}x{meta['height']}  "
             f"({meta['transfer']}/{meta['primaries']}, {meta['pix_fmt']})")
    if meta["n_video"] > 1:
        L.append(f"Video streams:    {meta['n_video']} -- analyzing v:{meta['v_index']} "
                 f"(largest resolution / base layer)")
    if meta["dovi_profile"] is not None:
        L.append(f"Dolby Vision:     profile {meta['dovi_profile']}")
        if meta["dovi_profile"] == 5:
            L.append("WARNING:          DV profile 5 (IPT, no HDR10 layer) -- "
                     "the Y' signal is NOT PQ luma here, results are unusable!")
    L.append(f"Duration:         {meta['duration']/60:.0f} min")
    if hdr["md_max"]:
        L.append(f"Mastering peak:   {hdr['md_max']:.0f} nits  (min {hdr['md_min']:.4f})")
    L.append(f"MaxCLL / MaxFALL: {hdr['max_cll'] or '-'} / {hdr['max_fall'] or '-'} nits")
    L.append(f"Anchor target:    {a_nits:.1f} nits  (anchor={args.anchor}, reference_white={args.white:.0f})")
    print("\n" + "\n".join(L))
    win = f", window {args.start:.0f}s+{args.duration:.0f}s" if args.duration > 0 else ""
    print(f"Sampling:         {'keyframes (fast)' if not args.precise else f'1 frame / {args.interval:g}s'}{win} ...",
          flush=True)

    def collect(fast):
        evs, avgs, bars, raw = [], [], [], 0
        eff_dur = args.duration if args.duration > 0 else meta["duration"] - args.start
        for frame in sample_frames(args.file, args.interval, crop, fast=fast,
                                   hwaccel=args.hwaccel, duration=eff_dur,
                                   v_index=meta["v_index"],
                                   start=args.start, window=args.duration):
            raw += 1
            if args.no_crop:
                content, removed = frame, 0.0
            else:
                content, removed = strip_bars(frame)
                if content is None:
                    continue  # essentially black frame
            avg_i = weighted_mean(content, grid)
            avg_n = float(pq_eotf(avg_i))
            if avg_n <= 0.01:
                continue  # skip black frames
            avgs.append(avg_n)
            bars.append(removed)
            evs.append(np.log2(a_nits / avg_n))
        return np.array(evs), np.array(avgs), np.array(bars), raw

    t0 = time.time()
    evs, avgs, bars, raw = collect(fast=not args.precise)
    if len(evs) < 10 and not args.precise:
        print(f"Only {raw} keyframe samples -- falling back to precise mode ...", flush=True)
        evs, avgs, bars, raw = collect(fast=False)
    if len(evs) < 10:
        sys.exit(f"Too few usable samples ({raw} decoded, {len(evs)} bright enough).")
    dt = time.time() - t0

    pos = np.maximum(evs, 0.0)
    med, p25, p75, p90 = np.percentile(pos, [50, 25, 75, 90])
    above_anchor = float((evs <= 0).mean() * 100)
    # Lit median: lit scenes only (>=1 nit). Intentionally (near-)black
    # scenes -- space, night, fades -- must not dominate the suggestion;
    # the auto exposure is not supposed to "rescue" them anyway.
    lit_mask = avgs >= 1.0
    lit = pos[lit_mask] if lit_mask.any() else pos
    lit_med = float(np.median(lit))
    lit_share = float(lit_mask.mean() * 100)
    if args.ref_median is not None:
        delta = lit_med - args.ref_median
        suggestion = max(round(args.base_limit + delta, 1), 0.0)
    else:
        delta = suggestion = None

    R = []
    R.append(f"Samples:          {len(evs)}  ({dt:.0f}s analysis, "
             f"{raw - len(evs)} black frames skipped)")
    R.append(f"Frame average:    median {np.median(avgs):.1f} nits   "
             f"p5 {np.percentile(avgs,5):.1f} / p95 {np.percentile(avgs,95):.1f}")
    if not args.no_crop and len(bars) and np.median(bars) > 0.02:
        R.append(f"Letterbox:        ~{np.median(bars)*100:.0f}% of the frame detected as "
                 f"hard-coded bars and excluded (content-only measurement)")
    R.append(f"Scenes above anchor (no lift needed): {above_anchor:.0f}%")
    R.append("")
    R.append("Lift needed toward anchor (EV):")
    R.append(f"                  median {med:.2f}   p25 {p25:.2f}   p75 {p75:.2f}   p90 {p90:.2f}   max {pos.max():.2f}")
    R.append("")
    R.append("Distribution:")
    R.append(ascii_histogram(pos, 0.0, 8.0))
    R.append("")
    R.append(f"Lit median:       {lit_med:.2f} EV  (lit scenes >=1 nit = {lit_share:.0f}% of samples;")
    R.append(f"                   overall median {med:.2f} EV incl. intentionally dark scenes)")
    if suggestion is not None:
        R.append(f"Reference delta:  {delta:+.2f} EV vs. your reference movie "
                 f"(lit-median {args.ref_median}, base {args.base_limit})")
        R.append(f"Suggestion:       auto_exposure_limit_postive={suggestion}")
        R.append(f"                  = base {args.base_limit} {'+' if delta>=0 else '-'} {abs(delta):.2f} EV master difference")
        if lit_share < 40:
            R.append(f"NOTE:             Only {lit_share:.0f}% lit scenes -- this movie is mostly "
                     f"dark by intent; treat the suggestion conservatively.")
        if abs(delta) < 0.3:
            R.append("Verdict:          Mastered about as bright as your reference -- baseline is fine.")
        elif delta > 1.5:
            R.append("Verdict:          Considerably darker master.")
        elif delta > 0.3:
            R.append("Verdict:          Noticeably darker than your reference.")
        else:
            R.append("Verdict:          Brighter master than your reference -- baseline is plenty.")
    else:
        R.append("")
        R.append("No reference set (--ref-median). CALIBRATION MODE:")
        R.append("  If this movie looks perfectly exposed on your setup, its lit")
        R.append(f"  median is your personal reference. From now on, run:")
        R.append(f"      --white {args.white:g} --ref-median {lit_med:.2f}")
        R.append("  Future runs will then suggest the limit_postive value that")
        R.append("  makes other movies' lit scenes match this reference.")

    lit_nits_med = float(np.median(avgs[lit_mask])) if lit_mask.any() else 0.0
    if lit_nits_med < 2.0:
        R.append("")
        R.append("WARNING:          Even lit scenes measure <2 nits median --")
        R.append("                  implausible for real movie content.")
        R.append("                  Possible causes: wrong stream (DV enhancement")
        R.append("                  layer), DV profile 5, or an exotic encode.")
        R.append("                  Check the header above (resolution/pix_fmt/DV profile).")

    print("\n".join(R))

    if not args.no_log:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        report_path = os.path.join(script_dir, f"{title}.hdr_analysis.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n\n" + "\n".join(R) + "\n")
        log_path = os.path.join(script_dir, "hdr_analysis_log.txt")
        newlog = not os.path.exists(log_path)
        sug_str = f"{suggestion:9.1f}" if suggestion is not None else "        -"
        with open(log_path, "a", encoding="utf-8") as f:
            if newlog:
                f.write(f"{'Movie':<40} {'litEV':>6} {'medEV':>6} {'p90EV':>6} "
                        f"{'medNits':>8} {'suggest':>9}\n")
            f.write(f"{title[:40]:<40} {lit_med:6.2f} {med:6.2f} {p90:6.2f} "
                    f"{np.median(avgs):8.1f} {sug_str}\n")
        print(f"\nReport:           {report_path}")
        print(f"Comparison log:   {log_path}")


if __name__ == "__main__":
    main()
