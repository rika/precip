#!/usr/bin/python2.7 -tt
"""

Copyright 2012 University Of Southern California

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0
                                                                                                                             
Unless required by applicable law or agreed to in writing,                                                                 
software distributed under the License is distributed on an "AS IS" BASIS,                                                 
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.                                                   
See the License for the specific language governing permissions and                                                        
limitations under the License. 

"""

import logging
import os
import random
import re
import socket
import subprocess
import time
import uuid
import threading

import paramiko

import boto
#from boto.ec2.connection import EC2Connection
from boto.ec2.regioninfo import RegionInfo
from boto.ec2.blockdevicemapping import EBSBlockDeviceType, BlockDeviceMapping
from boto.exception import EC2ResponseError

from oauth2client.client import GoogleCredentials
from googleapiclient.discovery import build

from azure_resource_manager import AzureResourceManager

__all__ = ["ExperimentException",
           "EC2Experiment",
           "NimbusExperiment",
           "EucalyptusExperiment",
           "OpenStackExperiment",
           "GCloudExperiment",
           "AzureExperiment"]


#logging.basicConfig(level=logging.WARN)
logger = logging.getLogger('precip')

# log to the console
console = logging.StreamHandler()

# default log level - make logger/console match
logger.setLevel(logging.DEBUG)
console.setLevel(logging.DEBUG)

# formatter
formatter = logging.Formatter("%(asctime)s %(levelname)7s:  %(message)s", "%Y-%m-%d %H:%M:%S")
console.setFormatter(formatter)
logger.addHandler(console)

# make boto log less
boto_logger = logging.getLogger('boto')
boto_logger.setLevel(level=logging.FATAL)


class SSHConnection:
    """ 
    Helper class for simple ssh functionality such as copying files and running commands.
    
    The only authentication method supported is ssh pub/priv key authentication.
    """
    
    def _new_connection(self, privkey, host, user):
        """
        Internal method for setting up a ssh connection. As the instances come up with different
        host keys all the time, the host key validation has been disabled.
        
        :return: a handle to the ssh connection
        """ 
        ssh = paramiko.SSHClient()
        hkeys = ssh.get_host_keys()
        hkeys.clear()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, 22, username=user, key_filename=privkey, allow_agent=False, look_for_keys=False)
        transport = ssh.get_transport()
        transport.set_keepalive(30)
        return ssh
    
    def run(self, privkey, host, user, cmd):
        """
        Runs a command on the remote machine.
        
        :return: exit code, stdout and stderr from the command
        """
        logger.debug("Running command on host %s as user %s: %s" % (host, user, cmd))
        out = ""
        err = ""
        ssh = self._new_connection(privkey, host, user)
        chan = ssh.get_transport().open_session()
        chan.get_pty()
        stdin = chan.makefile("wb", -1)
        stdout = chan.makefile("rb", -1)
        stderr = chan.makefile_stderr("rb", -1)
        chan.exec_command(cmd)
        stdin.flush()
        exit_code = chan.recv_exit_status()
        for line in stdout:
            out += line
        for line in stderr:
            err += line
        ssh.close()        
        return exit_code, out, err

    def put(self, privkey, host, user, local_path, remote_path):
        """
        Copies file from the local machine to the remote machine
        """
        ssh = self._new_connection(privkey, host, user)
        ftp = ssh.open_sftp()
        ftp.put(local_path, remote_path)
        ftp.close()
        ssh.close()

    def get(self, privkey, host, user, remote_path, local_path):
        """
        Copies file from the remote machine to the local machine
        """
        ssh = self._new_connection(privkey, host, user)
        ftp = ssh.open_sftp()
        ftp.get(remote_path, local_path)
        ftp.close()
        ssh.close()


class ExperimentException(Exception):
    """
    Class for grouping the most common experiment failures 
    """
    pass


class Instance:
    """
    Representation of an instance, and a few common attributes of that instance
    """
    
    id = None
    priv_addr = None
    pub_addr = None
    tags = []
    ec2_instance = None
    gce_boot_response = None
    azure_boot_thread = None
    is_fully_instanciated = False
    not_instanciated_correctly = False
    
    def __init__(self, instance_id):
        """
        :param instance_id: a unique identifier for the instance, for example the amazon instance id
        """
        self.id = instance_id
        self.tags = []
    
    def add_tag(self, tag):
        """
        Tagging is implemented in our own instance as some infrastructures (OpenStack, ...) have not implemented
        tagging in their API
        """
        self.tags.append(tag)
        
    def has_tags(self, tags):
        """
        Checks if the instance have all the tags queried for
        """
        try:
            for t in tags:
                # if the tag does not exists, we fail here with a ValueException
                self.tags.index(t)
        except ValueError:
            return False            
        return True
    
    def info(self):
        i = {}
        i["id"] = self.id
        i["public_address"]  = self.pub_addr
        i["private_address"] = self.priv_addr
        i["tags"] = self.tags
        return i


