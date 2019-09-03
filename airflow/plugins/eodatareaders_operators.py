import os
import glob
import logging

from airflow.models import BaseOperator
from airflow.plugins_manager import AirflowPlugin
from airflow.utils.decorators import apply_defaults
from eodatareaders.eo_data_reader import eoDataReader

log = logging.getLogger(__name__)

class eoDataReadersOp(BaseOperator):

    @apply_defaults
    def __init__(self, input_filepaths, input_params, *args, **kwargs):
        #self.operator_param = my_operator_param
        self.input_filepaths = input_filepaths
        self.input_params = input_params
        super(eoDataReadersOp, self).__init__(*args, **kwargs)

    #def execute(self, context, input_filepaths, params=None):
    def execute(self, context):
        # If folder is given, list files in folder
        # print('####')
        # print(self.input_filepaths, type(self.input_filepaths))
        # print('####')
        if isinstance(self.input_filepaths, str):
            if os.path.isdir(self.input_filepaths):
                # input string is a directory, list all its files
                filepaths = sorted(glob.glob(self.input_filepaths + '*'))
            else:
                # input string is single filepath
                filepaths = self.input_filepaths
        elif isinstance(self.input_filepaths, list):
            if os.path.isdir(self.input_filepaths[0]):
                # input list is list of lists, concatenate all files in all folders
                filepaths = []
                for folder in self.input_filepaths:
                    filepaths_tmp = sorted(glob.glob(folder + '*'))
                    filepaths = filepaths + filepaths_tmp
            else:
                # input list is list of strings (filepaths)
                filepaths = self.input_filepaths
        # print('+++++++++++')
        # print(filepaths)
        # print('+++++++++++')
        _ = eoDataReader(filepaths, self.input_params)
        #log.info("Hello World!")
        #log.info('operator_param: %s', self.operator_param)

class eoDataReadersPlugin(AirflowPlugin):
    name = "eo_data_readers_plugin"
    operators = [eoDataReadersOp]
