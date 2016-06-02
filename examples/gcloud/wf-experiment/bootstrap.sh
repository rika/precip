#!/bin/bash

MASTER_ADDR=$1
/etc/init.d/condor stop >/dev/null 2>&1 || /bin/true

# correct clock is important for most projects
sudo apt-get install ntpdate
service ntpdate start

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update
sudo apt-get install build-essential make gawk bison diffutils \
                     default-jre default-jdk openjdk-7-jre openjdk-7-jdk \
                     ganglia-monitor ganglia-monitor-python condor pegasus-wms  

/etc/init.d/condor stop >/dev/null 2>&1 || /bin/true


# Clear the Condor local config file - we use config.d instead
cat /dev/null >/etc/condor/condor_config.local

# Common Condor config between master and workers
cat >/etc/condor/config.d/50-main.conf <<EOF

CONDOR_HOST = $MASTER_ADDR

FILESYSTEM_DOMAIN = \$(FULL_HOSTNAME)
TRUST_UID_DOMAIN = True

DAEMON_LIST = MASTER, STARTD

# security
ALLOW_WRITE = 10.*
ALLOW_READ = \$(ALLOW_WRITE)

# default policy
START = True
SUSPEND = False
CONTINUE = True
PREEMPT = False
KILL = False

EOF

# Master gets extra packages/configs
if [ "x$MASTER_ADDR" = "x" ]; then
    yum -q -y install ganglia-gmetad ganglia-web

    cat >/etc/condor/config.d/90-master.conf <<EOF
CONDOR_HOST = \$(FULL_HOSTNAME)
DAEMON_LIST = MASTER, COLLECTOR, NEGOTIATOR, SCHEDD
EOF
fi

# Restarting daemons
/etc/init.d/condor start

# User to run the workflows as, and allow the experiment management
# ssh key to authenticate
adduser wf
mkdir -p ~wf/.ssh
cp ~/.ssh/authorized_keys ~wf/.ssh/
chown -R wf: ~wf/.ssh
    
# Master is the submit host, so deploy our workflow on it
if [ "x$MASTER_ADDR" = "x" ]; then
    # install the workflow tarball and wait script
    cd ~wf
    wget -q http://pegasus.isi.edu/static/precip/wf-experiment/montage.tar.gz
    tar xzf montage.tar.gz
    chown -R wf: montage*
fi

