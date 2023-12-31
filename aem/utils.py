import joblib
from pathlib import Path
from typing import Tuple, Optional, List, Union

import numpy as np
from scipy.signal import medfilt2d
import pandas as pd
from sklearn.neighbors import KDTree
from aem.config import twod_coords, threed_coords, Config, additional_cols_for_tracking, cluster_line_segment_id
from aem.logger import aemlogger as log

# distance within which an interpretation point is considered to contribute to target values
dis_tol = 100  # meters, distance tolerance used`


def determine_and_sort_by_dominant_line_direction(line_data, sort_by_col: Optional[str] = 'POINT_X'):
    x_max, x_min, y_max, y_min = extent_of_data(line_data)
    if not sort_by_col:  # try to determine based on longest extent
        if abs(x_max-x_min) > abs(y_max-y_min):
            sort_by_col = 'POINT_X'
        else:
            sort_by_col = 'POINT_Y'
    log.info(f"sorting line by {sort_by_col}")
    line_data = line_data.sort_values(by=[sort_by_col], ascending=[True])
    return line_data


def prepare_aem_data(conf: Config, aem_data: pd.DataFrame):
    """
    :param conf:
    :param in_scope_aem_data:
    :param interp_data: dataframe with
    :param include_thickness:
    :param include_conductivity_derivatives:
    :return:
    """
    aem_data.reset_index(drop=True, inplace=True)
    if conf.smooth_twod_covariates:
        for line in np.unique(aem_data.cluster_line_no):
            line_indices = aem_data.cluster_line_no == line
            row_loc = aem_data.index[line_indices]
            aem_line_data = aem_data[line_indices]
            aem_data.loc[row_loc, :] = determine_and_sort_by_dominant_line_direction(aem_line_data)
            aem_line_cond_data = aem_data[line_indices][conf.conductivity_cols]
            aem_data.loc[row_loc, conf.conductivity_cols] = apply_twod_median_filter(conf, aem_line_cond_data)

    aem_data.loc[:, conf.thickness_cols] = aem_data[conf.thickness_cols].cumsum(axis=1)
    conductivity_copy = aem_data[conf.conductivity_cols].copy()
    conductivity_copy.columns = conf.conductivity_derivatives_cols
    conductivity_diff = conductivity_copy.diff(axis=1, periods=-1)
    conductivity_diff = conductivity_diff.ffill(axis=1)
    aem_data.loc[:, conf.conductivity_derivatives_cols] = conductivity_diff
    return aem_data


def apply_twod_median_filter(conf: Config, aem_conductivities: pd.DataFrame):
    kernel_size = conf.smooth_covariates_kernel_size
    log.info(f"smooth conductivity data using scipy.signal.medfilt2d using kernel size {kernel_size}")
    aem_data = aem_conductivities.to_numpy()
    aem_data = medfilt2d(aem_data, kernel_size=kernel_size)
    df = pd.DataFrame(aem_data, columns=aem_conductivities.columns, index=aem_conductivities.index)
    return df


def select_required_data_cols(conf: Config):
    cols = select_cols_used_in_model(conf)[:]
    if not conf.predict:
        cols.append(conf.group_col)
    return cols + twod_coords + additional_cols_for_tracking


def select_cols_used_in_model(conf: Config):
    cols = conf.conductivity_cols[:]
    if conf.include_aem_covariates:
        cols += conf.aem_covariate_cols
    if conf.include_conductivity_derivatives:
        cols += conf.conductivity_derivatives_cols
    if conf.include_thickness:
        cols += conf.thickness_cols
    return cols


def extent_of_data(data: pd.DataFrame) -> Tuple[float, float, float, float]:
    x_min, x_max = min(data['POINT_X']), max(data['POINT_X'])
    y_min, y_max = min(data['POINT_Y']), max(data['POINT_Y'])
    return x_max, x_min, y_max, y_min


