FROM mambaorg/micromamba:1.5.10

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml

RUN micromamba create --yes --file /tmp/environment.yml \
    && micromamba clean --all --yes

USER root
WORKDIR /app

COPY --chown=$MAMBA_USER:$MAMBA_USER . /app

RUN mkdir -p /app/data/INPUT /app/data/OUTPUT \
    && chown -R $MAMBA_USER:$MAMBA_USER /app

ENV MPLBACKEND=Agg \
    TERM=xterm-256color \
    PYTHONPATH=/app:/app/FR \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8085

ENTRYPOINT ["micromamba", "run", "-n", "storcito"]
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8085"]
