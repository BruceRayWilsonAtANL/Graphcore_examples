#!/bin/bash
# ssh gc-poplar-02.ai.alcf.anl.gov
#
# Author:  Bill Arnold
#
# These should be done in /etc/profile.d but I was having trouble with PYTHONPATH
source /lambda_stor/software/graphcore/poplar_sdk/3.0.0/popart-ubuntu_20_04-3.0.0+5691-1e179b3b85/enable.sh
source /lambda_stor/software/graphcore/poplar_sdk/3.0.0/poplar-ubuntu_20_04-3.0.0+5691-1e179b3b85/enable.sh

# because it isn't even available in children like "screen"
POPLAR_SDK_ROOT=/lambda_stor/software/graphcore/poplar_sdk/3.0.0
export POPLAR_SDK_ROOT=$POPLAR_SDK_ROOT
rm -r ~/venvs/graphcore/poptorch30_env
###virtualenv -p python3.6 ~/workspace/poptorch30.env
virtualenv ~/venvs/graphcore/poptorch30_env
source ~/venvs/graphcore/poptorch30_env/bin/activate
pip install $POPLAR_SDK_ROOT/poptorch-3.0.0+86945_163b7ce462_ubuntu_20_04-cp38-cp38-linux_x86_64.whl

#export IPUOF_CONFIG_PATH=~/.ipuof.conf.d/lr21-1-16ipu.conf
export TF_POPLAR_FLAGS=--executable_cache_path=/home/wilsonb/tmp
export POPTORCH_CACHE_DIR=/home/wilsonb/tmp

export POPART_LOG_LEVEL=WARN
export POPLAR_LOG_LEVEL=WARN
export POPLIBS_LOG_LEVEL=WARN

export PYTHONPATH=/lambda_stor/software/graphcore/poplar_sdk/3.0.0/poplar-ubuntu_20_04-3.0.0+5691-1e179b3b85/python:$PYTHONPATH
