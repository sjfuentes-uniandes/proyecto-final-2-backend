# solventa-back

Monorepo de backend de Solventa: un directorio por servicio en `services/`, librería común en `libs/`, contratos en `contracts/`.

```bash
make dev-sync           # instala el workspace (uv)
make dev-up             # Postgres, LocalStack, WireMock y servicios
make test-unit SERVICE=api-socios
```

Cada servicio sigue arquitectura hexagonal: `domain/` → `application/` → `adapters/{inbound,outbound}/`.
