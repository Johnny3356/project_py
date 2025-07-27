# to build an image:
```
docker build -t iccad25_probc_env .
```

# to launch a container:
```
docker run --gpus all -it -v <root folder of your code>:/workspace -w /workspace iccad25_probc_env:latest bash
```

# make sure to use this conda env inside the container for development
```
source /opt/conda/etc/profile.d/conda.sh
source /opt/conda/etc/profile.d/mamba.sh
mamba activate probC_env
```






