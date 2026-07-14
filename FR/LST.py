import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt


def _env_breaks(name):
    """Fixed classification breakpoints from the environment (region-wide values computed once per run), overriding extent-local percentiles so tiled runs classify identically everywhere."""
    import os

    raw = os.environ.get(name, "")
    parts = [p for p in raw.replace(";", ",").split(",") if p.strip()]
    if len(parts) == 4:
        try:
            return tuple(float(p) for p in parts)
        except ValueError:
            pass
    return None


def _coerce_breaks(value):
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
    else:
        parts = list(value)
    if len(parts) != 4:
        raise ValueError("LST breakpoints must contain exactly four values")
    return tuple(float(part) for part in parts)


def Lst(input_lst, output_lst=None, output_lst_risk=None, show_plots=True):
    print('Executing LST layer...')

    import sys
    if sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty():
        while True:
            save_answer = input("Do you want to save the LST images? (y/n): ").strip().lower()
            if save_answer in ('y', 'n'):
                break
            print("Invalid input. Please enter 'y' or 'n'.")
    else:
        save_answer = 'y'  # non-interactive (engine subprocess): always save

    save_outputs = (save_answer == 'y')

    with rasterio.open(input_lst) as src:
        lst = src.read(1).astype('float32')
        meta_ref = src.meta.copy()
        nodata = src.nodata

    if nodata is not None:
        lst = np.where(lst == nodata, np.nan, lst)

    lst = np.where(~np.isfinite(lst), np.nan, lst)

    # Basic physical filtering for Kelvin
    valid = np.isfinite(lst) & (lst > 220.0) & (lst < 340.0)
    lst_clean = np.where(valid, lst, np.nan)

    if not np.any(valid):
        raise ValueError("The LST layer does not contain valid values after filtering.")

    print('Executing LST risk layer...')

    # Reclasification by percentiles: assign values 1-5 for risk levels
    fixed = _env_breaks("FFRM_LST_BREAKS")
    p20, p40, p60, p80 = fixed if fixed else np.percentile(lst_clean[valid], [20, 40, 60, 80])

    reclasificado = np.zeros_like(lst, dtype='int32')
    reclasificado[(lst_clean <= p20) & valid] = 1
    reclasificado[(lst_clean > p20) & (lst_clean <= p40)] = 2
    reclasificado[(lst_clean > p40) & (lst_clean <= p60)] = 3
    reclasificado[(lst_clean > p60) & (lst_clean <= p80)] = 4
    reclasificado[(lst_clean > p80) & valid] = 5
    
    base_dir = os.path.dirname(str(output_lst)) if output_lst else 'data/OUTPUT'
    out_dir_tif = base_dir
    out_dir_png = os.path.join(base_dir, 'PNGs')

    if output_lst is None:
        output_lst = os.path.join(out_dir_tif, 'LST.tif')

    if output_lst_risk is None:
        output_lst_risk = os.path.join(out_dir_tif, 'LST_risk_map.tif')

    print('Showing LST layer...')
    plt.figure(figsize=(8, 6))
    plt.imshow(lst_clean, cmap='inferno')
    plt.colorbar(label='LST (K)')
    plt.title('LST')
    plt.tight_layout()

    if save_outputs:
        os.makedirs(out_dir_png, exist_ok=True)
        plt.savefig(os.path.join(out_dir_png, 'lst.png'), dpi=300, bbox_inches='tight')

    if show_plots:
        plt.show()
    plt.close()

    print('Showing LST risk layer...')
    plt.figure(figsize=(8, 6))
    plt.imshow(
        np.where(reclasificado == 0, np.nan, reclasificado),
        cmap='RdYlGn_r',
        vmin=1,
        vmax=5
    )
    plt.colorbar(label='LST Risk (1=low, 5=high)')
    plt.title('LST Risk Map')
    plt.tight_layout()

    if save_outputs:
        os.makedirs(out_dir_png, exist_ok=True)
        plt.savefig(os.path.join(out_dir_png, 'lst_risk_map.png'), dpi=300, bbox_inches='tight')

    if show_plots:
        plt.show()
    plt.close()

    if save_outputs:
        print('Saving LST files...')
        os.makedirs(out_dir_tif, exist_ok=True)

        meta_lst = meta_ref.copy()
        meta_lst.update(driver='GTiff', dtype='float32', count=1, nodata=np.nan)

        with rasterio.open(output_lst, 'w', **meta_lst) as dst:
            dst.write(lst_clean.astype('float32'), 1)

        meta_recl = meta_ref.copy()
        meta_recl.update(driver='GTiff', dtype='int32', count=1, nodata=0)

        with rasterio.open(output_lst_risk, 'w', **meta_recl) as dst:
            dst.write(reclasificado.astype('int32'), 1)

        print(f"LST continuous saved in: {output_lst}")
        print(f"LST reclassified saved in: {output_lst_risk}")
        print(f"PNGs saved in: {out_dir_png}")
    else:
        print("Results not saved. Only displayed on screen.")

    print('LST Layer completed')


def lst_risk(input_lst, output_risk, *, breaks=None):
    """Non-interactive LST risk layer (Kelvin filter + percentile classes, as the original)."""
    with rasterio.open(input_lst) as src:
        lst = src.read(1).astype("float32")
        meta = src.meta.copy()
        nodata = src.nodata
    if nodata is not None:
        lst = np.where(lst == nodata, np.nan, lst)
    lst = np.where(~np.isfinite(lst), np.nan, lst)
    valid = np.isfinite(lst) & (lst > 220.0) & (lst < 340.0)
    if not np.any(valid):
        raise ValueError("The LST layer does not contain valid values after filtering.")
    lst_clean = np.where(valid, lst, np.nan)
    fixed = _coerce_breaks(breaks) or _env_breaks("FFRM_LST_BREAKS")
    p20, p40, p60, p80 = fixed if fixed else np.percentile(lst_clean[valid], [20, 40, 60, 80])
    r = np.zeros_like(lst, dtype="int32")
    r[(lst_clean <= p20) & valid] = 1
    r[(lst_clean > p20) & (lst_clean <= p40)] = 2
    r[(lst_clean > p40) & (lst_clean <= p60)] = 3
    r[(lst_clean > p60) & (lst_clean <= p80)] = 4
    r[(lst_clean > p80) & valid] = 5
    meta.update(driver="GTiff", dtype="int32", count=1, nodata=0)
    os.makedirs(os.path.dirname(str(output_risk)), exist_ok=True)
    with rasterio.open(output_risk, "w", **meta) as dst:
        dst.write(r, 1)
    return output_risk
