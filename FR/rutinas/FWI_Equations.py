"""
Vectorized script to calculate FWI parameters.
Optimized for NumPy arrays (no slow loops).
"""
import numpy as np


def ffmc(temp, hum, wind, rain, f0) -> np.ndarray:
    """
    Calculate the Fine Fuel Moisture Code (FFMC).
    
    The FFMC represents the moisture content of fine fuels (up to 16mm thick) that respond
    rapidly to changes in relative humidity and temperature. Ranges from 0 to 101.
    
    Args:
        temp: Air temperature in °C
        hum: Relative humidity in percentage (0-100)
        wind: Wind speed at 10m height in km/h
        rain: Precipitation in mm
        f0: Previous FFMC value
    
    Returns:
        np.ndarray: Calculated FFMC value (scale 0-101)
    
    References:
        Van Wagner, C.E. (1987). Development and Structure of the Canadian Forest Fire 
        Weather Index System. Canadian Forest Service Publication 35.
    """
    # Create copies to avoid modifying originals
    mo = 147.2 * ((101.0 - f0) / (59.5 + f0))
    
    # Rain logic (Vectorized)
    # If precipitation > 0.5mm
    rain_mask = rain > 0.5
    rf = np.zeros_like(rain)
    rf[rain_mask] = rain[rain_mask] - 0.5
    
    mr = np.copy(mo)
    
    # Case 1: mo > 150 and rain
    mask1 = rain_mask & (mo > 150)
    if np.any(mask1):
        mr[mask1] = mo[mask1] + 42.5 * rf[mask1] * np.exp(-100.0 / (251.0 - mo[mask1])) * \
                    (1.0 - np.exp(-6.93 / rf[mask1])) + \
                    0.0015 * ((mo[mask1] - 150.0)**2) * (rf[mask1]**0.5)

    # Case 2: mo <= 150 and rain
    mask2 = rain_mask & (mo <= 150)
    if np.any(mask2):
        mr[mask2] = mo[mask2] + 42.5 * rf[mask2] * np.exp(-100.0 / (251.0 - mo[mask2])) * \
                    (1.0 - np.exp(-6.93 / rf[mask2]))

    # Upper limit of MR
    mr[mr > 250.0] = 250.0
    
    # If no rain, mr equals mo
    mr[~rain_mask] = mo[~rain_mask]

    # Drying phase
    ed = 0.942 * (hum**0.679) + 11.0 * np.exp((hum - 100.0) / 10.0) + \
         0.18 * (21.1 - temp) * (1.0 - np.exp(-0.115 * hum))
    
    # Van Wagner eq. 5: exponent must be negative.
    ew = 0.618 * (hum**0.753) + 10.0 * np.exp((hum - 100.0) / 10.0) + \
         0.18 * (21.1 - temp) * (1.0 - np.exp(-0.115 * hum))

    m = np.copy(mr)
    
    # Drying phase (mr > ed)
    drying_mask = mr > ed
    if np.any(drying_mask):
        k0 = 0.424 * (1.0 - (hum[drying_mask]/100.0)**1.7) + \
             0.0694 * (wind[drying_mask]**0.5) * (1.0 - (hum[drying_mask]/100.0)**8)
        kd = k0 * 0.581 * np.exp(0.0365 * temp[drying_mask])
        m[drying_mask] = ed[drying_mask] + (mr[drying_mask] - ed[drying_mask]) * (10.0**(-kd))

    # Wetting phase (mr < ew)
    wetting_mask = mr < ew
    if np.any(wetting_mask):
        k1 = 0.424 * (1.0 - ((100.0-hum[wetting_mask])/100.0)**1.7) + \
             0.0694 * (wind[wetting_mask]**0.5) * (1.0 - ((100.0-hum[wetting_mask])/100.0)**8)
        kw = k1 * 0.581 * np.exp(0.0365 * temp[wetting_mask])
        m[wetting_mask] = ew[wetting_mask] - (ew[wetting_mask] - mr[wetting_mask]) * (10.0**(-kw))

    # Neutral range: m = mr (already copied)

    # Final calculation of F
    f = 59.5 * (250.0 - m) / (147.2 + m)
    f = np.clip(f, 0.0, 101.0)  # Ensure bounds 0-101
    
    return f

def dmc(temp, hum, rain, p0, month) -> np.ndarray:
    """
    Calculate the Duff Moisture Code (DMC).
    
    The DMC represents the moisture content of the duff and litter layer (2-4 cm thick).
    Responds more slowly to weather changes compared to FFMC.
    
    Args:
        temp: Air temperature in °C
        hum: Relative humidity in percentage (0-100)
        rain: Precipitation in mm
        p0: Previous DMC value
        month: Month of the year (1-12)
    
    Returns:
        np.ndarray: Calculated DMC value
    
    References:
        Van Wagner, C.E. (1987). Development and Structure of the Canadian Forest Fire 
        Weather Index System. Canadian Forest Service Publication 35.
    """
    Le_factors = [6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0]
    le = Le_factors[int(month) - 1]  # Assume single month for entire map
    
    # Ensure minimum temperature for calculation
    t_calc = np.maximum(temp, -1.1)
    
    # Effective rainfall
    rain_mask = rain > 1.5
    re = np.zeros_like(rain)
    re[rain_mask] = 0.92 * rain[rain_mask] - 1.27
    
    mo = 20.0 + np.exp(5.6348 - (p0 / 43.43))
    
    b = np.zeros_like(p0)
    # Calculate b based on p0
    mask_b1 = p0 <= 33
    b[mask_b1] = 100.0 / (0.5 + 0.3 * p0[mask_b1])
    
    mask_b2 = (p0 > 33) & (p0 <= 65)
    b[mask_b2] = 14.0 - 1.3 * np.log(p0[mask_b2])
    
    mask_b3 = p0 > 65
    b[mask_b3] = 6.2 * np.log(p0[mask_b3]) - 17.2
    
    mr = np.copy(mo)
    # Apply rainfall to mr
    if np.any(rain_mask):
        mr[rain_mask] = mo[rain_mask] + (1000.0 * re[rain_mask]) / (48.77 + b[rain_mask] * re[rain_mask])
        
    pr = 244.72 - 43.43 * np.log(np.maximum(mr - 20.0, 0.1))  # Avoid log(<=0)
    pr = np.maximum(pr, 0.0)
    
    # If no rain, pr is p0
    pr[~rain_mask] = p0[~rain_mask]
    
    # Drying
    k = 1.894 * (t_calc + 1.1) * (100.0 - hum) * le * 1e-6
    k = np.maximum(k, 0.0)  # K cannot be negative
    
    p = pr + 100.0 * k
    return p

