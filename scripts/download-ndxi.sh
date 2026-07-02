#!/usr/bin/env bash
set -Eeuo pipefail

# Download Sentinel-2 L2A B04, B08, B8A and B11 as separate one-band GeoTIFFs
# from the Copernicus Data Space Ecosystem Sentinel Hub Process API.
#
# Default area: rectangle covering mainland Spain, Balearic Islands,
# Ceuta, Melilla, and Canary Islands.
# CRS: OGC CRS84 (longitude, latitude)
#
# Authentication option 1: existing OAuth access token
#   ACCESS_TOKEN='eyJ...' ./download_spain_b04_b08.sh
#
# Authentication option 2: OAuth client credentials
#   SH_CLIENT_ID='...' SH_CLIENT_SECRET='...' \
#     ./download_spain_b04_b08.sh
#
# Optional overrides:
#   FROM='2026-06-07T00:00:00Z'
#   TO='2026-06-14T23:59:59Z'
#   MAX_CLOUD=30
#   WIDTH=2500
#   HEIGHT=2243
#   MOSAICKING_ORDER='mostRecent'   # or leastCC / leastRecent
#   OUT_DIR='spain_bands'
#   MIN_LON=-18.30 MIN_LAT=27.50 MAX_LON=4.50 MAX_LAT=43.90

for cmd in curl jq tar; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: required command not found: $cmd" >&2
        exit 1
    fi
done

# Spain rectangle, including the Canary Islands.
MIN_LON="${MIN_LON:--18.30}"
MIN_LAT="${MIN_LAT:-27.50}"
MAX_LON="${MAX_LON:-4.50}"
MAX_LAT="${MAX_LAT:-43.90}"

FROM="${FROM:-2026-06-07T00:00:00Z}"
TO="${TO:-2026-06-14T23:59:59Z}"
MAX_CLOUD="${MAX_CLOUD:-30}"
WIDTH="${WIDTH:-2500}"
HEIGHT="${HEIGHT:-2243}"
MOSAICKING_ORDER="${MOSAICKING_ORDER:-mostRecent}"
OUT_DIR="${OUT_DIR:-spain_bands}"

TOKEN_URL="https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL="https://sh.dataspace.copernicus.eu/process/v1"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$OUT_DIR"

# Obtain a token only when one was not supplied by the caller.
if [[ -z "${ACCESS_TOKEN:-}" ]]; then
    : "${SH_CLIENT_ID:?Set SH_CLIENT_ID and SH_CLIENT_SECRET, or provide ACCESS_TOKEN}"
    : "${SH_CLIENT_SECRET:?Set SH_CLIENT_ID and SH_CLIENT_SECRET, or provide ACCESS_TOKEN}"

    echo "Requesting OAuth access token..." >&2

    TOKEN_FILE="$TMP_DIR/token-response"
    TOKEN_STATUS="$(
        curl --silent --show-error \
            --request POST \
            --url "$TOKEN_URL" \
            --header "Content-Type: application/x-www-form-urlencoded" \
            --data "grant_type=client_credentials" \
            --data-urlencode "client_id=${SH_CLIENT_ID}" \
            --data-urlencode "client_secret=${SH_CLIENT_SECRET}" \
            --output "$TOKEN_FILE" \
            --write-out "%{http_code}"
    )"

    if [[ ! "$TOKEN_STATUS" =~ ^2[0-9][0-9]$ ]]; then
        echo "OAuth token request failed with HTTP status $TOKEN_STATUS:" >&2
        cat "$TOKEN_FILE" >&2
        echo >&2
        exit 1
    fi

    if ! ACCESS_TOKEN="$(jq --exit-status --raw-output '.access_token' "$TOKEN_FILE")"; then
        echo "OAuth response did not contain a valid access_token:" >&2
        cat "$TOKEN_FILE" >&2
        echo >&2
        exit 1
    fi
fi

EVALSCRIPT="$(cat <<'EVALSCRIPT_EOF'
//VERSION=3

function setup() {
    return {
        input: [{
            bands: ["B04", "B08", "B8A", "B11"],
            units: "DN"
        }],
        output: [
            {
                id: "B04",
                bands: 1,
                sampleType: "UINT16"
            },
            {
                id: "B08",
                bands: 1,
                sampleType: "UINT16"
            },
            {
                id: "B8A",
                bands: 1,
                sampleType: "UINT16"
            },
            {
                id: "B11",
                bands: 1,
                sampleType: "UINT16"
            }
        ]
    };
}

function evaluatePixel(sample) {
    return {
        B04: [sample.B04],
        B08: [sample.B08],
        B8A: [sample.B8A],
        B11: [sample.B11]
    };
}
EVALSCRIPT_EOF
)"

