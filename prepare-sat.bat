@echo off
cd ..
if exist sat rmdir /s /q sat
git clone https://github.com/SalomePlatform/sat.git sat
if exist sat_salome rmdir /s /q sat_salome
git clone https://github.com/SalomePlatform/sat_salome.git sat_salome
python sat\sat init --add_project "%CD%\sat_salome\salome.pyconf"
echo "ready..."