def weighted_target(interp_data: pd.DataFrame, tree: KDTree, covariates_including_xy: pd.Series, weighted_model,
                    conf: Config):
    x = covariates_including_xy[twod_coords].values.reshape(1, -1)
    ind, dist = tree.query_radius(x, r=conf.cutoff_radius, return_distance=True)
    ind, dist = ind[0], dist[0]
    if len(dist):
        dist += 1e-6  # add just in case of we have a zero distance
        df = interp_data.iloc[ind]
        class_association_num = covariates_including_xy[conf.target_class_indicator_col] if \
            conf.target_class_indicator_col is not None else None
        if class_association_num is not None:
            boolean_multiplier_based_on_class_association = \
                df[conf.target_class_indicator_col].values == class_association_num
            # if there are no observations with the cutoff_radius in the same/class_association we dont want the
            # weighted_depth and weighted_weight to be zero
            log.debug(f"boolean nultiplier {boolean_multiplier_based_on_class_association}")
            if np.any(boolean_multiplier_based_on_class_association) == 0:
                log.debug("All targets with cutoff radius are in the different ero_dep region!")
                return None, None
            else:
                log.debug("Some targets with cutoff radius are in the same ero_dep region!")
        else:
            boolean_multiplier_based_on_class_association = np.ones_like(dist, dtype=np.bool)
        depths = df.Z_coor.values[boolean_multiplier_based_on_class_association]
        dist = dist[boolean_multiplier_based_on_class_association]
        weighted_depth = np.sum(depths * (1 / dist) ** 2) / np.sum((1 / dist) ** 2)
        weights = df.weight.values[boolean_multiplier_based_on_class_association]
        if weighted_model:
            weighted_weight = np.sum(weights * (1 / dist) ** 2) / np.sum((1 / dist) ** 2)
        else:
            weighted_weight = None

        return weighted_depth, weighted_weight
    else:
        return None, None


def convert_to_xy(conf: Config, aem_data, interp_data):
    log.info("convert to xy and target values...")
    weighted_model = conf.weighted_model

    selected = []
    tree = KDTree(interp_data[twod_coords])
    target_depths = []
    target_weights = []
    for xy in aem_data.iterrows():
        i, covariates_including_xy_ = xy

        y, w = weighted_target(interp_data, tree, covariates_including_xy_, weighted_model, conf)
        if y is not None:
            if weighted_model:
                if w is not None:
                    selected.append(covariates_including_xy_)  # in 2d conductivities are already in xy
                    target_depths.append(y)
                    target_weights.append(w)
            else:
                selected.append(covariates_including_xy_)  # in 2d conductivities are already in xy
                target_depths.append(y)
                target_weights.append(1.0)
    X = pd.DataFrame(selected)
    y = pd.Series(target_depths, name='target', index=X.index)
    w = pd.Series(target_weights, name='weight', index=X.index)

    return {'covariates': X, 'targets': y, 'weights': w}


def create_interp_data(conf: Config, input_interp_data):
    weighted_model = conf.weighted_model
    if conf.target_type_col is not None:
        line = input_interp_data[(input_interp_data[conf.target_type_col].isin(conf.included_target_type_categories))]
    else:
        line = input_interp_data

    if ('POINT_X' not in line.columns) and ('POINT_Y' not in line.columns):
        line = __add_x_y(line)
    line = line.rename(columns={conf.target_col: 'Z_coor'})

    if weighted_model:
        required_cols = threed_coords + ['weight']
    else:
        required_cols = threed_coords
    if conf.target_class_indicator_col is not None:
        required_cols.append(conf.target_class_indicator_col)
    line_required = line[required_cols]
    return line_required


def __add_x_y(line: pd.DataFrame):
    coords = np.array([(p.x, p.y) for p in line.geometry])
    geom = pd.DataFrame(coords, columns=['POINT_X', 'POINT_Y'], index=line.index)
    line = line.merge(geom, left_index=True, right_index=True)
    return line


