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
        config.subscription_id,
        config.username,
        config.password,
        config.admin_username,
        config.group_name,
        config.storage_name,
        config.virtual_network_name,
        config.subnet_name,
        config.region,
        skip_setup = True
    )
    
    exp.provision(
        config.vm_size,
        config.image_publisher,
        config.image_offer,
        config.image_sku,
        config.image_version,
        tags=['master'],
        has_public_ip=False,
        count=1,
        boot_timeout=600
    )

    exp.wait()

    pprint(exp.list())
   
    exit_code_list, out_list, err_list = exp.run(["master"], "echo 'Hello world from a experiment instance'", admin_username)

except ExperimentException as e:
    print "ERROR: %s" % e

finally:
    if exp is not None:
        exp.deprovision()

