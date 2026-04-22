# escape=`
FROM mcr.microsoft.com/windows/servercore:ltsc2022

SHELL ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

ARG R_VERSION=4.4.0
ARG PYTHON_VERSION=3.11.9
ARG TRIVY_VERSION=0.69.3

RUN Invoke-WebRequest -Uri https://awscli.amazonaws.com/AWSCLIV2.msi -OutFile C:\AWSCLIV2.msi ; `
    Start-Process msiexec.exe -ArgumentList '/i C:\AWSCLIV2.msi /qn' -Wait ; `
    Remove-Item C:\AWSCLIV2.msi -Force

RUN Invoke-WebRequest -Uri "https://www.python.org/ftp/python/${env:PYTHON_VERSION}/python-${env:PYTHON_VERSION}-amd64.exe" -OutFile C:\python-installer.exe ; `
    Start-Process C:\python-installer.exe -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait ; `
    Remove-Item C:\python-installer.exe -Force

RUN Invoke-WebRequest -Uri "https://cran.r-project.org/bin/windows/base/old/${env:R_VERSION}/R-${env:R_VERSION}-win.exe" -OutFile C:\R-installer.exe ; `
    Start-Process C:\R-installer.exe -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=C:\R\R-4.4.0' -Wait ; `
    Remove-Item C:\R-installer.exe -Force ; `
    if (-not (Test-Path 'C:\R\R-4.4.0\bin\Rscript.exe')) { throw 'R installation did not create C:\R\R-4.4.0\bin\Rscript.exe' } ; `
    $env:Path = \"C:\R\R-4.4.0\bin;$env:Path\"

RUN Invoke-WebRequest -Uri https://cran.r-project.org/bin/windows/Rtools/rtools44/files/rtools44-6459-6401.exe -OutFile C:\rtools-installer.exe ; `
    Start-Process C:\rtools-installer.exe -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=C:\rtools44' -Wait ; `
    Remove-Item C:\rtools-installer.exe -Force

RUN $trivyArchive = \"trivy_${env:TRIVY_VERSION}_windows-64bit.zip\" ; `
    Invoke-WebRequest -Uri \"https://github.com/aquasecurity/trivy/releases/download/v${env:TRIVY_VERSION}/$trivyArchive\" -OutFile C:\trivy.zip ; `
    Expand-Archive -Path C:\trivy.zip -DestinationPath C:\trivy -Force ; `
    Remove-Item C:\trivy.zip -Force

RUN python -m pip install --upgrade pip boto3 ; `
    & 'C:\R\R-4.4.0\bin\Rscript.exe' -e \"options(repos=c(RSPM='https://packagemanager.posit.co/all/latest',CRAN='https://cloud.r-project.org')); install.packages(c('renv','ggplot2','isoband','rlang','vctrs'), dependencies=NA, Ncpus=1)\"

ENV SCRIPT_ROOT=C:\package-scanner\scripts
ENV R_SYSTEM_LIBRARY=C:\R\R-4.4.0\library
ENV PATH="C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0;C:\Program Files\Amazon\AWSCLIV2;C:\Program Files\Python311;C:\Program Files\Python311\Scripts;C:\R\R-4.4.0\bin;C:\rtools44\usr\bin;C:\rtools44\mingw64\bin;C:\trivy"

COPY scripts\materialize-r-environment.R C:\package-scanner\scripts\materialize-r-environment.R
COPY scripts\bundle-directory.py C:\package-scanner\scripts\bundle-directory.py
COPY scripts\extract-archive.py C:\package-scanner\scripts\extract-archive.py
COPY scripts\generate-r-materialization-summary.py C:\package-scanner\scripts\generate-r-materialization-summary.py
COPY scripts\generate-r-sbom.py C:\package-scanner\scripts\generate-r-sbom.py
COPY scripts\scan-r-vulnerabilities.py C:\package-scanner\scripts\scan-r-vulnerabilities.py
COPY scripts\preflight-r-native-deps.py C:\package-scanner\scripts\preflight-r-native-deps.py
COPY scripts\generate-r-governance-artifacts.py C:\package-scanner\scripts\generate-r-governance-artifacts.py
COPY scripts\run-r-ecs-task.ps1 C:\package-scanner\scripts\run-r-ecs-task.ps1

ENTRYPOINT ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\package-scanner\\scripts\\run-r-ecs-task.ps1"]