class Experiment:
    """
    Base class for all types of cloud implementations. This is what defines the experiment API.
    """
    
    def __init__(self, name = None):
        """
        Constructor for a new experiment - this will set up ~/.precip and ssh keys if they
        do not already exist in a way that you can use precip from multiple machines or 
        accounts at the same time. 
        """
        
        if name is None:
            self._name = str(int(time.time()))
        else:
            self._name = name

        self._instances = []
        
        self._conf_dir = os.path.join(os.environ["HOME"], ".precip")
        
        # checking/creating conf directory
        if not os.path.exists(self._conf_dir):
            os.makedirs(self._conf_dir)
        
        uid = self._get_account_id()
        
        # ssh keys setup 
        self._ssh_pubkey = os.path.join(self._conf_dir, "precip_"+uid+".pub")
        self._ssh_privkey = os.path.join(self._conf_dir, "precip_"+uid)
        if not os.path.exists(self._ssh_privkey):
            logger.info("Creating new ssh key in " + self._conf_dir)
            logger.info("You don't need to enter a passphrase, just leave it blank and press enter!")
            cmd = "ssh-keygen -q -t rsa -f " + self._ssh_privkey + " </dev/null"
            p = subprocess.Popen(cmd, shell=True)
            stdoutdata, stderrdata = p.communicate()
            rc = p.returncode
            if rc != 0:
                raise ExperimentException("Command '%s' failed with error code %s" % (cmd, rc))

    def __del__(self):
        """
        Deprovision all instances
        """
        self.deprovision([])

    def _instance_subset(self, tags):
        """
        Returns the subset of instances matching the tags
        """
        subset = []
        for i in self._instances:
            if i.has_tags(tags):
                subset.append(i)
        return subset
    
    def _is_valid_hostaddr(self, addr):
        """
        Checks if a host address is "external". Note that addr can be either an ip address
        or a fqdn.
        """
        
        re_ip = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
        r = re_ip.search(self._endpoint)
        if not r:
            # assume addr is a fqdn
            try:
                addr = socket.gethostbyname(addr)
            except socket.gaierror:
                # unable to dns look up addr
                return False

        # at this point addr should be in ip format
        
        # private up addresses are not valid (there is probably a better way to check this)
        if addr[:3] == "10." or addr[:4] == "192." or addr[:4] == "172.":
            return False 
        
        # make sure addr is a valid ip address
        try:
            socket.inet_aton(addr)
            # valid
        except socket.error:
            return False
        return True
    
    def _get_account_id(self):
        
        """
        Checks if account_id with uid stored in it exists.
        Returns the uid for this machine.
        """
        
        self._account_id = os.path.join(self._conf_dir, "account_id")
        
        if not os.path.exists(self._account_id):
            f = open(self._account_id,'w')
            uid = str(uuid.uuid4().get_hex())
            f.write(uid)
            f.close()
            
        else:
            f = open(self._account_id, 'r')
            uid = f.read().rstrip('\n')
            f.close()
        return uid   
        
    def provision(self, image_id, instance_type='m1.small', count=1, tags=None, boot_timeout=600, boot_max_tries=3):
        """
        Provision a new instance. Note that this method starts the provisioning cycle, but does not
        block for the instance to finish booting - for that, see wait()
        
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param count: Number of instances to provision. The default is 1.
        :param tags: Tags to add to the instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        :param boot_timeout: The amount of allowed time in seconds for an instance to boot
        :param boot_max_tries: The number of tries an instance is given to successfully boot
        """                                                                   
        pass
    
    def wait(self, tags=[]):
        """
        Barrier for all currently instances to finish booting and be accessible via external addresses.
        
        :param tags: set of tags to match against
        :param timeout: maximum timeout to wait for instances to come up
        :param maxretrycount: The number of retries in case of failure of provisioning the instances 
        """
        return True

    def list(self, tags=[]):
        """
        Provides a list of instances, and instance details, with the matching tags
        
        :param tags: set of tags to match against
        :return: list of instance descriptions
        """
        l = []
        for i in self._instance_subset(tags):
            l.append(i.info())
        return l
        
    def deprovision(self, tags=[]):
        """
        Deprovisions (terminates) instances with the matching tags
        
        :param tags: set of tags to match against
        """
        pass
    
    def get_public_hostnames(self, tags=[]):
        """
        Get the set of public hostnames (or IP addresses) for instances matching 'tags'
        
        :param tags: set of tags to match against
        """
        addresses = []
        for i in self._instance_subset(tags):
            addresses.append(i.pub_addr)
        return addresses

    def get_private_hostnames(self, tags=[]):
        """
        Get the set of private hostnames (or IP addresses) for instances matching 'tags'
        
        :param tags: set of tags to match against
        """
        addresses = []
        for i in self._instance_subset(tags):
            addresses.append(i.priv_addr)
        return addresses
    
    def get(self, tags, remote_path, local_path, user="root"):
        """
        Transfers a file from a set of remote machines matching the tags, and stores the file locally.
        If more than one instance matches the tags, an instance id will be appended to the local_path. 
        
        :param tags: set of tags to match against
        :param remote_path: location of the file on the remote instance
        :param local_path: local location for where to store the file
        """
        ssh = SSHConnection()
        iset = self._instance_subset(tags)
        
        # if the instance set is larger than one, enable the appending of 
        # instance id to the local path
        append_instance_id = True
        if len(iset) == 1:
            append_instance_id = False
            
        for i in self._instance_subset(tags):
            modified_local_path = local_path
            if append_instance_id:
                modified_local_path = local_path + "." + i.id
            # should we do checks on the target path? Directory check? Existing file check?
            ssh.get(self._ssh_privkey, i.pub_addr, user, remote_path, modified_local_path)

    def put(self, tags, local_path, remote_path, user="root", priv=False):
        """
        Transfers a local file to a set of instances matching the given tags
        
        :param tags: set of tags to match against
        :param local_path: local location for the source file
        :param remote_path: location of where to copy the file to
        :param user: user to transfer as, default is 'root'
        """
        ssh = SSHConnection()
        for i in self._instance_subset(tags):
            logger.info("Copying %s to %s on %s" % (local_path, remote_path, i.id))
            addr = i.pub_addr if priv is False else i.priv_addr
            ssh.put(self._ssh_privkey, addr, user, local_path, remote_path)
    
    def run(self, tags, cmd, user="root", check_exit_code=True, output_base_name=None, priv=False):
        """
        Runs a command on set of instances matching the tags given.
        
        :param tags: set of tags to match against
        :param cmd: command to run
        :param user: the user to run the command as
        :param check_exit_code: if true, non-zero exit codes will be considered fatal
        :param output_base_name: redirects output to a file instead of stdout
        """
        exit_code_list = []
        out_list = []
        err_list = []
        ssh = SSHConnection()
        for i in self._instance_subset(tags):
            if not i.is_fully_instanciated:
                raise ExperimentException("Can't ssh a not fully instanciated instance "+ i.id)
            logger.info("Scheduling command execution on %s: %s" % (i.id, cmd))
            exit_code = -1
            out = ""
            err = ""
            try:
                addr = i.pub_addr if priv is False else i.priv_addr
                exit_code, out, err = ssh.run(self._ssh_privkey, addr, user, cmd)
            except Exception, e:
                raise ExperimentException("Error running ssh command", e)

            if len(out) > 0:
                if output_base_name is not None:
                    fname = "%s.%s.stdout" %(output_base_name, i.id)
                    try:
                        f = open(fname, 'w')
                        f.write(out)
                        f.close()
                    except Exception, e:
                        raise ExperimentException("Unable to write to " + fname, e)
                else:
                    logger.info("  stdout: %s" % out)

            if len(err) > 0:
                if output_base_name is not None:
                    fname = "%s.%s.stderr" %(output_base_name, i.id)
                    try:
                        f = open(fname, 'w')
                        f.write(err)
                        f.close()
                    except Exception, e:
                        raise ExperimentException("Unable to write to " + fname, e)
                else:
                    logger.info("  stderr: %s" % err)
            
            exit_code_list.append(exit_code)
            out_list.append(out)
            err_list.append(err)
                
            if check_exit_code and exit_code != 0:
                print out_list, err_list
                raise ExperimentException("Command exited with exit code %d" % exit_code)
        
        return exit_code_list, out_list, err_list
                
    def copy_and_run(self, tags, local_script, args=[], user="root", check_exit_code=True):
        """
        Runs a local script on the remote instances matching the tags
        
        :param tags: set of tags to match against
        :param local_script: local script to copy and run
        :param args: list of arguments to pass to the script
        :param user: user to run the script as
        """
        fname = "/tmp/remote-exec.%d" % (random.randint(1, 10000000000)) 
        self.put(tags, local_script, fname, user=user)
        cmd = "cd /tmp && chmod 755 %s && %s" % (fname, fname)
        for a in args:
            cmd = cmd + " '" + a + "'"
        cmd = cmd + " && rm -f " + fname
        exit_codes, outs, errs = self.run(tags, cmd, user=user, check_exit_code=check_exit_code)
        return exit_codes, outs, errs


