#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

trap 'echo "ERROR: command failed at line ${LINENO}" >&2' ERR

###############################################################################
# Spain + Canary Islands roads and railways from OpenStreetMap / GeoFabrik
#
# Usage:
#   bash infrastructure.sh
#   bash infrastructure.sh /path/to/work-directory
#
# Optional settings:
#   TARGET_CRS=EPSG:4326 bash infrastructure.sh
#
#   ROAD_CLASSES=motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link \
#       bash infrastructure.sh
#
#   RAIL_CLASSES=rail,light_rail,subway,tram,narrow_gauge,monorail,funicular,preserved \
#       bash infrastructure.sh
###############################################################################

WORKDIR="${1:-${PWD}/geofabric}"

RAW_DIR="${WORKDIR}/raw"
FILTER_DIR="${WORKDIR}/filtered"
OUTPUT_DIR="${WORKDIR}/output"
TMP_DIR="${WORKDIR}/tmp"

mkdir -p \
    "${RAW_DIR}" \
    "${FILTER_DIR}" \
    "${OUTPUT_DIR}" \
    "${TMP_DIR}"

TARGET_CRS="${TARGET_CRS:-EPSG:4326}"

ROAD_CLASSES="${ROAD_CLASSES:-motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link}"

RAIL_CLASSES="${RAIL_CLASSES:-rail,light_rail,subway,tram,narrow_gauge,monorail,funicular,preserved}"

SPAIN_URL="https://download.geofabrik.de/europe/spain-latest.osm.pbf"
CANARY_URL="https://download.geofabrik.de/africa/canary-islands-latest.osm.pbf"

SPAIN_RAW="${RAW_DIR}/spain-latest.osm.pbf"
CANARY_RAW="${RAW_DIR}/canary-islands-latest.osm.pbf"

SPAIN_FILTERED="${FILTER_DIR}/spain-transport.osm.pbf"
CANARY_FILTERED="${FILTER_DIR}/canary-transport.osm.pbf"

MERGED_PBF="${OUTPUT_DIR}/spain-canary-transport.osm.pbf"

LAYER_NAME="spain_canary_transport"
SHP_BASE="${OUTPUT_DIR}/${LAYER_NAME}"
SHP_FILE="${SHP_BASE}.shp"

###############################################################################
# Dependency checks
###############################################################################

required_commands=(
    curl
    md5sum
    osmium
    ogr2ogr
    ogrinfo
)

for command_name in "${required_commands[@]}"; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "ERROR: missing required command: ${command_name}" >&2
        exit 1
    fi
done

if ! ogrinfo --formats 2>/dev/null | grep -q "OSM"; then
    echo "ERROR: your GDAL installation does not include the OSM driver." >&2
    exit 1
fi

if ! ogrinfo --formats 2>/dev/null | grep -q "ESRI Shapefile"; then
    echo "ERROR: your GDAL installation does not include the ESRI Shapefile driver." >&2
    exit 1
fi

###############################################################################
# Download a GeoFabrik extract and verify its checksum
###############################################################################

download_geofabrik() {
    local url="$1"
    local destination="$2"

    local directory
    local filename
    local checksum_file

    directory="$(dirname "${destination}")"
    filename="$(basename "${destination}")"
    checksum_file="${destination}.md5"

    echo
    echo "Getting checksum for ${filename}..."

    curl \
        --fail \
        --location \
        --retry 5 \
        --retry-delay 5 \
        --output "${checksum_file}" \
        "${url}.md5"

    if [[ -s "${destination}" ]]; then
        if (
            cd "${directory}"
            md5sum --check --status "${filename}.md5"
        ); then
            echo "Using existing verified file:"
            echo "  ${destination}"
            return
        fi

        echo "Existing file failed checksum verification; downloading again."
        rm -f "${destination}"
    fi

    echo "Downloading:"
    echo "  ${url}"

    rm -f "${destination}.part"

    curl \
        --fail \
        --location \
        --retry 5 \
        --retry-delay 5 \
        --continue-at - \
        --output "${destination}.part" \
        "${url}"

    mv "${destination}.part" "${destination}"

    echo "Verifying ${filename}..."

    (
        cd "${directory}"
        md5sum --check "${filename}.md5"
    )
}

###############################################################################
# Filter desired road and railway ways
###############################################################################

filter_transport() {
    local input_file="$1"
    local output_file="$2"

    echo
    echo "Filtering:"
    echo "  ${input_file}"

    osmium tags-filter \
        --overwrite \
        --output "${output_file}" \
        "${input_file}" \
        "w/highway=${ROAD_CLASSES}" \
        "w/railway=${RAIL_CLASSES}"
}

###############################################################################
# Download Spain and Canary Islands extracts
###############################################################################

download_geofabrik \
    "${SPAIN_URL}" \
    "${SPAIN_RAW}"

