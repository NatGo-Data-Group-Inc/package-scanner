# escape=`
FROM mcr.microsoft.com/windows/servercore:ltsc2022

SHELL ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

ARG R_VERSION=4.4.0
ARG TRIVY_VERSION=0.69.3

RUN Invoke-WebRequest -Uri https://awscli.amazonaws.com/AWSCLIV2.msi -OutFile C:\AWSCLIV2.msi ; `
    Start-Process msiexec.exe -ArgumentList '/i C:\AWSCLIV2.msi /qn' -Wait ; `
    Remove-Item C:\AWSCLIV2.msi -Force

RUN Invoke-WebRequest -Uri https://www.python.org/ftp/python/3.11.11/python-3.11.11-amd64.exe -OutFile C:\python-installer.exe ; `
    Start-Process C:\python-installer.exe -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait ; `
    Remove-Item C:\python-installer.exe -Force

RUN Invoke-WebRequest -Uri https://cran.r-project.org/bin/windows/base/R-4.4.0-win.exe -OutFile C:\R-installer.exe ; `
    Start-Process C:\R-installer.exe -ArgumentList '/VERYSILENT /CURRENTUSER /DIR=C:\R\R-4.4.0' -Wait ; `
    Remove-Item C:\R-installer.exe -Force ; `
    $env:Path = \"C:\R\R-4.4.0\bin;$env:Path\"

RUN Invoke-WebRequest -Uri https://cran.r-project.org/bin/windows/Rtools/rtools44/files/rtools44-6459-6401.exe -OutFile C:\rtools-installer.exe ; `
    Start-Process C:\rtools-installer.exe -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=C:\rtools44' -Wait ; `
    Remove-Item C:\rtools-installer.exe -Force

RUN $trivyArchive = \"trivy_${env:TRIVY_VERSION}_windows-64bit.zip\" ; `
    Invoke-WebRequest -Uri \"https://github.com/aquasecurity/trivy/releases/download/v${env:TRIVY_VERSION}/$trivyArchive\" -OutFile C:\trivy.zip ; `
    Expand-Archive -Path C:\trivy.zip -DestinationPath C:\trivy -Force ; `
    Remove-Item C:\trivy.zip -Force

RUN python -m pip install --upgrade pip boto3 ; `
    & 'C:\R\R-4.4.0\bin\Rscript.exe' -e \"install.packages('renv', repos='https://cloud.r-project.org')\"

ENV SCRIPT_ROOT=C:\package-scanner\scripts
ENV PATH=C:\R\R-4.4.0\bin;C:\rtools44\usr\bin;C:\rtools44\mingw64\bin;C:\trivy;%PATH%

COPY scripts\materialize-r-environment.R C:\package-scanner\scripts\materialize-r-environment.R
COPY scripts\bundle-directory.py C:\package-scanner\scripts\bundle-directory.py
COPY scripts\extract-archive.py C:\package-scanner\scripts\extract-archive.py
COPY scripts\generate-r-materialization-summary.py C:\package-scanner\scripts\generate-r-materialization-summary.py
COPY scripts\generate-r-sbom.py C:\package-scanner\scripts\generate-r-sbom.py
COPY scripts\scan-r-vulnerabilities.py C:\package-scanner\scripts\scan-r-vulnerabilities.py
COPY scripts\generate-r-governance-artifacts.py C:\package-scanner\scripts\generate-r-governance-artifacts.py
COPY scripts\run-r-ecs-task.ps1 C:\package-scanner\scripts\run-r-ecs-task.ps1

ENTRYPOINT ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\package-scanner\\scripts\\run-r-ecs-task.ps1"]
