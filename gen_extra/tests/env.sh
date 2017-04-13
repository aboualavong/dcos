#!/usr/bin/env bash

set -x
set -o errexit -o pipefail

./util/fetch_dcos.py

mkdir -p cache

if [ ! -e cache/build_venv ]
then
    rm -rf cache/build_venv
fi

python -m venv cache/build_venv
source cache/build_venv/bin/activate

pushd ext/upstream
./prep_local
popd

echo "Enviroment is ready"