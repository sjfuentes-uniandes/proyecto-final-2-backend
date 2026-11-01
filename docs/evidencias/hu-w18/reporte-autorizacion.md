# HU-W18: reporte de autorización (estado y listado de pólizas)

Actor: asesor del portal (grupo `asesores`). Regla: el grupo se valida primero en `bff-web`, antes de
llamar a `consulta`; `consulta` valida `X-Actor-Rol=ASESOR` antes de abrir la base.

| Actor / rol | Recurso | Esperado | Real | Prueba |
|---|---|---|---|---|
| Asesor | póliza proyectada | 200 con `estado` y `fechaActualizacion` | 200 | `bff-web/tests/unit/test_estado_polizas.py::test_el_asesor_recibe_el_estado_y_la_fecha_de_actualizacion`; N2 `test_n2_1`, `test_n2_2`, `test_n2_3` |
| Asesor | póliza inexistente | 404 `POLIZA_NO_ENCONTRADA` uniforme con `correlationId` | 404 | `consulta/tests/module/test_estado_polizas.py::test_inexistente_y_no_proyectada_responden_el_mismo_404`; `bff-web/tests/unit/test_estado_polizas.py::test_la_poliza_inexistente_es_404_uniforme` |
| Asesor | póliza aún no proyectada | 404, idéntico al de inexistente (no confirma su existencia) | 404 idéntico; tras proyectarse, 200 | N2 `test_n2_1_no_proyectada_es_404_y_tras_proyectarse_es_200_no_iniciada` |
| Otro grupo (`suscriptores`, etc.) | cualquiera | 403 `ROL_NO_AUTORIZADO`, sin llamar a `consulta` | 403 | `bff-web/tests/unit/test_estado_polizas.py::test_otro_grupo_es_403_sin_llamar_a_consulta`; N2 `test_n2_6` |
| Otro rol interno en `consulta` | cualquiera | 403 y la base NO se consulta | 403 | `consulta/tests/unit/test_api_estado.py::test_otro_rol_es_403_y_no_consulta_la_base`; módulo `test_otro_rol_es_403_sin_datos` |
| Sin sesión | cualquiera | 403 en `bff-web` (el grupo se valida primero; en producción el API Gateway rechaza antes) | 403 | `bff-web/tests/unit/test_estado_polizas.py::test_sin_usuario_autenticado_es_403_sin_llamar_a_consulta` |
| Sin `sub` (sin actor) | cualquiera | 401 `ACTOR_REQUERIDO` | 401 | `bff-web/tests/unit/test_estado_polizas.py::test_sin_sub_es_401_actor_requerido`; `consulta/tests/unit/test_api_estado.py::test_sin_actor_es_401` |
| `consulta` responde 401/403 al BFF | cualquiera | 503 `SERVICIO_NO_DISPONIBLE` sin detalles | 503 | `bff-web/tests/module/test_estado_polizas_w18.py::test_si_consulta_rechaza_al_bff_es_503` |

## Limitación

No existe el vínculo asesor → socio (ni tabla, ni claim, ni grupo), de modo que todo asesor ve todas las
pólizas proyectadas, igual que `puede_leer` de `cotizacion`. El AC2 por propietario (recurso de otro
asesor o socio) no es aplicable hoy: se cumple por rol y con un 404 uniforme que no confirma existencia.
Añadir la propiedad después requiere una columna nullable y un filtro en la misma consulta SQL.
