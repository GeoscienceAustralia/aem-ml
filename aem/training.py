import json
import joblib
from typing import Dict
import logging
import numpy as np
import pandas as pd
from sklearn.metrics import (
    explained_variance_score,
    make_scorer,
    r2_score,
    mean_squared_error,
    mean_absolute_error
)
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split, GroupKFold, KFold
from sklearn.utils import shuffle
from skopt import BayesSearchCV
from skopt.space import Real, Integer, Categorical
from hyperopt import fmin, tpe, anneal, Trials, space_eval
from hyperopt.hp import uniform, randint, choice, loguniform, quniform

from aem import utils
from aem.config import Config, cluster_line_segment_id, cluster_line_no
from aem.models import modelmaps
from aem.logger import aemlogger as log

# Make numpy printouts easier to read.
np.set_printoptions(precision=3, suppress=True)


regression_metrics = {
    'r2_score': lambda y, py, w:  r2_score(y, py, sample_weight=w),
    'expvar': lambda y, py, w: explained_variance_score(y, py, sample_weight=w),
    'mse': lambda y, py, w: mean_squared_error(y, py, sample_weight=w),
    'mae': lambda y, py, w: mean_absolute_error(y, py, sample_weight=w),
}


def bayesian_optimisation(X: pd.DataFrame, y: pd.Series, w: pd.Series, groups: pd.Series, conf: Config):

    reg = modelmaps[conf.algorithm](** conf.model_params)
    search_space = {k: eval(v) for k, v in conf.opt_params_space.items()}
    searchcv = BayesSearchCV(
        reg,
        search_spaces=search_space,
        ** conf.opt_searchcv_params,
        fit_params={'sample_weight': w},
        return_train_score=True,
        refit=False
    )
    log.info(f"Optimising params using BayesSearchCV .....")
    model_cols = utils.select_cols_used_in_model(conf)

    searchcv.fit(X[model_cols], y, groups=groups)

    log.info(f"Finished param optimisation using BayesSearchCV .....")
    log.info(f"Best score found using param optimisation {searchcv.best_score_}")

    with open(conf.optimised_model_params, 'w') as f:
        json.dump(searchcv.best_params_, f, sort_keys=True, indent=4)
        log.info(f"Saved bayesian search optimised params in {conf.optimised_model_params}")

    log.info("Score optimised model using train test split")
    all_scores = train_test_score(X, y, w, conf, searchcv.best_params_)

    score_string = "Optimised model scores:\n"
    # report model performance on screen
    for k, scores in all_scores.items():
        score_string += f"{k}:\n"
        for metric, score in scores.items():
            score_string += "{}\t= {}\n".format(metric, score)
    log.info(score_string)

    # and also save a scores json file on disc
    with open(conf.optimised_model_scores, 'w') as f:
        json.dump(all_scores, f, sort_keys=True, indent=4)
        log.info(f"Saved optimised model scores in file {conf.optimised_model_scores}")

    log.info("Now training final model using the optimised model params")
    opt_model = modelmaps[conf.algorithm](** searchcv.best_params_)
    opt_model.fit(X[model_cols], y, sample_weight=w)

    return opt_model


def setup_validation_data(X, y, weights, groups, cv_folds, random_state=None):
    le = LabelEncoder()
    le.fit(groups)
    le_groups = le.transform(groups)
    X, y, w, le_groups = shuffle(X, y, weights, le_groups, random_state=random_state)
    if len(np.unique(groups)) >= cv_folds:
        log.info(f'Using GroupKFold with {cv_folds} folds')
        cv = GroupKFold(n_splits=cv_folds)
    else:
        log.info(f'Using KFold with {cv_folds} folds')
        cv = KFold(n_splits=cv_folds, shuffle=True, random_state=random_state)
    return X, y, w, le_groups, cv


def train_test_score(X: pd.DataFrame, y: pd.Series, w: pd.Series, conf: Config, model_params: Dict):
    model = modelmaps[conf.algorithm](** model_params)

    model_cols = utils.select_cols_used_in_model(conf)
    # split data into two non-overlapping parts
    X_test, X_train, w_test, w_train, y_test, y_train = create_train_test_set_based_on_column(
        X, y, w, cluster_line_segment_id
    )
    model.fit(X_train[model_cols], y_train, sample_weight=w_train)

    train_scores = score_model(model, X_train[model_cols], y_train, w_train)
    test_scores = score_model(model, X_test[model_cols], y_test, w_test)
    all_scores = {'train_score': train_scores, 'test_score': test_scores}
    return all_scores


def create_train_test_set_based_on_column(X, y, w, col_name):
    aem_segments = np.unique(X[col_name])
    train_segments, test_segments = train_test_split(aem_segments, test_size=0.3)
    train_indices = X.index[X[cluster_line_segment_id].isin(train_segments)]
    test_indices = X.index[X[cluster_line_segment_id].isin(test_segments)]
    X_train, y_train, w_train = X.loc[train_indices, :], y.loc[train_indices], w.loc[train_indices]
    X_test, y_test, w_test = X.loc[test_indices, :], y.loc[test_indices], y.loc[test_indices]
    return X_test, X_train, w_test, w_train, y_test, y_train


def score_model(trained_model, X, y, w=None):
    scores = {}
    y_pred = trained_model.predict(X)
    for k, m in regression_metrics.items():
        scores[k] = m(y, y_pred, w)
    return scores
