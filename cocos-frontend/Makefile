IMAGE_NAME = futural-metasearch2
CONTAINER_NAME = futural-metasearch2
HOST_PORT = 7173
CONTAINER_PORT = 80

build:
	docker build -t $(IMAGE_NAME) .

run:
	docker run -d --name $(CONTAINER_NAME) -p $(HOST_PORT):$(CONTAINER_PORT) $(IMAGE_NAME)

logs:
	docker logs -f $(CONTAINER_NAME)

stop:
	docker stop $(CONTAINER_NAME) || true
	docker rm $(CONTAINER_NAME) || true

clean:
	docker image rm $(IMAGE_NAME) || true

.PHONY: build run logs stop clean