class AzureExperiment(Experiment):
    def __init__(self, azure_config, skip_setup = False, name = None):
        Experiment.__init__(self, name = name)
        
        self.config = azure_config
        self.skip_setup = skip_setup
        
        self._conn = None
        self._get_connection()
        
        self.counter = 0
        
    def _get_connection(self):
        """
        Establishes a connection to the cloud endpoint
        """
        if (self._conn != None):
            return
        
        self._conn = AzureResourceManager(
            self.config,
            self.skip_setup
        )
        
    def _start_instance(self, name, tags, has_public_ip):
        self._conn.create_vm(
            name,
            self._ssh_pubkey,
            tags=tags,
            has_public_ip=has_public_ip
        )
    
    def _finish_instanciation(self, instance):
        """
        Finishes booting and bootstraps an instace
        
        :param instance: the instance to check
        :return: True if the instance is ready, otherwise False
        """
        
        # check if we are already done
        if instance.is_fully_instanciated:
            return True
        
        if instance.not_instanciated_correctly:
            instance.boot_timeout = 0
            return False
        
        try:
            instance.azure_boot_thread.join()
        except Exception as e:
            logger.debug("Instance %s state is 'error - scheduling for possible retry" %instance.id)
            logger.debug("%s" % str(e))
            instance.not_instanciated_correctly = True
            return False
        
        # DONE
        
        # get public and private addresses
        instance.pub_addr = self._conn.get_pub_addr(instance.id)
        instance.priv_addr = self._conn.get_priv_addr(instance.id) 
        
        if instance.pub_addr != '':    
            # bootstrap the image
            exit_code = -1
            out = ""
            err = ""
            try:
                logger.debug("Will try to ssh to " + instance.id + " (" + instance.pub_addr + ")")
                ssh = SSHConnection()
                script_path = os.path.dirname(os.path.abspath(__file__)) + "/resources/vm-bootstrap.sh"
                ssh.put(self._ssh_privkey, instance.pub_addr, self.config.admin_username, script_path, "/tmp/vm-bootstrap.sh")
                exit_code, out, err = ssh.run(self._ssh_privkey, instance.pub_addr, self.config.admin_username, "sudo chmod 755 /tmp/vm-bootstrap.sh && sudo /tmp/vm-bootstrap.sh")
            except paramiko.SSHException, e:
                logger.debug("Failed to run bootstrap script on instance %s. Will retry later." % instance.id)
                logger.debug(str(e))
                return False
            except paramiko.SFTPError:
                logger.debug("Unable to ssh connect to instance %s. Will retry later." % instance.id)
                return False
            except socket.error:
                logger.debug("Unable to ssh connect to instance %s. Will retry later." % instance.id)
                return False
            
            
            if len(out) > 0:
                logger.debug("  stdout: %s" % out)
            if len(err) > 0:
                logger.debug("  stderr: %s" % err)
            if exit_code != 0:
                raise ExperimentException("Bootstrap script exited with error %d" % exit_code)
        
        
        logger.info("Instance %s has booted, priv address: %s, public address: %s" % (instance.id, instance.priv_addr, instance.pub_addr))

        instance.add_tag(instance.pub_addr)
        instance.is_fully_instanciated = True
        return True

    def _retry(self, instance):
        
        """
        In case of reaching timeout for an instance, retry will terminate the previous instance and 
        replace it with a new instance.
        
        :param instance: the instance to terminate and replace with a new one
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param tags: Tags to add to the new instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        """
        
        logger.info("Instance %s has reached timeout, and will be replaced with a new instance" % instance.id)
        try:
            self._conn.delete_vm(instance.id)
        except Exception as e:
            logger.info('Could not delete instance: %s' % instance.id)
            logger.debug("%s" % str(e))
            
        instance.azure_boot_thread = threading.Thread(
            target=self._start_instance,
            args=(
                instance.id,
                instance.inst_param['tags'],
                instance.inst_param['has_public_ip'],
            ),
        )
        
        instance.num_starts = instance.num_starts + 1
        instance.boot_time = int(time.time())
        instance.not_instanciated_correctly = False
        
        instance.azure_boot_thread.start()
    
    def provision(self, tags=None, has_public_ip=True, count=1, boot_timeout=400):
        """
        Provision a new instance. Note that this method starts the provisioning cycle, but does not
        block for the instance to finish booting - for that, see wait()
        
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param count: Number of instances to provision. The default is 1.
        :param tags: Tags to add to the instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        :param boot_timeout: The amount of allowed time in seconds for an instance to boot
        :param boot_max_tries: The number of tries an instance is given to successfully boot
        """   
      
        name = self._name.replace('_', '')
        if re.search('^(?:[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)$', name) is None:
            name = str(uuid.uuid4().get_hex())
    
        for _i in range(count):
            inst_id = name + '-' + str(self.counter)
            self.counter += 1
            
            inst_tags = list(tags)
            inst_tags.append("precip")
            inst_tags.append(inst_id)
            
            instance = Instance(inst_id)
            self._instances.append(instance)
            for t in inst_tags:
                instance.add_tag(t)
            
            instance.azure_boot_thread = threading.Thread(
                target=self._start_instance,
                args=(
                    inst_id,
                    inst_tags,
                    has_public_ip,
                ),
            )
                
            # keep track of parameters - we might need them for restarts later
            instance.num_starts = 1
            instance.boot_time = int(time.time())
            instance.boot_timeout = boot_timeout
            instance.boot_max_tries = 3
            instance.inst_param = {
                'tags' : inst_tags,
                'has_public_ip' : has_public_ip
            }
        
            instance.azure_boot_thread.start()


    def wait(self, tags=[]):
        """
        Barrier for all currently instances to finish booting and be accessible via external addresses.
        
        :param tags: set of tags to match against
        :param timeout: maximum timeout to wait for instances to come up
        :param maxretrycount: The number of retries in case of failure of provisioning the instances 
        """
        
        count_pending = -1
        while count_pending != 0:
            count_pending = 0
            current_time = int(time.time())

            for i in self._instance_subset(tags):
                if not self._finish_instanciation(i):
                    count_pending += 1

                    # did the instance timeout?
                    if current_time > i.boot_time + i.boot_timeout:
                        logger.info("Timeout reached while waiting for instances to boot")
                        logger.info("A common cause for this that your image does not allow the" + \
                                    " root user to login.")
                        logger.info("Another common cause is infrastructure problems, preventing" + \
                                    " the instance from booting correctly.")
                        if i.num_starts < i.boot_max_tries:
                            self._retry(i)
                        else:
                            raise ExperimentException("Timeout reached while waiting for instances to boot")

            if count_pending > 0:
                logger.info("Still waiting for %d instances to finish booting" % (count_pending))
                time.sleep(20)       


    def _deprovision(self, instance):
        attempts=3
        instance.is_fully_instanciated = False
        done = False
        while (done == False):
            try:
                logger.info("Deprovisioning instance: %s" % instance.id)
                self._conn.delete_vm(instance.id)
                done = True
            except Exception as e:
                logger.info('Could not deprovision instance: %s' % instance.id)
                logger.debug("%s" % str(e))
                
                if (attempts > 0):
                    logger.info('Retrying in some secs..')
                    time.sleep(20)
                    attempts = attempts-1
                else:
                    logger.info('Not retrying anymore')
                    done = True
                    

    def deprovision(self, tags=[]):
        """
        Deprovisions (terminates) instances with the matching tags
        
        :param tags: set of tags to match against
        """
        threads = {}
        subset = self._instance_subset(tags)
        for i in subset:
            threads[i] = threading.Thread(target=self._deprovision, args=(i,),)
            threads[i].start()
        
        logger.info("Waiting for deprovisioning to complete")
        for i in subset:
            threads[i].join()
            self._instances.remove(i)
            

        logger.info("Deprovisioning done")                 


