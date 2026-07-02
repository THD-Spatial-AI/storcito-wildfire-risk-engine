import earthaccess
from pathlib import Path

# Login once; persist=True saves credentials in ~/.netrc
earthaccess.login(strategy="interactive", persist=True)

# Bounding box order: min_lon, min_lat, max_lon, max_lat
bbox = (-18.2, 27.6, 4.4, 43.9)

granules = earthaccess.search_data(
    short_name="ASTGTM",
    version="003",
    provider="LPCLOUD",
    bounding_box=bbox,
    cloud_hosted=True,
    count=-1,  # return all matching granules
)

print(f"Found {len(granules)} granules")
for g in granules:
    print(g)

out_dir = Path("aster_gdem_v003")
files = earthaccess.download(granules, local_path=out_dir)

print("Downloaded:")
for f in files:
    print(f)