download_geofabrik \
    "${CANARY_URL}" \
    "${CANARY_RAW}"

###############################################################################
# Filter both extracts
###############################################################################

filter_transport \
    "${SPAIN_RAW}" \
    "${SPAIN_FILTERED}"

filter_transport \
    "${CANARY_RAW}" \
    "${CANARY_FILTERED}"

###############################################################################
# Merge Spain and the Canary Islands
###############################################################################

echo
echo "Merging the filtered extracts..."

osmium merge \
    --overwrite \
    --output "${MERGED_PBF}" \
    "${SPAIN_FILTERED}" \
    "${CANARY_FILTERED}"

###############################################################################
# Verify that all way-node references are present
###############################################################################

echo
echo "Checking OSM references..."

osmium check-refs "${MERGED_PBF}"

###############################################################################
# Remove any previous shapefile
###############################################################################

echo
echo "Removing previous shapefile output..."

rm -f \
    "${SHP_BASE}.shp" \
    "${SHP_BASE}.shx" \
    "${SHP_BASE}.dbf" \
    "${SHP_BASE}.prj" \
    "${SHP_BASE}.cpg" \
    "${SHP_BASE}.qix" \
    "${SHP_BASE}.sbn" \
    "${SHP_BASE}.sbx"

###############################################################################
# SQL query
#
# Important:
# The geometry column must be included explicitly. Without it, GDAL may create
# only a DBF table and no .shp geometry file.
###############################################################################

OGR_SQL=$(cat <<'SQL'
SELECT
    geometry,

    CAST(osm_id AS TEXT) AS osm_id,
    name,
    highway,
    railway,

    CASE
        WHEN highway IS NOT NULL AND railway IS NOT NULL
            THEN 'road_rail'
        WHEN highway IS NOT NULL
            THEN 'road'
        WHEN railway IS NOT NULL
            THEN 'railway'
        ELSE 'unknown'
    END AS transport,

    hstore_get_value(other_tags, 'ref') AS ref,
    hstore_get_value(other_tags, 'oneway') AS oneway,
    hstore_get_value(other_tags, 'maxspeed') AS maxspeed,
    hstore_get_value(other_tags, 'lanes') AS lanes,
    hstore_get_value(other_tags, 'surface') AS surface,
    hstore_get_value(other_tags, 'bridge') AS bridge,
    hstore_get_value(other_tags, 'tunnel') AS tunnel,
    hstore_get_value(other_tags, 'layer') AS layer,
    hstore_get_value(other_tags, 'service') AS service,
    hstore_get_value(other_tags, 'gauge') AS gauge,
    hstore_get_value(other_tags, 'electrified') AS electric,
    hstore_get_value(other_tags, 'operator') AS operator,
    hstore_get_value(other_tags, 'usage') AS usage

FROM lines

WHERE highway IS NOT NULL
   OR railway IS NOT NULL
SQL
)

###############################################################################
# Convert merged OSM PBF to one shapefile
#
# The destination is OUTPUT_DIR rather than the .shp filename. The Shapefile
# driver then creates OUTPUT_DIR/spain_canary_transport.shp using -nln.
###############################################################################

echo
echo "Creating shapefile:"
echo "  ${SHP_FILE}"

ogr2ogr \
    --config CPL_TMPDIR "${TMP_DIR}" \
    -f "ESRI Shapefile" \
    "${OUTPUT_DIR}" \
    "${MERGED_PBF}" \
    -dialect SQLite \
    -sql "${OGR_SQL}" \
    -nln "${LAYER_NAME}" \
    -nlt PROMOTE_TO_MULTI \
    -t_srs "${TARGET_CRS}" \
    -lco ENCODING=UTF-8 \
    -overwrite \
    -progress

###############################################################################
# Verify the shapefile
###############################################################################

required_output_files=(
    "${SHP_BASE}.shp"
    "${SHP_BASE}.shx"
    "${SHP_BASE}.dbf"
    "${SHP_BASE}.prj"
)

for output_file in "${required_output_files[@]}"; do
    if [[ ! -s "${output_file}" ]]; then
        echo
        echo "ERROR: expected output file is missing or empty:" >&2
        echo "  ${output_file}" >&2

        echo
        echo "Files currently present in ${OUTPUT_DIR}:"
        find "${OUTPUT_DIR}" \
            -maxdepth 1 \
            -type f \
            -printf '  %f  %s bytes\n' \
            | sort

        exit 1
    fi
done

###############################################################################
# Show output information
###############################################################################

echo
echo "Output created successfully:"
ls -lh "${SHP_BASE}".*

echo
echo "Merged filtered PBF:"
echo "  ${MERGED_PBF}"

echo
echo "Shapefile:"
echo "  ${SHP_FILE}"

echo
echo "Layer information:"

ogrinfo \
    -ro \
    -so \
    -al \
    "${SHP_FILE}"

echo
echo "Done."