class GCloudExperiment(Experiment):
    
    def __init__(self, project, zone, user, name = None):
        """
        Initializes an GCloud experiment
        
        :param zone: Google Cloud zone, for example us-central1-f
        :param project: Google Cloud project ID, for example causal-setting-00000

        """
        Experiment.__init__(self, name = name)
        
        self._zone = zone
        self._project = project
        self._user = user
        
        self._conn = None
    
        self._get_connection()
        self._ssh_keys_setup()
        
        self.counter = 0

    def _get_connection(self):
        """
        Establishes a connection to the cloud endpoint
        """
        if (self._conn != None):
            return
        
        credentials = GoogleCredentials.get_application_default()
        self._conn = build('compute', 'v1', credentials=credentials)
        
    def _ssh_keys_setup(self):
        
        """
        Makes sure we have our experiment keypair registered
        """
        uid = self._get_account_id()
        logger.info("Registering ssh pubkey of "+uid)
        
        with open(self._ssh_pubkey) as sshfile:
            contents = sshfile.read()
        
        # Get metadata from the cloud
        request = self._conn.projects().get(project=self._project)
        response = request.execute()
        
        # Check keyresponse['name']
        need_to_register = False
        body = response['commonInstanceMetadata']
        
        if 'items' in body:
            sshkey_found = False
            for item in body['items']:
                if item['key'] == 'sshKeys':
                    sshkey_found = True
                    if contents not in item['value']:
                        need_to_register = True
                        item['value'] = str(item['value']) + '\n' + self._user + ':' + contents
            if not sshkey_found: 
                body['items'].append({'value' : self._user + ':' + contents,
                                      'key' : 'sshKeys' })
        else:
            need_to_register = True
            body['items'] = [{'value' : self._user + ':' + contents,
                              'key' : 'sshKeys' }]
        
        # Register key
        # key is stored at:
        # https://console.developers.google.com/project/<your-project>/compute/metadata/sshKeys
        if need_to_register == True:
            response = self._conn.projects().setCommonInstanceMetadata(project=self._project,
                                                                       body=body).execute()
            if 'error' in response:
                    raise ExperimentException(response['error'])
        else:
            logger.info("pubkey is already registered")
            
    def _wait_for_operation(self, operation, timeout=300):
        logger.debug('Waiting for %s to finish..' % operation)
        
        init_time = int(time.time())

        while True:
            response = self._conn.zoneOperations().get(
                project=self._project,
                zone=self._zone,
                operation=operation).execute()
    
            if response['status'] == 'DONE':
                logger.debug("%s done" % operation)
                if 'error' in response:
                    raise ExperimentException(response['error'])
                return response
            else:
                if int(time.time()) > init_time + timeout:
                    raise ExperimentException('Timeout for operation: ' + operation)
                time.sleep(5)

    def _start_instance(self, name, machine_type, source_disk_image, disk_size, tags):
            
        config = {
            'name': name,
            'machineType': machine_type,
    
            # Specify the boot disk and the image to use as a source.
            'disks': [{
                    'boot': True,
                    'autoDelete': True,
                    'initializeParams': {
                        'sourceImage': source_disk_image,
                        'diskSizeGb': disk_size
                    }
                }
            ],
    
            # Specify a network interface with NAT to access the public
            # internet.
            'networkInterfaces': [{
                    'network': 'global/networks/default',
                    'accessConfigs': [{'type': 'ONE_TO_ONE_NAT',
                                       'name': 'External NAT'}]
                }
            ],
            
            # Tags
            'tags': {'items': list(set(tags))}
        }
        response = self._conn.instances().insert(project=self._project,
                                                     zone=self._zone,
                                                     body=config).execute()
        logger.info("Started instance %s, type %s" % (name, machine_type))    
        return response

    def _finish_instanciation(self, instance):
        """
        Finishes booting and bootstraps an instace
        
        :param instance: the instance to check
        :return: True if the instance is ready, otherwise False
        """
        
        # check if we are already done
        if instance.is_fully_instanciated:
            return True
        
        if instance.not_instanciated_correctly:
            instance.boot_timeout = 0
            return False
        
        operation = instance.gce_boot_response['name']
        response = self._conn.zoneOperations().get(project=self._project, 
                                                   zone=self._zone,
                                                   operation=operation).execute()

        if 'error' in response:
            logger.debug("Instance %s state is 'error - scheduling for possible retry" %instance.id)
            logger.debug("%s" % operation)
            for error in response['error']['errors']:
                logger.debug("%s : %s" % (error['code'], error['message']))
            instance.boot_timeout = 0
            return False
        
        if response['status'] in ['PENDING', 'RUNNING']: # RUNNING here means the boot script is still running, not the instance
            logger.debug("Instance %s is still pending" % instance.id)
            return False
        
        if response['status'] != 'DONE':
            logger.debug("Unexpected instance state for instance %s: %s" % (instance.id, response['status']))
            return False             
        
        # DONE
        
        # get instance data
        response = self._conn.instances().get(project=self._project,
                                              zone=self._zone,
                                              instance=instance.id).execute()
                
        # get public address
        instance.pub_addr = str(response['networkInterfaces'][0]['accessConfigs'][0]['natIP'])
            
        # bootstrap the image
        exit_code = -1
        out = ""
        err = ""
        try:
            logger.debug("Will try to ssh to " + instance.id + " (" + instance.pub_addr + ")")
            ssh = SSHConnection()
            script_path = os.path.dirname(os.path.abspath(__file__)) + "/resources/vm-bootstrap.sh"
            ssh.put(self._ssh_privkey, instance.pub_addr, self._user, script_path, "/tmp/vm-bootstrap.sh")
            exit_code, out, err = ssh.run(self._ssh_privkey, instance.pub_addr, self._user, "sudo chmod 755 /tmp/vm-bootstrap.sh && sudo /tmp/vm-bootstrap.sh")
            h_exit_code, fqdn, h_err = ssh.run(self._ssh_privkey, instance.pub_addr, self._user, "hostname -f")
            fqdn = fqdn.rstrip('\n').rstrip('\r')
        except paramiko.SSHException, e:
            logger.debug("Failed to run bootstrap script on instance %s. Will retry later." % instance.id)
            logger.debug(str(e))
            return False
        except paramiko.SFTPError:
            logger.debug("Unable to ssh connect to instance %s. Will retry later." % instance.id)
            return False
        except socket.error:
            logger.debug("Unable to ssh connect to instance %s. Will retry later." % instance.id)
            return False
        
        
        if len(out) > 0:
            logger.debug("  stdout: %s" % out)
        if len(err) > 0:
            logger.debug("  stderr: %s" % err)
        if len(fqdn) > 0:
            logger.debug("  hostname stdout: %s" % fqdn)
        if len(h_err) > 0:
            logger.debug("  hostname stderr: %s" % h_err)
        if exit_code != 0:
            raise ExperimentException("Bootstrap script exited with error %d" % exit_code)
        if h_exit_code != 0:
            raise ExperimentException("Bootstrap script exited with error %d" % h_exit_code)
        
        # fill remaining fields
        instance.priv_addr = fqdn
        
        logger.info("Instance %s has booted, public address: %s" % (instance.id, instance.pub_addr))

        instance.add_tag(instance.pub_addr)
        instance.is_fully_instanciated = True
        return True

    def _retry(self, instance):
        
        """
        In case of reaching timeout for an instance, retry will terminate the previous instance and 
        replace it with a new instance.
        
        :param instance: the instance to terminate and replace with a new one
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param tags: Tags to add to the new instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        """
        
        logger.info("Instance %s has reached timeout, and will be replaced with a new instance" % instance.id)
        try:
            request = self._conn.instances().delete(project=self._project,
                                                    zone=self._zone,
                                                    instance=instance.id)
            response = request.execute()
            self._wait_for_operation(response['name'])
        except Exception:
            logger.info('Could not delete instance: %s' % instance.id)
            
        response = self._start_instance(instance.id, instance.instance_type, instance.image_id, instance.disk_size, instance.tags)

        instance.gce_boot_response = response
        instance.num_starts = instance.num_starts + 1
        instance.boot_time = int(time.time())
        instance.not_instanciated_correctly = False

    def provision(self, source_disk_image, machine_type, count=1, tags=[], disk_size=10,
                  boot_timeout=900, boot_max_tries=3):
        """
        Provision a new instance. Note that this method starts the provisioning cycle, but does not
        block for the instance to finish booting - for that, see wait()
        
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param count: Number of instances to provision. The default is 1.
        :param tags: Tags to add to the instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        :param boot_timeout: The amount of allowed time in seconds for an instance to boot
        :param boot_max_tries: The number of tries an instance is given to successfully boot
        """   
      
        name = 'inst-' + self._name.replace('_', '')
        if re.search('^(?:[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)$', name) is None:
            name = 'inst-' + str(uuid.uuid4().get_hex())
        
        
        for _i in range(count):
            inst_id = name + '-' + str(self.counter)
            self.counter += 1
            # add basic tags
            inst_tags = list(tags)
            inst_tags.append("precip")
            inst_tags.append(inst_id)
            
            instance = Instance(inst_id)
            
            try:
                response = self._start_instance(inst_id, machine_type, source_disk_image, disk_size, inst_tags)
                instance.gce_boot_response = response
            except Exception as e:
                logger.info("%s" % str(e))
                instance.not_instanciated_correctly = True
            
            # keep track of parameters - we might need them for restarts later
            instance.num_starts = 1
            instance.boot_time = int(time.time())
            instance.boot_timeout = boot_timeout
            instance.boot_max_tries = 3
            instance.image_id = source_disk_image
            instance.instance_type = machine_type
            instance.disk_size = disk_size
            
            for t in inst_tags:
                instance.add_tag(t)
            
            self._instances.append(instance)

    def wait(self, tags=[]):
        """
        Barrier for all currently instances to finish booting and be accessible via external addresses.
        
        :param tags: set of tags to match against
        :param timeout: maximum timeout to wait for instances to come up
        :param maxretrycount: The number of retries in case of failure of provisioning the instances 
        """
        
        count_pending = -1
        while count_pending != 0:
            count_pending = 0
            current_time = int(time.time())

            for i in self._instance_subset(tags):
                if not self._finish_instanciation(i):
                    count_pending += 1

                    # did the instance timeout?
                    if current_time > i.boot_time + i.boot_timeout:
                        logger.info("Timeout reached while waiting for instances to boot")
                        logger.info("A common cause for this that your image does not allow the" + \
                                    " root user to login.")
                        logger.info("Another common cause is infrastructure problems, preventing" + \
                                    " the instance from booting correctly.")
                        if i.num_starts < i.boot_max_tries:
                            self._retry(i)
                        else:
                            raise ExperimentException("Timeout reached while waiting for instances to boot")

            if count_pending > 0:
                logger.info("Still waiting for %d instances to finish booting" % (count_pending))
                time.sleep(20)       
        
    def deprovision(self, tags=[]):
        """
        Deprovisions (terminates) instances with the matching tags
        
        :param tags: set of tags to match against
        """
        responses = []
        
        for i in self._instance_subset(tags):
            try:
                logger.info("Deprovisioning instance: %s" % i.id)
                request = self._conn.instances().delete(project=self._project,
                                                        zone=self._zone,
                                                        instance=i.id)
                response = request.execute()
                responses.append({'response' : response, 'id' : i.id})
            except Exception:
                logger.info('Could not deprovision instance: %s' % i.id)
            self._instances.remove(i)
        
        if len(responses) == 0:
            return
        
        logger.info("Waiting for deprovisioning to complete")    
        while len(responses) > 0:
            r = responses.pop()
            try:
                self._wait_for_operation(r['response']['name'])
            except ExperimentException, e:
                if "RESOURCE_NOT_READY" in str(e):
                    time.sleep(30)
                    request = self._conn.instances().delete(project=self._project,
                                                        zone=self._zone,
                                                        instance=r['id'])
                    response = request.execute()
                else:
                    logger.warn('Deprovisioning issued an warning: %s' %str(e))
                
        logger.info("Deprovisioning done")                 


