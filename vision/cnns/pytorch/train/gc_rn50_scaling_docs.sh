mkdir ~/graphcore
cd ~/graphcore
git clone https://github.com/graphcore/examples.git
source /software/graphcore/poplar_sdk/3.0.0/enable
mkdir -p ~/venvs/graphcore
rm -rf ~/venvs/graphcore/poptorch30_rn50_env
virtualenv ~/venvs/graphcore/poptorch30_rn50_env
source ~/venvs/graphcore/poptorch30_rn50_env/bin/activate
POPLAR_SDK_ROOT=/software/graphcore/poplar_sdk/3.0.0
export POPLAR_SDK_ROOT=$POPLAR_SDK_ROOT
pip install $POPLAR_SDK_ROOT/poptorch-3.0.0+86945_163b7ce462_ubuntu_20_04-cp38-cp38-linux_x86_64.whl
mkdir ${HOME}/tmp
export TF_POPLAR_FLAGS=--executable_cache_path=${HOME}/tmp
export POPTORCH_CACHE_DIR=${HOME}/tmp
export POPART_LOG_LEVEL=INFO
export POPLAR_LOG_LEVEL=INFO
export POPLIBS_LOG_LEVEL=INFO
export PYTHONPATH=/software/graphcore/poplar_sdk/3.0.0/poplar-ubuntu_20_04-3.0.0+5691-1e179b3b85/python:$PYTHONPATH
cd ${HOME}/graphcore/examples/vision/cnns/pytorch/
make install-turbojpeg
make install
make get-data
pip install -r requirements.txt
pip uninstall pillow -y
CC="cc -mavx2" pip install --no-cache-dir -U --force-reinstall pillow-simd
export DATASETS_DIR=/mnt/localdata/datasets/
HOST1=`ifconfig eno1 | grep "inet " | grep -o '[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}' | head -1`
HOST1=`ifconfig ens2f0 | grep "inet " | grep -o '[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}' | head -1`
OCT123=`echo "$HOST1" | cut -d "." -f 1,2,3`
OCT4=`echo "$HOST1" | cut -d "." -f 4`
HOST2=$OCT123.`expr $OCT4 + 1`
HOST3=$OCT123.`expr $OCT4 + 2`
HOST4=$OCT123.`expr $OCT4 + 3`
export HOSTS=$HOST1,$HOST2,$HOST3,$HOST4
export CLUSTER=c16
export TCP_IF_INCLUDE=ens2f0
export VIPU_CLI_API_HOST=$HOST1
VIPU_SERVER=${VIPU_SERVER:=$HOST1}
FIRST_PARTITION=`vipu-admin list partitions --api-host $VIPU_SERVER| grep ACTIVE | cut -d '|' -f 3 | cut -d ' ' -f 2 | head -1`
PARTITON=${PARTITION:=$FIRST_PARTITION}
cd train




export DATASETS_DIR=/mnt/localdata/datasets/
#./rn50_pod16.sh
