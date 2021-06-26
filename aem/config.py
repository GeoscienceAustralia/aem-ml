import yaml
from pathlib import Path
import geopandas as gpd


# column containing H, M, L categories corresponding to confidence levels of interpretation
confidence_indicator_col = 'BoundConf'

twod_coords = ['POINT_X', 'POINT_Y']
threed_coords = twod_coords + ['Z_coor']


class Config:
    """Class representing the global configuration of the aem scripts

    This class is *mostly* read-only, but it does also contain the Transform
    objects which have state.

    Parameters
    ----------
    yaml_file : string
        The path to the yaml config file.
    """
    def __init__(self, yaml_file):
        with open(yaml_file, 'r') as f:
            s = yaml.safe_load(f)

        self.name = Path(yaml_file).stem

        # output dir
        self.output_dir = s['output']['directory']
        Path(self.output_dir).mkdir(exist_ok=True)

        # data
        self.aem_folder = s['data']['aem_folder']
        self.interp_data = Path(self.aem_folder).joinpath(s['data']['interp_data'])
        self.aem_train_data = Path(self.aem_folder).joinpath(s['data']['aem_train_data'])
        self.aem_pred_data = Path(self.aem_folder).joinpath(s['data']['aem_pred_data'])
        self.shapefile_rows = s['data']['rows']

        # np randomisation
        self.numpy_seed = s['learning']['numpy_seed']

        # training
        self.algorithm = s['learning']['algorithm']
        self.model_params = s['learning']['params']
        self.include_aem_covariates = s['learning']['include_aem_covariates']
        self.include_thickness = s['learning']['include_thickness']
        self.include_conductivity_derivatives = s['learning']['include_conductivity_derivatives']

        # model parameter optimisation
        self.opt_space = s['learning']['optimisation']

        # weighted model params
        if 'weighted_model' in s['learning']:
            self.weighted_model = True
            self.weight_dict = s['learning']['weighted_model']['weights']
            self.weight_col = s['data']['weight_col']
        else:
            self.weighted_model = False

        # data description
        self.line_col = s['data']['line_col']
        self.conductivity_columns_starts_with = s['data']['conductivity_columns_starts_with']
        self.thickness_columns_starts_with = s['data']['thickness_columns_starts_with']
        self.aem_covariate_cols = s['data']['aem_covariate_cols']

        original_aem_data = gpd.GeoDataFrame.from_file(self.aem_train_data.as_posix(), rows=1)

        conductivity_cols = [c for c in original_aem_data.columns if c.startswith(self.conductivity_columns_starts_with)]
        d_conductivities = ['d_' + c for c in conductivity_cols]
        conductivity_and_derivatives_cols = conductivity_cols + d_conductivities
        thickness_cols = [t for t in original_aem_data.columns if t.startswith(self.thickness_columns_starts_with)]

        self.thickness_cols = thickness_cols
        self.conductivity_cols = conductivity_cols
        self.conductivity_derivatives_cols = d_conductivities
        self.conductivity_and_derivatives_cols = conductivity_and_derivatives_cols

        # co-ordination
        self.model_file = Path(self.output_dir).joinpath(self.name + ".model")
        self.outfile_scores = Path(self.output_dir).joinpath(self.name + "_scores.json")

        # outputs
        self.train_data = Path(self.output_dir).joinpath(self.name + "_train.csv")
        self.pred_data = Path(self.output_dir).joinpath(self.name + "_pred.csv")

        # test train val split
        self.train_fraction = s['data']['test_train_split']['train']
        self.test_fraction = s['data']['test_train_split']['test']
        self.val_fraction = s['data']['test_train_split']['val']
