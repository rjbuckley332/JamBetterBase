#!/usr/bin/env python3
"""Jamulus uploader: direct upload to S3 (JamBetter).

Flow:
- Wait for a Jam-* folder to "settle" (no recent writes)
- Build a singer-only leveled mix MP3 (excludes injector-bot)
- Upload WAVs + mix to S3 using friendly names derived from:
  - Session name entered on the website (NAME_MAP_FILE -> jam_key)
  - Jamulus client names (from the WAV filenames)

S3 layout:
  s3://<S3_BUCKET>/<S3_PREFIX>/<YYYY-MM-DD>/<SESSION_NAME>/<FILES>

Filename convention (all uppercase, padded):
  <FOLD6>_<YYMMDD>_<PART4>.wav
  <FOLD6>_<YYMMDD>_MIXL.mp3

Where:
- FOLD6 = first 6 chars of the website session name (A-Z0-9 only, '_' padding)
- PART4 = first 4 chars of the Jamulus client name (A-Z0-9 only, '_' padding)

Env vars:
- AWS_CLI_PATH: path to aws cli (default: /home/nds/.local/bin/aws)
- AWS_REGION: region for aws cli (default: us-east-1)
- S3_BUCKET: bucket name (default: pipedreamers-recordings-prod)
- S3_PREFIX: key prefix (default: vps/vps-0001/recordings)
- RECORDINGS_DIR: local recordings dir (default: /var/lib/jamulus/recordings)
- NAME_MAP_FILE: csv mapping jam_key -> session name (default: /home/nds/recording_name_map.csv)
- SETTLE_SECONDS: age threshold before upload (default: 45)
- POLL_SECONDS: poll interval (default: 20)

Notes:
- Writes marker files in RECORDINGS_DIR/.uploaded/<Jam-...>.done after successful upload.
- Does NOT delete local recordings.
"""

import os
import re
import subprocess
import time
import csv
from datetime import datetime, timedelta
from pathlib import Path


INCLUDE_INJECTOR_FLAG = '/tmp/jamulus_include_injector.flag'
INCLUDE_INJECTOR_MAP_FILE = '/tmp/jamulus_include_injector_map.csv'
METRONOME_TAINT_MAP_FILE = '/tmp/jamulus_metronome_taint_map.csv'


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if isinstance(v, str) and v.strip() else default


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


_JAM_DATE_RE = re.compile(r"^Jam-(\d{4})(\d{2})(\d{2})-")
_JAM_KEY_RE = re.compile(r"^Jam-(\d{8})-(\d{6})")


def jam_folder_date(jam_name: str) -> str:
    m = _JAM_DATE_RE.match(jam_name)
    if not m:
        return ''
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"


def jam_key(jam_name: str) -> str:
    """Return key like YYYYMMDD_HHMMSS for Jam-YYYYMMDD-HHMMSSmmm."""
    m = _JAM_KEY_RE.match(jam_name)
    if not m:
        return ''
    return f"{m.group(1)}_{m.group(2)}"


def sanitize(name: str) -> str:
    name = (name or '').strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[^A-Za-z0-9 _\-]+", "", name)
    return name[:80] if name else "Unnamed"


def _parse_map_key_ts(k: str):
    try:
        return datetime.strptime((k or '').strip(), '%Y%m%d_%H%M%S')
    except Exception:
        return None


def lookup_session_name(map_file: str, key: str) -> tuple[str | None, str | None]:
    """Resolve session name from map.

    Returns (session_name, matched_map_key).
    Tries, in order:
      1) exact key match
      2) ±4h/±5h shifted key match (handles local-vs-UTC mismatch)
      3) nearest timestamp within 6h
    """
    if not map_file or not key:
        return (None, None)
    try:
        if not os.path.exists(map_file):
            return (None, None)
        with open(map_file, newline='', encoding='utf-8') as f:
            rows = list(csv.reader(f))

        clean = []
        for i, row in enumerate(rows):
            if not row or len(row) < 2:
                continue
            k = row[0].strip()
            n = row[1].strip()
            if not k or not n:
                continue
            clean.append((i, k, n))

        # 1) exact, prefer latest
        for _, k, n in reversed(clean):
            if k == key:
                return (n, k)

        target = _parse_map_key_ts(key)

        # 2) shifted exact (timezone mismatch tolerance)
        if target is not None:
            for h in (4, 5, -4, -5):
                shifted = (target + timedelta(hours=h)).strftime('%Y%m%d_%H%M%S')
                for _, k, n in reversed(clean):
                    if k == shifted:
                        return (n, k)

        # 3) nearest within 6h
        if target is not None:
            best = None
            for idx, k, n in clean:
                ts = _parse_map_key_ts(k)
                if ts is None:
                    continue
                diff = abs((ts - target).total_seconds())
                if diff > 6 * 3600:
                    continue
                cand = (diff, idx, k, n)
                if best is None or cand[0] < best[0] or (cand[0] == best[0] and cand[1] > best[1]):
                    best = cand
            if best is not None:
                _, _, k, n = best
                return (n, k)

    except Exception:
        return (None, None)
    return (None, None)


