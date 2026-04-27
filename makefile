IMAGE ?= $(HOST)/logpose/logpose:latest

.PHONY: build-push
build-push:
	docker buildx build \
		--platform linux/amd64 \
		--provenance=false \
		--sbom=false \
		--tag $(IMAGE) \
		--push .
