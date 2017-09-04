@echo off
echo -- Steve --
echo Creating conda environment

conda create -n steve steve

echo Configuring Steve...

md "%USERPROFILE%\steve"
echo @echo off > "%USERPROFILE%\steve\steve.bat"

set stevepath=
for /f "delims=" %%i in ('conda info --root') do @set stevepath=%%i
set stevepath=%stevepath%\envs\steve\Scripts\steve.exe %%*
echo  %stevepath% >> "%USERPROFILE%\steve\steve.bat"

set /p jenkinsuser="Inform your Jenkins user: "
set /p jenkinstoken="Inform your Jenkins password (token): "

set jenkinsuser=name=%jenkinsuser%
set jenkinstoken=password=%jenkinstoken%

echo [user] > "%USERPROFILE%\.steve"
echo %jenkinsuser% >> "%USERPROFILE%\.steve"
echo %jenkinstoken% >> "%USERPROFILE%\.steve"

echo Steve configured.
