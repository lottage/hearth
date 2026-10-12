# Hearth image = the StoneSage image + the Hearth app, enabled (STONESAGE_APPS=hearth). One image per layer, each built
# FROM the one below; this is the one the Compose stack from `python -m lodestone.installer --docker` runs.
#
# Shipped as `Dockerfile` in the hearth repository:
#     docker build -t ghcr.io/lottage/hearth:latest .
# Needs the stonesage image. Until it is published, build it from the stonesage repository first and say so:
#     docker build -t stonesage:local ../stonesage
#     docker build --build-arg STONESAGE_IMAGE=stonesage:local -t ghcr.io/lottage/hearth:latest .
# The default is a release tag, never `latest`. digest: pin at release.
ARG STONESAGE_IMAGE=ghcr.io/lottage/stonesage:0.1.0
FROM ${STONESAGE_IMAGE}

# The platform and Lodestone are already in the base image's environment, so only Hearth itself is installed (its
# third-party requirements, websockets, redis and Pillow, are in the base too). pip builds from a copy, not from /src.
USER root
COPY pyproject.toml README.md LICENSE /src/
COPY hearth /src/hearth
RUN pip install --no-cache-dir --no-deps /src && rm -rf /src
USER stonesage

ENV STONESAGE_APPS=hearth
