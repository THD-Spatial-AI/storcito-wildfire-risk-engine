#!/usr/bin/env bash
set -uo pipefail

# Extract every .zip and .rar file in the current directory.
# Each archive is extracted into a directory named after the archive.

find . -maxdepth 1 -type f \( -iname '*.zip' -o -iname '*.rar' \) -print0 |
while IFS= read -r -d '' archive; do
    filename="${archive##*/}"
    output_dir="${filename%.*}"

    mkdir -p "$output_dir"
    echo "Extracting: $filename -> $output_dir/"

    case "${filename,,}" in
        *.zip)
            unzip -o "$archive" -d "$output_dir"
            ;;
        *.rar)
            if command -v unrar >/dev/null 2>&1; then
                unrar x -o+ "$archive" "$output_dir/"
            elif command -v 7z >/dev/null 2>&1; then
                7z x -y "$archive" "-o $output_dir"
            else
                echo "Error: Install 'unrar' or '7z' to extract RAR files." >&2
            fi
            ;;
    esac
done
