#!/usr/bin/python

import os, sys
import ConfigParser
import time
from pprint import pprint

from precip import *





if len(sys.argv) < 2:
    print("Usage: %s [config_file]" % sys.argv[0])
    sys.exit()

if not os.path.isfile(sys.argv[1]):
    print("Error: %s is not a file." % sys.argv[1])
    sys.exit()

try:
    config = ConfigParser.ConfigParser()
    config.read(sys.argv[1])
    
    subscription_id = config.get('Config', 'subscription_id')
    username = config.get('Config', 'username')
    password = config.get('Config', 'password')
    group_name = config.get('Config', 'group_name')
    storage_name = config.get('Config', 'storage_name')
    virtual_network_name = config.get('Config', 'virtual_network_name')
    subnet_name = config.get('Config', 'subnet_name')
    region = config.get('Config', 'region')
    admin_username = config.get('Config', 'admin_username')
    image_publisher = config.get('Config', 'image_publisher')
    image_offer = config.get('Config', 'image_offer')
    image_sku = config.get('Config', 'image_sku')
    image_version = config.get('Config', 'image_version')
    
except Exception as e:
    print e
    print("Could not read the configuration file '%s'." % sys.argv[1])
    sys.exit()

'''
from azure_resource_manager import AzureResourceManager
arm = AzureResourceManager(subscription_id, username, password, group_name, storage_name, virtual_network_name, subnet_name, region, skip_setup=True)

name = 'teste-ri0'
arm.create_vm(name, admin_username, '/home/ricardo/.precip/precip_precip.pub', image_publisher, image_offer, image_sku, image_version, has_public_ip=True)
print(arm.get_addrs(name))
arm.delete_vm(name)
sys.exit()
'''

exp = None
# Use try/except liberally in your experiments - the api is set up to
# raise ExperimentException on most errors
try:

    # Create a new OpenStack based experiment. In this case we pick
    # up endpoints and access/secret cloud keys from the environment
    # as exposing those is the common setup on FutureGrid
    exp = AzureExperiment(subscription_id, username, password, admin_username,
                 group_name, storage_name, virtual_network_name, subnet_name,
                 region, skip_setup = True)
    
    # Provision an instance based on the ami-0000004c. Note that tags are
    # used throughout the api to identify and manipulate instances. You 
    # can give an instance an arbitrary number of tags.
    exp.provision(image_publisher, image_offer, image_sku,
                  image_version, tags=['master'], count=1, boot_timeout=600)

    sys.stdout.flush()
    # Wait for all instances to boot and become accessible. The provision
    # method only starts the provisioning, and can be used to start a large
    # number of instances at the same time. The wait method provides a 
    # barrier to when it is safe to start the actual experiment.
    exp.wait()

    # Print out the details of the instance. The details include instance id,
    # private and public hostnames, and tags both defined by you and some
    # added by the api
    pprint(exp.list())
   
    # Run a command on the instances having the "test1" tag. In this case we
    # only have one instance, but if you had multiple instances with that
    # tag, the command would run on each one.
    exit_code_list, out_list, err_list = exp.run(["master"], "echo 'Hello world from a experiment instance'", admin_username)
    print exit_code_list
    print out_list
    print err_list

except ExperimentException as e:
    # This is the default exception for most errors in the api
    print "ERROR: %s" % e

finally:
    # Be sure to always deprovision the instances we have started. Putting
    # the deprovision call under finally: make the deprovisioning happening
    # even in the case of failure.
    if exp is not None:
        exp.deprovision()

