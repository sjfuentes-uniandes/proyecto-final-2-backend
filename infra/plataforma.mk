# Targets de la plataforma de las historias (infra/platform + infra/apps).
# Incluido desde el Makefile raíz. Guía completa: infra/DESPLIEGUE.md
#
#   make infra-desplegar ENV=int                  todo: plataforma, bases, imágenes, aplicaciones
#   make infra-microservicios SERVICES=clientes   imagen + despliegue de uno o varios servicios
#   make infra-destruir ENV=int                   elimina el ambiente completo
#
# Variables: ENV (int), AWS_REGION (us-east-1), SERVICES (todos los que tienen
# services/<servicio>/Dockerfile), AUTO_APPROVE=1 (sin confirmaciones), SRC_DIR (services).

ENV ?= int
AWS_REGION ?= us-east-1
AUTO_APPROVE ?= 0
SERVICES ?=
PLATAFORMA_SCRIPTS := scripts/plataforma
TF ?= terraform

export ENV AWS_REGION AUTO_APPROVE SERVICES

.PHONY: infra-ayuda infra-bootstrap infra-plataforma infra-bases infra-imagenes \
	infra-aplicaciones infra-microservicios infra-desplegar infra-destruir \
	infra-pausar infra-reanudar infra-plan infra-salidas infra-validar infra-probar

infra-ayuda: ## Lista los targets de la plataforma
	@grep -hE '^infra-[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{ printf "  %-22s %s\n", $$1, $$2 }'

infra-bootstrap: ## Una vez por cuenta: bucket de estado S3 y rol OIDC de GitHub
	@$(PLATAFORMA_SCRIPTS)/bootstrap.sh

infra-plataforma: ## Paso 1: red, datos, mensajería, identidad, borde y ECR
	@$(PLATAFORMA_SCRIPTS)/platform.sh apply

infra-bases: ## Paso 2: base y usuario por servicio en RDS (idempotente)
	@$(PLATAFORMA_SCRIPTS)/db-bootstrap.sh

infra-imagenes: ## Paso 3: build + push a ECR (SERVICES=a,b o todos con Dockerfile)
	@$(PLATAFORMA_SCRIPTS)/images.sh

infra-aplicaciones: ## Paso 4: servicios ECS con imagen en ECR, alarmas y tablero
	@$(PLATAFORMA_SCRIPTS)/apps.sh apply

infra-microservicios: ## Imagen + despliegue solo de SERVICES (requiere plataforma)
	@test -n "$(SERVICES)" || { echo "Indique SERVICES=servicio1,servicio2"; exit 1; }
	@$(PLATAFORMA_SCRIPTS)/images.sh
	@$(PLATAFORMA_SCRIPTS)/apps.sh apply

infra-desplegar: ## Todo el ambiente en orden (pasos 1 a 4)
	@$(PLATAFORMA_SCRIPTS)/platform.sh apply
	@$(PLATAFORMA_SCRIPTS)/db-bootstrap.sh
	@$(PLATAFORMA_SCRIPTS)/images.sh
	@$(PLATAFORMA_SCRIPTS)/apps.sh apply

infra-destruir: ## Elimina el ambiente completo (apps y luego platform)
	@$(PLATAFORMA_SCRIPTS)/destroy.sh

infra-probar: ## Smoke test del ambiente desplegado (SMOKE_ALERTA=1 prueba el correo de alertas)
	@$(PLATAFORMA_SCRIPTS)/smoke.sh

infra-pausar: ## Lleva todos los servicios a 0 tareas (sin costo de Fargate)
	@PAUSED=1 $(PLATAFORMA_SCRIPTS)/apps.sh apply

infra-reanudar: ## Vuelve a las réplicas configuradas
	@PAUSED=0 $(PLATAFORMA_SCRIPTS)/apps.sh apply

infra-plan: ## Plan de platform y apps sin aplicar
	@$(PLATAFORMA_SCRIPTS)/platform.sh plan
	@$(PLATAFORMA_SCRIPTS)/apps.sh plan

infra-salidas: ## URLs de APIs, portal, Cognito y tablero del ambiente
	@. $(PLATAFORMA_SCRIPTS)/lib.sh && tf_init platform && export_apps_vars && \
	  $(TF) -chdir=infra/platform output api_urls && $(TF) -chdir=infra/platform output web && \
	  $(TF) -chdir=infra/platform output cognito && tf_init apps && \
	  $(TF) -chdir=infra/apps output dashboard_url 2>/dev/null || true

infra-validar: ## fmt, validate y pruebas de Terraform (sin AWS)
	@for root in bootstrap platform apps; do \
	  echo "== $$root"; \
	  $(TF) -chdir=infra/$$root init -backend=false -input=false >/dev/null && \
	  $(TF) -chdir=infra/$$root fmt -check -recursive && \
	  $(TF) -chdir=infra/$$root validate -no-color || exit 1; \
	  if [ -d infra/$$root/tests ]; then $(TF) -chdir=infra/$$root test -no-color || exit 1; fi; \
	done
