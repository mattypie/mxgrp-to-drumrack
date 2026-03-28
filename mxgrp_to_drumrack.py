#!/usr/bin/env python3
"""
Maschine Expansion Kit (.mxgrp) → Ableton Live Drum Rack (.adg) Converter

Usage:
    python mxgrp_to_drumrack.py "D:/Native Instruments Library" [output_folder] [--force]

If output folder is omitted, creates "Drum Rack Presets" folder inside the NI library.
Copy generated .adg files to Ableton User Library to browse them as Drum Rack presets.
"""

import gzip
import os
import re
import shutil
import struct
import sys


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
# 4. Batch Converter
# ─────────────────────────────────────────────

def convert_expansion(expansion_path, output_dir, force=False, template_path=None):
    """Convert all .mxgrp kits in an expansion to Drum Rack presets.

    Returns: (converted_count, skipped_count)
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
        return 0, 0

    mxgrp_files = [f for f in os.listdir(kits_dir) if f.endswith('.mxgrp')]
    if not mxgrp_files:
        return 0, 0

    converted = 0
    skipped = 0
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

    return converted, skipped


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
    ])

    total_converted = 0
    total_skipped = 0
    total_expansions = 0

    for expansion_name in expansions:
        expansion_path = os.path.join(ni_library, expansion_name)
        print(f"[{expansion_name}]")

        converted, skipped = convert_expansion(
            expansion_path, output_dir, force=force, template_path=template_path
        )
        if converted > 0 or skipped > 0:
            total_expansions += 1
            total_converted += converted
            total_skipped += skipped
            if skipped > 0 and converted == 0:
                print(f"  - {skipped} kits up to date")
            elif skipped > 0:
                print(f"  ({skipped} skipped)")
        else:
            print(f"  - No kits found")

    # Create Properties.cfg for Ableton pack recognition (enables previews)
    if total_converted > 0:
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

    print()
    print(f"Done!")
    print(f"  Converted: {total_converted} Drum Rack presets")
    print(f"  Skipped:   {total_skipped}")
    print(f"  From:      {total_expansions} Expansions")
    print(f"  Output:    {output_dir}")
    if total_converted > 0:
        print()
        print(f"Add the output folder to Ableton's browser (Places > Add Folder).")


if __name__ == '__main__':
    main()
