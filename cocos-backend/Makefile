IMAGE_NAME=llm-backend
CONTAINER_NAME=llm-backend-container
HOST_PORT=7100
CONTAINER_PORT=7010

# ---- backend ----
IMAGE_NAME_DUAL = llm-backend-dual
CONTAINER_NAME_DUAL = llm-backend-dual
HOST_PORT_DUAL ?= 8010
CONTAINER_PORT_DUAL ?= 8010

build:
	docker build -t $(IMAGE_NAME) .
run:
	docker run -it -d \
		--name ${CONTAINER_NAME} \
		-p 7010:7010 \
		-v $(PWD)/logs:/var/log/llm-backend \
		--add-host=host.docker.internal:172.17.0.1 \
		$(IMAGE_NAME)

cli:
	docker exec -it $(CONTAINER_NAME) /bin/bash

logss:
	docker logs $(CONTAINER_NAME)

stop:
	docker stop $(CONTAINER_NAME)
	docker rm $(CONTAINER_NAME)

clean:
	docker image rm $(IMAGE_NAME)

build-dual:
	docker build -t $(IMAGE_NAME_DUAL) .

run-dual:
	# Important: we override the command to run the dual entrypoint and publish 8010
	docker run -it -d \
		--name $(CONTAINER_NAME_DUAL) \
		-e DUAL_BACKEND_PORT=$(CONTAINER_PORT_DUAL) \
		-p $(HOST_PORT_DUAL):$(CONTAINER_PORT_DUAL) \
		-v $(PWD)/logs-dual:/var/log/llm-backend-dual \
		--add-host=host.docker.internal:172.17.0.1 \
		$(IMAGE_NAME_DUAL) /start_backend_dual.sh

logs-dual:
	docker logs -f $(CONTAINER_NAME_DUAL)

cli-dual:
	docker exec -it $(CONTAINER_NAME_DUAL) /bin/bash

stop-dual:
	- docker stop $(CONTAINER_NAME_DUAL)
	- docker rm $(CONTAINER_NAME_DUAL)

clean-dual:
	- docker image rm $(IMAGE_NAME_DUAL)

.PHONY: build run cli stop clean build-dual run-dual logs-dual cli-dual stop-dual clean-dual

.PHONY: rebuild-dual
rebuild-dual:
	@echo "=== Rebuilding dual backend ==="
	@$(MAKE) stop-dual
	@$(MAKE) clean-dual
	@$(MAKE) build-dual
	@$(MAKE) run-dual
	@echo "=== Dual backend rebuild complete ==="

