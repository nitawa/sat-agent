#!/bin/bash
cd ..
rm -rf sat
git clone https://github.com/SalomePlatform/sat.git sat
rm -rf sat_salome
git clone https://github.com/SalomePlatform/sat_salome.git sat_salome
sat/sat init --add_project $PWD/sat_salome/salome.pyconf
echo "ready..."
