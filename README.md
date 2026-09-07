# sat-agent

**SAT Compilation Agent**: Turn natural-language compilation and porting requests into verified, reproducible **SALOME SAT build scripts (`build_result.sh`) and `.pyconf` configurations**.

Automates SALOME compilation, repository preparation, product dependency management, `.pyconf` updates, and platform porting across Linux distributions (**Slackware**, **Ubuntu**, **Debian**, **Fedora**, **RHEL/Rocky**, **Arch Linux**) using Docker and dynamic system prerequisite resolution.

---

## Features

- **Automated `.pyconf` Management**: Parse, edit, and generate SAT Python configuration files (`PyconfManager`). Programmatically adds products (`PRODUCTS['my_prod'] = {...}`) and updates application product lists.
- **Platform Porting & Docker Construction**: Dynamically builds Dockerfiles from scratch tailored for target Linux distributions, including **Slackware** (`vbatts/slackware:latest` + `slackpkg`), Ubuntu (`apt`), Fedora (`dnf`), and Arch (`pacman`).
- **Iterative System Prerequisite Resolution**: Maps compiler errors (missing headers like `GL/gl.h` or `hdf5.h`) directly to distribution-specific OS packages (`mesa`, `hdf5`, `libX11`), updates Dockerfiles, and resumes compilation.
- **Reproducible Script Generation**: Outputs standalone `build_result.sh` and `custom_app.conf` for reproducible builds outside the agent.

---

## Quick Start

```bash
cd /home/nghodban/workspace/AI/sat_agent
pip install -e .

# Run sat-agent targeting Slackware
sat-agent "Build SALOME 9.16.0 with custom GEOM product" --distro slackware --batch
```