def add_delta(line: pd.DataFrame, conf: Config, origin=None):
    """
    :param line:
    :param origin: origin of flight line, if not provided assumed ot be the at the lowest y value
    :return:
    """
    line = determine_and_sort_by_dominant_line_direction(line)
    line_cols = list(line.columns)
    line['POINT_X_diff'] = line['POINT_X'].diff()
    line['POINT_Y_diff'] = line['POINT_Y'].diff()
    line['delta'] = np.sqrt(line.POINT_X_diff ** 2 + line.POINT_Y_diff ** 2)
    line['delta'] = line['delta'].fillna(value=0.0)
    if origin is not None:
        line['delta'].iat[0] = np.sqrt(
            (line.POINT_X.iat[0] - origin[0]) ** 2 +
            (line.POINT_Y.iat[0] - origin[1]) ** 2
        )
    line['d'] = line['delta'].cumsum()
    line = line.sort_values(by=['d'], ascending=True)  # must sort by distance from origin of flight line
    cluster_id = str(np.unique(line.cluster_line_no)[0]) + '_'
    arrs = np.array_split(range(line.shape[0]), max(line.shape[0] // conf.aem_line_splits, 1))
    for i, a in enumerate(arrs):
        arrs[i] = np.ones_like(a) * i
    arr = np.concatenate(arrs).ravel().astype(str)
    line[cluster_line_segment_id] = pd.Series([cluster_id + b for b in arr], index=line.index)

    return line[line_cols + ['d', cluster_line_segment_id]]


def plot_2d_section_paper(
    X: pd.DataFrame,
    XP: pd.DataFrame,
    cluster_line_no,
    conf: Config,
    col_names: Optional[Union[str, List[str]]] = None,
    log_conductivity=False,
    slope=False,
    flip_column=False, v_min=0.3, v_max=0.8,
    topographic_drape=True,
    sort_vertically=True,
    ):
    if col_names is None:
        col_names = []
    if isinstance(col_names, str):
        col_names = [col_names]
    NN=30
    from scipy.signal import savgol_filter
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm, Normalize, SymLogNorm, PowerNorm
    # from matplotlib.colors import Colormap
    X = X[X.cluster_line_no == cluster_line_no]
    XP = XP[XP.cluster_line_no == cluster_line_no]
    X.d = X.d/1000
    XP.d = XP.d/1000
    X = determine_and_sort_by_dominant_line_direction(X, "POINT_Y")
    XP = determine_and_sort_by_dominant_line_direction(XP, 'POINT_Y')
    if slope:
        Z = X[conf.conductivity_derivatives_cols[:NN]]
        Z = Z - np.min(np.min((Z))) + 1.0e-10
    else:
        Z = X[conf.conductivity_cols[:NN]]
    if log_conductivity:
            Z = np.log10(Z)

    h = X[conf.thickness_cols[:NN]]
    h = h.mul(-1)
    elevation = X['demh1sv11'] if topographic_drape else 0
    elevation_stack = np.repeat(np.atleast_2d(elevation).T, h.shape[1], axis=1)
    h = h.add(elevation_stack)
    dd = X.d
    ddd = np.atleast_2d(dd).T
    d = np.repeat(ddd, h.shape[1], axis=1)
    fig, ax = plt.subplots(figsize=(6, 6))
    cmap = plt.get_cmap('turbo')

    if slope:
            norm = LogNorm(vmin=v_min, vmax=v_max)
    else:
        norm = Normalize(vmin=v_min, vmax=v_max)

    im = ax.pcolormesh(d, h, Z, norm=norm, cmap=cmap, linewidth=1, rasterized=True, shading='auto')
    fig.colorbar(im, ax=ax)
    # ax.set_ylim([300, 1100])
    ax.set_ylim([300, 800])
    ax.set_xlim([68, 83])
    axs = ax.twinx()
    if 'cv_pred' in X.columns:  # training/cross-val
            y_pred = X['cv_pred']
    elif 'oos_pred' in X.columns:
        y_pred = X['oos_pred']
    else:  # prediction
        y_pred = X['pred']
    if 'target' in XP.columns:
        target = XP['target']
        elevation_p = XP['demh1sv11']
        pred = savgol_filter(y_pred, 11, 3)
        ax.plot(XP.d, elevation_p - target, label='interpretation', linewidth=1.5, color='m')

    # pred = savgol_filter(y_pred, 11, 3)  # window size 51, polynomial order 3
    pred = y_pred
    # pred_upper = savgol_filter(pred - 2*np.sqrt(X.variance), 11, 3)  # window size 51, polynomial order 3
    # pred_lower = savgol_filter(pred + 2*np.sqrt(X.variance), 11, 3)  # window size 51, polynomial order 3
    pred_upper = pred - 2*np.sqrt(X.variance)
    pred_lower = pred + 2*np.sqrt(X.variance)
    ax.plot(X.d, elevation - pred, label='prediction', linewidth=1.5, color='k')
    y_upper = elevation-pred_upper
    y_lower = elevation-pred_lower
    # ax.plot(X.d, y_upper, "m-")
    # ax.plot(X.d, y_lower, "m-")
    # plt.fill_between(
    #     X.d,
    #     (elevation - pred) - pred_upper, (elevation - pred) - pred_lower, alpha=0.4, label='Predicted 80% interval'
    # )
    ax.plot(X.d, y_upper, label='lower 90% percentile', linewidth=1, color='k', ls='--')
    ax.plot(X.d, y_lower, label='upper 90% percentile', linewidth=1, color='k', ls='--')
    # plt.fill_between(
    #     X.d,
    #     (elevation - pred) - pred_upper, (elevation - pred) - pred_lower, alpha=0.4, label='Predicted 80% interval'
    # )


    # hack
    #     target = XP['target']
    #     elevation_p = XP['demh1sv11'] if topographic_drape else 0
    #     axs.plot(-XP.d, elevation_p - target, label='interpretation', linewidth=2, color='k')
    # hack end

    for c in col_names:
        axs.plot(X.d, -X[c] if flip_column else X[c], label=c, linewidth=1.5, color='red')

    ax.set_xlabel('distance along aem line (km)')
    ax.set_ylabel('depth (m)')

    if slope:
            plt.title("d(Conductivity) vs depth")
    else:
        plt.title("Conductivity vs depth")
    ax.legend()
    axs.legend()


def plot_2d_section(
    X: pd.DataFrame,
    cluster_line_no,
    conf: Config,
    col_names: Optional[Union[str, List[str]]] = None,
    log_conductivity=False,
    slope=False,
    flip_column=False, v_min=0.3, v_max=0.8,
    topographic_drape=True,
    sort_vertically=True,
    ):
    if col_names is None:
        col_names = []
    if isinstance(col_names, str):
        col_names = [col_names]

    from scipy.signal import savgol_filter
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm, Normalize, SymLogNorm, PowerNorm
    # from matplotlib.colors import Colormap
    X = X[X.cluster_line_no == cluster_line_no]
    X = determine_and_sort_by_dominant_line_direction(X)
    origin = (X.POINT_X.iat[0], X.POINT_Y.iat[0])
    if slope:
        Z = X[conf.conductivity_derivatives_cols]
        Z = Z - np.min(np.min((Z))) + 1.0e-10
    else:
        Z = X[conf.conductivity_cols]

    if log_conductivity:
        Z = np.log10(Z)

    h = X[conf.thickness_cols]
    h = h.mul(-1)
    elevation = X['demh1sv11'] if topographic_drape else 0
    elevation_stack = np.repeat(np.atleast_2d(elevation).T, h.shape[1], axis=1)
    h = h.add(elevation_stack)
    dd = X.d
    ddd = np.atleast_2d(dd).T
    d = np.repeat(ddd, h.shape[1], axis=1)
    fig, ax = plt.subplots(figsize=(40, 4))
    cmap = plt.get_cmap('turbo')

    if slope:
        norm = LogNorm(vmin=v_min, vmax=v_max)
    else:
        norm = Normalize(vmin=v_min, vmax=v_max)

    im = ax.pcolormesh(d, h, Z, norm=norm, cmap=cmap, linewidth=1, rasterized=True, shading='auto')
    fig.colorbar(im, ax=ax)
    # ax.set_ylim([-200, 0])
    axs = ax.twinx()
    if 'cv_pred' in X.columns:  # training/cross-val
        y_pred = X['cv_pred']
    elif 'oos_pred' in X.columns:
        y_pred = X['oos_pred']
    else:  # prediction
        y_pred = X['pred']
    if 'target' in X.columns:
        target = X['target']
        ax.plot(X.d, elevation - target, label='interpretation', linewidth=2, color='k')

    pred = savgol_filter(y_pred, 11, 3)  # window size 51, polynomial order 3
    ax.plot(X.d, elevation - pred, label='prediction', linewidth=2, color='c')

    for c in col_names:
        axs.plot(X.d, -X[c] if flip_column else X[c], label=c, linewidth=2, color='red')

    ax.set_xlabel('distance along aem line (m)')
    ax.set_ylabel('depth (m)')
    if slope:
        plt.title("d(Conductivity) vs depth")
    else:
        plt.title("Conductivity vs depth")

    ax.legend()
    axs.legend()
    # plt.show()
    # plt.savefig(str(cluster_line_no) + ".jpg")


def plot_conductivity(X: pd.DataFrame,
                      conf: Config,
                      cluster_line_no,
                      slope=False, flip_column=False, v_min=0.3, v_max=0.8,
                      log_conductivity=False
                      ):
    from scipy.signal import savgol_filter
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm, Normalize, SymLogNorm, PowerNorm
    from matplotlib.colors import Colormap
    X = X[X.cluster_line_no == cluster_line_no]
    if slope:
        Z = X[conf.conductivity_derivatives_cols]
        Z = Z - np.min(np.min((Z))) + 1.0e-10
    else:
        Z = X[conf.conductivity_cols]

    if log_conductivity:
        Z = np.log10(Z)

    h = X[conf.thickness_cols]
    dd = X.d
    ddd = np.atleast_2d(dd).T
    d = np.repeat(ddd, h.shape[1], axis=1)
    fig, ax = plt.subplots(figsize=(40, 4))
    cmap = plt.get_cmap('turbo')

    if slope:
        norm = LogNorm(vmin=v_min, vmax=v_max)
    else:
        norm = Normalize(vmin=v_min, vmax=v_max)

    im = ax.pcolormesh(d, -h, Z, norm=norm, cmap=cmap, linewidth=1, rasterized=True, shading='auto')
    fig.colorbar(im, ax=ax)

    ax.set_xlabel('distance along aem line (m)')
    ax.set_ylabel('depth (m)')
    if slope:
        plt.title("d(Conductivity) vs depth")
    else:
        plt.title("Conductivity vs depth")

    ax.legend()
    plt.show()


def export_model(model, conf: Config, model_type: str = 'learn'):
    if model_type not in {'learn', 'optimise'}:
        raise AttributeError("Model type must be one of 'learn' or 'optimise'")
    learned_model = model_type == 'learn'  # as opposed to optimised_model
    model_file = conf.model_file if learned_model else conf.optimised_model_file
    state_dict = {"model": model, "config": conf}
    with open(model_file, 'wb') as f:
        joblib.dump(state_dict, f)
        log.info(f"Wrote model on disc {model_file}")


def import_model(conf: Config, model_type: str = 'learn'):
    learned_model = model_type == 'learn'  # as opposed to optimised_model
    model_file = conf.model_file if learned_model else conf.optimised_model_file
    if not model_file.exists():
        raise FileExistsError(f"Model file {model_file.as_posix()} does not exist. Train or optimise model first!!")
    with open(model_file, 'rb') as f:
        state_dict = joblib.load(f)
        log.info(f"loaded trained model from location {conf.model_file}")
    model, conf = state_dict["model"], state_dict['config']
    return model, conf


def plot_cond_mesh(X, conf):
    return
#
# def plot_feature_importance(X, y, optimised_model: BayesSearchCV):
#     xgb_model = XGBRegressor(**optimised_model.best_params_)
#     xgb_model.fit(X, y)
#     non_zero_indices = xgb_model.feature_importances_ >= 0.001
#     non_zero_cols = X_all.columns[non_zero_indices]
#     non_zero_importances = xgb_model.feature_importances_[non_zero_indices]
#     sorted_non_zero_indices = non_zero_importances.argsort()
#     plt.barh(non_zero_cols[sorted_non_zero_indices], non_zero_importances[sorted_non_zero_indices])
#     plt.xlabel("Xgboost Feature Importance")
