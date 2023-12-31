import json
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.utils import shuffle
from sklearn.model_selection import cross_val_score, GroupKFold, KFold, cross_validate, GroupShuffleSplit
from sklearn.metrics import check_scoring
from hyperopt import fmin, tpe, anneal, Trials, space_eval
from hyperopt.hp import uniform, randint, choice, loguniform, quniform
from aem import utils
from aem.config import Config, cluster_line_segment_id, cluster_line_no
from aem.models import modelmaps
from aem.logger import aemlogger as log
from aem.training import setup_validation_data

hp_algo = {
    'bayes': tpe.suggest,
    'anneal': anneal.suggest
}


def optimise_model(X: pd.DataFrame, y: pd.Series, w: pd.Series, groups: pd.Series, conf: Config):
    """
    :param X: covaraite matrix
    :param y: targets
    :param w: weights for each target
    :param groups: group number for each target
    :param conf:
    :return:
    """
    trials = Trials()
    search_space = {k: eval(v) for k, v in conf.hp_params_space.items()}

    reg = modelmaps[conf.algorithm]
    has_random_state_arg = hasattr(reg(), 'random_state')
    bayes_or_anneal = conf.hyperopt_params.pop('algo') if 'algo' in conf.hyperopt_params else 'bayes'
    algo = hp_algo[bayes_or_anneal]
    cv_folds = conf.hyperopt_params.pop('cv') if 'cv' in conf.hyperopt_params else 5
    random_state = conf.hyperopt_params.pop('random_state')
    conf.bayes_or_anneal = bayes_or_anneal
    conf.algo = algo

    # shuffle data
    rstate = np.random.RandomState(random_state)
    scoring = conf.hyperopt_params.pop('scoring')
    scorer = check_scoring(reg(** conf.model_params), scoring=scoring)

    model_cols = utils.select_cols_used_in_model(conf)

    X, y, w, le_groups, cv = setup_validation_data(X, y, w, groups, cv_folds, random_state)

    log.info(f"shape of optimization data {X.shape}")

    def objective(params, random_state=random_state, cv=cv, X=X[model_cols], y=y):
        # the function gets a set of variable parameters in "param"
        all_params = {**conf.model_params}
        if has_random_state_arg:
            all_params.update(**params, random_state=random_state)
            model = reg(** all_params)
        else:
            all_params.update(** params)
            model = reg(** all_params)
        print("="*50)
        params_str = ''
        for k, v in all_params.items():
            params_str += f"{k}: {v}\n"
        log.info(f"Cross-validating param combination:\n{params_str}")
        cv_results = cross_validate(model, X, y,
                                    fit_params={'sample_weight': w},
                                    groups=le_groups, cv=cv, scoring={'score': scorer}, n_jobs=-1)
        score = 1 - cv_results['test_score'].mean()
        log.info(f"Loss: {score}")
        return score

    step = conf.hyperopt_params.pop('step') if 'step' in conf.hyperopt_params else 10
    max_evals = conf.hyperopt_params.pop('max_evals') if 'max_evals' in conf.hyperopt_params else 50

    log.info(f"Optimising params using Hyperopt {algo}")

    for i in range(0, max_evals + 1, step):
        # fmin runs until the trials object has max_evals elements in it, so it can do evaluations in chunks like this
        best = fmin(
            objective, search_space,
            ** conf.hyperopt_params,
            algo=algo,
            trials=trials,
            max_evals=i + step,
            rstate=rstate
        )
        # each step 'best' will be the best trial so far
        # params_str = ''
        # best = space_eval(search_space, best)
        # for k, v in best.items():
        #     params_str += f"{k}: {v}\n"
        log.info(f"Saving params after {i + step} trials best config: \n")
        # each step 'trials' will be updated to contain every result
        # you can save it to reload later in case of a crash, or you decide to kill the script
        pickle.dump(trials, open(Path(conf.output_dir).joinpath(f"hpopt_{i + step}.pkl"), "wb"))
        save_optimal(best, random_state, trials, objective, conf)

    log.info(f"Finished param optimisation using Hyperopt")
    all_params = {** conf.model_params}
    all_params.update(best)
    log.info("Now training final model using the optimised model params")
    opt_model = modelmaps[conf.algorithm](** all_params)
    opt_model.fit(X[model_cols], y, sample_weight=w)

    conf.optimised_model = True
    utils.export_model(opt_model, conf, model_type="optimise")
    return opt_model


def save_optimal(best, random_state, trials, objective, conf: Config):

    with open(conf.optimised_model_params, 'w') as f:
        all_params = {**conf.model_params, 'random_state': random_state}
        all_params.update(best)
        # json.dump(all_params, cls=NpEncoder)
        json.dump(all_params, f, sort_keys=True, indent=4, cls=NpEncoder)
        params_str = ''
        for k, v in all_params.items():
            params_str += f"{k}: {v}\n"
        log.info(f"Best params found:\n{params_str}")
        log.info(f"Saved hyperopt.{conf.bayes_or_anneal}.{conf.algo.__name__} "
                 f"optimised params in {conf.optimised_model_params}")

    params_space = []
    for t in trials.trials:
        l = {k: v[0] for k, v in t['misc']['vals'].items()}
        params_space.append(l)
    results = pd.DataFrame.from_dict(params_space, orient='columns')
    loss = [x['result']['loss'] for x in trials.trials]
    results.insert(0, 'loss', loss)
    log.info("Best Loss {:.3f} params {}".format(objective(best), best))
    results.sort_values(by='loss').to_csv(conf.optimisation_output_hpopt)


class NpEncoder(json.JSONEncoder):
    """
    see https://stackoverflow.com/a/57915246/3321542
    """
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)
