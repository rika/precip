#!/usr/bin/python

import os
import time
from pprint import pprint

from precip import *

exp = None

PROJECT = 'causal-setting-91114'
ZONE =  'us-central1-f'
USER = 'root'
MULTIPLIER = '2'

#IMAGE_PROJECT = 'ubuntu-os-cloud' # look at https://cloud.google.com/compute/docs/operating-systems/linux-os
#IMAGE_NAME = 'ubuntu-1504-vivid-v20150422' # to list images: gcloud compute images list --project [IMAGE_PROJECT]
IMAGE_NAME = 'ubuntu-generic-02'

SOURCE_DISK_IMAGE = 'projects/%s/global/images/%s' % (PROJECT, IMAGE_NAME)
MACHINE_TYPE = 'zones/%s/machineTypes/n1-standard-1' % ZONE
MACHINE_TYPE = 'zones/%s/machineTypes/f1-micro' % ZONE

# Use try/except liberally in your experiments - the api is set up to
# raise ExperimentException on most errors
try:

    # Create a new OpenStack based experiment. In this case we pick
    # up endpoints and access/secret cloud keys from the environment
    # as exposing those is the common setup on FutureGrid
    exp = GCloudExperiment(PROJECT, ZONE, USER)

    # Provision an instance based on the ami-0000004c. Note that tags are
    # used throughout the api to identify and manipulate instances. You 
    # can give an instance an arbitrary number of tags.
    exp.provision(SOURCE_DISK_IMAGE, MACHINE_TYPE, tags=['all','master'], count=1)
    exp.provision(SOURCE_DISK_IMAGE, MACHINE_TYPE, tags=['all','worker'], count=4)

    # Wait for all instances to boot and become accessible. The provision
    # method only starts the provisioning, and can be used to start a large
    # number of instances at the same time. The wait method provides a 
    # barrier to when it is safe to start the actual experiment.
    exp.wait()

    # Print out the details of the instance. The details include instance id,
    # private and public hostnames, and tags both defined by you and some
    # added by the api
    pprint(exp.list())
    
    master_addr = exp.get_private_hostnames(['master'])[0]

    for template in ['gmetad.conf.template', 'gmond.conf.template', 'slotstat.pyconf.template']:
        exp.put(['all'], template, '/root', user=USER)
    exp.copy_and_run(['master'], 'master.sh', args=[master_addr, MULTIPLIER], user=USER)
    exp.copy_and_run(['worker'], 'worker.sh', args=[master_addr, MULTIPLIER], user=USER)

    input("Press Enter to continue...")

except ExperimentException as e:
    # This is the default exception for most errors in the api
    print 'ERROR: %s' % e

finally:
    # Be sure to always deprovision the instances we have started. Putting
    # the deprovision call under finally: make the deprovisioning happening
    # even in the case of failure.
    if exp is not None:
        exp.deprovision()