class EC2Experiment(Experiment):
    
    def __init__(self, region, endpoint, access_key, secret_key, name = None):
        """
        Initializes an EC2 experiment
        
        :param region: Amazon EC2 region, for example us-west-2c
        :param endpoint: Amazon EC2 endpoint, for example ec2.us-west-2.amazonaws.com
        :param access_keys: Amazon EC2 access key
        :param secret_keys: Amazon EC2 secret key
        """        
        Experiment.__init__(self, name = name)
    
        self._region = region
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
    
        self._conn = None
        
        # some infrastructures do not support security groups
        self._security_groups_support = True
    
        self._get_connection()
        self._ssh_keys_setup()
        self._security_groups_setup()

    def _get_connection(self):
        """
        Establishes a connection to the cloud endpoint
        """
        if (self._conn != None):
            return
                                                                                                                    
        logger.info("Connecting to endpoint " + self._endpoint)
        
        re_endpoint = re.compile(r'(([\w]+)://)?([\w\.\-]*)(?::(\d+))?(/[\S]*)?')
        r = re_endpoint.search(self._endpoint)
        if not r:
            raise ExperimentException("Unable to parse endpoint: %s" % (self._endpoint))

        # Parse successful
        proto = r.group(2)
        host = r.group(3)
        port = r.group(4)
        path = r.group(5)
        
        if proto is None:
            proto = "http"
        
        if port is None:
            if proto == "http":
                port = 80
            else:
                port = 443
        else:
            port = int(port)
            
        if path is None:
            path = ""
            
        is_secure=(proto == "https")
        # Nimbus wants is_secure to be true
        if self._region == "nimbus":
            is_secure = True
                                                                                  
        region = RegionInfo(name=self._region, endpoint=host)   
                                                
        self._conn = boto.connect_ec2(
                        self._access_key,
                        self._secret_key,
                        is_secure=is_secure,
                        region=region,
                        port=port,
                        path=path)
    
        # this next line is due to a bug in early boto versions
        self._conn.host = host
    
        # do a query to validate that the connection works
        try:
            self._conn.get_all_instances()
        except EC2ResponseError, e:
            self.ec2_conn = None
            raise ExperimentException("Unable to talk to the service", e) 
             
    def _ssh_keys_setup(self):
        
        """
        Makes sure we have our experiment keypair registered
        """
        uid = self._get_account_id()
        keypairs = None
        try:
            keypairs = self._conn.get_key_pair("precip_"+uid)

            # TODO: verify that the existing keypair matches the one in ~/.precip
        except IndexError, ie:
            # not found on eucalyptus
            pass
        except EC2ResponseError, e:
            if e.error_code in ["InvalidKeyPair.NotFound", "KeypairNotFound", "EC2APIError"]:
                keypairs = None
            else:
                raise ExperimentException("Unable to query for key pair", e)
  
         
        if keypairs is None:
            logger.info("Registering ssh pubkey as 'precip_"+uid+"'")
            f = open(self._ssh_pubkey)
            contents = f.read()
            f.close()
            self._conn.import_key_pair("precip_"+uid, contents) 
              
    def _security_groups_setup(self):
        """
        Sets up the default security group
        """
        sgroups = None
        try:
            sgroups = self._conn.get_all_security_groups(["precip"])
        except EC2ResponseError, e:
            if e.error_code in ["InvalidGroup.NotFound", "SecurityGroupNotFoundForProject"]:
                sgroups = None
            else:
                raise ExperimentException("Unable to find security group", e)
        
        if sgroups is None:
            try:
                logger.info("Registering default security group 'precip'")
                sg = self._conn.create_security_group("precip", "FutureGrid Experiment Mangement default group")
                sg.authorize(ip_protocol='tcp', from_port=22, to_port=22, cidr_ip='0.0.0.0/0')
                sg.authorize(src_group=sg)
            except Exception:
                logger.warn("Security group seems to be broken - disabling support")
                self._security_groups_support = False
                pass

    def _start_instance(self, image_id, instance_type, ebs_size):
        """
        Creates a new instance
        
        :param instance: the instance to check
        :return: the instance Boto object
        """
        uid = self._get_account_id()
        boto_instance = None
        try:
            # block device maps is only needed if the user wants to specify ebs_size
            block_device_map = None
            if ebs_size is not None:
                dev_sda1 = EBSBlockDeviceType(delete_on_termination = True)
                dev_sda1.size = int(ebs_size)
                block_device_map = BlockDeviceMapping()
                block_device_map['/dev/sda1'] = dev_sda1
            #else:
            #    block_device_map = self._conn.get_image_attribute(image_id, 
            #                                                      attribute = 'blockDeviceMapping')

            # create a boto image object from the image id          
            image_obj = self._conn.get_image(image_id)
            if image_obj is None:
                raise ExperimentException("Image %s does not exist" %(image_id))

            if self._security_groups_support:
                res = image_obj.run(instance_type = instance_type,
                                    key_name = "precip_"+uid,
                                    instance_initiated_shutdown_behavior = "terminate",
                                    block_device_map = block_device_map,
                                    security_groups = ["precip"])
            else:
                res = image_obj.run(instance_type = instance_type,
                                    key_name = "precip_"+uid,
                                    instance_initiated_shutdown_behavior = "terminate",
                                    block_device_map = block_device_map)
            boto_instance = res.instances[0]

            logger.info("Started instance %s, type %s" % (boto_instance.id, instance_type))        
        except Exception as e:
            raise ExperimentException("Unable to provision a new instance", e)
        return res.instances[0]

    def _finish_instanciation(self, instance):
        """
        Finishes booting and bootstraps an instace
        
        :param instance: the instance to check
        :return: True if the instance is ready, otherwise False
        """
        instance_record = []
        count_ing = 0
        # check if we are already done
        if instance.is_fully_instanciated:
            return True
            
        # now, let's wait until the instance i up and running        
        ec2inst = instance.ec2_instance
        ec2inst.update()
        
        if ec2inst.state == "error":
            logger.debug("Instance %s state is 'error - scheduling for possible retry" %instance.id)
            instance.boot_timeout = 0
            return False
        
        if ec2inst.state != "pending" and ec2inst.state != "running":
            
            #raise ExperimentException("Unexpected instance state for instance %s: %s" % (instance.id, ec2inst.state))
            logger.debug("Unexpected instance state for instance %s: %s" % (instance.id, ec2inst.state))
            return False             
        
        if ec2inst.state == "pending":
            logger.debug("Instance %s is still pending" % instance.id)
            return False
        
        if ec2inst.public_dns_name is None or \
           ec2inst.public_dns_name == "" or \
           ec2inst.public_dns_name.startswith('10.'):
            # we did not get a public address assigned to us, do it now

            # first check if we have unused floating ips laying around
            addr_to_use = None
            addresses = self._conn.get_all_addresses()
            for address in addresses:
                if address.instance_id is None or address.instance_id == "":
                    addr_to_use = address.public_ip
                    break

            if not addr_to_use:
                logger.debug("Requesting a new public IP address")
                addr_to_use = self._conn.allocate_address()

            logger.debug("Setting public ip: %s" %(addr_to_use))
            ec2inst.use_ip(addr_to_use)
            return False
    
        if not self._is_valid_hostaddr(ec2inst.public_dns_name):
            logger.debug("Waiting for instance %s to boot and be assigned a public IP address" % instance.id)
            return False
        
        # fill out instance fields
        instance.priv_addr = ec2inst.private_dns_name
        instance.pub_addr = ec2inst.public_dns_name
            
        # bootstrap the image
        exit_code = -1
        out = ""
        err = ""
        try:
            logger.debug("Will try to ssh to " + ec2inst.public_dns_name)
            ssh = SSHConnection()
            script_path = os.path.dirname(os.path.abspath(__file__)) + "/resources/vm-bootstrap.sh"
            ssh.put(self._ssh_privkey, ec2inst.public_dns_name, "root", script_path, "/root/vm-bootstrap.sh")
            exit_code, out, err = ssh.run(self._ssh_privkey, ec2inst.public_dns_name, "root", "chmod 755 /root/vm-bootstrap.sh && /root/vm-bootstrap.sh")
        except paramiko.SSHException:
            logger.debug("Failed to run bootstrap script on instance %s. Will retry later." % instance.id)
            logger.debug("Out: " + out)
            logger.debug("Err: " + err)
            return False
        except paramiko.SFTPError:
            logger.debug("Unable to ssh connect to instance %s. Will retry later." % instance.id)
            return False
        except socket.error:
            logger.debug("Unable to ssh connect to instance %s. Will retry later." % instance.id)
            return False
        
        if len(out) > 0:
            logger.info("  stdout: %s" % out)
        if len(err) > 0:
            logger.info("  stderr: %s" % err)
        if exit_code != 0:
            raise ExperimentException("Bootstrap script exited with error %d" % exit_code)
        
        logger.info("Instance %s has booted, public address: %s" % (instance.id, ec2inst.public_dns_name))

        # add our tags
        try:
            ec2inst.add_tag("Name", "PRECIP - " + self._name)
            # can only add 10 on EC2
            tag_count = 1
            for t in instance.tags:
                if tag_count >= 10:
                    break
                ec2inst.add_tag(t, "1")
                tag_count += 1
        except Exception, e:
            # ignore - the infrastructure might not support user tags
            pass
        
        instance.add_tag(instance.pub_addr)
        instance.is_fully_instanciated = True
        return True

    def _retry(self, instance):
        
        """
        In case of reaching timeout for an instance, retry will terminate the previous instance and 
        replace it with a new instance.
        
        :param instance: the instance to terminate and replace with a new one
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param tags: Tags to add to the new instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        """
        
        uid = self._get_account_id()
        self._get_connection()
        
        logger.info("Instance %s has reached timeout, and will be replaced with a new instance" % instance.id)
        try:
            self._conn.terminate_instances(instance_ids=[instance.id])
        except AttributeError as e:
            logger.warn("Terminating issued an attribute warning")
        except Exception as e:
            logger.warn("Ignoring error while terminating instance", e)

        boto_inst = self._start_instance(instance.image_id, instance.instance_type, instance.ebs_size)
        instance.id = boto_inst.id
        instance.ec2_instance = boto_inst
        instance.num_starts = instance.num_starts + 1
        instance.boot_time = int(time.time())

            
    def provision(self, image_id, instance_type='m1.small', count=1, ebs_size=None, tags=None,
                  boot_timeout=900, boot_max_tries=3):
        """
        Provision a new instance. Note that this method starts the provisioning cycle, but does not
        block for the instance to finish booting - for that, see wait()
        
        :param image_id: The image id as specified by the cloud infrastructure
        :param instance_type: The instance type (m1.small, m1.large, ...)
        :param count: Number of instances to provision. The default is 1.
        :param tags: Tags to add to the instance - this is important as tags are used throughout the API 
                     to find and manipulate instances
        :param boot_timeout: The amount of allowed time in seconds for an instance to boot
        :param boot_max_tries: The number of tries an instance is given to successfully boot
        """   
        
        uid = self._get_account_id()
        
        self._get_connection()
               
        for i in range(count):
            boto_inst = self._start_instance(image_id, instance_type, ebs_size)

            instance = Instance(boto_inst.id)
            instance.ec2_instance = boto_inst

            # keep track of parameters - we might need them for restarts later
            instance.num_starts = 1
            instance.boot_time = int(time.time())
            instance.boot_timeout = boot_timeout
            instance.boot_max_tries = 3
            instance.image_id = image_id
            instance.instance_type = instance_type
            instance.ebs_size = ebs_size
            
            # add basic tags
            instance.add_tag("precip")
            instance.add_tag(instance.id)
            for t in tags:
                instance.add_tag(t)
            
            self._instances.append(instance)


    def wait(self, tags=[]):
        """
        Barrier for all currently instances to finish booting and be accessible via external addresses.
        
        :param tags: set of tags to match against
        :param timeout: maximum timeout to wait for instances to come up
        :param maxretrycount: The number of retries in case of failure of provisioning the instances 
        """
        
        count_pending = -1
        while count_pending != 0:
            count_pending = 0
            current_time = int(time.time())

            for i in self._instance_subset(tags):
                if not self._finish_instanciation(i):
                    count_pending += 1

                    # did the instance timeout?
                    if current_time > i.boot_time + i.boot_timeout:
                        logger.info("Timeout reached while waiting for instances to boot")
                        logger.info("A common cause for this that your image does not allow the" + \
                                    " root user to login.")
                        logger.info("Another common cause is infrastructure problems, preventing" + \
                                    " the instance from booting correctly.")
                        if i.num_starts < i.boot_max_tries:
                            self._retry(i)
                        else:
                            raise ExperimentException("Timeout reached while waiting for instances to boot")

            if count_pending > 0:
                logger.info("Still waiting for %d instances to finish booting" % (count_pending))
                time.sleep(30)       
        

    def deprovision(self, tags=[]):
        """
        Deprovisions (terminates) instances with the matching tags
        
        :param tags: set of tags to match against
        """
        self._get_connection()
        for i in self._instance_subset(tags):
            logger.info("Deprovisioning instance: %s" % i.id)
            try:
                self._conn.terminate_instances(instance_ids=[i.id])
            except AttributeError as e:
                logger.warn("Deprovisioning issued an attribute warning")
            self._instances.remove(i)


