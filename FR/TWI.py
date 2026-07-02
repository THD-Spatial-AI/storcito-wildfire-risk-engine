import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

def Twi(input_twi, output_twi=None, output_twi_risk=None, show_plots=True):
    print('Running TWI layer...')

    while True:
        save_answer = input("Do you want to save the TWI images (y/n): ").strip().lower()
        if save_answer in ('y', 'n'):
            break
        print("Enter 'y' or 'n'.")

    save_outputs = (save_answer == 'y')

    out_dir_tif = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re'
    out_dir_png = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\TWI'

    if output_twi is None:
        output_twi = os.path.join(out_dir_tif, 'twi.tif')

    if output_twi_risk is None:
        output_twi_risk = os.path.join(out_dir_tif, 'twi_risk_map.tif')

    print('Reading TWI raster...')
    with rasterio.open(input_twi) as src:
        twi_ma = src.read(1, masked=True).astype('float32')
        meta_ref = src.meta.copy()

    twi = twi_ma.filled(np.nan)
    valid = np.isfinite(twi)

    if not np.any(valid):
        raise ValueError("The TWI layer does not contain valid values.")

    print('Calculating TWI percentiles and risk...')
    p20, p40, p60, p80 = np.percentile(twi[valid], [20, 40, 60, 80])

    # Reclasification: assign values 1-5 for risk levels
    reclasificado = np.zeros(twi.shape, dtype=np.uint8)
    reclasificado[(twi <= p20) & valid] = 1
    reclasificado[(twi > p20) & (twi <= p40)] = 2
    reclasificado[(twi > p40) & (twi <= p60)] = 3
    reclasificado[(twi > p60) & (twi <= p80)] = 4
    reclasificado[(twi > p80) & valid] = 5

    if save_outputs:
        print('Saving TIFF files...')
        os.makedirs(out_dir_tif, exist_ok=True)

        meta_twi = meta_ref.copy()
        meta_twi.update(driver='GTiff', dtype='float32', count=1, nodata=np.nan)

        with rasterio.open(output_twi, 'w', **meta_twi) as dst:
            dst.write(twi.astype('float32'), 1)

        meta_recl = meta_ref.copy()
        meta_recl.update(driver='GTiff', dtype='uint8', count=1, nodata=0)

        with rasterio.open(output_twi_risk, 'w', **meta_recl) as dst:
            dst.write(reclasificado, 1)

        print(f"Continuous TWI saved at: {output_twi}")
        print(f"Reclassified TWI saved at: {output_twi_risk}")

    print('Preparing TWI visualization...')
    twi_plot = np.ma.masked_invalid(twi)
    risk_plot = np.ma.masked_where(reclasificado == 0, reclasificado)

    print('Displaying TWI layer...')
    plt.figure(figsize=(8, 6))
    plt.imshow(twi_plot, cmap='RdYlGn_r')
    plt.colorbar(label='TWI value')
    plt.title('TWI')
    plt.tight_layout()

    if save_outputs:
        os.makedirs(out_dir_png, exist_ok=True)
        plt.savefig(os.path.join(out_dir_png, 'twi.png'), dpi=300, bbox_inches='tight')

    if show_plots:
        plt.show()
    plt.close()

    print('Displaying TWI risk map...')
    plt.figure(figsize=(8, 6))
    plt.imshow(risk_plot, cmap='Reds', vmin=0, vmax=5)
    cbar = plt.colorbar(label='TWI risk (1=low, 5=high)')
    cbar.set_ticks([1, 2, 3, 4, 5])
    plt.title('TWI Risk Map')
    plt.tight_layout()

    if save_outputs:
        plt.savefig(os.path.join(out_dir_png, 'twi_risk_map.png'), dpi=300, bbox_inches='tight')
        print(f"PNGs saved in: {out_dir_png}")

    if show_plots:
        plt.show()
    plt.close()

    if not save_outputs:
        print("Results not saved. Only displayed on screen.")

    print('TWI Layer completed')
