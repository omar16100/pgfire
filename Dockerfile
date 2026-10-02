FROM postgres:17-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake ninja-build gcc make libcurl4-openssl-dev libjson-c-dev libssl-dev \
    libpq-dev postgresql-server-dev-17 && rm -rf /var/lib/apt/lists/*
WORKDIR /source
COPY CMakeLists.txt ./
COPY include include
COPY src src
COPY tests/unit tests/unit
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug \
    && cmake --build build && ctest --test-dir build --output-on-failure \
    && make -C src/guard PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config

FROM postgres:17-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4 libjson-c5 python3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /source/build/pgfire /usr/local/bin/pgfire
COPY --from=build /source/src/guard/pgfire_guard.so /usr/lib/postgresql/17/lib/
COPY tools/runtime.py /usr/local/bin/pgfire-runtime
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/local/share/doc/pgfire/
COPY LICENSES /usr/local/share/doc/pgfire/LICENSES
LABEL org.opencontainers.image.source="https://github.com/omar16100/pgfire" \
      org.opencontainers.image.licenses="Apache-2.0"
USER postgres
ENV PGDATA=/tmp/pgfire/data
ENTRYPOINT ["python3", "/usr/local/bin/pgfire-runtime"]