class OpenStackExperiment(EC2Experiment):
    """
    A class defining an experiment running on top of OpenStack
    """
    
    def __init__(self, endpoint, access_key, secret_key, name = None):
        """
        Initializes an OpenStack experiment
        
        :param endpoint: OpenStack endpoint
        :param access_keys: OpenStack access key
        :param secret_keys: OpenStack secret key
        """        
        EC2Experiment.__init__(self, "openstack", endpoint, access_key, secret_key)


class EucalyptusExperiment(EC2Experiment):
    """
    A class defining an experiment running on top of Eucalyptus
    """
    
    def __init__(self, endpoint, access_key, secret_key, name = None):
        """
        Initializes an Eucalyptus experiment
        
        :param endpoint: Eucalyptus endpoint
        :param access_keys: Eucalyptus access key
        :param secret_keys: Eucalyptus secret key
        """        
        EC2Experiment.__init__(self, "eucalyptus", endpoint, access_key, secret_key)


class NimbusExperiment(EC2Experiment):
    """
    A class defining an experiment running on top of Nimbus
    """
    
    def __init__(self, endpoint, access_key, secret_key, name = None):
        """
        Initializes a Nimbus experiment
        
        :param endpoint: Nimbus endpoint
        :param access_keys: Nimbus access key
        :param secret_keys: Nimbus secret key
        """       
        EC2Experiment.__init__(self, "nimbus", endpoint, access_key, secret_key)
