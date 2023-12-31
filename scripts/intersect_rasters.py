from typing import Dict
import csv
from collections import defaultdict
from os import path
from pathlib import Path
import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
from joblib import Parallel, delayed


def read_list_file(list_path: str):
    files = []
    csvfile = path.abspath(list_path)
    with open(csvfile, 'r') as f:
        reader = csv.reader(f)
        tifs = list(reader)
        tifs = [f[0].strip() for f in tifs
                if (len(f) > 0 and f[0].strip() and
                    f[0].strip()[0] != '#')]
    for f in tifs:
        files.append(path.abspath(f))
    return files


def generate_key_val(gl, shorts):
    eight_char_name = Path(gl).name[:8]
    if eight_char_name not in shorts:
        shorts[eight_char_name] += 1
    else:
        shorts[eight_char_name] += 1
    return gl, eight_char_name + str(shorts[eight_char_name])


def intersect_and_sample_shp(shp: Path, geotifs: Dict[str, str], dedupe: bool = False):
    print("====================================\n", f"intersecting {shp.as_posix()}")
    pts = gpd.read_file(shp)
    coords = np.array([(p.x, p.y) for p in pts.geometry])
    geom = pd.DataFrame(coords, columns=geom_cols, index=pts.index)
    pts = pts.merge(geom, left_index=True, right_index=True)
    if dedupe:
        tif_name = list(geotifs.keys())[0]
        tif = Path(tif_name)
        orig_cols = pts.columns
        with rasterio.open(tif) as src:
            # resample data to target shape
            data = src.read(
                out_shape=(
                    src.count,
                    int(src.height / downscale_factor),
                    int(src.width / downscale_factor)
                ),
                resampling=rasterio.enums.Resampling.bilinear
            )
            # scale image transform
            transform = src.transform * src.transform.scale(
                (src.width / data.shape[-1]),
                (src.height / data.shape[-2])
            )
            pts["rows"], pts["cols"] = rasterio.transform.rowcol(transform, coords[:, 0], coords[:, 1])

        # keep one only
        # pts_deduped = pts.sort_values(
        #     by=geom_cols, ascending=[True, True]
        # ).groupby(by=['rows', 'cols'], as_index=False).first()[orig_cols]

        # keep mean of repeated observations in a pixel
        #
        pts_count = pts.groupby(by=['rows', 'cols'], as_index=False).agg(pixel_count=('rows', 'count'))
        pts_mean = pts.groupby(by=['rows', 'cols'], as_index=False).mean()
        pts_deduped = pts_mean.merge(pts_count, how='inner', on=['rows', 'cols'])

        pts_deduped = gpd.GeoDataFrame(pts_deduped, geometry=gpd.points_from_xy(pts_deduped['POINT_X'],
                                                                                pts_deduped['POINT_Y']))
        # pts_deduped = pts.drop_duplicates(subset=['rows', 'cols'])[orig_cols]
        coords_deduped = np.array([(p.x, p.y) for p in pts_deduped.geometry])
    else:
        pts_deduped = pts
        coords_deduped = coords

    for i, (k, v) in enumerate(geotifs.items()):
        print(f"adding {i}/{len(geotifs)}: {k} to output dataframe")
        with rasterio.open(Path(k)) as src:
            pts_deduped[v] = [x[0] for x in src.sample(coords_deduped)]

    # pts_deduped = gpd.GeoDataFrame(pts_deduped, geometry=pts_deduped.geometry)
    output_dir = Path('out_resampled')
    output_dir.mkdir(exist_ok=True, parents=True)
    out_shp = output_dir.joinpath(shp.name)
    pts_deduped.to_file(out_shp.as_posix())
    print(f"saved intersected shapefile at {out_shp.as_posix()}")
    # pts.to_csv(Path("out").joinpath(shp.stem + ".csv"), index=False)
    return out_shp


if __name__ == '__main__':
    # local
    # tif_local = data_location.joinpath('LATITUDE_GRID1.tif')
    shapefile_location = Path("configs/data")
    shp = shapefile_location.joinpath('geochem_sites.shp')

    downscale_factor = 2  # keep 1 point in a 2x2 cell

    geom_cols = ['POINT_X', 'POINT_Y']

    covariastes_list = "configs/data/sirsam/covariates_list.txt"
    geotifs_list = read_list_file(list_path=covariastes_list)
    shorts = defaultdict(int)

    geotifs = {}

    for gl in geotifs_list:
        k, v = generate_key_val(gl, shorts)
        geotifs[k] = v

    # check all required files are available on disc
    for k, v in geotifs.items():
        print(f"checking if {k} exists")
        assert Path(k).exists()

    print("Add under the 'intersected_features:' section in yaml")
    print('='*100)
    for k, v in geotifs.items():
        print(f"\"{Path(k).name}\": \"{v}\"")

    print('='*100)

    out_shp = intersect_and_sample_shp(shp, geotifs, dedupe=False)
    df2 = gpd.GeoDataFrame.from_file(out_shp.as_posix())
    df3 = df2[list(geotifs.values())]
    df4 = df2.loc[(df3.isna().sum(axis=1) == 0) & ((np.abs(df3) < 1e10).sum(axis=1) == len(geotifs)), :]
    df5 = df2.loc[~((df3.isna().sum(axis=1) == 0) & ((np.abs(df3) < 1e10).sum(axis=1) == len(geotifs))), :]

    if df5.shape[0]:
        df4.to_file(out_shp.parent.joinpath(out_shp.stem + '_cleaned.shp'))
        print(f"Wrote clean shapefile {out_shp.parent.joinpath(out_shp.stem + '_cleaned.shp')}")
        df5.to_file(out_shp.parent.joinpath(out_shp.stem + '_cleaned_dropped.shp'))
    else:
        print(f"No points dropped and there for _cleaned.shp file is not createed'")
        print(f"No points dropped and there for _cleaned_dropped.shp file is not created")

# rets = Parallel(
#     n_jobs=-1,
#     verbose=100,
# )(delayed(intersect_and_sample_shp)(s) for s in shapefile_location.glob("*.shp"))
