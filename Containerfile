FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

RUN microdnf install -y --nodocs python3.12 \
    && microdnf clean all

WORKDIR /opt/app
COPY ocp_lifecycle_exporter/ ./ocp_lifecycle_exporter/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 1001
EXPOSE 9120
ENTRYPOINT ["python3.12", "-m", "ocp_lifecycle_exporter"]
