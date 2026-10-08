# Construir desde la raíz del repo: docker build -f services/simulador-aliados/Dockerfile .
FROM wiremock/wiremock:3.9.2
ENV PORT=8080
COPY --chmod=755 services/simulador-aliados/healthcheck /app/healthcheck
COPY services/simulador-aliados/mappings /home/wiremock/mappings
COPY services/simulador-aliados/__files /home/wiremock/__files