def lookup_bool_map(map_file: str, key: str) -> bool | None:
    """Resolve a boolean setting from a jam_key-indexed CSV map.

    Accepts exact keys and the same ±4h/±5h tolerance used elsewhere.
    """
    if not map_file or not key:
        return None
    try:
        if not os.path.exists(map_file):
            return None
        with open(map_file, newline='', encoding='utf-8') as f:
            rows = list(csv.reader(f))

        def _row_bool(row_key: str) -> bool | None:
            for row in reversed(rows):
                if not row or len(row) < 2:
                    continue
                if (row[0] or '').strip() != row_key:
                    continue
                v = (row[1] or '').strip().lower()
                return v in ('1', 'true', 'yes', 'on')
            return None

        hit = _row_bool(key)
        if hit is not None:
            return hit

        t = _parse_map_key_ts(key)
        if t is not None:
            for h in (4, 5, -4, -5):
                kk = (t + timedelta(hours=h)).strftime('%Y%m%d_%H%M%S')
                hit = _row_bool(kk)
                if hit is not None:
                    return hit
    except Exception:
        return None
    return None


def consume_name(map_file: str, key: str):
    """Remove the first matching key row (queue behavior) after successful upload."""
    try:
        if not map_file or not key or not os.path.exists(map_file):
            return
        with open(map_file, newline='', encoding='utf-8') as f:
            rows = list(csv.reader(f))
        out = []
        removed = False
        for row in rows:
            if (not removed) and row and row[0].strip() == key:
                removed = True
                continue
            out.append(row)
        if removed:
            tmp = map_file + '.tmp'
            with open(tmp, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerows(out)
            os.replace(tmp, map_file)
    except Exception:
        pass


def lookup_include_injector(map_file: str, key: str) -> bool | None:
    """Resolve include_injector for a jam key from csv: key,0|1 (latest wins)."""
    return lookup_bool_map(map_file, key)

def include_injector_enabled() -> bool:
    try:
        v = Path(INCLUDE_INJECTOR_FLAG).read_text().strip().lower()
        return v in ('1', 'true', 'yes', 'on')
    except Exception:
        return False

def wav_should_exclude(basename: str, include_injector: bool = False) -> bool:
    """Exclude jukebox/injector bot and any obvious non-singer tracks."""
    b = (basename or '').lower()
    if b.startswith('no_name-') and '127_0_0_1' in b:
        return True
    if not include_injector:
        if 'injector-bot' in b or 'injector_bot' in b or 'injectorbot' in b:
            return True
        if 'injector' in b and b.endswith('.wav'):
            return True
        if 'jukebox' in b and b.endswith('.wav'):
            return True
    return False


def _audio_channel_count(path: Path) -> int | None:
    """Return the number of audio channels in a WAV, if ffprobe can read it."""
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'a:0',
        '-show_entries', 'stream=channels',
        '-of', 'default=nw=1:nk=1',
        str(path),
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if p.returncode == 0:
            return int((p.stdout or '').strip())
    except Exception:
        pass
    return None


def _mono_fold_filter(path: Path) -> str | None:
    channels = _audio_channel_count(path)
    if channels and channels > 1:
        return 'pan=mono|c0=0.5*c0+0.5*c1'
    return None


def _pan_params_for_track(name: str) -> str:
    """Return an ffmpeg pan filter string for a barbershop stage layout."""
    n = (name or '').lower()

    # Stage order (listener perspective): Bari, Bass, Lead, Tenor.
    # Keep each part present in both channels so phone/laptop playback does not
    # hide hard-panned voices. Bass is trimmed a touch to avoid dominating mixes.
    if ('bari' in n) or ('tom' in n):
        return 'pan=stereo|FL=0.88*c0|FR=0.42*c0'
    if ('bass' in n) or re.search(r'\bed\b', n):
        return 'pan=stereo|FL=0.64*c0|FR=0.56*c0'
    if ('lead' in n) or ('rich' in n):
        return 'pan=stereo|FL=0.60*c0|FR=0.70*c0'
    if ('tenor' in n) or ('scott' in n):
        return 'pan=stereo|FL=0.42*c0|FR=0.88*c0'

    return 'pan=stereo|FL=0.65*c0|FR=0.65*c0'


def create_leveled_mix_mp3(session_folder: Path, output_base: str,
                           include_injector: bool = False,
                           target_i: int = -20, true_peak: int = -2, lra: int = 7,
                           analysis_seconds: int = 12) -> Path | None:
    """Create a loudness-leveled, panned stereo mix MP3 from singer WAVs.
    
    Uses static gain based on first N seconds analysis to preserve dynamics.
    This allows breakout/solo parts to naturally stand out in the mix.
    """
    wavs: list[Path] = []
    for p in sorted(session_folder.glob('*.wav')):
        if p.stat().st_size < 10240:
            continue
        if wav_should_exclude(p.name, include_injector=include_injector):
            continue
        wavs.append(p)

    if not wavs:
        return None

    out_mp3 = session_folder / f"{output_base}_MIXED_LEVELED.mp3"
    if out_mp3.exists() and out_mp3.stat().st_size > 20000:
        return out_mp3

    processed_dir = session_folder / 'processed'
    processed_dir.mkdir(exist_ok=True)

    processed_files: list[Path] = []
    for w in wavs:
        bn = w.stem
        out_wav = processed_dir / f"{bn}.norm.wav"
        processed_files.append(out_wav)

        # Step 1: Analyze first N seconds to get mean volume
        # Use volumedetect on a trimmed segment
        trim_filter = f"atrim=0:{analysis_seconds}"
        mono_fold = _mono_fold_filter(w)
        if mono_fold:
            trim_filter = f"{mono_fold},{trim_filter}"
        
        cmd_analyze = [
            'ffmpeg', '-nostdin', '-y',
            '-i', str(w),
            '-af', f"{trim_filter},volumedetect",
            '-f', 'null', '-'
        ]
        rc_analyze, out_analyze = run(cmd_analyze)
        if rc_analyze != 0:
            print(f"[mix] volume analysis failed for {w.name}\n{out_analyze}")
            return None
        
        # Parse mean_volume from volumedetect output
        mean_volume = _parse_mean_volume(out_analyze)
        if mean_volume is None:
            print(f"[mix] could not parse mean volume for {w.name}")
            return None
        
        # Step 2: Calculate static gain to reach target loudness
        # target_i is in LUFS, mean_volume is in dB
        # Simple approximation: gain = target - mean
        gain_db = target_i - mean_volume
        
        # Clamp gain to reasonable range (-20dB to +20dB)
        gain_db = max(-20, min(20, gain_db))
        
        print(f"[mix] {w.name}: mean={mean_volume:.1f}dB, gain={gain_db:+.1f}dB")
        
        # Step 3: Apply static gain + panning
        filters = []
        if mono_fold:
            filters.append(mono_fold)
        filters.append(f"volume={gain_db}dB")
        filters.append(_pan_params_for_track(bn))
        
        cmd = [
            'ffmpeg', '-nostdin', '-y',
            '-i', str(w),
            '-af', ','.join(filters),
            '-ar', '48000',
            '-c:a', 'pcm_s16le',
            str(out_wav),
        ]
        rc, out = run(cmd)
        if rc != 0:
            print(f"[mix] gain application failed for {w.name}\n{out}")
            return None

    inputs: list[str] = []
    for f in processed_files:
        inputs += ['-i', str(f)]
    in_tags = ''.join([f"[{i}:a]" for i in range(len(processed_files))])
    filter_complex = f"{in_tags}amix=inputs={len(processed_files)}:normalize=0,alimiter=limit=0.98[a]"

    cmd_mix = [
        'ffmpeg', '-nostdin', '-y',
        *inputs,
        '-filter_complex', filter_complex,
        '-map', '[a]',
        '-codec:a', 'libmp3lame',
        '-q:a', '2',
        str(out_mp3),
    ]
    rc2, out2 = run(cmd_mix)
    if rc2 != 0:
        print(f"[mix] mix failed for {session_folder.name}\n{out2}")
        return None

    try:
        for f in processed_files:
            f.unlink(missing_ok=True)
        if processed_dir.exists() and not any(processed_dir.iterdir()):
            processed_dir.rmdir()
    except Exception:
        pass

    return out_mp3


def _parse_mean_volume(volumedetect_output: str) -> float | None:
    """Parse mean_volume from ffmpeg volumedetect output.
    
    Example output line:
    [Parsed_volumedetect_0 @ 0x...] mean_volume: -27.5 dB
    """
    import re
    match = re.search(r'mean_volume:\s*([-\d.]+)\s*dB', volumedetect_output)
    if match:
        return float(match.group(1))
    return None


def _pad_upper_alnum(s: str, width: int) -> str:
    s = (s or '').upper()
    s = re.sub(r'[^A-Z0-9]+', '', s)
    s = s[:width]
    return s.ljust(width, '_')


def _client_code_from_wav(wav_path: Path) -> str:
    stem = wav_path.stem
    # Strip trailing _<digits> (Jamulus numbering)
    stem = re.sub(r'_\d+$', '', stem)
    return _pad_upper_alnum(stem, 4)


def _yymmdd_from_date_folder(date_folder: str) -> str:
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', date_folder or '')
    if not m:
        return time.strftime('%y%m%d')
    return m.group(1)[2:4] + m.group(2) + m.group(3)


def s3_cp(aws_cli: str, region: str, src: Path, dest: str) -> tuple[int, str]:
    cmd = [aws_cli, 's3', 'cp', str(src), dest, '--region', region]
    return run(cmd)


def _rewrite_lof_for_uploaded_wavs(lof_src: Path, wav_name_map: dict[str, str]) -> Path:
    """Return a rewritten .lof where referenced wav basenames match uploaded-friendly names.

    Also drops LOF lines that reference WAVs we did not upload (e.g. excluded tracks),
    so Audacity won't error on missing files.
    """
    if not wav_name_map:
        return lof_src

    out_lines: list[str] = []
    # replace longer names first to avoid accidental partial overlaps
    repl = sorted(wav_name_map.items(), key=lambda kv: len(kv[0]), reverse=True)

    wav_token_re = re.compile(r'([^\s\"]+\.wav)', re.IGNORECASE)

    for line in lof_src.read_text(encoding='utf-8', errors='replace').splitlines(True):
        m = wav_token_re.search(line)
        if m:
            tok = m.group(1)
            base = os.path.basename(tok)
            if base not in wav_name_map:
                # skip lines referencing excluded/unuploaded wavs
                continue
        for old, new in repl:
            line = line.replace(old, new)
        out_lines.append(line)

    tmp = lof_src.with_name(lof_src.name + '.jb_rewritten')
    tmp.write_text(''.join(out_lines), encoding='utf-8')
    return tmp


def _rewrite_rpp_for_uploaded_wavs(rpp_src: Path, wav_name_map: dict[str, str]) -> Path:
    """Return a rewritten .rpp where referenced wav basenames match uploaded-friendly names."""
    if not wav_name_map:
        return rpp_src

    txt = rpp_src.read_text(encoding='utf-8', errors='replace')
    repl = sorted(wav_name_map.items(), key=lambda kv: len(kv[0]), reverse=True)
    for old, new in repl:
        txt = txt.replace(old, new)

    tmp = rpp_src.with_name(rpp_src.name + '.jb_rewritten')
    tmp.write_text(txt, encoding='utf-8')
    return tmp


def main():
    recordings_dir = Path(_env('RECORDINGS_DIR', '/var/lib/jamulus/recordings'))
    uploaded_dir = recordings_dir / '.uploaded'
    uploaded_dir.mkdir(parents=True, exist_ok=True)

    name_map_file = _env('NAME_MAP_FILE', '/home/nds/recording_name_map.csv')

    aws_cli = _env('AWS_CLI_PATH', '/home/nds/.local/bin/aws')
    aws_region = _env('AWS_REGION', 'us-east-1')
    s3_bucket = _env('S3_BUCKET', 'pipedreamers-recordings-prod')
    s3_prefix = _env('S3_PREFIX', 'vps/vps-0001/recordings').strip('/')

    settle_seconds = int(_env('SETTLE_SECONDS', '45'))
    poll_seconds = int(_env('POLL_SECONDS', '20'))

    print(f"[uploader] recordings_dir={recordings_dir}")
    print(f"[uploader] aws_cli={aws_cli} region={aws_region}")
    print(f"[uploader] s3=s3://{s3_bucket}/{s3_prefix}/")
    print(f"[uploader] name_map_file={name_map_file}")
    print(f"[uploader] settle_seconds={settle_seconds} poll_seconds={poll_seconds}")

    while True:
        try:
            jam_dirs = sorted(
                [p for p in recordings_dir.glob('Jam-*') if p.is_dir()],
                key=lambda p: p.stat().st_mtime,
            )

            for d in jam_dirs:
                marker = uploaded_dir / (d.name + '.done')
                if marker.exists():
                    continue

                lof = next(d.glob('*.lof'), None)
                wavs = list(d.glob('*.wav'))
                if not lof or not wavs:
                    continue

                newest_mtime = max([f.stat().st_mtime for f in [lof, *wavs]])
                age = time.time() - newest_mtime
                if age < settle_seconds:
                    continue

                date_folder = jam_folder_date(d.name) or time.strftime('%Y-%m-%d')
                yymmdd = _yymmdd_from_date_folder(date_folder)
                key = jam_key(d.name)
                sess, matched_key = lookup_session_name(name_map_file, key) if key else (None, None)
                sess = (sess or '').strip()
                if not sess:
                    # If no website session name, fall back to Jam-* (still deterministic)
                    sess = d.name

                fold6 = _pad_upper_alnum(sess, 6)

                include_injector = lookup_include_injector(INCLUDE_INJECTOR_MAP_FILE, key)
                if include_injector is None:
                    include_injector = include_injector_enabled()
                metronome_tainted = lookup_bool_map(METRONOME_TAINT_MAP_FILE, key)
                if metronome_tainted:
                    include_injector = False
                    print(f"[uploader] metronome used in {d.name}; suppressing Jukebox/Injector-bot audio from uploads")

                # Build leveled mix MP3 locally before uploading. The Jukebox/Injector track
                # is included only when the website checkbox was checked for this take.
                try:
                    create_leveled_mix_mp3(d, output_base=sess, include_injector=include_injector)
                except Exception as e:
                    print(f"[mix] ERROR for {d.name}: {e}")

                s3_base = f"s3://{s3_bucket}/{s3_prefix}/{date_folder}/{sanitize(sess)}/"

                print(f"[uploader] S3 upload {d.name} -> {s3_base}")

                ok_all = True

                # Upload singer wavs (friendly names; jukebox/injector optional)
                wav_name_map: dict[str, str] = {}
                for w in sorted(wavs):
                    if wav_should_exclude(w.name, include_injector=include_injector):
                        continue
                    part4 = _client_code_from_wav(w)
                    dest_name = f"{fold6}_{yymmdd}_{part4}.wav"
                    wav_name_map[w.name] = dest_name
                    dest = s3_base + dest_name
                    rc, out = s3_cp(aws_cli, aws_region, w, dest)
                    if rc != 0:
                        ok_all = False
                        print(f"[uploader] ERROR s3_cp {w.name} -> {dest}\n{out}")

                # Upload leveled mix if present
                mix_src = d / f"{sess}_MIXED_LEVELED.mp3"
                if mix_src.exists() and mix_src.stat().st_size > 20000:
                    dest = s3_base + f"{fold6}_{yymmdd}_MIXL.mp3"
                    rc, out = s3_cp(aws_cli, aws_region, mix_src, dest)
                    if rc != 0:
                        ok_all = False
                        print(f"[uploader] ERROR s3_cp mix -> {dest}\n{out}")
                else:
                    print(f"[uploader] NOTE: mix missing (or too small): {mix_src.name}")

                # Upload RPP (Reaper project) with rewritten internal wav references
                rpp_files = sorted(d.glob('*.rpp'))
                for i, rpp in enumerate(rpp_files, start=1):
                    suffix = f"{i:02d}" if len(rpp_files) > 1 else ""
                    dest_name = f"{fold6}_{yymmdd}_PROJ{suffix}.rpp"
                    src = _rewrite_rpp_for_uploaded_wavs(rpp, wav_name_map)
                    dest = s3_base + dest_name
                    rc, out = s3_cp(aws_cli, aws_region, src, dest)
                    if rc != 0:
                        ok_all = False
                        print(f"[uploader] ERROR s3_cp rpp -> {dest}\n{out}")
                    else:
                        print(f"[uploader] uploaded: {dest_name}")

                # Upload LOF (Jamulus file list) rewritten to match uploaded wav names
                if lof:
                    dest_name = f"{fold6}_{yymmdd}_LIST.lof"
                    src = _rewrite_lof_for_uploaded_wavs(lof, wav_name_map)
                    dest = s3_base + dest_name
                    rc, out = s3_cp(aws_cli, aws_region, src, dest)
                    if rc != 0:
                        ok_all = False
                        print(f"[uploader] ERROR s3_cp lof -> {dest}\n{out}")
                    else:
                        print(f"[uploader] uploaded: {dest_name}")

                if not ok_all:
                    continue

                marker.write_text(time.strftime('%Y-%m-%d %H:%M:%S'))
                if matched_key:
                    consume_name(name_map_file, matched_key)
                print(f"[uploader] ok: {d.name} -> {sanitize(sess)}")

        except Exception as e:
            print(f"[uploader] LOOP_ERROR: {e}")

        time.sleep(poll_seconds)


if __name__ == '__main__':
    main()
