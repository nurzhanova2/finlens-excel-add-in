# FinLens analysis sandbox: one ephemeral, non-root, network-less job per run_analysis call.
# Runtime hardening (no network, read-only root, dropped capabilities, limits) is applied by
# api/app/adapters/sandbox.py on every `docker run`; this image only fixes the code and libraries.
FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel || true

RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sandbox

COPY finlens_runner.py /opt/finlens/finlens_runner.py
# Pre-build the matplotlib font cache so jobs start fast on a read-only filesystem.
RUN MPLCONFIGDIR=/opt/finlens/mpl MPLBACKEND=Agg python -c "import matplotlib.pyplot" \
    && chown -R root:root /opt/finlens \
    && chmod -R a+rX,go-w /opt/finlens

ENV MPLBACKEND=Agg \
    HOME=/tmp \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_MAX_THREADS=1

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "-B", "/opt/finlens/finlens_runner.py"]
