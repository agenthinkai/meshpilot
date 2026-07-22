# MeshPilot — llama.cpp CPU inference server
# Builds llama.cpp from source with optimal CPU flags detected at build time
FROM ubuntu:24.04

ARG LLAMA_CPP_VERSION=b3447
ARG CMAKE_BUILD_TYPE=Release

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    curl \
    libopenblas-dev \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Clone llama.cpp at pinned version
RUN git clone --depth 1 --branch ${LLAMA_CPP_VERSION} \
    https://github.com/ggerganov/llama.cpp.git . || \
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git .

# Build with OpenBLAS for CPU BLAS acceleration
# AMX/AVX-512 flags are auto-detected by CMake on the build host
RUN cmake -B build \
    -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE} \
    -DLLAMA_BLAS=ON \
    -DLLAMA_BLAS_VENDOR=OpenBLAS \
    -DLLAMA_NATIVE=ON \
    -DLLAMA_AVX=ON \
    -DLLAMA_AVX2=ON \
    -DLLAMA_F16C=ON \
    -DLLAMA_FMA=ON \
    && cmake --build build --config ${CMAKE_BUILD_TYPE} -j$(nproc) --target llama-server llama-quantize

RUN cp build/bin/llama-server /usr/local/bin/ && \
    cp build/bin/llama-quantize /usr/local/bin/ && \
    chmod +x /usr/local/bin/llama-server /usr/local/bin/llama-quantize

WORKDIR /

# Wrapper script that starts the server for whichever GGUF is in /models
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080
ENTRYPOINT ["/start.sh"]
