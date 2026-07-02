import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

def Lst(input_lst, output_lst=None, output_lst_risk=None, show_plots=True):
    print('Executing LST layer...')

    while True:
        save_answer = input("Do you want to save the LST images? (y/n): ").strip().lower()
        if save_answer in ('y', 'n'):
            break
        print("Invalid input. Please enter 'y' or 'n'.")

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

    #Reclasification by percentiles: assign values 1-5 for risk levels
    p20, p40, p60, p80 = np.percentile(lst_clean[valid], [20, 40, 60, 80])

    reclasificado = np.zeros_like(lst, dtype='int32')
    reclasificado[(lst_clean <= p20) & valid] = 1
    reclasificado[(lst_clean > p20) & (lst_clean <= p40)] = 2
    reclasificado[(lst_clean > p40) & (lst_clean <= p60)] = 3
    reclasificado[(lst_clean > p60) & (lst_clean <= p80)] = 4
    reclasificado[(lst_clean > p80) & valid] = 5
    
    out_dir_tif = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re'
    out_dir_png = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\LST'

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
