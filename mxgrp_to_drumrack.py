#!/usr/bin/env python3
"""
Maschine Expansion Kit (.mxgrp) → Ableton Live Drum Rack (.adg) Converter

Usage:
    python mxgrp_to_drumrack.py "D:/Native Instruments Library" [output_folder] [--force]

If output folder is omitted, creates "Drum Rack Presets" folder inside the NI library.
Copy generated .adg files to Ableton User Library to browse them as Drum Rack presets.
"""

import gzip
import json
import os
import re
import shutil
import struct
import sys
from datetime import datetime, timezone


# ─────────────────────────────────────────────
# 1. .mxgrp Parser (完成済み)
# ─────────────────────────────────────────────

def parse_mxgrp(filepath):
    """
    Parse a Maschine .mxgrp file and extract pad→sample mappings.
    Returns list of relative sample paths in pad order (Pad 1 first).
    """
    with open(filepath, 'rb') as f:
        data = f.read()

    wav_positions = [m.start() for m in re.finditer(b'.wav', data)]

    pads = []
    seen_paths = set()

    for pos in wav_positions:
        chunk_start = max(0, pos - 400)
        chunk_end = min(len(data), pos + 4)
        chunk = data[chunk_start:chunk_end]

        path = ''
        for b in chunk:
            if 32 <= b < 127:
                path += chr(b)
            elif path and b == 0:
                continue
            else:
                if '.wav' in path:
                    break
                path = ''

        if '.wav' not in path:
            continue

        sample_path = None
        for prefix in ['Samples/']:
            idx = path.find(prefix)
            if idx >= 0:
                sample_path = path[idx:]
                break

        if sample_path and sample_path not in seen_paths:
            seen_paths.add(sample_path)
            pads.append(sample_path)

    return pads


# ─────────────────────────────────────────────
# 2. WAV utilities
# ─────────────────────────────────────────────

def get_wav_sample_count(wav_path):
    """Read WAV header and return total sample count."""
    try:
        with open(wav_path, 'rb') as f:
            header = f.read(44)
        channels = struct.unpack('<H', header[22:24])[0]
        bits = struct.unpack('<H', header[34:36])[0]
        data_size = struct.unpack('<I', header[40:44])[0]
        bytes_per_sample = bits // 8 * channels
        return data_size // bytes_per_sample if bytes_per_sample > 0 else 0
    except Exception:
        return 0


# ─────────────────────────────────────────────
# 3. Template-based Ableton .adg Generator
# ─────────────────────────────────────────────

TEMPLATE_SEARCH_PATHS = [
    # Windows common locations
    "C:/ProgramData/Ableton",
    "D:/Ableton",
    "E:/Ableton",
    # macOS common locations
    os.path.expanduser("~/Library/Application Support/Ableton"),
    "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library",
]

TEMPLATE_FILENAME = "Wrong Sided Kit.adg"


def find_template():
    """Auto-detect the Drum Essentials template .adg file."""
    # Search common Ableton installation paths
    for base in TEMPLATE_SEARCH_PATHS:
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            if TEMPLATE_FILENAME in files:
                candidate = os.path.join(root, TEMPLATE_FILENAME)
                # Verify it's the right one (inside Drum Essentials)
                if 'Drum Essentials' in root:
                    return candidate
    # Broader search: check all drives on Windows
    if sys.platform == 'win32':
        import string
        for drive in string.ascii_uppercase:
            drive_path = f"{drive}:/"
            if not os.path.isdir(drive_path):
                continue
            # Search for Ableton/Factory Packs or just Drum Essentials
            for search_root in [
                os.path.join(drive_path, 'Ableton'),
                os.path.join(drive_path, 'ProgramData', 'Ableton'),
                os.path.join(drive_path, 'Ableton Live'),
            ]:
                if not os.path.isdir(search_root):
                    continue
                for root, dirs, files in os.walk(search_root):
                    if TEMPLATE_FILENAME in files and 'Drum Essentials' in root:
                        return os.path.join(root, TEMPLATE_FILENAME)
    return None