# Values are passed to jq as strings and converted with tonumber. This avoids
# the "invalid JSON text passed to --argjson" error caused by empty or malformed
# shell variables.
REQUEST_JSON="$(
    jq --null-input \
        --arg min_lon "$MIN_LON" \
        --arg min_lat "$MIN_LAT" \
        --arg max_lon "$MAX_LON" \
        --arg max_lat "$MAX_LAT" \
        --arg width "$WIDTH" \
        --arg height "$HEIGHT" \
        --arg max_cloud "$MAX_CLOUD" \
        --arg from "$FROM" \
        --arg to "$TO" \
        --arg mosaicking "$MOSAICKING_ORDER" \
        --arg evalscript "$EVALSCRIPT" \
        '{
            input: {
                bounds: {
                    bbox: [
                        ($min_lon | tonumber),
                        ($min_lat | tonumber),
                        ($max_lon | tonumber),
                        ($max_lat | tonumber)
                    ],
                    properties: {
                        crs: "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
                    }
                },
                data: [{
                    type: "sentinel-2-l2a",
                    dataFilter: {
                        timeRange: {
                            from: $from,
                            to: $to
                        },
                        maxCloudCoverage: ($max_cloud | tonumber),
                        mosaickingOrder: $mosaicking
                    }
                }]
            },
            output: {
                width: ($width | tonumber),
                height: ($height | tonumber),
                responses: [
                    {
                        identifier: "B04",
                        format: { type: "image/tiff" }
                    },
                    {
                        identifier: "B08",
                        format: { type: "image/tiff" }
                    },
                    {
                        identifier: "B8A",
                        format: { type: "image/tiff" }
                    },
                    {
                        identifier: "B11",
                        format: { type: "image/tiff" }
                    }
                ]
            },
            evalscript: $evalscript
        }'
)"

printf '%s\n' "$REQUEST_JSON" | jq . > "$OUT_DIR/request.json"

ARCHIVE_FILE="$TMP_DIR/response.tar"

echo "Downloading B04, B08, B8A and B11..." >&2
HTTP_STATUS="$(
    curl --silent --show-error \
        --request POST \
        --url "$PROCESS_URL" \
        --header "Authorization: Bearer ${ACCESS_TOKEN}" \
        --header "Content-Type: application/json" \
        --header "Accept: application/tar" \
        --data-binary "$REQUEST_JSON" \
        --output "$ARCHIVE_FILE" \
        --write-out "%{http_code}"
)"

if [[ ! "$HTTP_STATUS" =~ ^2[0-9][0-9]$ ]]; then
    echo "Copernicus Process API request failed with HTTP status $HTTP_STATUS:" >&2
    if jq . "$ARCHIVE_FILE" >/dev/null 2>&1; then
        jq . "$ARCHIVE_FILE" >&2
    else
        cat "$ARCHIVE_FILE" >&2
        echo >&2
    fi
    exit 1
fi

if ! tar -tf "$ARCHIVE_FILE" > "$TMP_DIR/archive-list.txt" 2>/dev/null; then
    echo "The API response was successful but was not a valid TAR archive." >&2
    echo "Response content:" >&2
    cat "$ARCHIVE_FILE" >&2
    echo >&2
    exit 1
fi

# Reject unsafe archive paths before extraction.
if grep -Eq '(^/|(^|/)\.\.(/|$))' "$TMP_DIR/archive-list.txt"; then
    echo "Refusing to extract an archive containing unsafe paths." >&2
    cat "$TMP_DIR/archive-list.txt" >&2
    exit 1
fi

tar -xf "$ARCHIVE_FILE" -C "$OUT_DIR"

B04_FILE="$OUT_DIR/B04.tif"
B08_FILE="$OUT_DIR/B08.tif"
B8A_FILE="$OUT_DIR/B8A.tif"
B11_FILE="$OUT_DIR/B11.tif"

if [[ ! -s "$B04_FILE" || ! -s "$B08_FILE" || ! -s "$B8A_FILE" || ! -s "$B11_FILE" ]]; then
    echo "The request completed, but the expected files were not found." >&2
    echo "Archive contents:" >&2
    cat "$TMP_DIR/archive-list.txt" >&2
    echo "Extracted files:" >&2
    find "$OUT_DIR" -maxdepth 2 -type f -print >&2
    exit 1
fi

echo "Saved:"
echo "  $B04_FILE  (red band, one-band UINT16 GeoTIFF)"
echo "  $B08_FILE  (near-infrared band, one-band UINT16 GeoTIFF)"
echo "  $B8A_FILE  (narrow near-infrared band, one-band UINT16 GeoTIFF)"
echo "  $B11_FILE  (SWIR band, one-band UINT16 GeoTIFF)"
echo "  $OUT_DIR/request.json"

if command -v gdalinfo >/dev/null 2>&1; then
    for band_file in "$B04_FILE" "$B08_FILE" "$B8A_FILE" "$B11_FILE"; do
        echo
        echo "$(basename "$band_file") summary:"
        gdalinfo "$band_file" | grep -E 'Size is|Coordinate System is|Band [0-9]'
    done
fi
