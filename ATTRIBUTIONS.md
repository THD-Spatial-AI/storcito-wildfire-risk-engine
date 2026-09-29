# Attributions

This file lists the original code, data sources and third-party software used by the STORCITO Wildfire Risk Engine, with their licences and required attribution statements. Licences of third-party components apply to those components; the code in this repository is licensed under the [MIT License](LICENSE).

Where a provider's licence is not stated below, follow the terms published on the provider's page before redistributing data or derived maps.

## Original Code

This engine builds on the original STORCITO wildfire-risk code developed at Universidade de Vigo ([Mat-GL-02/STORCITO](https://github.com/Mat-GL-02/STORCITO)). [CHANGELOG.md](CHANGELOG.md) documents the changes made relative to that code.

## Data Sources

| Source | Engine layer(s) | Licence / attribution |
|---|---|---|
| [IGN INSPIRE elevation model (MDT)](https://www.ign.es) via `servicios.idee.es` WCS | `dtm`, `mdt`, `twi` (elevation, slope, aspect, TWI; reference grid) | CC BY 4.0. Attribution: "Derived from MDT © Instituto Geográfico Nacional". |
| [MeteoGalicia WRF ARW 1 km](https://www.meteogalicia.gal) via THREDDS | `fwi` (hourly weather for the Canadian Fire Weather Index) | See MeteoGalicia terms of use. Credit: MeteoGalicia, Xunta de Galicia. |
| [Copernicus Sentinel-2 L2A](https://dataspace.copernicus.eu) (Copernicus Data Space Ecosystem) | `sentinel`, `hist-scenes` (NDVI, NDMI, dNBR) | Free and open Copernicus data policy. Attribution: "Contains modified Copernicus Sentinel data [year]". |
| [Copernicus Sentinel-3 SLSTR Level-2 LST](https://dataspace.copernicus.eu) (`SENTINEL3_SLSTR_L2_LST`) | `lst` | Free and open Copernicus data policy. Attribution: "Contains modified Copernicus Sentinel data [year]". |
| [CLC+ Backbone 2023](https://library.land.copernicus.eu/products/CLCplus_Backbone_2023_PUM_v1.html) | `clc` (preferred non-fuel exclusion mask) | Copernicus Land Monitoring Service data policy. Attribution: "© European Union, Copernicus Land Monitoring Service 2023, European Environment Agency (EEA)". |
| [CORINE Land Cover 2018](https://land.copernicus.eu/en/products/corine-land-cover) | `iuf` (WUI proxy; fallback exclusion mask) | Copernicus Land Monitoring Service data policy. Attribution: "© European Union, Copernicus Land Monitoring Service 2018, European Environment Agency (EEA)". |
| [MITECO Forest Map of Spain](https://www.miteco.gob.es) (`modelocombustible`) | `fuels` | See MITECO terms of use. Credit: Ministerio para la Transición Ecológica y el Reto Demográfico. |
| [OpenStreetMap](https://www.openstreetmap.org) via the [Geofabrik](https://download.geofabrik.de) Galicia extract | `infra`, WUI road preselection | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/). Attribution: "© OpenStreetMap contributors". |
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov) (MODIS active fire detections) | `hist` (informational burn-history overlay) | NASA open data. Acknowledgement: "We acknowledge the use of data and/or imagery from NASA's Fire Information for Resource Management System (FIRMS), part of NASA's Earth Science Data and Information System (ESDIS)." |
| [OpenDataSoft georef-spain](https://public.opendatasoft.com) (`georef-spain-comunidad-autonoma`, `georef-spain-provincia`, `georef-spain-municipio`) | `borders` | See the dataset pages on OpenDataSoft. |

## Software Libraries

Licences below were read from the installed packages where available.

### Python engine

| Library | Licence |
|---|---|
| [GDAL](https://gdal.org) (cite as <https://doi.org/10.5281/zenodo.5884351>) | MIT |
| [Rasterio](https://rasterio.readthedocs.io) | BSD-3-Clause |
| [GeoPandas](https://geopandas.org) | BSD-3-Clause |
| [Fiona](https://fiona.readthedocs.io) | BSD-3-Clause |
| [Shapely](https://shapely.readthedocs.io) | BSD-3-Clause |
| [NumPy](https://numpy.org) | BSD-3-Clause |
| [SciPy](https://scipy.org) | BSD-3-Clause |
| [pandas](https://pandas.pydata.org) | BSD-3-Clause |
| [Matplotlib](https://matplotlib.org) | Matplotlib License (PSF-based) |
| [netCDF4](https://unidata.github.io/netcdf4-python/) | MIT |
| [openpyxl](https://openpyxl.readthedocs.io) | MIT |
| [FastAPI](https://fastapi.tiangolo.com) | MIT |
| [Uvicorn](https://www.uvicorn.org) | BSD-3-Clause |
| [Gunicorn](https://gunicorn.org) | MIT |
| [HTTPX](https://www.python-httpx.org) | BSD-3-Clause |
| [psycopg2](https://www.psycopg.org) | LGPL-3.0-or-later |
| [earthaccess](https://github.com/nsidc/earthaccess) | MIT |
| [Rich](https://github.com/Textualize/rich) | MIT |
| [questionary](https://github.com/tmbo/questionary) | MIT |
| [pytest](https://pytest.org) | MIT |

### Infrastructure and tools

| Component | Licence |
|---|---|
| [PostgreSQL](https://www.postgresql.org) | PostgreSQL License |
| [PostGIS](https://postgis.net) / [pgRouting](https://pgrouting.org) (pgrouting/pgrouting image) | GPL-2.0-or-later |
| [GRASS GIS](https://grass.osgeo.org) (`r.fill.dir`, `r.topidx` for TWI) | GPL-2.0-or-later |
| [QGIS](https://qgis.org) (geotools image) | GPL-2.0-or-later |
| [HAProxy](https://www.haproxy.org) | GPL-2.0-or-later |
| [nginx](https://nginx.org) | BSD-2-Clause |
| [micromamba](https://mamba.readthedocs.io) | BSD-3-Clause |

## Research

- Novo, A., Fariñas-Álvarez, N., Martínez-Sánchez, J., González-Jorge, H., Fernández-Alonso, J. M., & Lorenzo, H. (2020). Mapping forest fire risk—A case study in Galicia (Spain). *Remote Sensing, 12*(22), 3705. <https://doi.org/10.3390/rs12223705> — AHP approach, static weights and default FWI class boundaries.
- Van Wagner, C. E. (1987). *Development and structure of the Canadian Forest Fire Weather Index System* (Forestry Technical Report 35). Canadian Forestry Service — Fire Weather Index equations.
- Saaty, R. W. (1987). The analytic hierarchy process—What it is and how it is used. *Mathematical Modelling, 9*(3–5), 161–176. <https://doi.org/10.1016/0270-0255(87)90473-8>
- Gao, B.-C. (1996). NDWI—A normalized difference water index for remote sensing of vegetation liquid water from space. *Remote Sensing of Environment, 58*(3), 257–266. <https://doi.org/10.1016/S0034-4257(96)00067-3> — basis for NDMI.
- Key, C. H., & Benson, N. C. (2006). Landscape assessment (LA). In *FIREMON: Fire effects monitoring and inventory system* (RMRS-GTR-164-CD). USDA Forest Service — basis for dNBR.