def dc(temp, rain, month, d0) -> np.ndarray:
    """
    Calculate the Drought Code (DC).
    
    The DC represents the moisture content of deep organic layers and peat (up to 10cm).
    Responds very slowly to precipitation changes and indicates prolonged drought conditions.
    
    Args:
        temp: Air temperature in °C
        rain: Precipitation in mm
        month: Month of the year (1-12)
        d0: Previous DC value
    
    Returns:
        np.ndarray: Calculated DC value
    
    References:
        Van Wagner, C.E. (1987). Development and Structure of the Canadian Forest Fire 
        Weather Index System. Canadian Forest Service Publication 35.
    """
    Lf_factors = [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6]
    lf = Lf_factors[int(month) - 1]
    
    t_calc = np.maximum(temp, -2.8)
    
    rain_mask = rain > 2.8
    rd = np.zeros_like(rain)
    rd[rain_mask] = 0.83 * rain[rain_mask] - 1.27
    
    qo = 800.0 * np.exp(-d0 / 400.0)
    qr = np.copy(qo)
    
    if np.any(rain_mask):
        qr[rain_mask] = qo[rain_mask] + 3.937 * rd[rain_mask]
        
    dr = 400.0 * np.log(800.0 / np.maximum(qr, 0.1))  # Avoid division by zero
    dr = np.maximum(dr, 0.0)
    
    # If no rain, dr is d0
    dr[~rain_mask] = d0[~rain_mask]
    
    # Drying
    v = 0.36 * (t_calc + 2.8) + lf
    v = np.maximum(v, 0.0)
    
    d = dr + 0.5 * v
    return d

def isi(wind, f) -> np.ndarray:
    """
    Calculate the Initial Spread Index (ISI).
    
    The ISI estimates the initial rate of fire spread based on fine fuel moisture (FFMC)
    and wind speed. Typical range: 0-40.
    
    Args:
        wind: Wind speed at 10m height in km/h
        f: FFMC value
    
    Returns:
        np.ndarray: Calculated ISI value
    
    References:
        Van Wagner, C.E. (1987). Development and Structure of the Canadian Forest Fire 
        Weather Index System. Canadian Forest Service Publication 35.
    """
    f_u = np.exp(0.05039 * wind)
    m = 147.2 * (101.0 - f) / (59.5 + f)
    f_f = 91.9 * np.exp(-0.1386 * m) * (1.0 + (m**5.31) / 4.93e7)
    r = 0.208 * f_u * f_f
    return r

def bui(p, d) -> np.ndarray:
    """
    Calculate the Build-up Index (BUI).
    
    The BUI combines DMC and DC into a single index that reflects the amount of fuel
    available to sustain fire spread. Typical range: 0-100.
    
    Args:
        p: DMC value
        d: DC value
    
    Returns:
        np.ndarray: Calculated BUI value
    
    References:
        Van Wagner, C.E. (1987). Development and Structure of the Canadian Forest Fire 
        Weather Index System. Canadian Forest Service Publication 35.
    """
    u = np.zeros_like(p)
    
    mask1 = p <= 0.4 * d
    if np.any(mask1):
        u[mask1] = 0.8 * p[mask1] * d[mask1] / (p[mask1] + 0.4 * d[mask1] + 1e-6)
        
    mask2 = ~mask1
    if np.any(mask2):
        u[mask2] = p[mask2] - (1.0 - (0.8 * d[mask2] / (p[mask2] + 0.4 * d[mask2] + 1e-6))) * \
                   (0.92 + (0.0114 * p[mask2])**1.7)
                   
    u = np.maximum(u, 0.0)
    return u

def fwi(r, u) -> np.ndarray:
    """
    Calculate the Fire Weather Index (FWI).
    
    The FWI is the final index that integrates ISI (fire spread rate) and BUI (fuel availability).
    Provides an estimate of forest fire danger. Typical scale: 0-100 (no upper limit).
    
    Args:
        r: ISI value (Initial Spread Index)
        u: BUI value (Build-up Index)
    
    Returns:
        np.ndarray: Calculated FWI value
    
    References:
        Van Wagner, C.E. (1987). Development and Structure of the Canadian Forest Fire 
        Weather Index System. Canadian Forest Service Publication 35.
    """
    f_d = np.zeros_like(u)
    
    mask1 = u <= 80.0
    f_d[mask1] = 0.626 * (u[mask1]**0.809) + 2.0
    
    mask2 = u > 80.0
    f_d[mask2] = 1000.0 / (25.0 + 108.64 * np.exp(-0.023 * u[mask2]))
    
    b = 0.1 * r * f_d
    
    s = np.zeros_like(b)
    mask_s = b > 1.0
    s[mask_s] = np.exp(2.72 * (0.434 * np.log(b[mask_s]))**0.647)
    s[~mask_s] = b[~mask_s]
    
    return s
