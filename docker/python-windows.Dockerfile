# escape=`
FROM mcr.microsoft.com/windows/servercore:ltsc2022

SHELL ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

ARG PYTHON_VERSION=3.11.9
ARG TRIVY_VERSION=0.69.3

RUN Invoke-WebRequest -Uri https://awscli.amazonaws.com/AWSCLIV2.msi -OutFile C:\AWSCLIV2.msi ; `
    Start-Process msiexec.exe -ArgumentList '/i C:\AWSCLIV2.msi /qn' -Wait ; `
    Remove-Item C:\AWSCLIV2.msi -Force

RUN Invoke-WebRequest -Uri "https://www.python.org/ftp/python/${env:PYTHON_VERSION}/python-${env:PYTHON_VERSION}-amd64.exe" -OutFile C:\python-installer.exe ; `
    Start-Process C:\python-installer.exe -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait ; `
    Remove-Item C:\python-installer.exe -Force

RUN Invoke-WebRequest -Uri https://micro.mamba.pm/api/micromamba/win-64/latest -OutFile C:\micromamba.tar.bz2 ; `
    New-Item -ItemType Directory -Force -Path C:\micromamba | Out-Null ; `
    tar -xjf C:\micromamba.tar.bz2 -C C:\micromamba ; `
    Remove-Item C:\micromamba.tar.bz2 -Force

RUN $trivyArchive = "trivy_${env:TRIVY_VERSION}_windows-64bit.zip" ; `
    Invoke-WebRequest -Uri "https://github.com/aquasecurity/trivy/releases/download/v${env:TRIVY_VERSION}/$trivyArchive" -OutFile C:\trivy.zip ; `
    Expand-Archive -Path C:\trivy.zip -DestinationPath C:\trivy -Force ; `
    Remove-Item C:\trivy.zip -Force

RUN python -m pip install --upgrade pip ; `
    python -m pip install --no-cache-dir boto3 conda-pack cyclonedx-bom pyyaml safety

ENV SCRIPT_ROOT=C:\package-scanner\scripts
ENV PYTHON_BIN=python
ENV MAMBA_BIN=C:\micromamba\Library\bin\micromamba.exe
ENV CONDA_PACK_BIN=conda-pack
ENV PATH="C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0;C:\Program Files\Amazon\AWSCLIV2;C:\Program Files\Python311;C:\Program Files\Python311\Scripts;C:\micromamba\Library\bin;C:\trivy"

COPY scripts\bundle-directory.py C:\package-scanner\scripts\bundle-directory.py
COPY scripts\extract-archive.py C:\package-scanner\scripts\extract-archive.py
COPY scripts\generate-governance-artifacts.py C:\package-scanner\scripts\generate-governance-artifacts.py
COPY scripts\generate-python-materialization-summary.py C:\package-scanner\scripts\generate-python-materialization-summary.py
COPY scripts\plan-python-environment-install.py C:\package-scanner\scripts\plan-python-environment-install.py
COPY scripts\run-python-ecs-task.ps1 C:\package-scanner\scripts\run-python-ecs-task.ps1

ENTRYPOINT ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\package-scanner\\scripts\\run-python-ecs-task.ps1"]
