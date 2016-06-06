#!/usr/bin/python2.7

import unittest
import os
import sys
import time
import traceback

from precip import AzureExperiment
from azure_config import AzureConfig



class TestAzureExperiment(unittest.TestCase):

    def setUp(self):
        try:
            home = os.path.expanduser('~')
            filepath = os.path.join(home, 'git', 'azure_config', 'config')
            self.config = AzureConfig(filepath)
        except Exception as e:
            print "ERROR: %s" % e
            traceback.print_exc(file=sys.stdout)


    def test_simple_provision(self):
        test_tag = 'simple_provision'
        result = False
        exp = None
        try:
            exp = AzureExperiment(
                self.config.subscription_id,
                self.config.username,
                self.config.password,
                self.config.admin_username,
                self.config.group_name,
                self.config.storage_name,
                self.config.virtual_network_name,
                self.config.subnet_name,
                self.config.region,
                skip_setup = True
            )
            exp.provision(
                self.config.image_publisher,
                self.config.image_offer,
                self.config.image_sku,
                self.config.image_version,
                tags=[test_tag],
                count=1,
                boot_timeout=600
            )
            exp.wait()
            exp.run([test_tag], "echo 'Hello world from a experiment instance'")
            result = True
        except Exception as e:
            print "ERROR: %s" % e
            traceback.print_exc(file=sys.stdout)
        finally:
            if exp is not None:
                exp.deprovision()
        self.assertTrue(result)

    def test_conc_provision(self):
        test_tag = 'conc_provision'
        result = False
        exp = None
        try:
            exp = AzureExperiment(
                self.config.subscription_id,
                self.config.username,
                self.config.password,
                self.config.admin_username,
                self.config.group_name,
                self.config.storage_name,
                self.config.virtual_network_name,
                self.config.subnet_name,
                self.config.region,
                skip_setup = True
            )
            exp.provision(
                self.config.image_publisher,
                self.config.image_offer,
                self.config.image_sku,
                self.config.image_version,
                tags=[test_tag],
                count=2,
                boot_timeout=600
            )
            exp.wait()
            exp.run([test_tag], "echo 'Hello world from a experiment instance'")
            result = True
        except Exception as e:
            print "ERROR: %s" % e
            traceback.print_exc(file=sys.stdout)
        finally:
            if exp is not None:
                exp.deprovision()
        self.assertTrue(result)

if __name__ == '__main__':
    unittest.main()
