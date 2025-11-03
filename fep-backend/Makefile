USER=ovidiu.ghibea
PARTITION=dgxa100
NODES=1
NTASKS=1
TIME=12:00:00
SCRIPT=slurm_job.sh
GPU_MODEL=tesla_a100
NUM_GPUS=2
JOB_NAME="f_webapp"
MEMORY=40G

ERR_FILE=$(JOB_NAME).err
OUT_FILE=$(JOB_NAME).out
ID_FILE=$(JOB_NAME).id

info_partitions:
	sinfo

info_gpus:
	sinfo -o "%10P %20N %10c %10m %25f %20G "

submit:
	sbatch --job-name=$(JOB_NAME) --output=$(OUT_FILE) --error=$(ERR_FILE) --mem=$(MEMORY) --partition=$(PARTITION) --nodes=$(NODES) --ntasks=$(NTASKS) --time=$(TIME) $(SCRIPT) | awk '{print $$4}' > $(ID_FILE)

submit_gpu:
	sbatch --job-name=$(JOB_NAME) --output=$(OUT_FILE) --error=$(ERR_FILE) --mem=$(MEMORY) \
	--partition=$(PARTITION) --nodes=1 --ntasks=$(NTASKS) --time=$(TIME) --gres=gpu:$(GPU_MODEL):3 \
	$(SCRIPT) | awk '{print $$4}' > $(ID_FILE)

submit_gpu_2:
	sbatch --job-name=$(JOB_NAME) --output=$(OUT_FILE) --error=$(ERR_FILE) --mem=$(MEMORY) \
    --partition=$(PARTITION) --nodelist=dgxa100-ncit-wn04 --nodes=1 --ntasks=$(NTASKS) \
    --time=$(TIME) --gres=gpu:$(GPU_MODEL):1 $(SCRIPT) | awk '{print $$4}' > $(ID_FILE)

partition:
	srun --pty -p $(PARTITION) --gres=gpu:$(GPU_MODEL):3 bash

submit_model:
	sbatch --job-name=$(JOB_NAME) --output=$(OUT_FILE) --error=$(ERR_FILE) --mem=$(MEMORY) \
	--partition=$(PARTITION) --nodelist=dgxa100-ncit-wn04 --nodes=1 --ntasks=$(NTASKS) \
	--time=$(TIME) --gres=gpu:$(GPU_MODEL):1 run_model_job.sh $(MODEL)


compression:
	$(MAKE) submit_model MODEL=compression

meteo:
	$(MAKE) submit_model MODEL=meteo

durangaldea:
	$(MAKE) submit_model MODEL=durangaldea
