###############################################################################
# Dockerfile for cadcontest/cad2025problemc:latest
###############################################################################

# 1) Full CUDA toolkit + compiler
FROM nvidia/cuda:11.8.0-devel-ubuntu20.04

# 2) Overwrite APT sources with a known-good Ubuntu 20.04 archive list
RUN printf "deb http://archive.ubuntu.com/ubuntu focal main universe restricted multiverse\n\
deb http://archive.ubuntu.com/ubuntu focal-security main universe restricted multiverse\n\
deb http://archive.ubuntu.com/ubuntu focal-updates main universe restricted multiverse\n" \
    > /etc/apt/sources.list

# 3) Install essential build tools and libraries
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git \
        wget \
        build-essential \
        libboost-dev \
        libboost-system-dev \
        libboost-filesystem-dev \
        protobuf-compiler \
        libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
COPY Mambaforge.sh /tmp/Mambaforge.sh
COPY probC_env.yaml /tmp/probC_env.yaml

RUN bash /tmp/Mambaforge.sh -b -p /opt/conda && rm /tmp/Mambaforge.sh

ENV PATH="/opt/conda/bin:${PATH}"
SHELL ["/bin/bash", "-c"]

RUN source /opt/conda/etc/profile.d/conda.sh && conda --version
RUN source /opt/conda/etc/profile.d/conda.sh && mamba --version
RUN source /opt/conda/etc/profile.d/conda.sh && mamba env create -f /tmp/probC_env.yaml

# 5) Default to bash (contestants can compile in C++/CUDA, activate conda, etc.)
CMD ["/bin/bash"]