_template_cache = None


def load_template(template_path=None):
    """Load and cache the reference .adg template XML and extract pad metadata."""
    global _template_cache
    if _template_cache is not None:
        return _template_cache

    if template_path is None:
        template_path = find_template()
        if template_path is None:
            raise FileNotFoundError(
                "Could not find Drum Essentials template. "
                "Install 'Drum Essentials' Pack in Ableton, or use --template option."
            )

    with gzip.open(template_path, 'rb') as f:
        xml = f.read().decode('utf-8')

    lines = xml.splitlines(True)

    # Extract per-pad sample paths from template (for string replacement)
    pad_blocks = []
    current_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('<DrumBranchPreset'):
            current_start = i
        elif stripped == '</DrumBranchPreset>' and current_start is not None:
            pad_blocks.append((current_start, i))
            current_start = None

    template_pad_paths = []
    for start, end in pad_blocks:
        block = ''.join(lines[start:end + 1])
        rel_paths = re.findall(r'<RelativePath Value="(Samples/[^"]+)"', block)
        abs_paths = re.findall(r'<Path Value="(/Volumes/[^"]+)"', block)
        if rel_paths and abs_paths:
            template_pad_paths.append((rel_paths[0], abs_paths[0]))
        else:
            template_pad_paths.append((None, None))

    # Find the second GroupDevicePreset (AudioEffectGroupDevice) to remove
    gdp_starts = []
    for i, line in enumerate(lines):
        if line.strip().startswith('<GroupDevicePreset Id="'):
            gdp_starts.append(i)

    effect_range = None
    if len(gdp_starts) > 1:
        second_start = gdp_starts[1]
        depth = 0
        for i in range(second_start, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith('<GroupDevicePreset'):
                depth += 1
            elif stripped == '</GroupDevicePreset>':
                depth -= 1
                if depth == 0:
                    effect_range = (second_start, i)
                    break

    _template_cache = (xml, lines, template_pad_paths, pad_blocks, effect_range)
    return _template_cache


def generate_drum_rack_adg(kit_name, pads_with_paths, template_path=None):
    """
    Generate Ableton Drum Rack .adg XML.

    pads_with_paths: list of (midi_note, sample_name, abs_path, sample_count)
    """
    xml_orig, lines_orig, template_pad_paths, pad_blocks, effect_range = \
        load_template(template_path)

    lines = list(lines_orig)

    # Step 1: Remove AudioEffectGroupDevice (template-specific effects)
    if effect_range:
        start, end = effect_range
        del lines[start:end + 1]

    result = ''.join(lines)

    # Step 2: Kit name
    result = result.replace('Wrong Sided Kit', kit_name)
    result = result.replace('Wrong SIded Kit', kit_name)
    result = result.replace('Created by: Mic Checkmate&#x0A;', '')
    result = result.replace('Created by: Mic Checkmate', '')

    # Step 3: Link=true (auto-detect sample boundaries)
    result = result.replace('<Link Value="false"', '<Link Value="true"')

    # Step 4: Replace sample paths per pad
    max_pads = min(len(pads_with_paths), len(template_pad_paths))
    for i in range(max_pads):
        midi_note, sample_name, abs_path, sample_count = pads_with_paths[i]
        old_rel, old_abs = template_pad_paths[i]
        if old_rel is None or old_abs is None:
            continue
        # Absolute path first (longer string), then relative
        result = result.replace(old_abs, abs_path)
        result = result.replace(old_rel, abs_path)

    # Step 5: Fix per-pad SampleEnd/DefaultDuration from actual WAV sample counts
    result_lines = result.splitlines(True)
    new_pad_blocks = []
    current_start = None
    for i, line in enumerate(result_lines):
        stripped = line.strip()
        if stripped.startswith('<DrumBranchPreset'):
            current_start = i
        elif stripped == '</DrumBranchPreset>' and current_start is not None:
            new_pad_blocks.append((current_start, i))
            current_start = None

    for pad_idx in range(min(len(pads_with_paths), len(new_pad_blocks))):
        midi_note, sample_name, abs_path, sample_count = pads_with_paths[pad_idx]
        if sample_count <= 0:
            continue

        start, end = new_pad_blocks[pad_idx]
        for i in range(start, end + 1):
            line = result_lines[i]
            line = re.sub(r'<SampleEnd Value="\d+"',
                          f'<SampleEnd Value="{sample_count}"', line)
            line = re.sub(r'<DefaultDuration Value="\d+"',
                          f'<DefaultDuration Value="{sample_count}"', line)
            # SustainLoop/ReleaseLoop End
            if '<SustainLoop>' in ''.join(result_lines[max(start, i-3):i+1]):
                line = re.sub(r'<End Value="\d+"',
                              f'<End Value="{sample_count}"', line)
            if '<ReleaseLoop>' in ''.join(result_lines[max(start, i-3):i+1]):
                line = re.sub(r'<End Value="\d+"',
                              f'<End Value="{sample_count}"', line)
            result_lines[i] = line

    return ''.join(result_lines)


def save_adg(xml_content, output_path):
    """Save XML as gzipped .adg file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with gzip.open(output_path, 'wb') as f:
        f.write(xml_content.encode('utf-8'))


# ─────────────────────────────────────────────
# 4. Genre Tagging (XMP Metadata)
# ─────────────────────────────────────────────

# Manual mapping: NI Expansion name → Ableton genre keyword
# Ported from BeatCraftSurvivors expansion_palette_map.json
EXPANSION_GENRE_MAP = {
    # Funk
    "Amplified Funk": "Funk", "Disco and Funk": "Funk", "Neo Boogie": "Funk",
    "Bumpin Flava": "Funk", "Crate Cuts": "Funk", "Rhythm Source": "Funk",
    "Feel It": "Funk", "Backyard Jams": "Funk", "Rare Vibrations": "Funk",
    # Hip Hop
    "40s Very Own - Drums": "Hip Hop", "40s Very Own - Keys": "Hip Hop",
    "Queensbridge Story": "Hip Hop", "True School": "Hip Hop",
    "Marble Rims": "Hip Hop", "Pure Drip": "Hip Hop", "Drop Squad": "Hip Hop",
    "Platinum Bounce": "Hip Hop", "Borough Chops": "Hip Hop", "Stacks": "Hip Hop",
    "Homage": "Hip Hop", "Basement Era": "Hip Hop", "London Grit": "Hip Hop",
    "Lucid Mission": "Hip Hop", "Lo-Fi Glow": "Hip Hop",
    "PRISM Organic Lofi Drums": "Hip Hop", "Hazy Days Samples": "Hip Hop",
    "Cloud Supply": "Hip Hop", "Indigo Dust": "Hip Hop", "Melted Vibes": "Hip Hop",
    # Techno
    "Body Mechanik": "Techno", "Decoded Forms": "Techno", "Mechanix": "Techno",
    "Neon Drive": "Techno", "Nocturnal State": "Techno", "Pulse": "Techno",
    "Pulse Box": "Techno", "Drum State": "Techno", "Byte Riot": "Techno",
    "Nacht": "Techno", "Schema - Dark": "Techno", "Carbon Decay": "Techno",
    "Warped Symmetry": "Techno", "Charge": "Techno",
    # House
    "Our House": "House", "Bounce": "House", "Bump": "House",
    "Higher Place": "House", "Sway": "House", "Magnetic Coast": "House",
    "Elastic Thump": "House", "Mother Board": "House", "Progressive Trance": "House",
    # Jazz
    "Velvet Lounge": "Jazz",
    # Ambient
    "Halcyon Sky": "Ambient", "Ethereal Earth": "Ambient",
    "Astral Flutter": "Ambient", "Infinite Escape": "Ambient",
    "Opaline Drift": "Ambient", "Cavern Floor": "Ambient",
    "Drift Theory Samples": "Ambient", "Faded Reels": "Ambient",
    "Faded Reels Samples": "Ambient", "Burnt Hues Samples": "Ambient",
    "Prismatic Bliss": "Ambient", "Solar Breeze": "Ambient",
    "Lilac Glare": "Ambient", "Haze": "Ambient",
    # Afrobeats → Reggaeton (closest Ableton genre)
    "Afrobeats": "Reggaeton", "Caribbean Current": "Reggaeton",
    "West Africa": "Reggaeton", "Cuba": "Latin", "Global Shake": "Reggaeton",
    "Rising Crescent": "Reggaeton", "Golden Kingdom": "Reggaeton",
    # Experimental
    "Chromatic Fire": "Experimental", "Conflux": "Experimental",
    "Glaze": "Experimental", "Glaze 2": "Experimental",
    "Spectrum Quake": "Experimental",
    # Trap
    "Latin Trap": "Trap", "Lazer Dice": "Trap", "Empire Breaks": "Trap",
    "Platinum Pop": "Trap", "Hot Vocals": "Trap",
    # DnB
    "Drum Breaks": "Drum & Bass", "Drum Lab": "Drum & Bass",
    "Free Form": "Drum & Bass", "Deep Matter": "Drum & Bass",
    "Molten Veil": "Drum & Bass", "Drive": "Drum & Bass",
    "Liquid Energy": "Drum & Bass",
    # Soul
    "Soul Gold": "Soul", "Soul Magic": "Soul", "Soul Magic Samples": "Soul",
    "Soul Sessions": "Soul", "RnB Licks": "R&B", "Deft Lines": "Soul",
    # Dub
    "Echo Versions": "Dub", "Bazzazian Tapes": "Dub",
}

# Keyword fallback: expansion name → Ableton genre
GENRE_KEYWORD_RULES = [
    (["funk", "disco"], "Funk"),
    (["hip hop", "hiphop", "hip-hop", "lo-fi", "lofi", "chill"], "Hip Hop"),
    (["techno", "industrial"], "Techno"),
    (["house", "garage"], "House"),
    (["jazz", "swing"], "Jazz"),
    (["ambient", "drone", "ethereal"], "Ambient"),
    (["afro", "african", "world"], "Reggaeton"),
    (["experimental", "glitch", "noise"], "Experimental"),
    (["trap", "drill", "808"], "Trap"),
    (["drum and bass", "dnb", "jungle", "breakbeat", "breaks"], "Drum & Bass"),
    (["soul", "gospel"], "Soul"),
    (["r&b", "rnb"], "R&B"),
    (["dub", "reggae", "roots"], "Dub"),
    (["pop"], "Pop"),
    (["rock"], "Rock"),
    (["electro"], "Electro"),
    (["trance"], "Trance"),
]

# Ableton genre → XML-safe string (for & → &amp; etc.)
def _xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def classify_genre(expansion_name):
    """Classify an NI Expansion name into an Ableton genre keyword."""
    # Strip common suffixes
    cleaned = expansion_name
    for suffix in [" Library", " library", " Samples", " samples"]:
        cleaned = cleaned.replace(suffix, "")
    cleaned = cleaned.strip()

    # 1. Manual map
    if cleaned in EXPANSION_GENRE_MAP:
        return EXPANSION_GENRE_MAP[cleaned]
    if expansion_name in EXPANSION_GENRE_MAP:
        return EXPANSION_GENRE_MAP[expansion_name]

    # 2. Keyword fallback
    lower = expansion_name.lower()
    for keywords, genre in GENRE_KEYWORD_RULES:
        for kw in keywords:
            if kw in lower:
                return genre

    return None


def generate_xmp(items, pack_id="ni-expansions-drumrack"):
    """
    Generate Ableton-compatible XMP metadata XML.

    items: list of (file_path, genre, kit_type) tuples
        file_path: relative path within the output folder (e.g. "Expansion Name/Kit.adg")
        genre: Ableton genre string (e.g. "Hip Hop") or None
        kit_type: "Hybrid Kit", "Synth Kit", "Acoustic Kit", etc.
    """
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 5.6.0">',
        '   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
        '      <rdf:Description rdf:about=""',
        '            xmlns:dc="http://purl.org/dc/elements/1.1/"',
        '            xmlns:ablFR="https://ns.ableton.com/xmp/fs-resources/1.0/"',
        '            xmlns:xmp="http://ns.adobe.com/xap/1.0/">',
        '         <dc:format>application/vnd.ableton.folder</dc:format>',
        f'         <ablFR:resource>pack</ablFR:resource>',
        f'         <ablFR:packUniqueId>{pack_id}</ablFR:packUniqueId>',
        '         <ablFR:items>',
        '            <rdf:Bag>',
    ]

    for file_path, genre, kit_type in items:
        escaped_path = _xml_escape(file_path)
        lines.append('               <rdf:li rdf:parseType="Resource">')
        lines.append(f'                  <ablFR:filePath>{escaped_path}</ablFR:filePath>')
        lines.append('                  <ablFR:keywords>')
        lines.append('                     <rdf:Bag>')
        # Kit type tag
        lines.append(f'                        <rdf:li>Drums|Drum Kit|{_xml_escape(kit_type)}</rdf:li>')
        # Genre tag
        if genre:
            lines.append(f'                        <rdf:li>Genres|{_xml_escape(genre)}</rdf:li>')
        lines.append('                     </rdf:Bag>')
        lines.append('                  </ablFR:keywords>')
        lines.append('               </rdf:li>')

    lines.extend([
        '            </rdf:Bag>',
        '         </ablFR:items>',
        f'         <xmp:CreatorTool>mxgrp-to-drumrack</xmp:CreatorTool>',
        f'         <xmp:CreateDate>{now}</xmp:CreateDate>',
        f'         <xmp:MetadataDate>{now}</xmp:MetadataDate>',
        '      </rdf:Description>',
        '   </rdf:RDF>',
        '</x:xmpmeta>',
    ])
    return '\n'.join(lines)


# XMP filename used by Ableton (constant UUID across all packs)
XMP_FILENAME = "c55d131f-2661-5add-aece-29afb7099dfa.xmp"


# ─────────────────────────────────────────────
# 5. Batch Converter
# ─────────────────────────────────────────────

def convert_expansion(expansion_path, output_dir, force=False, template_path=None):
    """Convert all .mxgrp kits in an expansion to Drum Rack presets.

    Returns: (converted_count, skipped_count, kit_names)
        kit_names: list of kit filenames that exist in output (converted + skipped)
    """
    expansion_name = os.path.basename(expansion_path)
    kits_dir = None

    for root, dirs, files in os.walk(expansion_path):
        if os.path.basename(root).lower() == 'kits':
            kits_dir = root
            break
        if os.path.basename(root).lower() == 'groups':
            kits_sub = os.path.join(root, 'Kits')
            if os.path.isdir(kits_sub):
                kits_dir = kits_sub
                break

    if not kits_dir:
        return 0, 0, []

    mxgrp_files = [f for f in os.listdir(kits_dir) if f.endswith('.mxgrp')]
    if not mxgrp_files:
        return 0, 0, []

    converted = 0
    skipped = 0
    kit_names = []
    for mxgrp_file in mxgrp_files:
        mxgrp_path = os.path.join(kits_dir, mxgrp_file)
        kit_name = os.path.splitext(mxgrp_file)[0]

        output_subdir = os.path.join(output_dir, expansion_name)
        output_file = os.path.join(output_subdir, f"{kit_name}.adg")

        if not force and os.path.isfile(output_file):
            mxgrp_mtime = os.path.getmtime(mxgrp_path)
            adg_mtime = os.path.getmtime(output_file)
            if mxgrp_mtime <= adg_mtime:
                skipped += 1
                kit_names.append(kit_name)
                continue

        try:
            sample_paths = parse_mxgrp(mxgrp_path)
        except Exception as e:
            print(f"  x  Error parsing {mxgrp_file}: {e}")
            continue

        if not sample_paths:
            print(f"  x  No samples found in {mxgrp_file}")
            continue

        # Build pad list
        pads = []
        missing = 0
        for i, rel_path in enumerate(sample_paths):
            if i >= 16:  # Template has max 16 pads
                break
            midi_note = 36 + i
            abs_path = os.path.join(expansion_path, rel_path).replace('\\', '/')
            sample_name = os.path.splitext(os.path.basename(rel_path))[0]

            # Get sample count from WAV file
            wav_file = os.path.join(expansion_path, rel_path)
            if os.path.isfile(wav_file):
                sample_count = get_wav_sample_count(wav_file)
            else:
                wav_alt = os.path.join(expansion_path, rel_path.replace('/', '\\'))
                if os.path.isfile(wav_alt):
                    sample_count = get_wav_sample_count(wav_alt)
                else:
                    sample_count = 0
                    missing += 1

            pads.append((midi_note, sample_name, abs_path, sample_count))

        display_name = f"{expansion_name} - {kit_name}"
        xml = generate_drum_rack_adg(display_name, pads, template_path)
        save_adg(xml, output_file)

        # Copy preview .ogg if available
        previews_dir = os.path.join(kits_dir, '.previews')
        ogg_src = os.path.join(previews_dir, f"{mxgrp_file}.ogg")
        if os.path.isfile(ogg_src):
            ogg_dst_dir = os.path.join(
                output_dir, 'Ableton Folder Info', 'Previews', expansion_name
            )
            os.makedirs(ogg_dst_dir, exist_ok=True)
            shutil.copy2(ogg_src, os.path.join(ogg_dst_dir, f"{kit_name}.adg.ogg"))

        pad_count = len(pads)
        status = f"({missing} missing)" if missing else ""
        print(f"  + {kit_name}: {pad_count} pads {status}")
        converted += 1
        kit_names.append(kit_name)

    return converted, skipped, kit_names


def main():
    force = '--force' in sys.argv
    template_arg = None
    args = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == '--force':
            i += 1
            continue
        if sys.argv[i] == '--template' and i + 1 < len(sys.argv):
            template_arg = sys.argv[i + 1]
            i += 2
            continue
        args.append(sys.argv[i])
        i += 1

    if len(args) < 1:
        print("Usage: python mxgrp_to_drumrack.py <NI Library Path> [Output Path] [--force] [--template <path>]")
        print()
        print("Options:")
        print("  --force              Regenerate all .adg files")
        print("  --template <path>    Template .adg file path")
        print("                       (default: auto-detect from Ableton installation)")
        print()
        print('Examples:')
        print('  python mxgrp_to_drumrack.py "D:\\Native Instruments Library"')
        print('  python mxgrp_to_drumrack.py "D:\\Native Instruments Library" --force')
        print('  python mxgrp_to_drumrack.py "D:\\Native Instruments Library" "D:\\NI Drum Racks"')
        sys.exit(1)

    ni_library = args[0]
    output_dir = args[1] if len(args) >= 2 else os.path.join(ni_library, "NI Drum Racks")

    if not os.path.isdir(ni_library):
        print(f"Error: folder not found: {ni_library}")
        sys.exit(1)

    # Resolve template path
    if template_arg:
        template_path = template_arg
    else:
        print("Searching for Ableton Drum Essentials template...")
        template_path = find_template()

    if not template_path or not os.path.isfile(template_path):
        print(f"Error: template .adg not found.")
        print(f"  Install 'Drum Essentials' Pack in Ableton Live,")
        print(f"  or use --template to specify the path manually.")
        sys.exit(1)
    else:
        print(f"  Found: {template_path}")

    try:
        load_template(template_path)
    except Exception as e:
        print(f"Error: failed to load template: {e}")
        sys.exit(1)

    mode = "Full rebuild" if force else "Incremental (new/updated only)"
    print(f"Maschine Kit -> Ableton Drum Rack Converter")
    print(f"  Input:    {ni_library}")
    print(f"  Output:   {output_dir}")
    print(f"  Template: {template_path}")
    print(f"  Mode:     {mode}")
    print()

    expansions = sorted([
        d for d in os.listdir(ni_library)
        if os.path.isdir(os.path.join(ni_library, d))
        and d != "Drum Rack Presets"
        and d != "NI Drum Racks"
    ])

    total_converted = 0
    total_skipped = 0
    total_expansions = 0
    xmp_items = []  # (file_path, genre, kit_type) for XMP generation
    genre_stats = {}  # genre → count

    for expansion_name in expansions:
        expansion_path = os.path.join(ni_library, expansion_name)
        print(f"[{expansion_name}]")

        converted, skipped, kit_names = convert_expansion(
            expansion_path, output_dir, force=force, template_path=template_path
        )
        if converted > 0 or skipped > 0:
            total_expansions += 1
            total_converted += converted
            total_skipped += skipped

            # Classify genre for this expansion
            genre = classify_genre(expansion_name)
            if genre:
                genre_stats[genre] = genre_stats.get(genre, 0) + len(kit_names)

            # Collect XMP items for all kits (converted + skipped)
            for kit_name in kit_names:
                file_path = f"{expansion_name}/{kit_name}.adg"
                xmp_items.append((file_path, genre, "Hybrid Kit"))

            genre_label = f" [{genre}]" if genre else ""
            if skipped > 0 and converted == 0:
                print(f"  - {skipped} kits up to date{genre_label}")
            elif skipped > 0:
                print(f"  ({skipped} skipped){genre_label}")
            elif genre:
                print(f"  genre: {genre}")
        else:
            print(f"  - No kits found")

    # Create Properties.cfg for Ableton pack recognition (enables previews)
    if total_converted > 0 or xmp_items:
        afi_dir = os.path.join(output_dir, 'Ableton Folder Info')
        os.makedirs(afi_dir, exist_ok=True)
        cfg_path = os.path.join(afi_dir, 'Properties.cfg')
        if not os.path.isfile(cfg_path):
            with open(cfg_path, 'w') as f:
                f.write('Ableton#04I\n\nFolderConfigData\n{\n')
                f.write('  String PackUniqueID = "ni-expansions-drumrack";\n')
                f.write('  String PackDisplayName = "NI Drum Racks";\n')
                f.write('  String PackVendor = "";\n')
                f.write('  Int PackMinorVersion = 1;\n')
                f.write('  Int PackMajorVersion = 1;\n')
                f.write('  Int PackRevision = 1;\n')
                f.write('}\n')

        # Generate XMP metadata with genre tags
        if xmp_items:
            xmp_content = generate_xmp(xmp_items)
            xmp_path = os.path.join(afi_dir, XMP_FILENAME)
            with open(xmp_path, 'w', encoding='utf-8') as f:
                f.write(xmp_content)
            tagged = sum(1 for _, g, _ in xmp_items if g)
            print(f"\n  XMP tags: {tagged}/{len(xmp_items)} presets tagged")

    print()
    print(f"Done!")
    print(f"  Converted: {total_converted} Drum Rack presets")
    print(f"  Skipped:   {total_skipped}")
    print(f"  From:      {total_expansions} Expansions")
    print(f"  Output:    {output_dir}")
    if genre_stats:
        print(f"  Genres:    {', '.join(f'{g}({c})' for g, c in sorted(genre_stats.items(), key=lambda x: -x[1]))}")
    if total_converted > 0:
        print()
        print(f"Add the output folder to Ableton's browser (Places > Add Folder).")


if __name__ == '__main__':
    main()
