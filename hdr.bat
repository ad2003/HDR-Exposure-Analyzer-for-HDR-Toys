@echo off
REM ============================================================
REM  hdr-exposure-analyzer -- drag & drop launcher
REM
REM  Drop one or more HDR10 .mkv/.mp4 files onto this .bat.
REM  Each movie is analyzed in sequence; results are printed
REM  here and written to <movie>.hdr_analysis.txt plus one
REM  summary line in hdr_analysis_log.txt (next to the script).
REM
REM  EDIT THESE VALUES FOR YOUR SETUP:
REM    --white       your display peak in nits (same value as
REM                  reference_white in your hdr-toys config)
REM    --ref-median  your calibrated reference (lit median EV of
REM                  a movie that already looks perfect for you;
REM                  run once WITHOUT this flag to calibrate)
REM    --base-limit  offset added to the suggestion (0 = the
REM                  suggestion equals the pure master difference)
REM    --hwaccel     GPU decoding -- remove if it causes issues
REM ============================================================

cd /d "%~dp0"
for %%F in (%*) do (
    python "%~dp0hdr_exposure_analyzer.py" "%%~F" --white 431 --ref-median 2.54 --base-limit 0 --hwaccel
)
pause