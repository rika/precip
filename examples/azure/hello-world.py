#!/usr/bin/python

import os, sys
import ConfigParser
import time
from pprint import pprint

from precip import AzureExperiment
from precip import ExperimentException
from azure_config import AzureConfig




if len(sys.argv) < 2:
    print("Usage: %s [config_file]" % sys.argv[0])
    sys.exit()

config = AzureConfig(sys.argv[1])


exp = None
try:

    exp = AzureExperiment(
        config,
        skip_setup = True,
        name='test'
    )
    
    exp.provision(
        tags=['master'],
        has_public_ip=True,
        count=1,
        boot_timeout=600
    )

    exp.wait()

    pprint(exp.list())
   
    exit_code_list, out_list, err_list = exp.run(["master"], "echo 'Hello world from a experiment instance'", config.admin_username)

    raw_input('Press ENTER...')

except ExperimentException as e:
    print "ERROR: %s" % e

finally:
    if exp is not None:
        exp.deprovision()

